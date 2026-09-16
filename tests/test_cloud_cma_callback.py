from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cloud_cma_callback import callback_delivery_url, fetch_result


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class CloudCmaCallbackTests(unittest.TestCase):
    def test_callback_url_contains_encoded_secret(self):
        result = callback_delivery_url("https://worker.example/", "abc/123" + "x" * 32)
        self.assertIn("/callback/abc%2F123", result)

    def test_fetch_result_returns_pdf_url(self):
        with patch(
            "cloud_cma_callback.urlopen",
            return_value=FakeResponse({"pdf_url": "https://cloudcma.com/pdf/abc"}),
        ):
            result = fetch_result("https://worker.example", "a" * 64, "x" * 40)
        self.assertEqual(result, "https://cloudcma.com/pdf/abc")

    def test_fetch_result_returns_none_for_pending(self):
        error = HTTPError("https://worker.example", 404, "pending", {}, None)
        with patch("cloud_cma_callback.urlopen", side_effect=error):
            result = fetch_result("https://worker.example", "a" * 64, "x" * 40)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
