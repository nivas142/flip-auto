from __future__ import annotations

import unittest

from deal_screening import (
    extract_deal_facts,
    format_screening_result,
    normalize_address_key,
    screen_deal,
)


class DealScreeningTests(unittest.TestCase):
    def test_extracts_common_wholesaler_fields(self):
        facts = extract_deal_facts(
            address="3462 E Hearn Road, Phoenix, AZ 85032",
            city="Phoenix",
            price="$430,000",
            summary=(
                "Single Family Home • 5 Bed / 2 Bath • 2,104 SF • "
                "Built 1976 • Pool ARV: $690,000 Rehab: $42,500"
            ),
        )

        self.assertEqual(facts.ask, 430_000)
        self.assertEqual(facts.claimed_arv, 690_000)
        self.assertEqual(facts.explicit_rehab, 42_500)
        self.assertEqual(facts.sqft, 2_104)
        self.assertEqual(facts.beds, 5)
        self.assertEqual(facts.baths, 2)
        self.assertEqual(facts.year_built, 1976)

    def test_calculates_profit_basis_and_mao(self):
        facts = extract_deal_facts(
            address="3462 E Hearn Rd, Phoenix, AZ 85032",
            city="Phoenix",
            price="$430,000",
            summary="5 Bed / 2 Bath • 2,104 SF • Built 1976 • ARV: $690,000 • Rehab: $42,500",
        )
        result = screen_deal(facts)

        self.assertEqual(result.selling_costs, 48_300)
        self.assertEqual(result.other_costs, 12_000)
        self.assertEqual(result.projected_profit, 157_200)
        self.assertAlmostEqual(result.basis_percent, 472_500 / 690_000)
        self.assertEqual(result.mao, 537_200)
        self.assertLessEqual(result.score, 84)
        self.assertIn("UNVERIFIED", format_screening_result(result))

    def test_missing_claimed_arv_is_not_scored(self):
        facts = extract_deal_facts(
            address="1204 W McNair St, Chandler, AZ 85224",
            city="Chandler",
            price="$399,000",
            summary="3 Bed / 2 Bath • 1,500 SF • Built 1982",
        )
        result = screen_deal(facts)

        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(result.score)
        self.assertIn("claimed ARV", result.missing_fields)

    def test_parses_abbreviated_arv_range_conservatively(self):
        facts = extract_deal_facts(
            address="2024 E 7th Ave, Mesa, AZ 85204",
            city="Mesa",
            price="$375,000",
            summary="1,872 SF • ARV: $500K–$525K",
        )

        self.assertEqual(facts.claimed_arv, 500_000)
        self.assertEqual(screen_deal(facts).status, "price_dependent")

    def test_assumes_rehab_from_square_feet(self):
        facts = extract_deal_facts(
            address="1 Main St, Mesa, AZ 85201",
            city="Mesa",
            price="$300,000",
            summary="1,800 SF ARV: $475,000",
        )
        result = screen_deal(facts, {"default_rehab_per_sqft": 25})

        self.assertEqual(result.rehab, 45_000)
        self.assertTrue(result.rehab_is_assumed)

    def test_risk_flags_reduce_score(self):
        common = {
            "address": "1 Main St, Mesa, AZ 85201",
            "city": "Mesa",
            "price": "$300,000",
        }
        clean = screen_deal(extract_deal_facts(summary="1,800 SF ARV: $500,000", **common))
        risky = screen_deal(
            extract_deal_facts(
                summary="1,800 SF ARV: $500,000. Occupied; buyer handles eviction. No interior access.",
                **common,
            )
        )

        self.assertIn("occupied/eviction", risky.risk_flags)
        self.assertIn("no interior access", risky.risk_flags)
        self.assertLess(risky.score, clean.score)
        self.assertEqual(risky.status, "high_risk_review")

    def test_address_normalization_dedupes_common_variants(self):
        self.assertEqual(
            normalize_address_key("3462 E Hearn Road, Phoenix, AZ 85032"),
            normalize_address_key("3462 E. Hearn Rd Phoenix AZ 85032"),
        )


if __name__ == "__main__":
    unittest.main()
