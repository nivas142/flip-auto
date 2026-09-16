from __future__ import annotations

import io
import imaplib
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import patch

import monitor
from valuation import ValuationResult


def build_email_bytes(*, from_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "nobody@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = "<test-message@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


def build_html_email_bytes(*, from_addr: str, subject: str, html: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "nobody@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = "<test-html-message@example.com>"
    msg.set_content("HTML deal email")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, raw_message: bytes):
        self.raw_message = raw_message
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))

    def select(self, folder: str):
        self.calls.append(("select", (folder,)))
        return "OK", [b"1"]

    def search(self, charset, criteria):
        self.calls.append(("search", (criteria,)))
        return "OK", [b"1"]

    def fetch(self, msg_id, query):
        self.calls.append(("fetch", (msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id), query)))
        return "OK", [(b"1", self.raw_message)]

    def store(self, msg_id, flags, value):
        self.calls.append(("store", (msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id), flags, value)))
        return "OK", [b""]

    def close(self):
        self.calls.append(("close", None))
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout", None))
        return "BYE", [b""]


class MonitorFilterTests(unittest.TestCase):
    def test_cloud_cma_request_is_deduped_without_persisting_raw_address(self):
        deal = monitor.PropertyDeal(
            city="Gilbert",
            address="2010 E Arabian Dr, Gilbert, AZ 85296",
            price="$378,000",
            details_url="",
            image_url="",
            summary="4 beds 3 baths 1,625 sqft built 1997",
        )
        state: dict = {}
        budget = [3]
        cfg = {
            "api_key": "secret",
            "result_email": "agent@example.com",
            "min_listings": 25,
            "days_old": 180,
        }

        with patch.object(monitor, "request_quick_cma") as request_mock:
            request_mock.return_value.accepted = True
            request_mock.return_value.status_code = 200
            first = monitor.request_cloud_cma_for_deal(deal, cfg, state, budget)
            second = monitor.request_cloud_cma_for_deal(deal, cfg, state, budget)

        self.assertEqual(first.status, "pending")
        self.assertEqual(second.status, "pending")
        self.assertEqual(request_mock.call_count, 1)
        self.assertEqual(budget[0], 2)
        serialized_state = str(state)
        self.assertNotIn("Arabian", serialized_state)
        self.assertNotIn("Gilbert", serialized_state)

    def test_screening_emits_each_matching_city_deal(self):
        raw_message = build_html_email_bytes(
            from_addr="Deals <deals@example.com>",
            subject="New Arizona deals",
            html="""
                <table>
                  <tr><td>123 Main St, Mesa, AZ 85201 ARV: $500K Price: $325,000
                    <a href="https://example.com/mesa">Photos / Details</a></td></tr>
                  <tr><td>456 Oak Rd, Chandler, AZ 85224 ARV: $600K Price: $400,000
                    <a href="https://example.com/chandler">Photos / Details</a></td></tr>
                </table>
            """,
        )
        fake_imap = FakeIMAP(raw_message)
        account_cfg = {
            "label": "email",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "INBOX",
            "lookback_minutes": 60,
            "sender_filters": ["deals@example.com"],
            "subject_filters": [],
            "cities": ["Chandler", "Mesa"],
        }

        independent = ValuationResult(
            status="complete",
            source="test_comps",
            arv_low=500_000,
            arv_likely=515_000,
            arv_high=530_000,
            confidence="medium",
            subject_square_footage=1_800,
            comparables=(),
        )
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch.object(
            monitor, "request_cloud_cma_for_deal", return_value=independent
        ):
            results = monitor.scan_email_account(
                account_cfg,
                {"enabled": True},
                {"enabled": True, "api_key": "test"},
            )

        self.assertEqual(len(results), 2)
        self.assertEqual({item.city for item in results}, {"Mesa", "Chandler"})

    def test_structured_deal_id_dedupes_address_variants_across_sources(self):
        first = monitor.PropertyDeal(
            city="Mesa",
            address="3462 E Hearn Road, Mesa, AZ 85205",
            price="$300,000",
            details_url="",
            image_url="",
            summary="1,800 SF ARV: $475,000",
        )
        second = monitor.PropertyDeal(
            city="Mesa",
            address="3462 E. Hearn Rd Mesa AZ 85205",
            price="$300,000",
            details_url="",
            image_url="",
            summary="1,800 SF ARV: $475K",
        )

        first_alert = monitor.build_deal_alert(
            deal=first,
            label="gmail",
            from_header="first@example.com",
            subject="Deal one",
            received_at="",
            screening_cfg={"enabled": True},
        )
        second_alert = monitor.build_deal_alert(
            deal=second,
            label="zoho",
            from_header="second@example.com",
            subject="Deal two",
            received_at="",
            screening_cfg={"enabled": True},
        )

        self.assertEqual(first_alert.item_id, second_alert.item_id)

    def test_account_can_clear_inherited_email_filters(self):
        config = {
            "email": {
                "imap_host": "imap.example.com",
                "username": "default@example.com",
                "password": "secret",
                "sender_filters": ["listingupdates@flexmail.flexmls.com"],
                "subject_filters": ["Copy: Subscription Investing"],
                "sender_subject_filters": {
                    "listingupdates@flexmail.flexmls.com": ["Copy: Subscription Investing"],
                },
                "cities": ["Chandler"],
                "accounts": [
                    {
                        "label": "zoho",
                        "imap_host": "imap.zoho.com",
                        "username": "zoho@example.com",
                        "password": "secret",
                        "sender_filters": [],
                        "subject_filters": [],
                        "sender_subject_filters": {},
                    }
                ],
            }
        }

        accounts = monitor.collect_email_accounts(config)
        zoho = next(account for account in accounts if account["label"] == "zoho")

        self.assertEqual(zoho["sender_filters"], [])
        self.assertEqual(zoho["subject_filters"], [])
        self.assertEqual(zoho["sender_subject_filters"], {})
        self.assertEqual(zoho["cities"], ["Chandler"])

    def test_sender_and_subject_must_both_match(self):
        raw_message = build_email_bytes(
            from_addr="Listing Updates <listingupdates@flexmail.flexmls.com>",
            subject="Copy: Subscription Investing",
            body="Chandler deal details inside.",
        )
        fake_imap = FakeIMAP(raw_message)

        account_cfg = {
            "label": "email",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "INBOX",
            "lookback_minutes": 60,
            "sender_filters": ["listingupdates@flexmail.flexmls.com"],
            "subject_filters": ["Copy: Subscription Investing"],
            "cities": ["Chandler"],
        }

        stderr = io.StringIO()
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch(
            "sys.stderr",
            stderr,
        ):
            results = monitor.scan_email_account(account_cfg)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].city, "Chandler")
        self.assertEqual(results[0].title, "Email match: Chandler")
        self.assertIn(("store", ("1", "+FLAGS", "\\Seen")), fake_imap.calls)
        self.assertIn("Email account email (imap.example.com): scanned 1 messages, matched 1.", stderr.getvalue())

    def test_nonmatching_subject_is_ignored(self):
        raw_message = build_email_bytes(
            from_addr="Listing Updates <listingupdates@flexmail.flexmls.com>",
            subject="Copy: Something Else",
            body="Chandler deal details inside.",
        )
        fake_imap = FakeIMAP(raw_message)

        account_cfg = {
            "label": "email",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "INBOX",
            "lookback_minutes": 60,
            "sender_filters": ["listingupdates@flexmail.flexmls.com"],
            "subject_filters": ["Copy: Subscription Investing"],
            "cities": ["Chandler"],
        }

        stderr = io.StringIO()
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch(
            "sys.stderr",
            stderr,
        ):
            results = monitor.scan_email_account(account_cfg)

        self.assertEqual(results, [])
        fetch_queries = [call[1][1] for call in fake_imap.calls if call[0] == "fetch"]
        self.assertEqual(fetch_queries, ["(BODY.PEEK[HEADER])"])
        self.assertIn("Email account email (imap.example.com): scanned 1 messages, matched 0.", stderr.getvalue())

    def test_sender_specific_subject_filter_allows_other_senders(self):
        raw_message = build_email_bytes(
            from_addr="Deals <deals@exohomesolutions.com>",
            subject="New Chandler property",
            body="Chandler deal details inside.",
        )
        fake_imap = FakeIMAP(raw_message)

        account_cfg = {
            "label": "email",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "INBOX",
            "lookback_minutes": 60,
            "sender_filters": [
                "deals@exohomesolutions.com",
                "listingupdates@flexmail.flexmls.com",
            ],
            "subject_filters": [],
            "sender_subject_filters": {
                "listingupdates@flexmail.flexmls.com": ["Copy: Subscription Investing"],
            },
            "cities": ["Chandler"],
        }

        stderr = io.StringIO()
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch(
            "sys.stderr",
            stderr,
        ):
            results = monitor.scan_email_account(account_cfg)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].city, "Chandler")
        self.assertIn(("store", ("1", "+FLAGS", "\\Seen")), fake_imap.calls)
        self.assertIn("Email account email (imap.example.com): scanned 1 messages, matched 1.", stderr.getvalue())

    def test_sender_specific_subject_filter_blocks_configured_sender_only(self):
        raw_message = build_email_bytes(
            from_addr="Listing Updates <listingupdates@flexmail.flexmls.com>",
            subject="Copy: Something Else",
            body="Chandler deal details inside.",
        )
        fake_imap = FakeIMAP(raw_message)

        account_cfg = {
            "label": "email",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "INBOX",
            "lookback_minutes": 60,
            "sender_filters": [
                "deals@exohomesolutions.com",
                "listingupdates@flexmail.flexmls.com",
            ],
            "subject_filters": [],
            "sender_subject_filters": {
                "listingupdates@flexmail.flexmls.com": ["Copy: Subscription Investing"],
            },
            "cities": ["Chandler"],
        }

        stderr = io.StringIO()
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch(
            "sys.stderr",
            stderr,
        ):
            results = monitor.scan_email_account(account_cfg)

        self.assertEqual(results, [])
        fetch_queries = [call[1][1] for call in fake_imap.calls if call[0] == "fetch"]
        self.assertEqual(fetch_queries, ["(BODY.PEEK[HEADER])"])
        self.assertNotIn(("store", ("1", "+FLAGS", "\\Seen")), fake_imap.calls)
        self.assertIn("Email account email (imap.example.com): scanned 1 messages, matched 0.", stderr.getvalue())

    def test_sellwholesalehouses_footer_city_does_not_alert(self):
        raw_message = build_email_bytes(
            from_addr="Dispo <dispo@sellwholesalehouses.com>",
            subject="New wholesale opportunity",
            body=(
                "Property details do not include a monitored city.\n\n"
                "Company footer\n"
                "123 Example Rd, Chandler, AZ 85225\n"
                "Unsubscribe from this list"
            ),
        )
        fake_imap = FakeIMAP(raw_message)

        account_cfg = {
            "label": "zoho",
            "imap_host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
            "folder": "Off-Market-Deals",
            "lookback_minutes": 60,
            "sender_filters": [],
            "subject_filters": [],
            "sender_subject_filters": {},
            "cities": ["Chandler"],
        }

        stderr = io.StringIO()
        with patch.object(imaplib, "IMAP4_SSL", return_value=fake_imap), patch(
            "sys.stderr",
            stderr,
        ):
            results = monitor.scan_email_account(account_cfg)

        self.assertEqual(results, [])
        self.assertIn(("store", ("1", "+FLAGS", "\\Seen")), fake_imap.calls)
        self.assertIn("Email account zoho (imap.example.com): scanned 1 messages, matched 0.", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
