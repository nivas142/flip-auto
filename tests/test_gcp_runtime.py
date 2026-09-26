from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from gcp_runtime import (
    FirestoreShadowStore, LeaseBusy, LeaseLost, MAX_STATE_BYTES,
    RuntimeConfigurationError, StateTooLarge, build_config, make_store, run_shadow,
    serialize_state,
)


class FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self.data = copy.deepcopy(data)

    def to_dict(self):
        return self.data


class FakeDocument:
    def __init__(self):
        self.data = None

    def get(self, *, transaction):
        return FakeSnapshot(self.data)


class FakeTransaction:
    def set(self, document, data):
        document.data = copy.deepcopy(data)


def fake_transact(callback):
    return callback(FakeTransaction())


class GcpConfigTests(unittest.TestCase):
    def setUp(self):
        self.template = {
            "email": {"cities": ["Mesa"], "accounts": [{"enabled": True}]},
            "gsheet": {"enabled": True, "credentials_json": "example-key.json"},
            "valuation": {"api_key": "template-key"},
            "screening": {"target_profit": 50000},
            "telegram": {"enabled": True, "bot_token": "template-token"},
            "state_file": "production-state.json",
        }
        self.env = {
            "EMAIL_USERNAME": "test@example.com",
            "EMAIL_APP_PASSWORD": "password",
            "CLOUD_CMA_CALLBACK_BASE_URL": "https://callback.example.com",
            "CLOUD_CMA_WEBHOOK_SECRET": "callback-secret",
        }

    def test_shadow_only_and_no_outbound_credentials(self):
        cfg = build_config(self.template, self.env)
        self.assertEqual(cfg["execution_mode"], "shadow")
        self.assertEqual(cfg["valuation"]["execution_mode"], "shadow")
        self.assertEqual(cfg["valuation"]["api_key"], "")
        self.assertEqual(cfg["valuation"]["max_requests_per_run"], 0)
        self.assertFalse(cfg["telegram"]["enabled"])
        self.assertFalse(cfg["twilio"]["enabled"])
        self.assertFalse(cfg["gsheet"]["enabled"])
        self.assertEqual(cfg["email"]["accounts"], [])
        self.assertNotIn("state_file", cfg)
        self.assertEqual(self.template["valuation"]["api_key"], "template-key")

    def test_live_or_empty_execution_mode_rejected(self):
        for mode in ("live", "", "dry-run"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeConfigurationError):
                build_config(self.template, {**self.env, "FLIP_AUTO_EXECUTION_MODE": mode})

    def test_missing_credentials_or_callback_rejected(self):
        for setting in self.env:
            with self.subTest(setting=setting), self.assertRaises(RuntimeConfigurationError):
                build_config(self.template, {**self.env, setting: ""})

    def test_missing_template_or_city_allowlist_rejected(self):
        with self.assertRaises(RuntimeConfigurationError):
            build_config({}, self.env)
        self.template["email"]["cities"] = []
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, self.env)

    def test_zoho_is_explicit_pair(self):
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, {**self.env, "ZOHO_EMAIL_USERNAME": "zoho@example.com"})
        cfg = build_config(self.template, {
            **self.env, "ZOHO_EMAIL_USERNAME": "zoho@example.com", "ZOHO_EMAIL_APP_PASSWORD": "zoho-password",
        })
        self.assertEqual(len(cfg["email"]["accounts"]), 1)
        self.assertTrue(cfg["email"]["accounts"][0]["enabled"])

    def test_callback_url_and_lookback_validated(self):
        for url in ("http://insecure.example", "https://u:p@example.com", "https://example.com/#secret"):
            with self.subTest(url=url), self.assertRaises(RuntimeConfigurationError):
                build_config(self.template, {**self.env, "CLOUD_CMA_CALLBACK_BASE_URL": url})
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, {**self.env, "EMAIL_LOOKBACK_HOURS": "0"})

    def test_sheet_is_optional_public_only(self):
        cfg = build_config(self.template, {**self.env, "GSHEET_PUBLIC_CSV_URL": "https://docs.google.com/example.csv"})
        self.assertTrue(cfg["gsheet"]["enabled"])
        self.assertEqual(cfg["gsheet"]["credentials_json"], "")
        with self.assertRaises(RuntimeConfigurationError):
            build_config(self.template, {**self.env, "GSHEET_SERVICE_ACCOUNT_JSON": "private-key"})

    def test_firestore_project_and_named_database_required_before_sdk_load(self):
        with self.assertRaises(RuntimeConfigurationError):
            make_store({})
        for database in ("(default)", "", "bad/path"):
            with self.subTest(database=database), self.assertRaises(RuntimeConfigurationError):
                make_store({"GOOGLE_CLOUD_PROJECT": "test-project", "FIRESTORE_DATABASE_ID": database})


class ShadowStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        self.document = FakeDocument()
        self.store = FirestoreShadowStore(self.document, fake_transact, owner="first", clock=lambda: self.now)
        self.other = FirestoreShadowStore(self.document, fake_transact, owner="second", clock=lambda: self.now)

    def test_lease_contention_and_existing_state_beats_seed(self):
        self.assertEqual(self.store.acquire({"seen": ["existing"]}), {"seen": ["existing"]})
        with self.assertRaises(LeaseBusy):
            self.other.acquire()
        self.store.finish({"seen": ["updated"]}, outcome="success")
        self.assertEqual(self.other.acquire({"seen": ["wrong-seed"]}), {"seen": ["updated"]})

    def test_expired_owner_cannot_release_or_save_newer_lease(self):
        self.store.acquire()
        self.now += timedelta(minutes=21)
        self.other.acquire()
        snapshot = copy.deepcopy(self.document.data)
        with self.assertRaises(LeaseLost):
            self.store.finish({"seen": ["stale"]}, outcome="success")
        with self.assertRaises(LeaseLost):
            self.store.abandon()
        self.assertEqual(self.document.data, snapshot)

    def test_expired_lease_cannot_save_even_without_a_new_owner(self):
        self.store.acquire()
        self.now += timedelta(minutes=21)
        with self.assertRaises(LeaseLost):
            self.store.finish({}, outcome="success")

    def test_state_size_and_non_object_guard(self):
        with self.assertRaises(StateTooLarge):
            serialize_state({"payload": "x" * MAX_STATE_BYTES})
        with self.assertRaises(RuntimeConfigurationError):
            serialize_state([])
        with self.assertRaises(ValueError):
            serialize_state({"bad": float("nan")})

    def test_refuses_non_shadow_stored_document(self):
        self.document.data = {"execution_mode": "live", "state_json": "{}"}
        with self.assertRaises(RuntimeConfigurationError):
            self.store.acquire()

    def test_success_saves_shadow_state(self):
        def runner(config, state):
            state["last_run"] = {"would_alert": 1, "sent": 0}
            return 0
        self.assertEqual(run_shadow({"execution_mode": "shadow"}, self.store, runner), 0)
        self.assertIsNone(self.document.data["lease_owner"])
        self.assertEqual(self.document.data["last_outcome"], "success")
        self.assertEqual(json.loads(self.document.data["state_json"])["last_run"]["sent"], 0)

    def test_monitor_failure_saves_diagnostics_but_fails_job(self):
        def runner(config, state):
            state["last_run"] = {"errors": 1}
            return 1
        with self.assertRaises(RuntimeError):
            run_shadow({"execution_mode": "shadow"}, self.store, runner)
        self.assertEqual(self.document.data["last_outcome"], "failed")
        self.assertEqual(json.loads(self.document.data["state_json"])["last_run"]["errors"], 1)

    def test_monitor_exception_saves_safe_partial_diagnostics(self):
        def runner(config, state):
            state["last_run"] = {"errors": 1}
            raise OSError("network failure")
        with self.assertRaises(OSError):
            run_shadow({"execution_mode": "shadow"}, self.store, runner)
        self.assertEqual(self.document.data["last_outcome"], "failed")
        self.assertIsNone(self.document.data["lease_owner"])

    def test_oversized_mutation_releases_lease_without_overwriting_state(self):
        def runner(config, state):
            state["huge"] = "x" * MAX_STATE_BYTES
            return 0
        with self.assertRaises(StateTooLarge):
            run_shadow({"execution_mode": "shadow"}, self.store, runner, seed={"seen": ["good"]})
        self.assertEqual(json.loads(self.document.data["state_json"]), {"seen": ["good"]})
        self.assertIsNone(self.document.data["lease_owner"])

    def test_run_rejects_live_before_acquiring_lease(self):
        runner = Mock()
        with self.assertRaises(RuntimeConfigurationError):
            run_shadow({"execution_mode": "live"}, self.store, runner)
        runner.assert_not_called()
        self.assertIsNone(self.document.data)


if __name__ == "__main__":
    unittest.main()
