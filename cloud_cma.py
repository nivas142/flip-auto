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


def _parse_short_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%y").date()
    except (TypeError, ValueError):
        return None


def _subject_from_pages(pages: list[str], requested_address: str) -> dict[str, Any]:
    subject: dict[str, Any] = {"formattedAddress": requested_address}
    for page in pages:
        if "Map of Comparable Listings" not in page:
            continue
        match = re.search(
            r"\bSubject\s+(?P<address>.+?)\s+(?P<beds>\d+(?:\.\d+)?)\s+"
            r"(?P<baths>\d+(?:\.\d+)?)\s+(?P<sqft>[\d,]+)\s+-",
            page,
            re.IGNORECASE,
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
    if not re.search(r"\bCLOSED\b", page, re.IGNORECASE):
        return None
    header = re.search(
        r"^(?P<address>\d{1,6}\s+.+)(?P<city>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}),\s*"
        r"(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})\s+"
        r"MLS\s*#(?P<mls>[A-Za-z0-9-]+)",
        page,
        re.MULTILINE,
    )
    summary = re.search(
        r"\$(?P<price>[\d,]+)\s+"
        r"(?P<beds>\d+(?:\.\d+)?)\s+Beds\s*"
        r"(?P<baths>\d+(?:\.\d+)?)\s+Baths\s+"
        r"(?P<sqft>[\d,]+)\s+Sq\.\s*Ft\.\s*.*?"
        r"CLOSED\s+(?P<sold_date>\d{1,2}/\d{1,2}/\d{2}).*?"
        r"Year Built\s*(?P<year>\d{4}).*?"
        r"Days on market:\s*(?P<dom>\d+)",
        page,
        re.IGNORECASE | re.DOTALL,
    )
    if not header or not summary:
        return None

    sold_date = _parse_short_date(summary.group("sold_date"))
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
    address = (
        f"{_clean(header.group('address'))}, {_clean(header.group('city'))}, "
        f"{header.group('state')} {header.group('zip')}"
    )
    return {
        "formattedAddress": address,
        "mlsNumber": header.group("mls"),
        "status": "closed",
        "soldPrice": _integer(summary.group("price")),
        "soldDate": sold_date.isoformat() if sold_date else "",
        "daysOld": max(0, days_old),
        "beds": _float(summary.group("beds")),
        "baths": _float(summary.group("baths")),
        "squareFootage": _integer(summary.group("sqft")),
        "yearBuilt": _integer(summary.group("year")),
        "daysOnMarket": _integer(summary.group("dom")),
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
    for page in pages:
        comp = _parse_closed_detail_page(page, as_of=effective_date)
        if not comp or comp["mlsNumber"] in seen_mls:
            continue
        seen_mls.add(comp["mlsNumber"])
        comparables.append(comp)

    return {
        "subjectProperty": subject,
        "comparables": comparables,
        "source": "cloud_cma_armls",
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
