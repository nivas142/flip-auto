#!/usr/bin/env python3
"""Read one approved retained CMA with deployed modules; never deliver alerts.

This is a fixed migration fixture, not a general URL or Python execution service.
It needs no secrets, mailbox connection, callback access, or persistent state.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import cloud_cma
import deal_screening
import monitor
import valuation


REPORT_URL = "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817"
ADDRESS = "2010 E Arabian Dr, Gilbert, AZ 85296"
AS_OF = date(2026, 9, 26)
SUBJECT = {"squareFootage": 1625, "beds": 4, "baths": 3, "yearBuilt": 1997}
# The retained map shows three beds. Production explicitly overrides report
# facts with the approved incoming deal facts, as did the earlier smoke test.
EXPECTED_REPORT_SUBJECT = {"squareFootage": 1625, "beds": 3, "baths": 3}
VALUATION_CONFIG = {
    "enabled": True, "minimum_comps": 3, "days_old": 180,
    "size_tolerance": 0.20, "year_tolerance": 10, "max_radius": 1.0,
}
SCREENING_CONFIG = {
    "enabled": True, "target_profit": 50_000, "selling_cost_percent": 0.07,
    "other_costs": 12_000, "max_basis_percent": 0.80,
    "default_rehab_per_sqft": 20, "fallback_rehab": 35_000,
}
# Reviewed against the retained report and deployed production module sources.
EXPECTED_REPORT_SHA256 = "0fbe05a1ec669c6eb1548e6cc5fdf0ab4507a3e245309ae56225689677c40972"
EXPECTED_RESULT_SHA256 = "7907073ef91c53d371fd5bf0a29b9932ec3829c6598ef2fb5211b98638b2ad7e"
EXPECTED_MODULE_SHA256 = {
    "cloud_cma": "1cb780240bc9f02b752a189d1418ddaddd49115fd530c2bb83ec0c8af09be22e",
    "valuation": "b8400c04f325f3c4b4296aa0d8113ea5e1fc09876cee998563053d9441a605f4",
    "deal_screening": "ceb04a8d3e68ab99ff1b267d6da94d31b4a6cac8affd675a1df8da1e970060b9",
    "monitor": "759f7dc616c3d01aebc36cef1f4f6e8858952d5407ec6c9840df5a2ca34003ec",
}


class ReplayError(RuntimeError):
    """Only fixed, non-secret diagnostic messages belong in this exception."""


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def screen(ask, comp_valuation):
    deal = monitor.PropertyDeal(
        city="Gilbert", address=ADDRESS, price=str(ask), details_url=REPORT_URL,
        image_url="", summary="4 bed / 3 bath / 1,625 sqft / built 1997.",
    )
    facts = deal_screening.extract_deal_facts(
        address=deal.address, city=deal.city, price=deal.price, summary=deal.summary,
    )
    result = deal_screening.screen_deal(facts, comp_valuation, SCREENING_CONFIG)
    alert = monitor.build_deal_alert(
        deal=deal, label="retained-cma-replay", from_header="Existing Cloud CMA report",
        subject="Read-only migration replay", received_at="",
        screening_cfg=SCREENING_CONFIG, valuation=comp_valuation,
    )
    if result.status == "valuation_required" or result.missing_fields:
        raise ReplayError("Replay screening unexpectedly lacks required inputs")
    return {**asdict(result), "would_notify": alert.notify}


def run_replay(pdf_bytes=None):
    modules = {
        module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (cloud_cma, deal_screening, monitor, valuation)
    }
    if modules != EXPECTED_MODULE_SHA256:
        raise ReplayError("Deployed calculation modules differ from the reviewed baseline")
    if pdf_bytes is None:
        pdf_bytes = cloud_cma.download_cloud_cma_pdf(
            REPORT_URL, timeout=90, max_bytes=200 * 1024 * 1024,
        )
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    if EXPECTED_REPORT_SHA256 and pdf_hash != EXPECTED_REPORT_SHA256:
        raise ReplayError("Retained PDF does not match the reviewed baseline hash")

    payload = cloud_cma.parse_cloud_cma_pdf(
        pdf_bytes, requested_address=ADDRESS, as_of=AS_OF,
    )
    parsed_subject = payload.get("subjectProperty") or {}
    report_address = deal_screening.normalize_address_key(
        str(parsed_subject.get("reportAddress") or "")
    )
    allowed_addresses = {
        deal_screening.normalize_address_key("2010 E Arabian Dr"),
        deal_screening.normalize_address_key(ADDRESS),
    }
    if report_address not in allowed_addresses:
        raise ReplayError("Retained PDF subject address is missing or does not match")
    if any(parsed_subject.get(key) != value for key, value in EXPECTED_REPORT_SUBJECT.items()):
        raise ReplayError("Retained PDF subject facts do not match the reviewed report")
    payload["subjectProperty"] = {**parsed_subject, **SUBJECT}
    comp_valuation = valuation.calculate_comp_valuation(payload, VALUATION_CONFIG)
    if comp_valuation.status != "complete" or len(comp_valuation.comparables) < 3:
        raise ReplayError("Retained PDF did not produce at least three eligible closed comps")
    if not all(isinstance(value, int) and value > 0 for value in (
        comp_valuation.arv_low, comp_valuation.arv_likely, comp_valuation.arv_high,
    )):
        raise ReplayError("Retained PDF valuation did not produce a valid ARV range")

    actual = screen(378_000, comp_valuation)
    # A separate synthetic ask exercises the positive notification decision.
    # This is a test input, never a new valuation or offer recommendation.
    synthetic = screen(250_000, comp_valuation)
    if synthetic["status"] != "candidate" or synthetic["would_notify"] is not True:
        raise ReplayError("Synthetic candidate did not pass the notification decision")
    selected = sorted(
        (asdict(comp) for comp in comp_valuation.comparables),
        key=lambda comp: (comp["mls_number"], comp["address"]),
    )
    results = {
        "selected_comparables_sha256": digest(selected),
        "eligible_closed_comps": len(selected),
        "arv_low": comp_valuation.arv_low, "arv_likely": comp_valuation.arv_likely,
        "arv_high": comp_valuation.arv_high, "confidence": comp_valuation.confidence,
        "actual_fixture": actual,
        "synthetic_candidate_fixture": {"synthetic": True, **synthetic},
    }
    result_hash = digest(results)
    if EXPECTED_RESULT_SHA256 and result_hash != EXPECTED_RESULT_SHA256:
        raise ReplayError("Replay results differ from the reviewed baseline")
    return {
        "fixture": "arabian-retained-cma-2026-09-26", "as_of": AS_OF.isoformat(),
        "report_subject": EXPECTED_REPORT_SUBJECT, "approved_subject_override": SUBJECT,
        "report_sha256": pdf_hash,
        "input_sha256": digest({
            "report_sha256": pdf_hash, "address": ADDRESS, "subject": SUBJECT,
            "ask": 378_000, "synthetic_ask": 250_000, "as_of": AS_OF.isoformat(),
            "valuation": VALUATION_CONFIG, "screening": SCREENING_CONFIG,
        }),
        "module_sha256": modules, "pypdf_version": importlib.metadata.version("pypdf"),
        "parser_version": cloud_cma.CLOUD_CMA_PARSER_VERSION,
        "parse_diagnostics": payload.get("parseDiagnostics") or {},
        "results": results, "result_sha256": result_hash,
        "baseline_verified": bool(EXPECTED_REPORT_SHA256 and EXPECTED_RESULT_SHA256),
        "side_effects": {"alerts_sent": 0, "cma_requests": 0, "persistent_state_writes": 0},
    }


def main():
    try:
        result = run_replay()
        print("[CMA_REPLAY] " + json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ReplayError) else type(exc).__name__
        print(f"[CMA_REPLAY_ERROR] {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
