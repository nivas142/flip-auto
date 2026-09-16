"""Pure, side-effect-free first-pass underwriting for wholesale deal alerts.

Sender-provided ARV is intentionally ignored. The calculations only accept an
independent comp valuation produced by the valuation module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from valuation import ValuationResult


MONEY_TOKEN_RE = r"\$?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s*[kKmM]?"


@dataclass(frozen=True)
class DealFacts:
    address: str
    city: str
    ask: int | None
    explicit_rehab: int | None
    sqft: int | None
    beds: float | None
    baths: float | None
    year_built: int | None
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class ScreeningResult:
    status: str
    label: str
    score: int | None
    confidence: str
    ask: int | None
    arv_low: int | None
    arv_likely: int | None
    arv_high: int | None
    valuation_source: str
    comp_count: int
    rehab: int | None
    rehab_is_assumed: bool
    selling_costs: int | None
    other_costs: int | None
    projected_profit: int | None
    basis_percent: float | None
    mao: int | None
    risk_flags: tuple[str, ...]
    missing_fields: tuple[str, ...]


DEFAULT_SCREENING_CONFIG: dict[str, Any] = {
    "enabled": True,
    "target_profit": 50_000,
    "selling_cost_percent": 0.07,
    "other_costs": 12_000,
    "max_basis_percent": 0.80,
    "default_rehab_per_sqft": 20,
    "fallback_rehab": 35_000,
}


RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("occupied/eviction", re.compile(r"\b(?:occupied|eviction|post[- ]?possession|tenant)\b", re.I)),
    ("no interior access", re.compile(r"\b(?:no interior access|interior access not available|drive[- ]?by only)\b", re.I)),
    ("fire damage", re.compile(r"\b(?:fire damage|fire damaged|burn(?:ed|t))\b", re.I)),
    ("foundation", re.compile(r"\bfoundation\b", re.I)),
    ("unpermitted work", re.compile(r"\b(?:unpermitted|not permitted|no permits?)\b", re.I)),
    ("septic", re.compile(r"\bseptic\b", re.I)),
)

HIGH_RISK_FLAGS = {
    "occupied/eviction",
    "no interior access",
    "fire damage",
    "foundation",
    "unpermitted work",
}


def parse_money(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value > 0 else None
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*([kKmM]?)", str(value))
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    suffix = match.group(2).lower()
    if suffix == "k":
        amount *= 1_000
    elif suffix == "m":
        amount *= 1_000_000
    rounded = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return rounded if rounded > 0 else None


def _first_number(patterns: tuple[str, ...], text: str, *, as_float: bool = False) -> float | int | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = match.group(1).replace(",", "")
        try:
            return float(value) if as_float else int(float(value))
        except ValueError:
            continue
    return None


def _labeled_money(labels: tuple[str, ...], text: str) -> int | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"\b(?:{label_pattern})\s*:?\s*(?P<amount>{MONEY_TOKEN_RE})", text, re.I)
    return parse_money(match.group("amount")) if match else None


def normalize_address_key(address: str) -> str:
    normalized = address.lower().replace("#", " unit ")
    normalized = re.sub(r"\b(?:street)\b", "st", normalized)
    normalized = re.sub(r"\b(?:road)\b", "rd", normalized)
    normalized = re.sub(r"\b(?:drive)\b", "dr", normalized)
    normalized = re.sub(r"\b(?:avenue)\b", "ave", normalized)
    normalized = re.sub(r"\b(?:boulevard)\b", "blvd", normalized)
    normalized = re.sub(r"\b(?:lane)\b", "ln", normalized)
    normalized = re.sub(r"\b(?:court)\b", "ct", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def extract_deal_facts(*, address: str, city: str, price: str, summary: str) -> DealFacts:
    text = " ".join((summary or "").split())
    ask = parse_money(price) or _labeled_money(("all-in price", "wholesale price", "asking price", "price"), text)
    explicit_rehab = _labeled_money(("rehab", "repairs", "renovation"), text)
    sqft = _first_number(
        (
            r"\b([0-9][0-9,]{2,})\s*(?:sq\.?\s*ft\.?|sqft|sf)\b",
            r"\b(?:property\s+)?(?:sits|standing)\s+at\s+([0-9][0-9,]{2,})\s+square\s+feet\b",
        ),
        text,
    )
    beds = _first_number((r"\b([0-9]+(?:\.5)?)\s*(?:bed|beds|br)\b",), text, as_float=True)
    baths = _first_number((r"\b([0-9]+(?:\.5)?)\s*(?:bath|baths|ba)\b",), text, as_float=True)
    year_built = _first_number(
        (r"\b(?:built|year\s+built|year\s+build(?:\s+is)?)\s*:?[ ]*(19[0-9]{2}|20[0-9]{2})\b",),
        text,
    )
    risk_flags = tuple(label for label, pattern in RISK_PATTERNS if pattern.search(text))
    return DealFacts(
        address=address,
        city=city,
        ask=ask,
        explicit_rehab=explicit_rehab,
        sqft=int(sqft) if sqft is not None else None,
        beds=float(beds) if beds is not None else None,
        baths=float(baths) if baths is not None else None,
        year_built=int(year_built) if year_built is not None else None,
        risk_flags=risk_flags,
    )


def merged_screening_config(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(DEFAULT_SCREENING_CONFIG)
    if raw_config:
        result.update(raw_config)
    return result


def screen_deal(
    facts: DealFacts,
    valuation: ValuationResult | None = None,
    raw_config: dict[str, Any] | None = None,
) -> ScreeningResult:
    config = merged_screening_config(raw_config)
    missing: list[str] = []
    if facts.ask is None:
        missing.append("ask")
    if valuation is None or valuation.status != "complete" or valuation.arv_low is None:
        missing.append("independent comp ARV")

    rehab_is_assumed = facts.explicit_rehab is None
    if facts.explicit_rehab is not None:
        rehab = facts.explicit_rehab
    elif facts.sqft:
        rehab = int(round(facts.sqft * float(config["default_rehab_per_sqft"])))
    else:
        rehab = int(config["fallback_rehab"])

    if missing:
        return ScreeningResult(
            status="valuation_required",
            label="⚪ VALUATION REQUIRED",
            score=None,
            confidence="insufficient",
            ask=facts.ask,
            arv_low=valuation.arv_low if valuation else None,
            arv_likely=valuation.arv_likely if valuation else None,
            arv_high=valuation.arv_high if valuation else None,
            valuation_source=valuation.source if valuation else "none",
            comp_count=len(valuation.comparables) if valuation else 0,
            rehab=rehab,
            rehab_is_assumed=rehab_is_assumed,
            selling_costs=None,
            other_costs=None,
            projected_profit=None,
            basis_percent=None,
            mao=None,
            risk_flags=facts.risk_flags,
            missing_fields=tuple(missing),
        )

    assert facts.ask is not None and valuation is not None and valuation.arv_low is not None
    # Use the lower end of our independent comp range for automated screening.
    # The likely/high values remain visible for human review.
    underwritten_arv = valuation.arv_low
    selling_costs = int(round(underwritten_arv * float(config["selling_cost_percent"])))
    other_costs = int(config["other_costs"])
    target_profit = int(config["target_profit"])
    max_basis = float(config["max_basis_percent"])
    projected_profit = underwritten_arv - facts.ask - rehab - selling_costs - other_costs
    basis_percent = (facts.ask + rehab) / underwritten_arv
    mao = underwritten_arv - rehab - selling_costs - other_costs - target_profit

    profit_points = max(0.0, min(40.0, 40.0 * projected_profit / max(target_profit, 1)))
    basis_room = (0.95 - basis_percent) / max(0.95 - max_basis, 0.01)
    basis_points = max(0.0, min(30.0, 30.0 * basis_room))
    completeness_points = 10.0
    completeness_points += 4.0 if facts.sqft else 0.0
    completeness_points += 2.0 if facts.beds is not None else 0.0
    completeness_points += 2.0 if facts.baths is not None else 0.0
    completeness_points += 2.0 if facts.year_built else 0.0
    risk_penalty = min(20.0, len(facts.risk_flags) * 7.0)

    # Public-data comps still lack ARMLS photos/concessions and a renovation
    # condition review, so they cannot promote a lead into the 85+ tier alone.
    score = min(84, max(0, int(round(profit_points + basis_points + completeness_points - risk_penalty))))
    high_risk = bool(set(facts.risk_flags) & HIGH_RISK_FLAGS)
    strong_economics = projected_profit >= target_profit and basis_percent <= max_basis
    if strong_economics and not high_risk:
        status, label = "candidate", "🟢 CMA CANDIDATE"
    elif strong_economics and high_risk:
        status, label = "high_risk_review", "🟡 HIGH-RISK REVIEW"
    elif projected_profit >= max(15_000, int(target_profit * 0.30)) and basis_percent < 0.90:
        status, label = "price_dependent", "🟡 PRICE DEPENDENT"
    else:
        status, label = "pass", "🔴 PRELIMINARY PASS"

    complete_count = sum(
        value is not None
        for value in (facts.ask, facts.sqft, facts.beds, facts.baths, facts.year_built)
    )
    confidence = valuation.confidence if complete_count >= 4 and not rehab_is_assumed else "low"

    return ScreeningResult(
        status=status,
        label=label,
        score=score,
        confidence=confidence,
        ask=facts.ask,
        arv_low=valuation.arv_low,
        arv_likely=valuation.arv_likely,
        arv_high=valuation.arv_high,
        valuation_source=valuation.source,
        comp_count=len(valuation.comparables),
        rehab=rehab,
        rehab_is_assumed=rehab_is_assumed,
        selling_costs=selling_costs,
        other_costs=other_costs,
        projected_profit=projected_profit,
        basis_percent=basis_percent,
        mao=max(0, mao),
        risk_flags=facts.risk_flags,
        missing_fields=(),
    )


def format_money(value: int | None) -> str:
    return "Unknown" if value is None else f"${value:,.0f}"


def format_screening_result(result: ScreeningResult) -> str:
    lines = [f"Screen: {result.label}"]
    if result.score is not None:
        lines.append(f"Pre-screen score: {result.score}/100 ({result.confidence} confidence)")
    if result.arv_low is not None:
        lines.append(
            "Independent comp ARV: "
            f"{format_money(result.arv_low)}–{format_money(result.arv_high)} "
            f"(likely {format_money(result.arv_likely)})"
        )
        lines.append(f"Underwritten ARV: {format_money(result.arv_low)}")
        lines.append(f"Comps used: {result.comp_count} | Source: {result.valuation_source}")
    rehab_suffix = " (assumed)" if result.rehab_is_assumed else " (provided)"
    lines.append(f"Rehab allowance: {format_money(result.rehab)}{rehab_suffix}")
    if result.projected_profit is not None:
        lines.append(f"Preliminary profit: {format_money(result.projected_profit)}")
        lines.append(f"Purchase + rehab / underwritten ARV: {result.basis_percent:.1%}")
        lines.append(f"Target MAO: {format_money(result.mao)}")
    if result.risk_flags:
        lines.append(f"Risk flags: {', '.join(result.risk_flags)}")
    if result.missing_fields:
        lines.append(f"Missing: {', '.join(result.missing_fields)}")
    lines.append("Next step: Verify condition and final ARV with ARMLS before making an offer")
    return "\n".join(lines)
