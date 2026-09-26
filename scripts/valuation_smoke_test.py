#!/usr/bin/env python3
"""Run one real Cloud CMA report through valuation and send a TEST alert."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from cloud_cma import download_cloud_cma_pdf, parse_cloud_cma_pdf
from monitor import AlertItem, PropertyDeal, build_deal_alert, send_telegram_alert
from valuation import calculate_comp_valuation


REPORT_URL = "https://cloudcma.com/pdf/53882d4d8b32c72fe05cf1bd9b053817"
ADDRESS = "2010 E Arabian Dr, Gilbert, AZ 85296"


def main() -> int:
    pdf = download_cloud_cma_pdf(REPORT_URL)
    payload = parse_cloud_cma_pdf(
        pdf,
        requested_address=ADDRESS,
        subject_overrides={
            "squareFootage": 1625,
            "beds": 4,
            "baths": 3,
            "yearBuilt": 1997,
        },
        as_of=datetime.now(timezone.utc).date(),
    )
    valuation = calculate_comp_valuation(
        payload,
        {
            "enabled": True,
            "minimum_comps": 3,
            "days_old": 180,
            "size_tolerance": 0.20,
            "year_tolerance": 10,
            "max_radius": 1.0,
        },
    )
    if valuation.status != "complete":
        raise RuntimeError(f"Smoke-test valuation unavailable: {valuation.reason}")

    item = build_deal_alert(
        deal=PropertyDeal(
            city="Gilbert",
            address=ADDRESS,
            price="$378,000",
            details_url=REPORT_URL,
            image_url="",
            summary=(
                "4 bed / 3 bath / 1,625 sqft / built 1997. "
                "Existing Cloud CMA parser-v2 smoke test."
            ),
        ),
        label="valuation-smoke-test",
        from_header="Existing Cloud CMA report",
        subject="TEST — valuation and Telegram path",
        received_at="",
        screening_cfg={
            "enabled": True,
            "target_profit": 50_000,
            "selling_cost_percent": 0.07,
            "other_costs": 12_000,
            "max_basis_percent": 0.80,
            "default_rehab_per_sqft": 20,
            "fallback_rehab": 35_000,
        },
        valuation=valuation,
    )
    diagnostics = payload.get("parseDiagnostics") or {}
    test_item = AlertItem(
        source=item.source,
        item_id=item.item_id,
        title=f"🧪 TEST — {item.title}",
        city=item.city,
        notify=True,
        body=(
            "Delivery test only. The normal engine would not notify because this "
            "property is currently classified as a preliminary pass.\n"
            f"Parser diagnostics: {diagnostics}\n"
            f"{item.body}"
        ),
    )
    send_telegram_alert(
        {
            "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        },
        test_item,
    )
    print(
        "Valuation smoke test sent: "
        f"{diagnostics.get('parsedClosedComparables', 0)} parsed comps, "
        f"likely ARV ${valuation.arv_likely:,}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
