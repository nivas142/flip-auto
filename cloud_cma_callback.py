"""Client for the private Cloudflare Cloud CMA callback worker."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def callback_delivery_url(base_url: str, secret: str) -> str:
    if not base_url.strip():
        raise ValueError("Cloud CMA callback base URL is missing")
    if not secret.strip():
        raise ValueError("Cloud CMA webhook secret is missing")
    return f"{base_url.rstrip('/')}/callback/{quote(secret.strip(), safe='')}"


def fetch_result(
    base_url: str,
    job_id: str,
    secret: str,
    *,
    timeout: int = 30,
) -> str | None:
    """Return a completed PDF URL, or None while the report is pending."""
    url = f"{base_url.rstrip('/')}/results/{quote(job_id, safe='')}"
    request = Request(
        url,
        headers={"Authorization": f"Bearer {secret}", "User-Agent": "flip-auto/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    pdf_url = str(payload.get("pdf_url") or "").strip()
    if not pdf_url:
        raise ValueError("Cloud CMA callback result has no PDF URL")
    return pdf_url


def delete_result(
    base_url: str,
    job_id: str,
    secret: str,
    *,
    timeout: int = 30,
) -> None:
    url = f"{base_url.rstrip('/')}/results/{quote(job_id, safe='')}"
    request = Request(
        url,
        method="DELETE",
        headers={"Authorization": f"Bearer {secret}", "User-Agent": "flip-auto/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
    except HTTPError as exc:
        if exc.code != 404:
            raise
