from __future__ import annotations

import io
import imaplib
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import patch

import monitor


def build_email_bytes(*, from_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "nobody@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = "<test-message@example.com>"
    msg.set_content(body)
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


if __name__ == "__main__":
    unittest.main()
