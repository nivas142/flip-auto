from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gcp_cma_recovery_verify",
    ROOT / "scripts/gcp_cma_recovery_verify.py",
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryVerificationTests(unittest.TestCase):
    def test_wrong_report_stops_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.txt"
            path.write_text("not the retained report", encoding="utf-8")
            with patch.object(recovery.cloud_cma, "parse_cloud_cma_pages") as parser:
                with self.assertRaisesRegex(recovery.RecoveryVerificationError, "does not match"):
                    recovery.verify(path)
                parser.assert_not_called()

    def test_cli_errors_do_not_echo_private_path(self):
        private_path = "/tmp/private-address-report.txt"
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            self.assertEqual(recovery.main([private_path]), 1)
        self.assertIn("verification failed", errors.getvalue())
        self.assertNotIn(private_path, errors.getvalue())


if __name__ == "__main__":
    unittest.main()
