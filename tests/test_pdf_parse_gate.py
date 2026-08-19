from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight_pdf.py"


def run_gate(bundle: Path) -> dict:
    result = subprocess.run([sys.executable, str(PREFLIGHT), "--bundle", str(bundle)], capture_output=True, text=True)
    report = json.loads(result.stdout)
    report["_returncode"] = result.returncode
    return report


class PdfParseGateTest(unittest.TestCase):
    def make_bundle(self, student: bytes, teacher: bytes = b"%PDF-1.4\ntruncated") -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        bundle = Path(td.name)
        for name, data in (("student.pdf", student), ("teacher.pdf", teacher)):
            (bundle / name).write_bytes(data)
        (bundle / "render-manifest.json").write_text(json.dumps({"assessment_id": "x", "inputs": {}, "outputs": {}}), encoding="utf-8")
        return bundle

    def test_fake_header_is_parse_invalid_not_header_pass(self):
        report = run_gate(self.make_bundle(b"%PDF-1.4\nfake"))
        self.assertNotEqual(report["_returncode"], 0)
        self.assertTrue(any(e["code"] == "PDF_PARSE_INVALID" for e in report["errors"]))

    def test_truncated_pdf_is_parse_invalid(self):
        report = run_gate(self.make_bundle(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>"))
        self.assertNotEqual(report["_returncode"], 0)
        self.assertTrue(any(e["code"] == "PDF_PARSE_INVALID" for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
