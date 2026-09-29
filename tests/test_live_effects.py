from __future__ import annotations

import copy
import io
import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import monitor
from cloud_cma import CloudCmaReportTooLarge, CloudCmaSubmission
from test_monitor_filters import build_email_bytes
from test_shadow_mode import ReadOnlyIMAP
from valuation import ValuationResult


class DurableGuard:
    """Model durable writes, including a lost completion acknowledgment."""

    def __init__(self):
        self.persisted = {}
        self.inflight = None
        self.events = []
        self.fail_complete = False

    def begin(self, kind, key, state):
        if self.inflight is not None:
            raise RuntimeError("Unresolved effect")
        self.inflight = (kind, key)
        self.persisted = copy.deepcopy(state)
        self.events.append(("begin", kind, key))

    def complete(self, state):
        if self.fail_complete:
            raise RuntimeError("Checkpoint unavailable")
        self.persisted = copy.deepcopy(state)
        self.inflight = None
        self.events.append(("complete",))

    def checkpoint(self, state):
        self.persisted = copy.deepcopy(state)
        self.events.append(("checkpoint",))


class LiveEffectsTests(unittest.TestCase):
    def setUp(self):
        self.guard = DurableGuard()
        self.config = {
            "execution_mode": "live", "strict_errors": True,
            "read_only_mailbox": True, "_effect_guard": self.guard,
            "telegram": {"enabled": True, "bot_token": "PRIVATE", "chat_id": "test"},
        }
        self.item = monitor.AlertItem("email", "deal-id", "Mesa deal", "PRIVATE", "Mesa")
        self.deal = monitor.PropertyDeal(
            city="Mesa", address="123 Main St, Mesa, AZ 85201", price="$300,000",
            details_url="", image_url="", summary="1,600 sqft",
        )
        self.key = monitor.cma_address_hash(self.deal.address)
        self.valuation_config = {
            "enabled": True, "execution_mode": "live", "_effect_guard": self.guard,
            "retain_callback_results": True, "api_key": "PRIVATE",
            "callback_base_url": "https://callback.example", "callback_secret": "PRIVATE",
        }

    def run_matches(self, matches, state, send):
        with patch.object(monitor, "scan_emails", return_value=matches), \
             patch.object(monitor, "scan_sheet", return_value=[]), \
             patch.object(monitor, "send_alert", side_effect=send):
            return monitor.run_monitor(self.config, state)

    def test_alert_success_is_checkpointed_before_next_effect(self):
        state = {"seen": ["prior"]}
        next_item = monitor.AlertItem("email", "next-id", "Next deal", "body", "Mesa")

        def send(config, item):
            self.assertEqual(self.guard.inflight, ("telegram_alert", item.item_id))
            self.assertNotIn(item.item_id, state["seen"])
            if item.item_id == "next-id":
                self.assertIn("deal-id", self.guard.persisted["seen"])
            return True

        self.assertEqual(self.run_matches([self.item, next_item], state, send), 0)
        self.assertEqual(self.guard.persisted["seen"], ["prior", "deal-id", "next-id"])
        self.assertIsNone(self.guard.inflight)

    def test_uncertain_alert_keeps_marker_unseen_and_stops(self):
        state = {"seen": []}
        send = Mock(side_effect=TimeoutError("PRIVATE BOT TOKEN"))
        with self.assertRaisesRegex(RuntimeError, r"^Alert delivery failed \(TimeoutError\)$"):
            self.run_matches([self.item, self.item], state, send)
        self.assertEqual(state["seen"], [])
        self.assertEqual(self.guard.persisted["seen"], [])
        self.assertEqual(self.guard.inflight, ("telegram_alert", "deal-id"))
        send.assert_called_once()

    def test_lost_success_checkpoint_does_not_allow_replay(self):
        state = {"seen": []}
        self.guard.fail_complete = True
        send = Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, "Checkpoint unavailable"):
            self.run_matches([self.item], state, send)
        self.assertEqual(state["seen"], ["deal-id"])
        self.assertEqual(self.guard.persisted["seen"], [])
        self.assertEqual(self.guard.inflight, ("telegram_alert", "deal-id"))
        with self.assertRaisesRegex(RuntimeError, "Unresolved effect"):
            self.run_matches([self.item], copy.deepcopy(self.guard.persisted), send)
        send.assert_called_once()

    def test_suppressed_seen_is_durable_without_send(self):
        item = monitor.AlertItem("email", "suppressed-id", "Rejected deal", "body", "Mesa", notify=False)
        send = Mock()
        self.assertEqual(self.run_matches([item], {}, send), 0)
        send.assert_not_called()
        self.assertEqual(self.guard.persisted["seen"], ["suppressed-id"])
        self.assertEqual(self.guard.events, [("checkpoint",)])

    def test_cma_success_records_request_before_marker_clears(self):
        state = {}
        budget = [1]

        def request(**kwargs):
            self.assertEqual(self.guard.inflight, ("cma_request", self.key))
            self.assertNotIn(self.key, self.guard.persisted["cma_requests"])
            return CloudCmaSubmission(accepted=True, status_code=202)

        with patch.object(monitor, "request_quick_cma", side_effect=request):
            result = monitor.request_cloud_cma_for_deal(self.deal, self.valuation_config, state, budget)
        self.assertEqual(result.status, "pending")
        self.assertIn(self.key, self.guard.persisted["cma_requests"])
        self.assertIsNone(self.guard.inflight)
        self.assertEqual(budget, [0])

    def test_cma_uncertainty_or_nonacceptance_keeps_marker(self):
        for outcome in (TimeoutError("PRIVATE"), CloudCmaSubmission(False, 503)):
            with self.subTest(outcome=type(outcome).__name__):
                self.guard = DurableGuard()
                self.valuation_config["_effect_guard"] = self.guard
                state = {}
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(monitor, "request_quick_cma", **kwargs), self.assertRaises(Exception):
                    monitor.request_cloud_cma_for_deal(self.deal, self.valuation_config, state, [1])
                self.assertEqual(state["cma_requests"], {})
                self.assertEqual(self.guard.inflight, ("cma_request", self.key))

    def test_live_callbacks_retained_for_complete_and_oversize_reports(self):
        complete = ValuationResult(
            status="complete", source="cloud_cma_armls_comps", arv_low=450000,
            arv_likely=470000, arv_high=490000, confidence="low",
            subject_square_footage=1600, comparables=(),
        )
        for oversize in (False, True):
            with self.subTest(oversize=oversize):
                state = {"cma_requests": {self.key: datetime.now(monitor.UTC).isoformat()}}
                with patch.object(monitor, "fetch_result", return_value="https://report.example"), \
                     patch.object(monitor, "download_cloud_cma_pdf", side_effect=CloudCmaReportTooLarge() if oversize else None, return_value=b"%PDF"), \
                     patch.object(monitor, "parse_cloud_cma_pdf", return_value={"subjectProperty": {}}), \
                     patch.object(monitor, "calculate_comp_valuation", return_value=complete), \
                     patch.object(monitor, "delete_result") as delete:
                    result = monitor.request_cloud_cma_for_deal(self.deal, self.valuation_config, state, [1])
                self.assertEqual(result.status, "unavailable" if oversize else "complete")
                delete.assert_not_called()

    def test_strict_scan_failure_is_sanitized_and_stops_before_alerts(self):
        with patch.object(monitor, "scan_emails", side_effect=RuntimeError("PRIVATE")), \
             patch.object(monitor, "scan_sheet") as sheet, \
             patch.object(monitor, "send_alert") as send, \
             patch("sys.stderr", new_callable=io.StringIO) as output:
            state = {}
            self.assertEqual(monitor.run_monitor(self.config, state), 1)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(state["last_run"]["errors"], 1)
        sheet.assert_not_called()
        send.assert_not_called()

    def test_production_requires_notifier_before_scan(self):
        self.config["telegram"]["enabled"] = False
        with patch.object(monitor, "scan_emails") as scan, self.assertRaisesRegex(ValueError, "notifier"):
            monitor.run_monitor(self.config, {})
        scan.assert_not_called()

    def test_strict_mailbox_readonly_and_search_failures_raise(self):
        raw = build_email_bytes(from_addr="deals@example.com", subject="Deal", body="Mesa deal")
        mail = ReadOnlyIMAP(raw)
        mail.search = Mock(return_value=("NO", []))
        account = {"imap_host": "imap.example.com", "username": "PRIVATE", "password": "PRIVATE"}
        with patch.object(monitor.imaplib, "IMAP4_SSL", return_value=mail), \
             patch("sys.stderr", new_callable=io.StringIO) as output, \
             self.assertRaisesRegex(RuntimeError, "IMAP search failed"):
            monitor.scan_email_account(account, strict_errors=True, read_only_mailbox=True)
        self.assertIn(("select", ("INBOX", True)), mail.calls)
        self.assertNotIn("store", [call[0] for call in mail.calls])
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_telegram_requires_positive_api_confirmation(self):
        for body in ({"ok": True, "result": {"message_id": 1}}, {"ok": False}, {"ok": True}, []):
            with self.subTest(body=body):
                response = Mock()
                response.read.return_value = json.dumps(body).encode()
                opened = Mock()
                opened.__enter__ = Mock(return_value=response)
                opened.__exit__ = Mock(return_value=False)
                with patch.object(monitor, "urlopen", return_value=opened):
                    if isinstance(body, dict) and body.get("result"):
                        monitor.send_telegram_alert(self.config["telegram"], self.item, require_confirmation=True)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                            monitor.send_telegram_alert(self.config["telegram"], self.item, require_confirmation=True)


if __name__ == "__main__":
    unittest.main()
