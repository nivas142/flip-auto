from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs

from cloud_cma import (
    CloudCmaReportTooLarge,
    download_cloud_cma_pdf,
    parse_cloud_cma_pages,
    request_quick_cma,
)
from valuation import calculate_comp_valuation


MAP_PAGE = """Map of Comparable Listings
STATUS: S = CLOSED P = PENDING A = ACTIVE
MLS # STATUS ADDRESS BEDS BATHS SQFT PRICE
1 Subject 2010 E Arabian Dr 4 3.00 1,625 -
2 7051111 S 754 S SORRELL Lane 4 3.00 1,610 $425,000
"""


def detail_page(*, mls: str, address: str, price: int, sqft: int, sold: str) -> str:
    return f"""{address} Gilbert, AZ 85296 MLS #{mls}
${price:,} 4 Beds 3.00 Baths {sqft:,} Sq. Ft. ($264 / sqft)
CLOSED {sold} Year Built 1997 Days on market: 3
Details
Prop Type: Single Family Residence
County: Maricopa
Subdivision: FINLEY FARMS SOUTH PARCEL 18
Full baths: 3.0
Lot Size (sqft): 5,124
Garages: 2
List date: 7/7/26
Sold date: {sold}
List Price: ${price:,}
Orig list price: ${price - 5_000:,}
Pool Features: None
"""


class CloudCmaTests(unittest.TestCase):
    def test_download_rejects_content_length_over_limit_before_reading(self):
        class FakeResponse:
            headers = {"Content-Type": "application/pdf", "Content-Length": "101"}
            read_called = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size=-1):
                self.read_called = True
                return b"%PDF"

        response = FakeResponse()
        with patch("cloud_cma.urlopen", return_value=response):
            with self.assertRaises(CloudCmaReportTooLarge):
                download_cloud_cma_pdf("https://cloudcma.com/pdf/test", max_bytes=100)
        self.assertFalse(response.read_called)

    def test_download_rejects_chunked_pdf_over_limit(self):
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self):
                self.body = b"%PDF-" + b"x" * 100

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size=-1):
                if not self.body:
                    return b""
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        with patch("cloud_cma.urlopen", return_value=FakeResponse()):
            with self.assertRaises(CloudCmaReportTooLarge):
                download_cloud_cma_pdf("https://cloudcma.com/pdf/test", max_bytes=100)

    def test_quick_cma_uses_webhook_without_email_delivery(self):
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"ok"

        def fake_urlopen(request, timeout):
            captured.update(parse_qs(request.data.decode("utf-8")))
            return FakeResponse()

        with patch("cloud_cma.urlopen", side_effect=fake_urlopen):
            result = request_quick_cma(
                api_key="api-secret",
                address="2010 E Arabian Dr, Gilbert, AZ 85296",
                callback_url="https://worker.example/callback/secret",
                job_id="a" * 64,
            )

        self.assertTrue(result.accepted)
        self.assertEqual(captured["job_id"], ["a" * 64])
        self.assertIn("callback_url", captured)
        self.assertNotIn("email_to", captured)
        self.assertNotIn("email_subject", captured)

    def test_parses_closed_mls_details_and_subject(self):
        pages = [
            MAP_PAGE,
            detail_page(
                mls="7051111",
                address="754 S SORRELL Lane",
                price=425_000,
                sqft=1_610,
                sold="8/7/26",
            ),
        ]
        payload = parse_cloud_cma_pages(
            pages,
            requested_address="2010 E Arabian Dr, Gilbert, AZ 85296",
            as_of=date(2026, 9, 16),
        )

        self.assertEqual(payload["subjectProperty"]["squareFootage"], 1_625)
        self.assertEqual(payload["subjectProperty"]["beds"], 4)
        self.assertEqual(len(payload["comparables"]), 1)
        comp = payload["comparables"][0]
        self.assertEqual(comp["soldPrice"], 425_000)
        self.assertEqual(comp["mlsNumber"], "7051111")
        self.assertFalse(comp["hasPool"])

    def test_cloud_cma_average_or_list_price_cannot_feed_arv(self):
        pages = [MAP_PAGE]
        for index, price in enumerate((425_000, 435_000, 445_000), start=1):
            pages.append(
                detail_page(
                    mls=f"700000{index}",
                    address=f"{700 + index} S COMP Lane",
                    price=price,
                    sqft=1_610 + index,
                    sold="8/7/26",
                )
            )
        payload = parse_cloud_cma_pages(
            pages,
            requested_address="2010 E Arabian Dr, Gilbert, AZ 85296",
            as_of=date(2026, 9, 16),
        )
        payload["suggestedPrice"] = 9_999_999
        payload["comparables"].append(
            {
                "status": "active",
                "soldPrice": 9_999_999,
                "squareFootage": 1_625,
                "daysOld": 1,
            }
        )

        result = calculate_comp_valuation(payload)

        self.assertEqual(result.status, "complete")
        self.assertLess(result.arv_high, 500_000)
        self.assertEqual(len(result.comparables), 3)


if __name__ == "__main__":
    unittest.main()
