from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from test_print_support import ROOT, prepare_positive


class LayoutWhitespaceTest(unittest.TestCase):
    def test_positive_fixture_has_no_excessive_continuous_vertical_gap(self):
        temp, bundle = prepare_positive()
        self.addCleanup(temp.cleanup)
        report = json.loads((bundle / "print-validation-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "PRINT_PREFLIGHT_PASS")
        ratios = [page["empty_ratio"] for pdf in report["pdfs"] for page in pdf["pages"]]
        self.assertLessEqual(max(ratios), 0.15)

    def test_zero_content_pdf_is_hard_failed(self):
        temp, bundle = prepare_positive()
        self.addCleanup(temp.cleanup)
        (bundle / "student.pdf").write_bytes(b"%PDF-1.4\nnot a real document")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "preflight_pdf.py"), "--bundle", str(bundle)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(any(error["code"] == "PDF_PARSE_INVALID" for error in report["errors"]), report)


if __name__ == "__main__":
    unittest.main()
