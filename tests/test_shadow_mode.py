from __future__ import annotations

import io
import unittest
from datetime import datetime
from unittest.mock import patch

import monitor
from cloud_cma import CloudCmaReportTooLarge
from test_monitor_filters import FakeIMAP, build_email_bytes
from valuation import ValuationResult


class ReadOnlyIMAP(FakeIMAP):
    def select(self, folder, readonly=False):
        self.calls.append(("select", (folder, readonly)))
        return "OK", [b"1"]


class ShadowModeTests(unittest.TestCase):
    def setUp(self):
        self.deal = monitor.PropertyDeal(
            city="Mesa", address="123 Main St, Mesa, AZ 85201",
            price="$300,000", details_url="", image_url="", summary="1,600 sqft",
        )
        self.valuation_config = {
            "enabled": True, "execution_mode": "shadow", "api_key": "unused",
            "callback_base_url": "https://callback.example", "callback_secret": "unused",
        }
        self.key = monitor.cma_address_hash(self.deal.address)
        self.result = ValuationResult(
            status="complete", source="cloud_cma_armls_comps", arv_low=450_000,
            arv_likely=470_000, arv_high=490_000, confidence="low",
            subject_square_footage=1600, comparables=(),
        )

    def test_shadow_never_requests_new_cma(self):
        state = {}
        budget = [10]
        with patch.object(monitor, "request_quick_cma") as request, patch.object(
            monitor, "fetch_result"
        ) as fetch:
            result = monitor.request_cloud_cma_for_deal(
                self.deal, self.valuation_config, state, budget
            )
        request.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(result.status, "pending")
        self.assertEqual(state["cma_requests"], {})
        self.assertEqual(budget, [10])

    def test_shadow_parses_existing_callback_without_deleting(self):
        state = {"cma_requests": {self.key: datetime.now(monitor.UTC).isoformat()}}
        with patch.object(monitor, "fetch_result", return_value="https://report.example") as fetch, \
             patch.object(monitor, "download_cloud_cma_pdf", return_value=b"%PDF"), \
             patch.object(monitor, "parse_cloud_cma_pdf", return_value={"subjectProperty": {}}), \
             patch.object(monitor, "calculate_comp_valuation", return_value=self.result), \
             patch.object(monitor, "delete_result") as delete, \
             patch.object(monitor, "request_quick_cma") as request:
            result = monitor.request_cloud_cma_for_deal(
                self.deal, self.valuation_config, state, [10]
            )
        self.assertEqual(result.status, "complete")
        fetch.assert_called_once()
        delete.assert_not_called()
        request.assert_not_called()

    def test_shadow_oversize_callback_is_not_deleted(self):
        state = {"cma_requests": {self.key: "2026-09-26"}}
        with patch.object(monitor, "fetch_result", return_value="https://report.example"), \
             patch.object(monitor, "download_cloud_cma_pdf", side_effect=CloudCmaReportTooLarge()), \
             patch.object(monitor, "delete_result") as delete:
            result = monitor.request_cloud_cma_for_deal(self.deal, self.valuation_config, state, [10])
        self.assertEqual(result.status, "unavailable")
        delete.assert_not_called()

    def test_shadow_mailbox_read_only_and_cma_guard_propagates(self):
        raw = build_email_bytes(
            from_addr="deals@example.com", subject="Deal",
            body="123 Main St, Mesa, AZ 85201\nAsk: $300,000\n1,600 sqft",
        )
        mail = ReadOnlyIMAP(raw)
        config = {
            "execution_mode": "shadow",
            "email": {"enabled": True, "imap_host": "imap.example.com",
                      "username": "test", "password": "test", "cities": ["Mesa"]},
            "valuation": {"enabled": True, "max_requests_per_run": 10},
            "screening": {"enabled": True},
        }
        state = {}
        with patch.object(monitor.imaplib, "IMAP4_SSL", return_value=mail), \
             patch.object(monitor, "detect_email_city", return_value=("Mesa", [self.deal])), \
             patch.object(monitor, "request_quick_cma") as request:
            monitor.scan_emails(config, state=state)
        self.assertIn(("select", ("INBOX", True)), mail.calls)
        self.assertNotIn("store", [call[0] for call in mail.calls])
        request.assert_not_called()
        self.assertEqual(state["last_run"]["eligible_deals"], 1)
        self.assertEqual(state["last_run"]["valuations_pending"], 1)

    def test_shadow_does_not_send_even_with_notifiers_enabled(self):
        item = monitor.AlertItem("email", "id", "Candidate: Mesa", "PRIVATE BODY", "Mesa")
        config = {"execution_mode": "shadow", "telegram": {"enabled": True}, "twilio": {"enabled": True}}
        state = {}
        with patch.object(monitor, "scan_emails", return_value=[item]), \
             patch.object(monitor, "scan_sheet", return_value=[]), \
             patch.object(monitor, "send_alert") as send, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            result = monitor.run_monitor(config, state)
        send.assert_not_called()
        self.assertEqual(result, 0)
        self.assertEqual(state["last_run"]["sent"], 0)
        self.assertEqual(state["last_run"]["would_alert"], 1)
        self.assertNotIn("PRIVATE BODY", output.getvalue())

    def test_shadow_scan_failure_is_nonzero_and_redacted(self):
        with patch.object(monitor, "scan_emails", side_effect=RuntimeError("SECRET")), \
             patch.object(monitor, "scan_sheet", return_value=[]), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            state = {}
            result = monitor.run_monitor({"execution_mode": "shadow"}, state)
        self.assertEqual(result, 1)
        self.assertEqual(state["last_run"]["errors"], 1)
        self.assertNotIn("SECRET", errors.getvalue())

    def test_invalid_mode_is_rejected_before_scanning(self):
        with patch.object(monitor, "scan_emails") as scan:
            with self.assertRaises(ValueError):
                monitor.run_monitor({"execution_mode": "shdow"}, {})
        scan.assert_not_called()

    def test_shadow_sheet_failure_is_nonzero(self):
        config = {"execution_mode": "shadow", "gsheet": {"enabled": True}}
        with patch.object(monitor, "scan_emails", return_value=[]), \
             patch.object(monitor, "load_public_sheet_rows", side_effect=RuntimeError("SECRET")), \
             patch("sys.stderr", new_callable=io.StringIO) as output:
            state = {}
            result = monitor.run_monitor(config, state)
        self.assertEqual(result, 1)
        self.assertEqual(state["last_run"]["errors"], 1)
        self.assertNotIn("SECRET", output.getvalue())

    def test_shadow_missing_fetch_literal_fails(self):
        raw = build_email_bytes(from_addr="deals@example.com", subject="Deal", body="Mesa deal")
        config = {"imap_host": "imap.example.com", "username": "PRIVATE_USERNAME", "password": "test"}
        for payloads in ([b""], [raw, b""]):
            with self.subTest(payloads=len(payloads)):
                mail = ReadOnlyIMAP(raw)
                with patch.object(monitor.imaplib, "IMAP4_SSL", return_value=mail), \
                     patch.object(monitor, "extract_fetch_bytes", side_effect=payloads), \
                     patch("sys.stderr", new_callable=io.StringIO) as output:
                    with self.assertRaisesRegex(RuntimeError, "payload missing"):
                        monitor.scan_email_account(config, execution_mode="shadow")
                self.assertNotIn("PRIVATE_USERNAME", output.getvalue())

    def test_shadow_cli_cannot_touch_production_state(self):
        with patch.object(monitor.Path, "exists", return_value=True), \
             patch.object(monitor, "load_yaml", return_value={"execution_mode": "shadow"}), \
             patch.object(monitor, "load_state") as read, \
             patch.object(monitor, "save_state") as write:
            self.assertEqual(monitor.main(), 1)
        read.assert_not_called()
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
