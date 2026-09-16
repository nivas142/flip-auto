"""Transparent ARV calculation from normalized closed MLS comparables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Comparable:
    address: str
    price: int
    square_footage: int
    year_built: int | None
    distance: float | None
    days_old: int
    correlation: float
    adjusted_value: int
    weight: float
    mls_number: str = ""
    beds: float | None = None
    baths: float | None = None
    subdivision: str = ""
    has_pool: bool | None = None


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
    "provider": "cloud_cma",
    "max_radius": 1.0,
    "days_old": 180,
    "size_tolerance": 0.20,
    "year_tolerance": 10,
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


def _similarity(raw: dict[str, Any], subject: dict[str, Any]) -> float:
    subject_sqft = int(_number(subject.get("squareFootage")))
    comp_sqft = int(_number(raw.get("squareFootage")))
    size_score = 1.0 - min(1.0, abs(comp_sqft - subject_sqft) / max(subject_sqft, 1))
    parts = [(size_score, 0.50)]

    subject_year = int(_number(subject.get("yearBuilt"))) or None
    comp_year = int(_number(raw.get("yearBuilt"))) or None
    if subject_year and comp_year:
        year_score = 1.0 - min(1.0, abs(comp_year - subject_year) / 20.0)
        parts.append((year_score, 0.20))

    subject_beds = _number(subject.get("beds"), -1)
    comp_beds = _number(raw.get("beds"), -1)
    if subject_beds >= 0 and comp_beds >= 0:
        parts.append((max(0.0, 1.0 - abs(subject_beds - comp_beds) * 0.35), 0.15))

    subject_baths = _number(subject.get("baths"), -1)
    comp_baths = _number(raw.get("baths"), -1)
    if subject_baths >= 0 and comp_baths >= 0:
        parts.append((max(0.0, 1.0 - abs(subject_baths - comp_baths) * 0.30), 0.15))

    total_weight = sum(weight for _, weight in parts)
    score = sum(value * weight for value, weight in parts) / total_weight
    subject_subdivision = str(subject.get("subdivision") or "").strip().lower()
    comp_subdivision = str(raw.get("subdivision") or "").strip().lower()
    if subject_subdivision and comp_subdivision:
        if subject_subdivision in comp_subdivision or comp_subdivision in subject_subdivision:
            score = min(1.0, score + 0.08)
        else:
            score *= 0.90
    return score


def _eligible_comparables(
    payload: dict[str, Any],
    *,
    subject_sqft: int,
    size_tolerance: float,
    year_tolerance: int,
    max_radius: float,
    days_old: int,
) -> list[Comparable]:
    subject = payload.get("subjectProperty") or {}
    subject_year = int(_number(subject.get("yearBuilt"))) or None
    subject_type = str(subject.get("propertyType") or "").strip().lower()
    subject_beds = _number(subject.get("beds"), -1)
    subject_baths = _number(subject.get("baths"), -1)
    results: list[Comparable] = []

    for raw in payload.get("comparables") or []:
        # A confirmed closed price is mandatory. Never fall back to a generic
        # provider price or an active/pending list price.
        if str(raw.get("status") or "").lower() not in {"closed", "sold", "s"}:
            continue
        price = int(_number(raw.get("soldPrice")))
        comp_sqft = int(_number(raw.get("squareFootage")))
        if price <= 0 or comp_sqft <= 0:
            continue
        age = int(_number(raw.get("daysOld"), days_old + 1))
        if age > days_old:
            continue
        if abs(comp_sqft - subject_sqft) / subject_sqft > size_tolerance:
            continue

        distance_value = raw.get("distance")
        distance = _number(distance_value, -1.0) if distance_value is not None else None
        if distance is not None and distance >= 0 and distance > max_radius:
            continue

        comp_type = str(raw.get("propertyType") or "").strip().lower()
        if subject_type and comp_type and subject_type not in comp_type and comp_type not in subject_type:
            continue
        comp_year = int(_number(raw.get("yearBuilt"))) or None
        if subject_year and comp_year and abs(comp_year - subject_year) > year_tolerance:
            continue

        comp_beds = _number(raw.get("beds"), -1)
        comp_baths = _number(raw.get("baths"), -1)
        if subject_beds >= 0 and comp_beds >= 0 and abs(subject_beds - comp_beds) > 1:
            continue
        if subject_baths >= 0 and comp_baths >= 0 and abs(subject_baths - comp_baths) > 1:
            continue

        correlation = _similarity(raw, subject)
        adjusted_value = int(round((price / comp_sqft) * subject_sqft))
        recency_weight = max(0.20, 1.0 - (age / max(days_old, 1)) * 0.60)
        distance_weight = 1.0 if distance is None or distance < 0 else 1.0 / max(0.20, 1.0 + distance)
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
                mls_number=str(raw.get("mlsNumber") or ""),
                beds=comp_beds if comp_beds >= 0 else None,
                baths=comp_baths if comp_baths >= 0 else None,
                subdivision=str(raw.get("subdivision") or ""),
                has_pool=raw.get("hasPool") if isinstance(raw.get("hasPool"), bool) else None,
            )
        )
    return sorted(results, key=lambda comp: comp.weight, reverse=True)


def calculate_comp_valuation(
    payload: dict[str, Any],
    raw_config: dict[str, Any] | None = None,
) -> ValuationResult:
    """Calculate an ARV range using only confirmed closed-sale prices."""
    config = merged_valuation_config(raw_config)
    subject = payload.get("subjectProperty") or {}
    subject_sqft = int(_number(subject.get("squareFootage"))) or None
    if not subject_sqft:
        return ValuationResult(
            status="unavailable",
            source="cloud_cma_armls_comps",
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
        "max_radius": float(config["max_radius"]),
        "days_old": int(config["days_old"]),
    }
    comps = _eligible_comparables(payload, **kwargs)
    minimum = int(config["minimum_comps"])
    if len(comps) < minimum:
        kwargs.update(
            size_tolerance=min(0.30, float(config["size_tolerance"]) + 0.10),
            year_tolerance=int(config["year_tolerance"]) + 5,
        )
        comps = _eligible_comparables(payload, **kwargs)

    if len(comps) < minimum:
        return ValuationResult(
            status="unavailable",
            source="cloud_cma_armls_comps",
            arv_low=None,
            arv_likely=None,
            arv_high=None,
            confidence="insufficient",
            subject_square_footage=subject_sqft,
            comparables=tuple(comps),
            reason=f"only {len(comps)} eligible closed comps",
        )

    weighted_values = [(comp.adjusted_value, comp.weight) for comp in comps]
    arv_low = _weighted_quantile(weighted_values, 0.25)
    arv_likely = _weighted_quantile(weighted_values, 0.50)
    arv_high = _weighted_quantile(weighted_values, 0.75)
    average_correlation = sum(comp.correlation for comp in comps) / len(comps)
    distance_verified = all(comp.distance is not None and comp.distance >= 0 for comp in comps)
    if distance_verified and len(comps) >= 8 and average_correlation >= 0.85:
        confidence = "high"
    elif distance_verified and len(comps) >= 5 and average_correlation >= 0.78:
        confidence = "medium"
    else:
        confidence = "low"

    return ValuationResult(
        status="complete",
        source="cloud_cma_armls_comps",
        arv_low=arv_low,
        arv_likely=arv_likely,
        arv_high=arv_high,
        confidence=confidence,
        subject_square_footage=subject_sqft,
        comparables=tuple(comps),
        reason="distance not numerically verified" if not distance_verified else "",
    )


def pending_valuation(reason: str = "Cloud CMA report requested") -> ValuationResult:
    return ValuationResult(
        status="pending",
        source="cloud_cma_armls_comps",
        arv_low=None,
        arv_likely=None,
        arv_high=None,
        confidence="insufficient",
        subject_square_footage=None,
        comparables=(),
        reason=reason,
    )


def unavailable_valuation(reason: str) -> ValuationResult:
    return ValuationResult(
        status="unavailable",
        source="cloud_cma_armls_comps",
        arv_low=None,
        arv_likely=None,
        arv_high=None,
        confidence="insufficient",
        subject_square_footage=None,
        comparables=(),
        reason=reason,
    )
