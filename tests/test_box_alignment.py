from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

from test_print_support import PRINT_POSITIVE, ROOT


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _word_bank_assessment(assessment: dict) -> None:
    item = assessment["items"][0]
    item.pop("options", None)
    item.update(
        {
            "item_type": "word_bank_fill",
            "stem": "Complete the sentence: The school club meets at [b1].",
            "blanks": [{"blank_id": "b1", "position": 1, "target": "time"}],
            "word_bank": ["school", "home", "today"],
            "answer": {"blank_answers": [{"blank_id": "b1", "value": "school"}]},
            "rationale": "The first word completes the sentence.",
        }
    )
    assessment["request"]["item_type_plan"] = [
        {"item_type": "word_bank_fill", "item_count": 1, "score_each": 2},
        {"item_type": "single_choice", "item_count": len(assessment["items"]) - 1, "score_each": 2},
    ]
    assessment["request"]["total_score"] = len(assessment["items"]) * 2
    assessment["blueprint"]["request"] = json.loads(json.dumps(assessment["request"]))
    assessment["blueprint"]["sections"] = [
        {**line, "score_total": line["item_count"] * line["score_each"]}
        for line in assessment["request"]["item_type_plan"]
    ]
    assessment["blueprint"]["score_check"] = {
        "expected_total": assessment["request"]["total_score"],
        "computed_total": assessment["request"]["total_score"],
    }


def _render_word_bank() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="mse-box-geometry-")
    root = Path(temp.name)
    source = root / "source"
    shutil.copytree(PRINT_POSITIVE, source)
    assessment = json.loads((source / "assessment.json").read_text(encoding="utf-8"))
    _word_bank_assessment(assessment)
    _write_json(source / "assessment.json", assessment)
    request_path = source / "render-request.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render_pdf.py"),
            "--request",
            str(request_path),
            "--bundle-out",
            str(root / "bundle"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return temp, root / "bundle"


class BoxAlignmentTest(unittest.TestCase):
    def test_word_bank_manifest_contains_measured_two_axis_geometry(self):
        temp, bundle = _render_word_bank()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        boxed = [block for block in manifest["blocks"] if block.get("box")]
        self.assertTrue(boxed, "positive box test must exercise a real WordBankBox")
        for block in boxed:
            x0, y0, x1, y1 = block["bbox_pt"]
            self.assertLess(x0, x1)
            self.assertLess(y0, y1)
            self.assertGreaterEqual(x0, 0)
            self.assertGreaterEqual(y0, 0)
            self.assertLessEqual(x1, 595.276 + 1)
            self.assertLessEqual(y1, 841.890 + 1)
            box = block["box"]
            bx0, by0, bx1, by1 = box["box_bbox_pt"]
            cx0, cy0, cx1, cy1 = box["content_bbox_pt"]
            self.assertLess(bx0, bx1)
            self.assertLess(by0, by1)
            self.assertGreaterEqual(bx0, 0)
            self.assertGreaterEqual(by0, 0)
            self.assertLessEqual(bx1, 595.276 + 1)
            self.assertLessEqual(by1, 841.890 + 1)
            self.assertTrue(bx0 <= cx0 <= cx1 <= bx1)
            self.assertTrue(by0 <= cy0 <= cy1 <= by1)
            self.assertLessEqual(box["horizontal_center_delta_pt"], 2)
            self.assertLessEqual(box["vertical_center_delta_pt"], 2)
            self.assertGreaterEqual(box["font_size_pt"], 10.5)
            self.assertGreaterEqual(min(box["padding_pt"].values()), 8)

    def test_word_bank_border_does_not_cross_text(self):
        temp, bundle = _render_word_bank()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        boxed = [block for block in manifest["blocks"] if block.get("box")]
        for block in boxed:
            document_name = str(block["document"])
            page_number = int(block["page"])
            box = block["box"]["box_bbox_pt"]
            with fitz.open(bundle / f"{document_name}.pdf") as document:
                page = document[page_number - 1]
                box_rect = fitz.Rect(box[0], page.rect.height - box[3], box[2], page.rect.height - box[1])
                spans = [
                    fitz.Rect(span["bbox"])
                    for text_block in page.get_text("dict").get("blocks", [])
                    if text_block.get("type") == 0
                    for line in text_block.get("lines", [])
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                ]
                for border_y in (box_rect.y0, box_rect.y1):
                    self.assertFalse(
                        any(span.x0 < box_rect.x1 and span.x1 > box_rect.x0 and span.y0 <= border_y <= span.y1 for span in spans),
                        (document_name, page_number, border_y, box_rect),
                    )


if __name__ == "__main__":
    unittest.main()
