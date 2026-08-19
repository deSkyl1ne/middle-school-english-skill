from __future__ import annotations

import json
import fitz
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_print_support import PRINT_POSITIVE, ROOT, prepare_positive
from test_prd_fixture_runtime import create_source, last_json, run


class MatchingLayoutsTest(unittest.TestCase):
    def test_matching_diagnostics_have_three_distinct_measured_layouts(self):
        with tempfile.TemporaryDirectory(prefix="mse-matching-layout-") as td:
            root = Path(td)
            source = root / "source"
            shutil.copytree(PRINT_POSITIVE, source)
            assessment = json.loads((source / "assessment.json").read_text(encoding="utf-8"))
            assessment["items"] = [assessment["items"][0]]
            item = assessment["items"][0]
            item.update({"item_type": "reading_matching", "passage": "Read the short descriptions and match each prompt with the best option.", "prompts": [{"prompt_id": f"p{i}", "text": f"Prompt {i} describes a full school activity sentence."} for i in range(1, 6)], "options": [{"option_id": chr(65 + i), "text": f"Option {chr(65 + i)} describes a complete school activity."} for i in range(7)], "answer": {"matches": [{"prompt_id": f"p{i}", "option_id": chr(65 + i - 1)} for i in range(1, 6)]}, "score": 10, "canonical_item_ids": ["g7s2-unit-01-text-type-001"]})
            assessment["request"]["item_type_plan"] = [{"item_type": "reading_matching", "item_count": 1, "score_each": 10}]
            assessment["request"]["total_score"] = 10
            assessment["blueprint"]["request"] = assessment["request"]
            assessment["blueprint"]["sections"] = [{"item_type": "reading_matching", "item_count": 1, "score_each": 10, "score_total": 10}]
            assessment["blueprint"]["score_check"] = {"expected_total": 10, "computed_total": 10}
            assessment["blueprint"]["coverage_targets"] = [{"canonical_item_id": "g7s2-unit-01-text-type-001", "target_role": "primary", "planned_item_count": 1}]
            assessment_path = source / "assessment.json"
            assessment_path.write_text(json.dumps(assessment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_assessment.py"), "--assessment", str(assessment_path), "--include-candidates"], capture_output=True, text=True, check=False)
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
            (source / "content-validation-report.json").write_text(report.stdout, encoding="utf-8")
            bundle = root / "bundle"
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "render_pdf.py"), "--request", str(source / "render-request.json"), "--bundle-out", str(bundle)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest.get("matching", [])), 2)
            self.assertEqual(len(manifest.get("response_areas", [])), 10)
            for diagnostic in manifest.get("matching", []):
                layouts = [candidate.get("layout") for candidate in diagnostic.get("candidates", [])]
                self.assertEqual(set(layouts), {"card-grid", "stacked", "dual-independent-flow"})
                self.assertGreaterEqual(len({(candidate.get("page_count"), candidate.get("break_count"), candidate.get("max_non_response_empty_ratio")) for candidate in diagnostic["candidates"]}), 2)

    def test_matching_renderer_is_not_a_markdown_or_placeholder_path(self):
        temp, bundle = prepare_positive()
        self.addCleanup(temp.cleanup)
        text = "\n".join((bundle / name).read_bytes().decode("latin-1", errors="ignore") for name in ("student.pdf", "teacher.pdf"))
        self.assertNotIn("AssetBlock", text)
        self.assertNotIn("![](", text)

    def test_student_matching_text_uses_formal_numbers_without_machine_title_or_ids(self):
        with tempfile.TemporaryDirectory(prefix="mse-matching-visible-") as td:
            root = Path(td)
            source = create_source("print-matching-5x7-standard", root)
            assessment_path = source / "assessment.json"
            assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
            matching_item = next(item for item in assessment["items"] if item.get("item_type") == "reading_matching")
            matching_item["prompts"] = [
                {"prompt_id": f"p{index}", "text": f"Question {40 + index}: Prompt {index} keeps its formal number."}
                for index in range(1, 6)
            ]
            assessment_path.write_text(json.dumps(assessment, ensure_ascii=False), encoding="utf-8")
            bundle = root / "bundle"
            rendered = run([
                sys.executable,
                str(ROOT / "scripts" / "render_pdf.py"),
                "--request", str(source / "render-request.json"),
                "--bundle-out", str(bundle),
            ])
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            self.assertEqual(last_json(rendered).get("status"), "RENDERED")
            with fitz.open(bundle / "student.pdf") as document:
                text = "\n".join(page.get_text() for page in document)
            self.assertNotIn("Matching", text)
            self.assertIsNone(re.search(r"\b[qpb]\d+\b", text, re.IGNORECASE))
            self.assertIn("41. Prompt 1", text)
            self.assertIn("45. Prompt 5", text)
            self.assertIn("A.", text)
            with fitz.open(bundle / "teacher.pdf") as document:
                teacher_text = "\n".join(page.get_text() for page in document)
            self.assertIn("Matching", teacher_text)
            for prompt_id in ("p1", "p5"):
                self.assertIn(f"{prompt_id}.", teacher_text)
            self.assertIn("Answer:", teacher_text)
            self.assertIn("Rationale:", teacher_text)
            preflight = run([
                sys.executable,
                str(ROOT / "scripts" / "preflight_pdf.py"),
                "--bundle", str(bundle),
            ])
            report = last_json(preflight)
            self.assertEqual(preflight.returncode, 0, report)
            self.assertEqual(report.get("status"), "PRINT_PREFLIGHT_PASS", report)
            self.assertFalse(any(error.get("code") == "STUDENT_CONTENT_MISMATCH" for error in report.get("errors", [])), report)
            self.assertEqual([error for error in report.get("errors", []) if error.get("code") == "LAYOUT_EXCESSIVE_WHITESPACE"], [], report)


if __name__ == "__main__":
    unittest.main()
