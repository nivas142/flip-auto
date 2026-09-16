from __future__ import annotations

import unittest

from valuation import calculate_comp_valuation


def valuation_payload() -> dict:
    return {
        # Deliberately absurd: the engine must never use the provider's AVM.
        "price": 9_999_999,
        "subjectProperty": {
            "formattedAddress": "1 Main St, Mesa, AZ 85201",
            "squareFootage": 2_000,
            "yearBuilt": 2000,
            "propertyType": "Single Family",
        },
        "comparables": [
            {
                "formattedAddress": "10 A St",
                "status": "closed",
                "soldPrice": 480_000,
                "squareFootage": 1_900,
                "yearBuilt": 1998,
                "propertyType": "Single Family",
                "distance": 0.2,
                "daysOld": 30,
                "correlation": 0.92,
            },
            {
                "formattedAddress": "20 B St",
                "status": "closed",
                "soldPrice": 500_000,
                "squareFootage": 2_000,
                "yearBuilt": 2001,
                "propertyType": "Single Family",
                "distance": 0.4,
                "daysOld": 50,
                "correlation": 0.88,
            },
            {
                "formattedAddress": "30 C St",
                "status": "closed",
                "soldPrice": 520_000,
                "squareFootage": 2_100,
                "yearBuilt": 2003,
                "propertyType": "Single Family",
                "distance": 0.6,
                "daysOld": 75,
                "correlation": 0.85,
            },
            {
                "formattedAddress": "40 D St",
                "status": "closed",
                "soldPrice": 495_000,
                "squareFootage": 1_980,
                "yearBuilt": 1999,
                "propertyType": "Single Family",
                "distance": 0.3,
                "daysOld": 90,
                "correlation": 0.82,
            },
            {
                "formattedAddress": "50 E St",
                "status": "closed",
                "soldPrice": 510_000,
                "squareFootage": 2_050,
                "yearBuilt": 2000,
                "propertyType": "Single Family",
                "distance": 0.8,
                "daysOld": 120,
                "correlation": 0.80,
            },
            # Rejected: outside size, year, or radius constraints.
            {
                "formattedAddress": "60 Too Large St",
                "status": "closed",
                "soldPrice": 900_000,
                "squareFootage": 3_500,
                "yearBuilt": 2000,
                "propertyType": "Single Family",
                "distance": 0.5,
                "daysOld": 20,
                "correlation": 0.95,
            },
            {
                "formattedAddress": "70 Too Old St",
                "status": "closed",
                "soldPrice": 250_000,
                "squareFootage": 2_000,
                "yearBuilt": 1960,
                "propertyType": "Single Family",
                "distance": 0.4,
                "daysOld": 20,
                "correlation": 0.95,
            },
            {
                "formattedAddress": "80 Too Far St",
                "status": "closed",
                "soldPrice": 800_000,
                "squareFootage": 2_000,
                "yearBuilt": 2000,
                "propertyType": "Single Family",
                "distance": 3.0,
                "daysOld": 20,
                "correlation": 0.95,
            },
        ],
    }


class ValuationTests(unittest.TestCase):
    def test_builds_range_from_eligible_comps_and_ignores_provider_avm(self):
        result = calculate_comp_valuation(valuation_payload())

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.comparables), 5)
        self.assertGreater(result.arv_likely, 450_000)
        self.assertLess(result.arv_likely, 600_000)
        self.assertNotEqual(result.arv_likely, valuation_payload()["price"])

    def test_requires_minimum_comparable_count(self):
        payload = valuation_payload()
        payload["comparables"] = payload["comparables"][:1]

        result = calculate_comp_valuation(payload)

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "only 1 eligible closed comps")


if __name__ == "__main__":
    unittest.main()
