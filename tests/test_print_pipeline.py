from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def no_fixture_tail_padding():
    names = {
        "MSE_PRD_ITEM_COUNT": "17",
        "MSE_ALL_TYPES_OPENING_REPEATS": "0",
        "MSE_PRD_CLOSING_REPEATS": "0",
        "MSE_ALL_TYPES_CLOSING_REPEATS": "0",
    }
    old = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    try:
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RealPrintPipelineTest(unittest.TestCase):
    def test_student_single_choice_groups_are_page_atomic_without_tail_layout_errors(self):
        from test_prd_fixture_runtime import create_source, last_json, run

        for assessment_name in ("print-font-fallback-valid", "print-basic-all-types"):
            with self.subTest(assessment_name=assessment_name), tempfile.TemporaryDirectory(prefix="mse-choice-page-atomic-") as td:
                root = Path(td)
                source = create_source(assessment_name, root)
                bundle = root / "bundle"
                rendered = run([
                    sys.executable,
                    str(ROOT / "scripts" / "render_pdf.py"),
                    "--request", str(source / "render-request.json"),
                    "--bundle-out", str(bundle),
                ])
                self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
                manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
                assessment = json.loads((source / "assessment.json").read_text(encoding="utf-8"))
                choice_ids = {item["item_id"] for item in assessment["items"] if item.get("item_type") == "single_choice"}
                for item_id in choice_ids:
                    pages = {
                        block["page"]
                        for block in manifest["blocks"]
                        if block.get("document") == "student" and block.get("source_item_id") == item_id
                    }
                    self.assertEqual(len(pages), 1, item_id)
                preflight = run([
                    sys.executable,
                    str(ROOT / "scripts" / "preflight_pdf.py"),
                    "--bundle", str(bundle),
                ])
                report = last_json(preflight)
                self.assertEqual(preflight.returncode, 0, report)
                self.assertEqual(report.get("status"), "PRINT_PREFLIGHT_PASS", report)
                self.assertFalse(any(error.get("code") == "STUDENT_CONTENT_MISMATCH" for error in report.get("errors", [])), report)
                layout_errors = [error for error in report.get("errors", []) if error.get("code") == "LAYOUT_EXCESSIVE_WHITESPACE"]
                self.assertEqual(layout_errors, [], report)

    def test_direct_render_parses_real_student_and_teacher_pdfs(self):
        from test_print_support import prepare_positive

        with no_fixture_tail_padding():
            temp, out = prepare_positive()
        self.addCleanup(temp.cleanup)
        for name in ("student.pdf", "teacher.pdf"):
            with fitz.open(out / name) as document:
                self.assertGreaterEqual(document.page_count, 1)
                self.assertTrue(all(page.get_text().strip() for page in document))
        self.assertTrue((out / "print-validation-report.json").exists())

    def test_image_occupancy_is_backed_by_real_pdf_image_geometry(self):
        from test_prd_fixture_runtime import create_source, run

        with tempfile.TemporaryDirectory(prefix="mse-print-image-") as td:
            root = Path(td)
            source = create_source("print-asset-valid-grayscale", root)
            bundle = root / "bundle"
            result = run([
                sys.executable,
                str(ROOT / "scripts" / "render_pdf.py"),
                "--request", str(source / "render-request.json"),
                "--bundle-out", str(bundle),
            ])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
            asset_blocks = [block for block in manifest["blocks"] if block.get("role") == "asset"]
            self.assertEqual({block["document"] for block in asset_blocks}, {"student", "teacher"})
            for block in asset_blocks:
                with fitz.open(bundle / f"{block['document']}.pdf") as document:
                    page = document[block["page"] - 1]
                    expected = fitz.Rect(block["bbox_pt"][0], page.rect.height - block["bbox_pt"][3], block["bbox_pt"][2], page.rect.height - block["bbox_pt"][1])
                    image_rects = [rect for image in page.get_images(full=True) for rect in page.get_image_rects(image[0])]
                    self.assertTrue(any(expected.intersects(rect) for rect in image_rects), (block, image_rects))


if __name__ == "__main__":
    unittest.main()
