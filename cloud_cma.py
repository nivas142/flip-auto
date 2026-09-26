"""Cloud CMA request and report parsing helpers.

Cloud CMA is used only as an ARMLS data delivery mechanism.  This module never
uses a Cloud CMA suggested price.  It extracts closed-sale records from the
generated PDF and normalizes them for the valuation module.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pypdf import PdfReader


UTC = timezone.utc
CLOUD_CMA_WIDGET_URL = "https://cloudcma.com/cmas/widget"
META_REFRESH_RE = re.compile(
    r"url=(https?://[^\"'<>\s]+\.pdf(?:\?[^\"'<>\s]+)?)",
    re.IGNORECASE,
)
DEFAULT_MAX_REPORT_BYTES = 200 * 1024 * 1024
MAX_WRAPPER_BYTES = 2 * 1024 * 1024
CLOUD_CMA_PARSER_VERSION = 2


class CloudCmaReportTooLarge(ValueError):
    """Raised when a Cloud CMA response exceeds the configured safe ceiling."""


@dataclass(frozen=True)
class CloudCmaSubmission:
    accepted: bool
    status_code: int


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def request_quick_cma(
    *,
    api_key: str,
    address: str,
    callback_url: str,
    job_id: str,
    sqft: int | None = None,
    beds: float | None = None,
    baths: float | None = None,
    min_listings: int = 25,
    months_back: int = 6,
    template: str = "Web Leads",
    timeout: int = 30,
) -> CloudCmaSubmission:
    """Request one asynchronous Quick CMA delivered only by webhook."""
    if not api_key.strip():
        raise ValueError("Cloud CMA API key is missing")
    if not address.strip():
        raise ValueError("Cloud CMA subject address is missing")
    if not callback_url.strip():
        raise ValueError("Cloud CMA callback URL is missing")
    if not job_id.strip():
        raise ValueError("Cloud CMA callback job ID is missing")

    fields: dict[str, str] = {
        "api_key": api_key.strip(),
        "address": address.strip(),
        "callback_url": callback_url.strip(),
        "job_id": job_id.strip(),
        "title": f"Flip Auto CMA [{job_id[:16]}] {address}",
        "headline": "Investment Pre-Screen CMA",
        "min_listings": str(max(10, min(int(min_listings), 40))),
        "months_back": str(max(1, min(int(months_back), 12))),
        "template": template,
    }
    if sqft:
        fields["sqft"] = str(int(sqft))
    if beds is not None:
        fields["beds"] = f"{beds:g}"
    if baths is not None:
        fields["baths"] = f"{baths:g}"

    request = Request(
        CLOUD_CMA_WIDGET_URL,
        data=urlencode(fields).encode("utf-8"),
        headers={
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "flip-auto/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        # Consume the response so connection and HTTP errors surface here, but
        # do not log it because providers sometimes echo request parameters.
        response.read()
        status = int(getattr(response, "status", 200))
    return CloudCmaSubmission(accepted=200 <= status < 300, status_code=status)


def _read_limited(response: Any, max_bytes: int) -> bytes:
    """Read a response incrementally without buffering beyond the limit."""
    raw_length = str(response.headers.get("Content-Length", "")).strip()
    if raw_length.isdigit() and int(raw_length) > max_bytes:
        raise CloudCmaReportTooLarge(
            f"Cloud CMA report is {int(raw_length):,} bytes; limit is {max_bytes:,}"
        )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise CloudCmaReportTooLarge(
                f"Cloud CMA response exceeded the {max_bytes:,}-byte limit"
            )
    return b"".join(chunks)


def download_cloud_cma_pdf(
    url: str,
    *,
    timeout: int = 90,
    max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
) -> bytes:
    """Download a Cloud CMA PDF, including its HTML meta-refresh wrapper."""
    if max_bytes <= 0:
        raise ValueError("Cloud CMA PDF size limit must be positive")
    request = Request(url, headers={"User-Agent": "flip-auto/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        response_limit = max_bytes if "application/pdf" in content_type else min(
            max_bytes, MAX_WRAPPER_BYTES
        )
        body = _read_limited(response, response_limit)
    if body.startswith(b"%PDF") or "application/pdf" in content_type:
        return body

    wrapper = body.decode("utf-8", errors="replace")
    match = META_REFRESH_RE.search(html.unescape(wrapper))
    if not match:
        raise ValueError("Cloud CMA report link did not resolve to a PDF")
    direct_url = match.group(1).replace("&amp;", "&")
    direct_request = Request(direct_url, headers={"User-Agent": "flip-auto/1.0"})
    with urlopen(direct_request, timeout=timeout) as response:
        pdf = _read_limited(response, max_bytes)
    if not pdf.startswith(b"%PDF"):
        raise ValueError("Cloud CMA returned a non-PDF report")
    return pdf


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [page.extract_text() or "" for page in reader.pages]


def _field(pattern: str, text: str, *, flags: int = re.IGNORECASE) -> str:
    match = re.search(pattern, text, flags)
    return _clean(match.group(1)) if match else ""


def _integer(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_report_date(value: str) -> date | None:
    cleaned = (value or "").strip()
    for date_format in ("%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(cleaned, date_format).date()
        except (TypeError, ValueError):
            continue
    return None


def _labeled_value(labels: tuple[str, ...], text: str, value_pattern: str) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    return _field(
        rf"(?<!\w)(?:{label_pattern})(?!\w)\s*(?:[#:=-]\s*)*({value_pattern})",
        text,
        flags=re.IGNORECASE,
    )


def _listing_address(page: str, mls_number: str) -> str:
    """Best-effort display address; valuation never depends on this field."""
    match = re.search(
        r"(?P<address>\d{1,6}\s+.{2,100}?\b(?:"
        r"Street|St|Road|Rd|Drive|Dr|Lane|Ln|Avenue|Ave|Court|Ct|Way|Place|Pl|"
        r"Boulevard|Blvd|Trail|Trl|Circle|Cir|Parkway|Pkwy"
        r")\b(?:\s+(?:#|Unit\s+)?[A-Za-z0-9-]+)?)\s*,?\s+"
        r"(?P<city>[A-Za-z][A-Za-z .'-]{1,40})\s*,\s*"
        r"(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})",
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return f"MLS #{mls_number}"
    return (
        f"{_clean(match.group('address'))}, {_clean(match.group('city'))}, "
        f"{match.group('state').upper()} {match.group('zip')}"
    )


def _subject_from_pages(pages: list[str], requested_address: str) -> dict[str, Any]:
    subject: dict[str, Any] = {"formattedAddress": requested_address}
    for page in pages:
        if not re.search(r"Map\s+of\s+Comparable\s+Listings", page, re.IGNORECASE):
            continue
        match = re.search(
            r"\bSubject\s+(?P<address>.+?)\s+(?P<beds>\d+(?:\.\d+)?)\s+"
            r"(?P<baths>\d+(?:\.\d+)?)\s+(?P<sqft>[\d,]+)\s+(?:-|N/?A)",
            page,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        subject.update(
            {
                "reportAddress": _clean(match.group("address")),
                "beds": _float(match.group("beds")),
                "baths": _float(match.group("baths")),
                "squareFootage": _integer(match.group("sqft")),
            }
        )
        break
    return subject


def _parse_closed_detail_page(page: str, *, as_of: date) -> dict[str, Any] | None:
    if not re.search(r"\b(?:CLOSED|SOLD)\b", page, re.IGNORECASE):
        return None

    mls_number = _labeled_value(("MLS", "MLS #", "MLS No"), page, r"[A-Za-z0-9-]+")
    if not mls_number:
        return None

    date_token = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    sold_date_text = _labeled_value(
        ("Sold date", "Close date", "Closed date", "COE date", "Close of escrow"),
        page,
        date_token,
    )
    if not sold_date_text:
        sold_date_text = _field(
            rf"\b(?:CLOSED|SOLD)\b\s*[:#-]?\s*({date_token})",
            page,
            flags=re.IGNORECASE,
        )
    sold_date = _parse_report_date(sold_date_text)
    if not sold_date:
        return None

    sold_price_text = _labeled_value(
        ("Sold price", "Close price", "Closed price", "Sale price"),
        page,
        r"\$?[\d,]+",
    )
    summary = re.search(
        r"\$(?P<price>[\d,]+)\s+"
        r"(?P<beds>\d+(?:\.\d+)?)\s+Beds?\s*"
        r"(?P<baths>\d+(?:\.\d+)?)\s+Baths?\s+"
        r"(?P<sqft>[\d,]+)\s+(?:Sq\.?\s*Ft\.?|SQFT|SF)\b",
        page,
        re.IGNORECASE | re.DOTALL,
    )
    if not sold_price_text and summary:
        # Cloud CMA displays the closed price as the headline amount on closed
        # listing detail pages. It is not taken from List Price/Original Price.
        sold_price_text = summary.group("price")

    beds_text = _labeled_value(("Beds", "Bedrooms", "Total bedrooms"), page, r"\d+(?:\.\d+)?")
    baths_text = _labeled_value(("Baths", "Bathrooms", "Total bathrooms"), page, r"\d+(?:\.\d+)?")
    sqft_text = _labeled_value(
        ("Living area", "Square feet", "Sq ft", "Sqft", "Approx sqft"),
        page,
        r"[\d,]+",
    )
    if summary:
        # The headline layout is value-before-label ("4 Beds 3 Baths").
        # Prefer its explicitly paired captures over label-first field parsing.
        beds_text = summary.group("beds")
        baths_text = summary.group("baths")
        sqft_text = summary.group("sqft")

    sold_price = _integer(sold_price_text)
    square_footage = _integer(sqft_text)
    if not sold_price or not square_footage:
        return None

    days_old = (as_of - sold_date).days if sold_date else 9999
    subdivision = _field(
        r"Subdivision:\s*(.+?)(?=\n(?:Style|Full baths|Acres|Lot Size|Garages|List date|Sold date|Off-market date|Updated|Assoc Fee|Taxes|High|Middle|Elementary):)",
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    prop_type = _field(
        r"Prop Type:\s*(.+?)(?=\n(?:County|Subdivision|Style|Full baths|Acres|Lot Size|Garages|List date):)",
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    pool_text = _field(r"Pool Features:\s*([^\n]+)", page)
    has_pool = bool(pool_text and pool_text.lower() not in {"none", "no"})
    year_text = _labeled_value(("Year built", "Yr built"), page, r"\d{4}")
    dom_text = _labeled_value(("Days on market", "DOM"), page, r"\d+")
    return {
        "formattedAddress": _listing_address(page, mls_number),
        "mlsNumber": mls_number,
        "status": "closed",
        "soldPrice": sold_price,
        "soldDate": sold_date.isoformat() if sold_date else "",
        "daysOld": max(0, days_old),
        "beds": _float(beds_text),
        "baths": _float(baths_text),
        "squareFootage": square_footage,
        "yearBuilt": _integer(year_text),
        "daysOnMarket": _integer(dom_text),
        "propertyType": prop_type,
        "subdivision": subdivision,
        "lotSize": _integer(_field(r"Lot Size \(sqft\):\s*([\d,]+)", page)),
        "garages": _float(_field(r"Garages:\s*([\d.]+)", page)),
        "hasPool": has_pool,
        "listPrice": _integer(_field(r"List Price:\s*\$([\d,]+)", page)),
        "originalListPrice": _integer(_field(r"Orig list price:\s*\$([\d,]+)", page)),
    }


def parse_cloud_cma_pages(
    pages: list[str],
    *,
    requested_address: str,
    subject_overrides: dict[str, Any] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Normalize a Cloud CMA report into subject and closed MLS records."""
    effective_date = as_of or datetime.now(UTC).date()
    subject = _subject_from_pages(pages, requested_address)
    if subject_overrides:
        subject.update({k: v for k, v in subject_overrides.items() if v is not None})

    comparables: list[dict[str, Any]] = []
    seen_mls: set[str] = set()
    closed_page_count = 0
    for page in pages:
        if re.search(r"\b(?:CLOSED|SOLD)\b", page, re.IGNORECASE):
            closed_page_count += 1
        comp = _parse_closed_detail_page(page, as_of=effective_date)
        if not comp or comp["mlsNumber"] in seen_mls:
            continue
        seen_mls.add(comp["mlsNumber"])
        comparables.append(comp)

    return {
        "subjectProperty": subject,
        "comparables": comparables,
        "source": "cloud_cma_armls",
        "parseDiagnostics": {
            "pageCount": len(pages),
            "closedPageCount": closed_page_count,
            "parsedClosedComparables": len(comparables),
        },
        # Explicitly no provider-estimated value is returned or consumed.
    }


def parse_cloud_cma_pdf(
    pdf_bytes: bytes,
    *,
    requested_address: str,
    subject_overrides: dict[str, Any] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    return parse_cloud_cma_pages(
        extract_pdf_pages(pdf_bytes),
        requested_address=requested_address,
        subject_overrides=subject_overrides,
        as_of=as_of,
    )
