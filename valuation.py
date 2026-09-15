"""Independent property-data adapter and transparent comp-based ARV model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Comparable:
    address: str
    price: int
    square_footage: int
    year_built: int | None
    distance: float
    days_old: int
    correlation: float
    adjusted_value: int
    weight: float


@dataclass(frozen=True)
class ValuationResult:
    status: str
    source: str
    arv_low: int | None
    arv_likely: int | None
    arv_high: int | None
    confidence: str
    subject_square_footage: int | None
    comparables: tuple[Comparable, ...]
    reason: str = ""


DEFAULT_VALUATION_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider": "rentcast",
    "max_radius": 1.0,
    "days_old": 180,
    "comp_count": 25,
    "size_tolerance": 0.20,
    "year_tolerance": 10,
    "min_correlation": 0.75,
    "minimum_comps": 3,
}


def merged_valuation_config(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(DEFAULT_VALUATION_CONFIG)
    if raw_config:
        config.update(raw_config)
    return config


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _weighted_quantile(values: list[tuple[int, float]], quantile: float) -> int:
    ordered = sorted(values, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    threshold = total_weight * quantile
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return int(round(value / 1_000.0) * 1_000)
    return int(round(ordered[-1][0] / 1_000.0) * 1_000)


def _eligible_comparables(
    payload: dict[str, Any],
    *,
    subject_sqft: int,
    size_tolerance: float,
    year_tolerance: int,
    min_correlation: float,
    max_radius: float,
    days_old: int,
) -> list[Comparable]:
    subject = payload.get("subjectProperty") or {}
    subject_year = int(_number(subject.get("yearBuilt"))) or None
    subject_type = str(subject.get("propertyType") or "").strip().lower()
    results: list[Comparable] = []

    for raw in payload.get("comparables") or []:
        price = int(_number(raw.get("price")))
        comp_sqft = int(_number(raw.get("squareFootage")))
        if price <= 0 or comp_sqft <= 0:
            continue
        distance = _number(raw.get("distance"), 99.0)
        age = int(_number(raw.get("daysOld"), days_old + 1))
        correlation = _number(raw.get("correlation"))
        if distance > max_radius or age > days_old or correlation < min_correlation:
            continue
        if abs(comp_sqft - subject_sqft) / subject_sqft > size_tolerance:
            continue

        comp_type = str(raw.get("propertyType") or "").strip().lower()
        if subject_type and comp_type and subject_type != comp_type:
            continue
        comp_year = int(_number(raw.get("yearBuilt"))) or None
        if subject_year and comp_year and abs(comp_year - subject_year) > year_tolerance:
            continue

        adjusted_value = int(round((price / comp_sqft) * subject_sqft))
        recency_weight = max(0.20, 1.0 - (age / max(days_old, 1)) * 0.60)
        distance_weight = 1.0 / max(0.20, 1.0 + distance)
        weight = max(correlation, 0.10) ** 2 * recency_weight * distance_weight
        results.append(
            Comparable(
                address=str(raw.get("formattedAddress") or "Unknown"),
                price=price,
                square_footage=comp_sqft,
                year_built=comp_year,
                distance=distance,
                days_old=age,
                correlation=correlation,
                adjusted_value=adjusted_value,
                weight=weight,
            )
        )
    return sorted(results, key=lambda comp: comp.weight, reverse=True)


def calculate_comp_valuation(
    payload: dict[str, Any],
    raw_config: dict[str, Any] | None = None,
) -> ValuationResult:
    config = merged_valuation_config(raw_config)
    subject = payload.get("subjectProperty") or {}
    subject_sqft = int(_number(subject.get("squareFootage"))) or None
    if not subject_sqft:
        return ValuationResult(
            status="unavailable",
            source="rentcast_comps",
            arv_low=None,
            arv_likely=None,
            arv_high=None,
            confidence="insufficient",
            subject_square_footage=None,
            comparables=(),
            reason="subject square footage unavailable",
        )

    kwargs = {
        "subject_sqft": subject_sqft,
        "size_tolerance": float(config["size_tolerance"]),
        "year_tolerance": int(config["year_tolerance"]),
        "min_correlation": float(config["min_correlation"]),
        "max_radius": float(config["max_radius"]),
        "days_old": int(config["days_old"]),
    }
    comps = _eligible_comparables(payload, **kwargs)
    minimum = int(config["minimum_comps"])
    if len(comps) < minimum:
        # A controlled relaxation is more useful than silently expanding to
        # distant or old sales. Radius and age stay fixed.
        kwargs.update(
            size_tolerance=min(0.30, float(config["size_tolerance"]) + 0.10),
            year_tolerance=int(config["year_tolerance"]) + 5,
            min_correlation=max(0.65, float(config["min_correlation"]) - 0.10),
        )
        comps = _eligible_comparables(payload, **kwargs)

    if len(comps) < minimum:
        return ValuationResult(
            status="unavailable",
            source="rentcast_comps",
            arv_low=None,
            arv_likely=None,
            arv_high=None,
            confidence="insufficient",
            subject_square_footage=subject_sqft,
            comparables=tuple(comps),
            reason=f"only {len(comps)} eligible comps",
        )

    weighted_values = [(comp.adjusted_value, comp.weight) for comp in comps]
    arv_low = _weighted_quantile(weighted_values, 0.25)
    arv_likely = _weighted_quantile(weighted_values, 0.50)
    arv_high = _weighted_quantile(weighted_values, 0.75)
    average_correlation = sum(comp.correlation for comp in comps) / len(comps)
    if len(comps) >= 8 and average_correlation >= 0.85:
        confidence = "high"
    elif len(comps) >= 5 and average_correlation >= 0.78:
        confidence = "medium"
    else:
        confidence = "low"

    return ValuationResult(
        status="complete",
        source="rentcast_comps",
        arv_low=arv_low,
        arv_likely=arv_likely,
        arv_high=arv_high,
        confidence=confidence,
        subject_square_footage=subject_sqft,
        comparables=tuple(comps),
    )


def fetch_independent_valuation(
    address: str,
    raw_config: dict[str, Any] | None,
) -> ValuationResult:
    config = merged_valuation_config(raw_config)
    if not config.get("enabled", False):
        return ValuationResult(
            status="disabled",
            source="none",
            arv_low=None,
            arv_likely=None,
            arv_high=None,
            confidence="insufficient",
            subject_square_footage=None,
            comparables=(),
            reason="independent valuation is disabled",
        )
    if str(config.get("provider", "rentcast")).lower() != "rentcast":
        raise ValueError("Unsupported valuation provider")
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        return ValuationResult(
            status="unavailable",
            source="rentcast_comps",
            arv_low=None,
            arv_likely=None,
            arv_high=None,
            confidence="insufficient",
            subject_square_footage=None,
            comparables=(),
            reason="RentCast API key is missing",
        )

    query = urlencode(
        {
            "address": address,
            "maxRadius": config["max_radius"],
            "daysOld": config["days_old"],
            "compCount": config["comp_count"],
            "lookupSubjectAttributes": "true",
        }
    )
    request = Request(
        f"https://api.rentcast.io/v1/avm/value?{query}",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return calculate_comp_valuation(payload, config)
