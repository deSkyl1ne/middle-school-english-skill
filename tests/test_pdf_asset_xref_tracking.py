from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from test_prd_fixture_runtime import (  # noqa: E402
    SCRIPTS,
    assessment_for_case,
    coverage_targets,
    run,
    update_plan,
    write_json,
)


class PdfAssetXrefTrackingTest(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        previous_item_count = os.environ.get("MSE_PRD_ITEM_COUNT")
        os.environ["MSE_PRD_ITEM_COUNT"] = "55"
        try:
            assessment = assessment_for_case("print-asset-xref-tracking", "asset")
        finally:
            if previous_item_count is None:
                os.environ.pop("MSE_PRD_ITEM_COUNT", None)
            else:
                os.environ["MSE_PRD_ITEM_COUNT"] = previous_item_count
        items = assessment["items"]
        references = {
            "asset-a": [items[0]["item_id"], items[2]["item_id"], items[20]["item_id"]],
            "asset-b": [items[1]["item_id"], items[21]["item_id"]],
        }
        for item in items:
            item["stimulus_assets"] = []
        for asset_id, item_ids in references.items():
            for item_id in item_ids:
                item = next(value for value in items if value["item_id"] == item_id)
                item["stimulus_assets"] = [{
                    "asset_id": asset_id,
                    "semantic_role": "required_context",
                    "placement": "after_stem",
                    "required_for_answer": True,
                    "caption": f"Registered {asset_id} stimulus.",
                }]
        update_plan(assessment, items)
        assessment["blueprint"]["coverage_targets"] = coverage_targets(items)
        write_json(source / "assessment.json", assessment)

        for asset_id, marker in (("asset-a", "a"), ("asset-b", "b")):
            image = Image.new("1", (1600, 1600), 1)
            draw = ImageDraw.Draw(image)
            if marker == "a":
                draw.rectangle((80, 80, 1520, 1520), outline=0, width=48)
                draw.line((120, 120, 1480, 1480), fill=0, width=32)
            else:
                draw.ellipse((120, 120, 1480, 1480), outline=0, width=48)
                draw.line((800, 120, 800, 1480), fill=0, width=32)
            image.save(source / f"{asset_id}.pbm", format="PPM")

        asset_manifest = {
            "schema_version": "1.0.0",
            "assets": [
                {
                    "asset_id": asset_id,
                    "file": f"{asset_id}.pbm",
                    "semantic_role": "required_context",
                    "required_for_answer": True,
                    "rights_status": "cc_public_domain",
                    "linked_item_ids": item_ids,
                    "pixel_width": 1600,
                    "pixel_height": 1600,
                    "color_mode": "1",
                    "measured_dpi": 300,
                    "contrast_ratio": 21.0,
                    "cropped": False,
                }
                for asset_id, item_ids in references.items()
            ],
        }
        write_json(source / "asset-manifest.json", asset_manifest)

        legacy = root / "legacy"
        rendered = run([
            sys.executable,
            str(SCRIPTS / "render_assessment.py"),
            "--input", str(source / "assessment.json"),
            "--out-dir", str(legacy),
        ])
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        validation = run([
            sys.executable,
            str(SCRIPTS / "validate_assessment.py"),
            "--assessment", str(source / "assessment.json"),
            "--student", str(legacy / "student.md"),
            "--teacher", str(legacy / "teacher.md"),
            "--answer-sheet", str(legacy / "answer-sheet.json"),
            "--include-candidates",
        ])
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        (source / "content-validation-report.json").write_text(validation.stdout, encoding="utf-8")

        positive = TEST_ROOT / "fixtures" / "print-positive"
        (source / "generic-cn-junior-english-v1.json").write_bytes((positive / "generic-cn-junior-english-v1.json").read_bytes())
        request = {
            "schema_version": "1.0.0",
            "assessment_path": "assessment.json",
            "validation_report_path": "content-validation-report.json",
            "base_profile_path": "generic-cn-junior-english-v1.json",
            "asset_manifest_path": "asset-manifest.json",
            "outputs": ["student_pdf", "teacher_pdf", "answer_sheet"],
            "page": {"size": "A4", "orientation": "portrait"},
            "locale": "zh-CN",
            "illustration_mode": "original-grayscale",
        }
        write_json(source / "render-request.json", request)
        return source

    @staticmethod
    def _image_infos(page: fitz.Page) -> list[dict[str, object]]:
        return [info for info in page.get_image_info(xrefs=True) if int(info.get("xref", 0)) > 0]

    @staticmethod
    def _same_rect(left: fitz.Rect, right: fitz.Rect, tolerance: float = 1.0) -> bool:
        return all(
            abs(float(getattr(left, side)) - float(getattr(right, side))) <= tolerance
            for side in ("x0", "y0", "x1", "y1")
        )

    def _assert_bindings(self, bundle: Path) -> dict[str, dict[str, int]]:
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        records = {record["asset_id"]: record for record in manifest["assets"]}
        self.assertEqual(set(records), {"asset-a", "asset-b"})
        bindings: dict[str, dict[str, int]] = {}
        for document_name in ("student", "teacher"):
            with fitz.open(bundle / f"{document_name}.pdf") as document:
                infos_by_page = {
                    page_number: self._image_infos(page)
                    for page_number, page in enumerate(document, 1)
                }
                xrefs_by_page = {
                    page_number: {int(image[0]) for image in page.get_images(full=True)}
                    for page_number, page in enumerate(document, 1)
                }
                asset_blocks = [
                    block for block in manifest["blocks"]
                    if block.get("document") == document_name and block.get("role") == "asset"
                ]
                self.assertEqual(len(asset_blocks), 5)
                self.assertEqual(sum(len(infos) for infos in infos_by_page.values()), 5)
                for block in asset_blocks:
                    page = document[block["page"] - 1]
                    expected = fitz.Rect(
                        block["bbox_pt"][0],
                        page.rect.height - block["bbox_pt"][3],
                        block["bbox_pt"][2],
                        page.rect.height - block["bbox_pt"][1],
                    )
                    matches = [
                        info for info in infos_by_page[block["page"]]
                        if self._same_rect(fitz.Rect(*info["bbox"]), expected)
                    ]
                    self.assertEqual(len(matches), 1, (document_name, block, infos_by_page[block["page"]]))
                    asset_id = block["asset_id"]
                    xref = int(matches[0]["xref"])
                    self.assertIn(xref, xrefs_by_page[block["page"]])
                    self.assertEqual(xref, records[asset_id]["pdf_xrefs"][document_name])
                    bindings.setdefault(asset_id, {})[document_name] = xref
                asset_a_pages = {block["page"] for block in asset_blocks if block["asset_id"] == "asset-a"}
                self.assertGreater(len(asset_a_pages), 1)
                self.assertNotEqual(
                    records["asset-a"]["pdf_xrefs"][document_name],
                    records["asset-b"]["pdf_xrefs"][document_name],
                )
        return bindings

    def test_each_asset_placement_uses_its_saved_pdf_image_xref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-asset-xref-") as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            bundles = []
            for name in ("bundle-one", "bundle-two"):
                bundle = root / name
                result = run([
                    sys.executable,
                    str(SCRIPTS / "render_pdf.py"),
                    "--request", str(source / "render-request.json"),
                    "--bundle-out", str(bundle),
                ])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                bundles.append(bundle)

            first = self._assert_bindings(bundles[0])
            second = self._assert_bindings(bundles[1])
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
