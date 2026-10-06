#!/usr/bin/env python3
"""Offline verification of the retained production CMA text with parser v4."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cloud_cma
from valuation import calculate_comp_valuation


EXPECTED_TEXT_SHA256 = "d2a1c3ea7a48a1d40a1fa327ff5c1fdf8430503b731d248ac0f6d12c3a63e69e"
EXPECTED_RESULT_SHA256 = "fe62ead23bfb74aacd0349b08306da7743697dc479da95c2a5bfa68199afaf0d"
AS_OF = date(2026, 10, 5)


class RecoveryVerificationError(RuntimeError):
    """Only fixed, non-sensitive messages are emitted to the operator."""


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != EXPECTED_TEXT_SHA256:
        raise RecoveryVerificationError("Recovery text does not match the retained report")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryVerificationError("Recovery text is not valid UTF-8") from exc

    pages: list[str] = []
    for chunk in text.split("===== PAGE ")[1:]:
        number, marker, body = chunk.partition(" =====\n")
        if not marker or not number.strip().isdigit():
            raise RecoveryVerificationError("Recovery page markers are malformed")
        pages.append(body)
    if len(pages) != 86 or cloud_cma.CLOUD_CMA_PARSER_VERSION != 4:
        raise RecoveryVerificationError("Recovery parser generation or page count differs")

    payload = cloud_cma.parse_cloud_cma_pages(
        pages,
        requested_address="retained recovery subject",
        as_of=AS_OF,
    )
    valuation = calculate_comp_valuation(payload)
    verification = {
        "subject_sqft": (payload.get("subjectProperty") or {}).get("squareFootage"),
        "diagnostics": payload.get("parseDiagnostics") or {},
        "comparables": sorted(
            (
                {
                    "mls": comp.get("mlsNumber"),
                    "sold_date": comp.get("soldDate"),
                    "sold_price": comp.get("soldPrice"),
                    "sqft": comp.get("squareFootage"),
                }
                for comp in payload.get("comparables") or []
            ),
            key=lambda value: str(value["mls"]),
        ),
        "valuation": {
            "status": valuation.status,
            "eligible": len(valuation.comparables),
            "subject_sqft": valuation.subject_square_footage,
            "arv_low": valuation.arv_low,
            "arv_likely": valuation.arv_likely,
            "arv_high": valuation.arv_high,
            "confidence": valuation.confidence,
        },
    }
    result_sha256 = _digest(verification)
    if result_sha256 != EXPECTED_RESULT_SHA256:
        raise RecoveryVerificationError("Recovery parser results differ from the reviewed baseline")
    return {
        "verified": True,
        "parser_version": cloud_cma.CLOUD_CMA_PARSER_VERSION,
        "result_sha256": result_sha256,
        "page_count": len(pages),
        "parsed_closed_comps": len(payload.get("comparables") or []),
        "eligible_closed_comps": len(valuation.comparables),
        "side_effects": {
            "alerts_sent": 0,
            "cma_requests": 0,
            "persistent_state_writes": 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("[CMA_RECOVERY_ERROR] Exactly one recovery text path is required", file=sys.stderr)
        return 2
    try:
        result = verify(Path(args[0]))
    except (OSError, RecoveryVerificationError):
        print("[CMA_RECOVERY_ERROR] Retained report verification failed", file=sys.stderr)
        return 1
    except Exception:
        print("[CMA_RECOVERY_ERROR] Unexpected offline verification failure", file=sys.stderr)
        return 1
    print("[CMA_RECOVERY] " + json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
