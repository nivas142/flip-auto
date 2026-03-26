#!/usr/bin/env python3
"""
Single-run monitor for:
1) IMAP email inbox
2) Google Sheets rows

If configured city keywords are matched, sends alerts via Telegram
(or Twilio if Telegram is disabled).
Designed to be run on a cadence (e.g., every 10 minutes) via Task Scheduler/cron.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import imaplib
import yaml
from bs4 import BeautifulSoup


UTC = timezone.utc
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@dataclass
class AlertItem:
    source: str
    item_id: str
    title: str
    body: str
    city: str


@dataclass
class PropertyDeal:
    city: str
    address: str
    price: str
    details_url: str
    image_url: str
    summary: str


ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[^,\n]+,\s*[A-Za-z .#'-]+,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\b"
)
ADDRESS_FLEX_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .#'/-]{2,100}?(?:,\s*|\s+)[A-Za-z .'-]{2,40}(?:,\s*|\s+)[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\b"
)
PRICE_RE = re.compile(r"\bPrice:\s*(\$[0-9][0-9,]*(?:\s*\+\s*[^•\n]+)?)", flags=re.IGNORECASE)
PLAIN_PRICE_RE = re.compile(
    r"\b(?:All-in\s+Price|Wholesale\s+Price|Price)\s*:\s*(\$[0-9][0-9,]*(?:\.[0-9]{2})?)",
    flags=re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\])>]+", flags=re.IGNORECASE)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen": []}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("seen", [])
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def is_bad_local_proxy(value: str) -> bool:
    proxy = normalize_text(value).lower()
    return proxy in {
        "http://127.0.0.1:9",
        "https://127.0.0.1:9",
        "http://localhost:9",
        "https://localhost:9",
    }


@contextmanager
def bypass_invalid_local_proxies():
    removed: dict[str, str] = {}
    try:
        for key in PROXY_ENV_KEYS:
            value = os.environ.get(key)
            if value and is_bad_local_proxy(value):
                removed[key] = value
                del os.environ[key]
        yield
    finally:
        os.environ.update(removed)


def contains_city(text: str, cities: list[str]) -> str | None:
    lower = text.lower()
    for city in cities:
        if city.lower() in lower:
            return city
    return None


CARTER_SENDER_ALIASES = {
    "deals@carterbuyaz.com",
    "deals@carterbuysaz.com",
}


def detect_email_city(msg: Message, from_header: str, subject: str, body: str, cities: list[str]) -> tuple[str | None, list["PropertyDeal"]]:
    parsed_deals = extract_property_deals_from_email(msg, cities)
    sender = from_header.lower()

    # Carter emails include city names in the footer; restrict matching to the
    # subject line and extracted property deals to avoid footer-only alerts.
    if any(alias in sender for alias in CARTER_SENDER_ALIASES):
        city = contains_city(subject, cities)
        if city:
            return city, parsed_deals
        if parsed_deals:
            return parsed_deals[0].city, parsed_deals
        return None, parsed_deals

    full_text = f"{subject}\n{body}"
    return contains_city(full_text, cities), parsed_deals


def parse_email_subject(msg: Message) -> str:
    raw = msg.get("Subject", "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def parse_email_timestamp(msg: Message) -> str:
    raw_date = normalize_text(msg.get("Date", ""))
    if not raw_date:
        return ""
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return raw_date


def parse_email_datetime(msg: Message) -> datetime | None:
    raw_date = normalize_text(msg.get("Date", ""))
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def parse_email_body(msg: Message) -> str:
    texts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = part.get("Content-Disposition", "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                texts.append(decoded)
            elif content_type == "text/html":
                texts.append(BeautifulSoup(decoded, "html.parser").get_text(" "))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                texts.append(BeautifulSoup(decoded, "html.parser").get_text(" "))
            else:
                texts.append(decoded)

    return normalize_text("\n".join(texts))


def decode_email_part(msg: Message, content_type: str) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            if part.get_content_type() != content_type:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        if parts:
            return max(parts, key=len)
        return ""

    if msg.get_content_type() != content_type:
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def extract_property_deals_from_plain_text(msg: Message, cities: list[str]) -> list[PropertyDeal]:
    plain_text = decode_email_part(msg, "text/plain")
    if not plain_text:
        return []

    deals: list[PropertyDeal] = []
    seen_addresses: set[str] = set()

    for price_match in PLAIN_PRICE_RE.finditer(plain_text):
        window_start = max(0, price_match.start() - 700)
        window_end = min(len(plain_text), price_match.end() + 700)
        window = plain_text[window_start:window_end]

        address_matches = list(ADDRESS_FLEX_RE.finditer(window))
        if not address_matches:
            continue
        address = normalize_text(address_matches[-1].group(0))

        city = contains_city(address, cities) or contains_city(window, cities)
        if not city:
            continue

        key = address.lower()
        if key in seen_addresses:
            continue
        seen_addresses.add(key)

        after_price = window[price_match.end() - window_start :]
        detail_link_match = URL_RE.search(after_price)
        details_url = normalize_text(detail_link_match.group(0)) if detail_link_match else ""

        price = normalize_text(price_match.group(1))
        summary = normalize_text(window.replace(address, "", 1))[:260]
        deals.append(
            PropertyDeal(
                city=city,
                address=address,
                price=price,
                details_url=details_url,
                image_url="",
                summary=summary,
            )
        )

    return deals


def extract_property_deals_from_email(msg: Message, cities: list[str]) -> list[PropertyDeal]:
    html_text = decode_email_part(msg, "text/html")

    if not html_text:
        return extract_property_deals_from_plain_text(msg, cities)

    soup = BeautifulSoup(html_text, "html.parser")
    deals: list[PropertyDeal] = []
    seen_addresses: set[str] = set()

    for link in soup.find_all("a", href=True):
        link_text = normalize_text(link.get_text(" ", strip=True)).lower()
        is_deal_link = (
            ("photos" in link_text and "detail" in link_text)
            or ("pictures" in link_text and "video" in link_text)
            or "click here to view" in link_text
        )
        if not is_deal_link:
            continue

        card_td = link.find_parent("td")
        if card_td is None:
            continue
        card_text = normalize_text(card_td.get_text(" ", strip=True))
        if not card_text:
            continue

        address_match = ADDRESS_RE.search(card_text) or ADDRESS_FLEX_RE.search(card_text)
        if not address_match:
            continue

        address = normalize_text(address_match.group(0))
        city = contains_city(address, cities) or contains_city(card_text, cities)
        if not city:
            continue

        key = address.lower()
        if key in seen_addresses:
            continue
        seen_addresses.add(key)

        price_match = PRICE_RE.search(card_text)
        price = normalize_text(price_match.group(1)) if price_match else ""
        details_url = normalize_text(str(link.get("href", "")))

        image_url = ""
        card_row = card_td.find_parent("tr")
        if card_row is not None:
            img = card_row.find("img", src=True)
            if img is not None:
                image_url = normalize_text(str(img.get("src", "")))

        summary = normalize_text(card_text.replace(address, "", 1).replace("Photos / Details", ""))
        deals.append(
            PropertyDeal(
                city=city,
                address=address,
                price=price,
                details_url=details_url,
                image_url=image_url,
                summary=summary,
            )
        )

    if deals:
        return deals
    return extract_property_deals_from_plain_text(msg, cities)


def stable_id(parts: list[str]) -> str:
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_public_csv_url(sheet_cfg: dict[str, Any]) -> str | None:
    public_csv_url = normalize_text(str(sheet_cfg.get("public_csv_url", "")))
    if public_csv_url:
        return public_csv_url

    public_url = normalize_text(str(sheet_cfg.get("public_url", "")))
    if not public_url:
        return None

    parsed = urlparse(public_url)
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", parsed.path)
    if not match:
        raise ValueError("gsheet.public_url is not a valid Google Sheets URL.")

    spreadsheet_id = match.group(1)
    query = parse_qs(parsed.query)

    cfg_gid = normalize_text(str(sheet_cfg.get("public_gid", "")))
    gid = cfg_gid or query.get("gid", ["0"])[0]

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def load_public_sheet_rows(sheet_cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], str] | None:
    csv_url = build_public_csv_url(sheet_cfg)
    if not csv_url:
        return None

    with bypass_invalid_local_proxies():
        with urlopen(csv_url, timeout=30) as response:
            raw = response.read()

    decoded = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded))
    rows: list[dict[str, Any]] = [dict(row) for row in reader]
    return rows, csv_url


def get_row_value(row: dict[str, Any], column_name: str) -> str:
    target = normalize_text(column_name).lower()
    if not target:
        return ""
    for key, value in row.items():
        if normalize_text(str(key)).lower() == target:
            return normalize_text(str(value))
    return ""


def get_first_column_value(row: dict[str, Any]) -> str:
    for value in row.values():
        text = normalize_text(str(value))
        if text:
            return text
    return ""


def row_snippets(row: dict[str, Any], content_columns: list[str]) -> list[str]:
    snippets: list[str] = []

    if content_columns:
        for col in content_columns:
            val = get_row_value(row, col)
            if val:
                snippets.append(f"{col}: {val}")
        if snippets:
            return snippets

    for key, value in row.items():
        key_text = normalize_text(str(key))
        value_text = normalize_text(str(value))
        if not key_text and not value_text:
            continue
        if "disclaimer! please read" in key_text.lower():
            if value_text:
                snippets.append(value_text)
            continue
        if key_text and value_text:
            snippets.append(f"{key_text}: {value_text}")
        elif value_text:
            snippets.append(value_text)
        else:
            snippets.append(key_text)

    return snippets


def scan_emails(config: dict[str, Any]) -> list[AlertItem]:
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled", False):
        return []

    host = email_cfg["imap_host"]
    username = email_cfg["username"]
    password = email_cfg["password"]
    folder = email_cfg.get("folder", "INBOX")
    lookback_hours = email_cfg.get("lookback_hours")
    if lookback_hours is not None:
        lookback_minutes = int(lookback_hours) * 60
    else:
        lookback_minutes = int(email_cfg.get("lookback_minutes", 30))
    sender_filters = [s.lower() for s in email_cfg.get("sender_filters", [])]
    cities = email_cfg.get("cities", [])
    cutoff_dt = datetime.now(UTC) - timedelta(minutes=lookback_minutes)

    results: list[AlertItem] = []

    mail = imaplib.IMAP4_SSL(host)
    mail.login(username, password)
    try:
        status, _ = mail.select(folder)
        if status != "OK":
            raise RuntimeError(f"Could not select folder '{folder}'")

        since_date = cutoff_dt.strftime("%d-%b-%Y")
        status, msg_ids = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            return []

        for msg_id in msg_ids[0].split():
            status, data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue
            raw = data[0][1]
            msg = message_from_bytes(raw)
            msg_dt = parse_email_datetime(msg)
            if msg_dt is not None and msg_dt < cutoff_dt:
                continue

            from_header = normalize_text(msg.get("From", ""))
            if sender_filters and not any(sender in from_header.lower() for sender in sender_filters):
                continue

            subject = normalize_text(parse_email_subject(msg))
            received_at = parse_email_timestamp(msg)
            body = parse_email_body(msg)
            city, parsed_deals = detect_email_city(msg, from_header, subject, body, cities)
            if not city:
                continue

            city_deals = [deal for deal in parsed_deals if deal.city.lower() == city.lower()]

            msg_key = msg.get("Message-ID", "") or str(msg_id, errors="ignore")
            item_id = stable_id(["email", msg_key, subject, city])

            title = f"Email match: {city}"
            if city_deals:
                lines = [f"From: {from_header}", f"Subject: {subject}"]
                if received_at:
                    lines.append(f"Received: {received_at}")
                lines.append("Deals:")
                for deal in city_deals[:3]:
                    headline = deal.address
                    if deal.price:
                        headline = f"{headline} | {deal.price}"
                    lines.append(headline)
                    if deal.details_url:
                        lines.append(f"Details: {deal.details_url}")
                    if deal.image_url:
                        lines.append(f"Image: {deal.image_url}")
                    if deal.summary:
                        lines.append(f"Info: {deal.summary[:180]}")
                content = "\n".join(lines)
            else:
                preview = normalize_text(body)[:500]
                timestamp_line = f"\nReceived: {received_at}" if received_at else ""
                content = f"From: {from_header}\nSubject: {subject}{timestamp_line}\n{preview}"

            results.append(
                AlertItem(
                    source="email",
                    item_id=item_id,
                    title=title,
                    body=content,
                    city=city,
                )
            )
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()

    return results


def scan_sheet(config: dict[str, Any]) -> list[AlertItem]:
    sheet_cfg = config.get("gsheet", {})
    if not sheet_cfg.get("enabled", False):
        return []
    content_columns = sheet_cfg.get("content_columns", [])
    row_id_column = sheet_cfg.get("row_id_column", "id")
    cities = sheet_cfg.get("cities", [])
    source_key = ""

    public_data: tuple[list[dict[str, Any]], str] | None = None
    try:
        public_data = load_public_sheet_rows(sheet_cfg)
    except Exception as exc:
        print(
            f"[WARN] Google Sheet public fetch failed ({exc.__class__.__name__}: {exc}). "
            "Will try service-account access if configured.",
            file=sys.stderr,
        )

    if public_data is not None:
        rows, source_key = public_data
    else:
        credentials_file = normalize_text(str(sheet_cfg.get("credentials_json", "")))
        spreadsheet_id = normalize_text(str(sheet_cfg.get("spreadsheet_id", "")))
        worksheet_name = sheet_cfg.get("worksheet", "Sheet1")

        if not credentials_file or not spreadsheet_id:
            print(
                "[WARN] Google Sheet not accessible: no public URL and missing "
                "service-account settings. Continuing without sheet matches.",
                file=sys.stderr,
            )
            return []

        try:
            import gspread
            import json as json_module
            from google.oauth2.service_account import Credentials

            scopes = [
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ]

            # Some exported JSON key files include a UTF-8 BOM; load with
            # utf-8-sig so service-account auth remains robust.
            with Path(credentials_file).open("r", encoding="utf-8-sig") as f:
                service_account_info = json_module.load(f)

            with bypass_invalid_local_proxies():
                creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
                gc = gspread.authorize(creds)

                sh = gc.open_by_key(spreadsheet_id)
                ws = sh.worksheet(worksheet_name)
                rows = ws.get_all_records()
            source_key = f"{spreadsheet_id}:{worksheet_name}"
        except Exception as exc:
            print(
                f"[WARN] Google Sheet service-account access failed "
                f"({exc.__class__.__name__}: {exc}). Continuing without sheet matches.",
                file=sys.stderr,
            )
            return []

    results: list[AlertItem] = []

    for row in rows:
        first_column_val = get_first_column_value(row)
        city_match = contains_city(first_column_val, cities)
        if not city_match:
            continue

        row_id_val = get_row_value(row, row_id_column)
        if not row_id_val:
            row_id_val = stable_id([json.dumps(row, sort_keys=True, ensure_ascii=True)])

        snippets = row_snippets(row, content_columns)
        if not snippets:
            snippets.append(normalize_text(json.dumps(row, ensure_ascii=True)))

        body = "\n".join(snippets)
        item_id = stable_id(["gsheet", source_key, row_id_val, city_match])

        results.append(
            AlertItem(
                source="gsheet",
                item_id=item_id,
                title=f"Sheet match: {city_match}",
                body=body[:900],
                city=city_match,
            )
        )

    return results


def send_sms_alert(twilio_cfg: dict[str, Any], item: AlertItem) -> None:
    from twilio.rest import Client

    client = Client(twilio_cfg["account_sid"], twilio_cfg["auth_token"])

    msg = (
        f"{item.title}\n"
        f"City: {item.city}\n"
        f"Source: {item.source}\n"
        f"{item.body}"
    )
    if len(msg) > 1500:
        msg = msg[:1490] + "..."

    create_kwargs: dict[str, Any] = {
        "to": twilio_cfg["to_number"],
        "body": msg,
    }
    messaging_service_sid = normalize_text(str(twilio_cfg.get("messaging_service_sid", "")))
    if messaging_service_sid:
        create_kwargs["messaging_service_sid"] = messaging_service_sid
    else:
        create_kwargs["from_"] = twilio_cfg["from_number"]

    client.messages.create(**create_kwargs)


def send_telegram_alert(telegram_cfg: dict[str, Any], item: AlertItem) -> None:
    bot_token = normalize_text(str(telegram_cfg["bot_token"]))
    chat_id = normalize_text(str(telegram_cfg["chat_id"]))

    msg = (
        f"{item.title}\n"
        f"City: {item.city}\n"
        f"Source: {item.source}\n"
        f"{item.body}"
    )
    if len(msg) > 3900:
        msg = msg[:3890] + "..."

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    req = Request(url=url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=30):
        pass


def send_alert(config: dict[str, Any], item: AlertItem) -> bool:
    telegram_cfg = config.get("telegram", {})
    if telegram_cfg.get("enabled", False):
        send_telegram_alert(telegram_cfg, item)
        return True

    twilio_cfg = config.get("twilio", {})
    if twilio_cfg.get("enabled", False):
        send_sms_alert(twilio_cfg, item)
        return True

    return False


def main() -> int:
    config_path = Path("config.yaml")
    if not config_path.exists():
        print("Missing config.yaml. Copy config.example.yaml to config.yaml and edit values.")
        return 1

    config = load_yaml(config_path)

    state_path = Path(config.get("state_file", "state/monitor_state.json"))
    state = load_state(state_path)
    seen = set(state.get("seen", []))

    telegram_cfg = config.get("telegram", {})
    twilio_cfg = config.get("twilio", {})
    if not telegram_cfg.get("enabled", False) and not twilio_cfg.get("enabled", False):
        print("No notifier enabled; running in dry-run mode.")

    matches: list[AlertItem] = []
    try:
        matches.extend(scan_emails(config))
    except Exception as exc:
        print(
            f"[WARN] Email scan failed ({exc.__class__.__name__}: {exc}). Continuing.",
            file=sys.stderr,
        )
    try:
        matches.extend(scan_sheet(config))
    except Exception as exc:
        print(
            f"[WARN] Sheet scan failed ({exc.__class__.__name__}: {exc}). Continuing.",
            file=sys.stderr,
        )

    sent = 0
    for item in matches:
        if item.item_id in seen:
            continue

        if not send_alert(config, item):
            print(f"[DRY RUN] {item.title}\n{item.body}\n")

        seen.add(item.item_id)
        sent += 1

    # Keep state bounded.
    state["seen"] = list(seen)[-100:]
    save_state(state_path, state)

    print(f"Processed {len(matches)} matches, sent {sent} new alerts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
