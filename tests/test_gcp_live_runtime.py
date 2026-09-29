from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from gcp_live_runtime import (
    DATABASE_ID, EFFECT_RESERVE_SECONDS, EXECUTION_BUDGET_SECONDS, FirestoreLiveStore, LEASE_SECONDS,
    LeaseBusy, LeaseLost, MAX_STATE_BYTES, MonitorFailed, RuntimeConfigurationError,
    StateTooLarge, UnresolvedEffect, build_config, make_store, run_live,
    serialize_state, validate_import_state, main,
)


class Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self.data = copy.deepcopy(data)

    def to_dict(self):
        return self.data


class Document:
    def __init__(self, data):
        self.data = copy.deepcopy(data)

    def get(self, *, transaction):
        return Snapshot(self.data)


class Transaction:
    def set(self, document, data):
        document.data = copy.deepcopy(data)


def transact(callback):
    return callback(Transaction())


def production_state():
    return {
        "seen": ["already-alerted"],
        "last_run": {"mode": "live", "errors": 0},
        "cma_requests": {"a" * 64: "2026-09-29T00:00:00+00:00"},
    }


def production_document(now):
    encoded = serialize_state(production_state())
    return {
        "execution_mode": "live", "schema_version": 1, "enabled": True,
        "state_json": encoded, "inflight_effect": None,
        "lease_owner": None, "lease_until": None,
        "source_repository": "nivas142/flip-auto", "source_commit": "a" * 40,
        "source_state_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "imported_at": now,
    }


class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "schema_version": 1, "email_lookback_hours": 48,
            "zoho_imap_host": "imappro.zoho.com", "zoho_folder": "Off-Market-Deals",
            "zoho_lookback_hours": 48, "gsheet_public_csv_url": "",
            "gsheet_public_url": "https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=0",
            "gsheet_spreadsheet_id": "sheet-id",
        }
        self.env = {
            "FLIP_AUTO_EXECUTION_MODE": "live", "GOOGLE_CLOUD_PROJECT": "flip-auto",
            "EMAIL_USERNAME": "mailbox@example.com", "EMAIL_APP_PASSWORD": "gmail-secret",
            "ZOHO_EMAIL_USERNAME": "zoho@example.com", "ZOHO_EMAIL_APP_PASSWORD": "zoho-secret",
            "CLOUD_CMA_API_KEY": "cma-secret", "CLOUD_CMA_WEBHOOK_SECRET": "callback-secret",
            "CLOUD_CMA_CALLBACK_BASE_URL": "https://callback.example.com",
            "TELEGRAM_BOT_TOKEN": "telegram-secret", "TELEGRAM_CHAT_ID": "chat-secret",
            "FLIP_AUTO_LIVE_SETTINGS_JSON": json.dumps(self.settings),
        }
        self.template = {
            "email": {"cities": ["Mesa"], "sender_filters": ["info@rezamp.com"],
                      "sender_subject_filters": {"special@example.com": ["Deals"]}, "folder": "INBOX"},
            "valuation": {}, "screening": {},
            "gsheet": {"cities": ["Mesa"], "public_gid": 0},
            "state_file": "state/monitor_state.json",
        }

    def config(self, **settings):
        env = dict(self.env)
        env["FLIP_AUTO_LIVE_SETTINGS_JSON"] = json.dumps({**self.settings, **settings})
        return build_config(self.template, env)

    def test_live_credentials_rules_sheet_and_guards_preserved(self):
        cfg = self.config()
        self.assertEqual(cfg["execution_mode"], "live")
        self.assertTrue(cfg["strict_errors"])
        self.assertTrue(cfg["read_only_mailbox"])
        self.assertTrue(cfg["valuation"]["retain_callback_results"])
        self.assertEqual(cfg["valuation"]["max_requests_per_run"], 1)
        self.assertEqual(cfg["valuation"]["max_requests_per_day"], 10)
        self.assertEqual(cfg["email"]["sender_filters"], ["info@rezamp.com"])
        self.assertEqual(cfg["email"]["sender_subject_filters"], self.template["email"]["sender_subject_filters"])
        self.assertEqual(cfg["email"]["cities"], ["Mesa"])
        self.assertEqual(cfg["email"]["accounts"][0]["sender_filters"], [])
        self.assertEqual(cfg["email"]["accounts"][0]["imap_host"], "imappro.zoho.com")
        self.assertEqual(cfg["email"]["accounts"][0]["password"], "zoho-secret")
        self.assertTrue(cfg["telegram"]["enabled"])
        self.assertFalse(cfg["twilio"]["enabled"])
        self.assertTrue(cfg["gsheet"]["enabled"])
        self.assertEqual(cfg["gsheet"]["public_url"], self.settings["gsheet_public_url"])
        self.assertEqual(cfg["gsheet"]["credentials_json"], "")
        self.assertNotIn("state_file", cfg)
        self.assertNotIn("accounts", self.template["email"])

    def test_every_secret_and_explicit_live_mode_are_required(self):
        for key in self.env:
            with self.subTest(key=key):
                env = dict(self.env)
                env.pop(key)
                with self.assertRaises(RuntimeConfigurationError):
                    build_config(self.template, env)
        for mode in ("shadow", "", "dry-run"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeConfigurationError):
                build_config(self.template, {**self.env, "FLIP_AUTO_EXECUTION_MODE": mode})

    def test_settings_schema_and_verified_host_are_required(self):
        for settings in (
            {**self.settings, "extra": "unexpected"},
            {k: v for k, v in self.settings.items() if k != "gsheet_spreadsheet_id"},
            {**self.settings, "schema_version": True},
            {**self.settings, "schema_version": 2},
            {**self.settings, "zoho_imap_host": "imap.zoho.com"},
            {**self.settings, "zoho_folder": "\n"},
        ):
            with self.subTest(settings=settings), self.assertRaises(RuntimeConfigurationError):
                build_config(self.template, {**self.env, "FLIP_AUTO_LIVE_SETTINGS_JSON": json.dumps(settings)})

    def test_public_sheet_cannot_silently_disappear(self):
        with self.assertRaises(RuntimeConfigurationError):
            self.config(gsheet_public_csv_url="", gsheet_public_url="")
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, {**self.env, "GSHEET_SERVICE_ACCOUNT_JSON": "private-key"})
        cfg = self.config(gsheet_public_csv_url="https://docs.google.com/example.csv")
        self.assertEqual(cfg["gsheet"]["public_url"], "")
        self.assertEqual(cfg["gsheet"]["public_csv_url"], "https://docs.google.com/example.csv")
        cfg = self.config(gsheet_spreadsheet_id="")
        self.assertEqual(cfg["gsheet"]["spreadsheet_id"], "sheet-id")

    def test_invalid_hours_urls_and_project_do_not_echo_values(self):
        for value in (0, 169, True, [], "secret-value", 1.5):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError) as error:
                self.config(email_lookback_hours=value)
            self.assertNotIn("secret-value", str(error.exception))
        for value in ("http://example.com", "https://user:secret-value@example.com", "https://example.com/#secret-value"):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError) as error:
                build_config(self.template, {**self.env, "CLOUD_CMA_CALLBACK_BASE_URL": value})
            self.assertNotIn("secret-value", str(error.exception))
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, {**self.env, "GOOGLE_CLOUD_PROJECT": "different"})

    def test_store_namespace_is_fixed_before_loading_cloud_sdk(self):
        for env in ({}, {"GOOGLE_CLOUD_PROJECT": "other", "FIRESTORE_DATABASE_ID": DATABASE_ID},
                    {"GOOGLE_CLOUD_PROJECT": "flip-auto", "FIRESTORE_DATABASE_ID": "flip-auto"},
                    {"GOOGLE_CLOUD_PROJECT": "flip-auto", "FIRESTORE_DATABASE_ID": "(default)"}):
            with self.subTest(env=env), self.assertRaises(RuntimeConfigurationError):
                make_store(env)

    def test_main_requires_cloud_run_execution_before_any_cloud_access(self):
        for value in ("", "flip-auto-shadow-abc12", "arbitrary-name", "flip-auto-live-../wrong"):
            with self.subTest(value=value), patch.dict("os.environ", {"CLOUD_RUN_EXECUTION": value}, clear=True), \
                    patch("gcp_live_runtime.make_store") as store, patch("sys.stderr"):
                self.assertEqual(main(), 1)
                store.assert_not_called()


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        self.doc = Document(production_document(self.now))
        self.elapsed = 0.0
        self.store = FirestoreLiveStore(self.doc, transact, owner="first", clock=lambda: self.now, monotonic_clock=lambda: self.elapsed)
        self.other = FirestoreLiveStore(self.doc, transact, owner="second", clock=lambda: self.now)
        self.config = {"execution_mode": "live", "strict_errors": True, "valuation": {}, "_execution_name": "flip-auto-live-abc12"}

    def test_missing_disabled_shadow_or_unprovenanced_state_never_seeds(self):
        original = production_document(self.now)
        candidates = [None, {**original, "enabled": False}, {**original, "execution_mode": "shadow"},
                      {**original, "schema_version": True}, {**original, "enabled": "true"},
                      {**original, "source_repository": "other/repo"}, {**original, "source_commit": "main"},
                      {**original, "source_state_sha256": "wrong"}, {**original, "imported_at": None}]
        for data in candidates:
            with self.subTest(data=data):
                self.doc.data = copy.deepcopy(data)
                with self.assertRaises(RuntimeConfigurationError):
                    self.store.acquire()
                self.assertEqual(self.doc.data, data)

    def test_invalid_or_shadow_tracking_state_rejected(self):
        for state in ({}, {"seen": []}, {"seen": "bad", "last_run": {"mode": "live"}},
                      {"seen": [], "last_run": {"mode": "shadow"}},
                      {**production_state(), "execution_mode": "shadow"},
                      {**production_state(), "cma_requests": []}):
            with self.subTest(state=state), self.assertRaises(RuntimeConfigurationError):
                validate_import_state(state)
        self.assertEqual(validate_import_state(production_state()), production_state())

    def test_cma_tracking_requires_opaque_keys_and_aware_timestamps(self):
        for field in ("cma_requests", "cma_reports_processed", "cma_reports_rejected"):
            for value in ("invalid", "2026-09-29T00:00:00", "2026-09-29", 123, {}, None):
                with self.subTest(field=field, value=value), self.assertRaises(RuntimeConfigurationError):
                    validate_import_state({**production_state(), field: {"a" * 64: value}})
            with self.assertRaises(RuntimeConfigurationError):
                validate_import_state({**production_state(), field: {"raw-address": self.now.isoformat()}})
            valid = {**production_state(), field: {"a" * 64: "2026-09-29T00:00:00Z"}}
            self.assertEqual(validate_import_state(valid), valid)
        for record in ({}, "2026-09-29T00:00:00Z", {"timestamp": "invalid"},
                       {"timestamp": self.now.isoformat(), "reason": []},
                       {"timestamp": self.now.isoformat(), "parser_version": True}):
            with self.subTest(record=record), self.assertRaises(RuntimeConfigurationError):
                validate_import_state({**production_state(), "cma_reports_unavailable": {"a" * 64: record}})
        valid = {**production_state(), "cma_reports_unavailable": {
            "a" * 64: {"timestamp": self.now.isoformat(), "reason": "No comps", "parser_version": 2},
        }}
        self.assertEqual(validate_import_state(valid), valid)
        self.assertNotIn("finished_at", validate_import_state(production_state())["last_run"])

    def test_acquire_preserves_import_and_blocks_overlapping_execution(self):
        state = self.store.acquire()
        self.assertEqual(state, production_state())
        self.assertEqual(self.doc.data["lease_until"], self.now + timedelta(seconds=LEASE_SECONDS))
        with self.assertRaises(LeaseBusy):
            self.other.acquire()
        state["seen"].append("new")
        self.store.finish(state, outcome="success")
        self.assertEqual(self.other.acquire()["seen"], ["already-alerted", "new"])

    def test_expired_or_replaced_owner_cannot_mutate_state(self):
        state = self.store.acquire()
        self.now += timedelta(seconds=LEASE_SECONDS + 1)
        self.other.acquire()
        before = copy.deepcopy(self.doc.data)
        for operation in (lambda: self.store.finish(state, outcome="failed"), self.store.abandon,
                          lambda: self.store.begin("cma_request", "key", state),
                          lambda: self.store.checkpoint(state)):
            with self.assertRaises(LeaseLost):
                operation()
            self.assertEqual(self.doc.data, before)

    def test_begin_requires_enough_time_for_request_and_journals_first(self):
        state = self.store.acquire()
        state["cma_requests"]["b" * 64] = self.now.isoformat()
        self.store.begin("cma_request", "request-key", state)
        self.assertEqual(json.loads(self.doc.data["state_json"]), state)
        self.assertEqual(self.doc.data["inflight_effect"]["key"], "request-key")
        self.assertEqual(self.doc.data["inflight_effect"]["kind"], "cma_request")
        self.store.complete(state)
        self.now += timedelta(seconds=LEASE_SECONDS - EFFECT_RESERVE_SECONDS)
        with self.assertRaises(LeaseLost):
            self.store.begin("telegram_alert", "item-id", state)
        self.assertIsNone(self.doc.data["inflight_effect"])

    def test_process_deadline_blocks_new_effect_with_lease_still_valid(self):
        state = self.store.acquire()
        self.elapsed = EXECUTION_BUDGET_SECONDS - EFFECT_RESERVE_SECONDS
        with self.assertRaises(LeaseLost):
            self.store.begin("telegram_alert", "item-id", state)
        self.assertGreater(self.doc.data["lease_until"], self.now)
        self.assertIsNone(self.doc.data["inflight_effect"])
        self.store.finish(state, outcome="failed")

    def test_delayed_journal_commit_cannot_start_late_effect(self):
        state = self.store.acquire()
        def delayed_transact(callback):
            result = transact(callback)
            self.elapsed = EXECUTION_BUDGET_SECONDS
            return result
        self.store.transact = delayed_transact
        with self.assertRaises(LeaseLost):
            self.store.begin("telegram_alert", "item-id", state)
        self.assertIsNotNone(self.doc.data["inflight_effect"])
        self.store.finish(state, outcome="failed")
        with self.assertRaises(UnresolvedEffect):
            self.other.acquire()

    def test_nested_effect_and_checkpoint_blocked_until_completion(self):
        state = self.store.acquire()
        self.store.begin("telegram_alert", "item-id", state)
        with self.assertRaises(UnresolvedEffect):
            self.store.begin("cma_request", "request-key", state)
        with self.assertRaises(UnresolvedEffect):
            self.store.checkpoint(state)
        state["seen"].append("item-id")
        self.store.complete(state)
        self.assertIsNone(self.doc.data["inflight_effect"])
        self.assertEqual(json.loads(self.doc.data["state_json"])["seen"], ["already-alerted", "item-id"])
        self.store.checkpoint(state)

    def test_unresolved_marker_survives_finish_and_expired_lease(self):
        state = self.store.acquire()
        self.store.begin("telegram_alert", "item-id", state)
        marker = copy.deepcopy(self.doc.data["inflight_effect"])
        self.store.finish(state, outcome="success")
        self.assertEqual(self.doc.data["inflight_effect"], marker)
        self.assertEqual(self.doc.data["last_outcome"], "failed")
        self.assertIsNone(self.doc.data["lease_owner"])
        self.now += timedelta(days=1)
        with self.assertRaises(UnresolvedEffect):
            self.other.acquire()

    def test_pause_stops_new_effect_but_allows_completed_state_to_persist(self):
        state = self.store.acquire()
        self.store.begin("telegram_alert", "item-id", state)
        self.doc.data["enabled"] = False
        state["seen"].append("item-id")
        self.store.complete(state)
        self.store.checkpoint(state)
        with self.assertRaises(RuntimeConfigurationError):
            self.store.begin("cma_request", "request-key", state)
        self.store.finish(state, outcome="failed")
        self.assertIn("item-id", json.loads(self.doc.data["state_json"])["seen"])
        with self.assertRaises(RuntimeConfigurationError):
            self.other.acquire()

    def test_complete_requires_matching_owned_journal(self):
        state = self.store.acquire()
        with self.assertRaises(UnresolvedEffect):
            self.store.complete(state)
        self.store.begin("telegram_alert", "item-id", state)
        self.doc.data["inflight_effect"]["key"] = "different-item"
        with self.assertRaises(UnresolvedEffect):
            self.store.complete(state)
        self.assertIsNotNone(self.doc.data["inflight_effect"])

    def test_success_binds_guard_for_notifications_and_valuations(self):
        def runner(config, state):
            self.assertIs(config["_effect_guard"], self.store)
            self.assertIs(config["valuation"]["_effect_guard"], self.store)
            config["_effect_guard"].begin("telegram_alert", "item-id", state)
            state["seen"].append("item-id")
            config["_effect_guard"].complete(state)
            return 0
        self.assertEqual(run_live(self.config, self.store, runner), 0)
        self.assertEqual(self.doc.data["last_outcome"], "success")
        self.assertIsNone(self.doc.data["lease_owner"])
        self.assertEqual(json.loads(self.doc.data["state_json"])["last_run"]["execution_name"], "flip-auto-live-abc12")

    def test_monitor_scan_errors_fail_even_on_zero_exit_result(self):
        def runner(config, state):
            state["last_run"]["errors"] = 1
            return 0
        with self.assertRaises(MonitorFailed):
            run_live(self.config, self.store, runner)
        self.assertEqual(self.doc.data["last_outcome"], "failed")
        self.assertEqual(json.loads(self.doc.data["state_json"])["last_run"]["errors"], 1)
        self.assertEqual(json.loads(self.doc.data["state_json"])["last_run"]["execution_name"], "flip-auto-live-abc12")

    def test_nonzero_result_or_exception_fails_without_losing_checkpoint(self):
        for failure in (1, OSError("secret network error")):
            self.doc.data = production_document(self.now)
            def runner(config, state):
                state["seen"].append("checkpointed")
                self.store.checkpoint(state)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            with self.assertRaises((MonitorFailed, OSError)):
                run_live(self.config, self.store, runner)
            self.assertEqual(self.doc.data["last_outcome"], "failed")
            self.assertIn("checkpointed", json.loads(self.doc.data["state_json"])["seen"])

    def test_caught_external_exception_still_fails_and_blocks_replay(self):
        def runner(config, state):
            self.store.begin("cma_request", "request-key", state)
            return 0  # Even a swallowed network error cannot look successful.
        with self.assertRaises(UnresolvedEffect):
            run_live(self.config, self.store, runner)
        self.assertEqual(self.doc.data["inflight_effect"]["key"], "request-key")
        with self.assertRaises(UnresolvedEffect):
            self.other.acquire()

    def test_invalid_state_cannot_overwrite_last_durable_snapshot(self):
        def runner(config, state):
            self.store.begin("telegram_alert", "item-id", state)
            state["huge"] = "x" * MAX_STATE_BYTES
            raise OSError("unknown outcome")
        with self.assertRaises(StateTooLarge):
            run_live(self.config, self.store, runner)
        self.assertNotIn("huge", json.loads(self.doc.data["state_json"]))
        self.assertIsNotNone(self.doc.data["inflight_effect"])
        self.assertIsNone(self.doc.data["lease_owner"])

    def test_invalid_config_stops_before_acquiring_state(self):
        runner = Mock()
        for config in ({"execution_mode": "shadow", "strict_errors": True, "valuation": {}},
                       {"execution_mode": "live", "valuation": {}},
                       {"execution_mode": "live", "strict_errors": True}):
            with self.assertRaises(RuntimeConfigurationError):
                run_live(config, self.store, runner)
        runner.assert_not_called()
        self.assertIsNone(self.doc.data["lease_owner"])

    def test_serializer_rejects_invalid_json_and_bounded_size(self):
        for state in ([], {"bad": float("nan")}, {"bad": object()}):
            with self.assertRaises(RuntimeConfigurationError):
                serialize_state(state)
        with self.assertRaises(StateTooLarge):
            serialize_state({"huge": "x" * MAX_STATE_BYTES})


if __name__ == "__main__":
    unittest.main()
