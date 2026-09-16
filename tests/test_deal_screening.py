from __future__ import annotations

import unittest

from deal_screening import (
    extract_deal_facts,
    format_screening_result,
    normalize_address_key,
    screen_deal,
)
from valuation import Comparable, ValuationResult


def independent_valuation(
    *, low: int = 650_000, likely: int = 670_000, high: int = 690_000
) -> ValuationResult:
    comps = tuple(
        Comparable(
            address=f"{number} Comp St, Phoenix, AZ",
            price=low + number * 5_000,
            square_footage=2_000,
            year_built=1976,
            distance=0.1 * number,
            days_old=20 * number,
            correlation=0.90,
            adjusted_value=low + number * 5_000,
            weight=1.0,
        )
        for number in range(1, 6)
    )
    return ValuationResult(
        status="complete",
        source="cloud_cma_armls_comps",
        arv_low=low,
        arv_likely=likely,
        arv_high=high,
        confidence="medium",
        subject_square_footage=2_104,
        comparables=comps,
    )


class DealScreeningTests(unittest.TestCase):
    def test_extracts_fields_but_ignores_sender_arv(self):
        facts = extract_deal_facts(
            address="3462 E Hearn Road, Phoenix, AZ 85032",
            city="Phoenix",
            price="$430,000",
            summary=(
                "Single Family Home • 5 Bed / 2 Bath • 2,104 SF • "
                "Built 1976 • Pool ARV: $9,999,999 Rehab: $42,500"
            ),
        )

        self.assertEqual(facts.ask, 430_000)
        self.assertFalse(hasattr(facts, "claimed_arv"))
        self.assertEqual(facts.explicit_rehab, 42_500)
        self.assertEqual(facts.sqft, 2_104)
        self.assertEqual(facts.beds, 5)
        self.assertEqual(facts.baths, 2)
        self.assertEqual(facts.year_built, 1976)

    def test_calculations_use_conservative_independent_arv(self):
        facts = extract_deal_facts(
            address="3462 E Hearn Rd, Phoenix, AZ 85032",
            city="Phoenix",
            price="$430,000",
            summary=(
                "5 Bed / 2 Bath • 2,104 SF • Built 1976 • "
                "ARV: $9,999,999 • Rehab: $42,500"
            ),
        )
        result = screen_deal(facts, independent_valuation())

        self.assertEqual(result.selling_costs, 45_500)
        self.assertEqual(result.other_costs, 12_000)
        self.assertEqual(result.projected_profit, 120_000)
        self.assertAlmostEqual(result.basis_percent, 472_500 / 650_000)
        self.assertEqual(result.mao, 500_000)
        self.assertLessEqual(result.score, 84)
        rendered = format_screening_result(result)
        self.assertIn("Independent comp ARV", rendered)
        self.assertNotIn("claimed", rendered.lower())
        self.assertNotIn("9,999,999", rendered)

    def test_missing_independent_valuation_is_not_scored(self):
        facts = extract_deal_facts(
            address="1204 W McNair St, Chandler, AZ 85224",
            city="Chandler",
            price="$399,000",
            summary="3 Bed / 2 Bath • 1,500 SF • Built 1982 • ARV $900,000",
        )
        result = screen_deal(facts)

        self.assertEqual(result.status, "valuation_required")
        self.assertIsNone(result.score)
        self.assertIn("independent comp ARV", result.missing_fields)

    def test_assumes_rehab_from_square_feet(self):
        facts = extract_deal_facts(
            address="1 Main St, Mesa, AZ 85201",
            city="Mesa",
            price="$300,000",
            summary="1,800 SF ARV: $9,999,999",
        )
        result = screen_deal(
            facts,
            independent_valuation(low=475_000, likely=490_000, high=505_000),
            {"default_rehab_per_sqft": 25},
        )

        self.assertEqual(result.rehab, 45_000)
        self.assertTrue(result.rehab_is_assumed)

    def test_risk_flags_reduce_score_and_block_candidate(self):
        common = {
            "address": "1 Main St, Mesa, AZ 85201",
            "city": "Mesa",
            "price": "$300,000",
        }
        valuation = independent_valuation(low=500_000, likely=515_000, high=530_000)
        clean = screen_deal(extract_deal_facts(summary="1,800 SF", **common), valuation)
        risky = screen_deal(
            extract_deal_facts(
                summary="1,800 SF. Occupied; buyer handles eviction. No interior access.",
                **common,
            ),
            valuation,
        )

        self.assertIn("occupied/eviction", risky.risk_flags)
        self.assertIn("no interior access", risky.risk_flags)
        self.assertLess(risky.score, clean.score)
        self.assertEqual(clean.status, "candidate")
        self.assertEqual(risky.status, "high_risk_review")

    def test_address_normalization_dedupes_common_variants(self):
        self.assertEqual(
            normalize_address_key("3462 E Hearn Road, Phoenix, AZ 85032"),
            normalize_address_key("3462 E. Hearn Rd Phoenix AZ 85032"),
        )


if __name__ == "__main__":
    unittest.main()
