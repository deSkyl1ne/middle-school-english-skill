from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prd_fixture_runtime import create_compact_source, create_source, last_json, run


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class CompactProfileRuntimeTest(unittest.TestCase):
    def test_compact_profile_runs_real_render_preflight_and_two_projection_gates(self):
        with tempfile.TemporaryDirectory(prefix="mse-compact-profile-") as td:
            root = Path(td)
            source = create_compact_source(root)
            bundle = root / "bundle"
            result = run([
                sys.executable,
                str(SCRIPTS / "render_pdf.py"),
                "--request",
                str(source / "render-request.json"),
                "--bundle-out",
                str(bundle),
            ])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(last_json(result).get("status"), "RENDERED")
            preflight = run([
                sys.executable,
                str(SCRIPTS / "preflight_pdf.py"),
                "--bundle",
                str(bundle),
            ])
            self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
            manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["profiles"]["base"]["path"], "generic-cn-compact-v1.json")
            self.assertEqual(manifest["profiles"]["resolved"]["path"], "resolved-profile.json")
            self.assertTrue((bundle / manifest["profiles"]["resolved"]["path"]).is_file())
            report = json.loads((bundle / "print-validation-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PRINT_PREFLIGHT_PASS")
            self.assertFalse(report["errors"])
            self.assertEqual({pdf["document"] for pdf in report["pdfs"]}, {"student", "teacher"})
            for pdf in report["pdfs"]:
                self.assertTrue(pdf["pages"])
                self.assertLessEqual(max(page["empty_ratio"] for page in pdf["pages"]), 0.15)

    def test_student_choice_starts_on_new_page_after_long_preceding_item(self):
        with tempfile.TemporaryDirectory(prefix="mse-choice-pagination-") as td:
            root = Path(td)
            with patch.dict(os.environ, {"MSE_PRD_CUSTOM_ITEM_COUNT": "2", "MSE_TASK_PASSAGE_UNITS": "80"}):
                source = create_source("print-task-reading-three-short-answers", root)
            bundle = root / "bundle"
            rendered = run([
                sys.executable,
                str(SCRIPTS / "render_pdf.py"),
                "--request", str(source / "render-request.json"),
                "--bundle-out", str(bundle),
            ])
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
            items = json.loads((source / "assessment.json").read_text(encoding="utf-8"))["items"]
            choice_id = next(item["item_id"] for item in items if item["item_type"] == "single_choice")
            preceding_id = next(item["item_id"] for item in items if item["item_id"] != choice_id)
            choice_blocks = [block for block in manifest["blocks"] if block.get("document") == "student" and block.get("source_item_id") == choice_id]
            preceding_blocks = [block for block in manifest["blocks"] if block.get("document") == "student" and block.get("source_item_id") == preceding_id]
            self.assertEqual(len({block["page"] for block in choice_blocks}), 1)
            self.assertGreater(min(block["page"] for block in choice_blocks), max(block["page"] for block in preceding_blocks))

    def test_student_pdf_text_projection_has_no_machine_labels(self):
        with tempfile.TemporaryDirectory(prefix="mse-student-pdf-text-") as td:
            root = Path(td)
            source = create_compact_source(root)
            bundle = root / "bundle"
            rendered = run([
                sys.executable,
                str(SCRIPTS / "render_pdf.py"),
                "--request", str(source / "render-request.json"),
                "--bundle-out", str(bundle),
            ])
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            with fitz.open(bundle / "student.pdf") as document:
                student_text = "\n".join(page.get_text() for page in document)
            self.assertIsNone(re.search(r"\bQuestion\s+\d+\b|Blank\s+b|Response format:|Score:|Answer:|Rationale|canonical|\b[qpb]\d+\b", student_text, re.IGNORECASE))
            self.assertIn("1.", student_text)
            self.assertIn("A.", student_text)
            with fitz.open(bundle / "teacher.pdf") as document:
                teacher_text = "\n".join(page.get_text() for page in document)
            self.assertIn("Answer:", teacher_text)
            self.assertIn("Rationale:", teacher_text)


if __name__ == "__main__":
    unittest.main()
