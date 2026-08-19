from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prd_fixture_runtime import create_source, run

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "scripts" / "render_pdf.py"
PREFLIGHT = ROOT / "scripts" / "preflight_pdf.py"


class Fr11GeometryAdversarialTest(unittest.TestCase):
    def prepared_bundle(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        td = tempfile.TemporaryDirectory(prefix="mse-fr11-")
        root = Path(td.name)
        source = create_source("print-font-fallback-valid", root)
        bundle = root / "bundle"
        result = run([sys.executable, str(RENDER), "--request", str(source / "render-request.json"), "--bundle-out", str(bundle)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return td, bundle

    def preflight(self, bundle: Path) -> dict:
        result = subprocess.run([sys.executable, str(PREFLIGHT), "--bundle", str(bundle)], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stdout)

    def test_giant_ordinary_block_bbox_fails_unused_fit_with_context(self) -> None:
        td, bundle = self.prepared_bundle()
        self.addCleanup(td.cleanup)
        path = bundle / "render-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        block = next(value for value in manifest["blocks"] if value.get("document") == "student" and value.get("role") == "heading")
        block["bbox_pt"] = [0, 0, 595.276, 841.890]
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        report = self.preflight(bundle)
        findings = [error for error in report["errors"] if error["code"] == "LAYOUT_UNUSED_FIT" and error.get("block_id") == block["block_id"]]
        self.assertTrue(findings, report["errors"])
        self.assertEqual({"page", "ratio", "block_id"}.issubset(findings[0]), True)

    def test_giant_declared_reserve_fails_without_hiding_real_geometry(self) -> None:
        td, bundle = self.prepared_bundle()
        self.addCleanup(td.cleanup)
        path = bundle / "render-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        source_block = next(value for value in manifest["blocks"] if value.get("document") == "student" and value.get("role") == "heading")
        reserve = dict(source_block)
        reserve["block_id"] = "fr11-forged-reserve"
        reserve["role"] = "declared_page_reserve"
        reserve["bbox_pt"] = [0, 0, 595.276, 841.890]
        manifest["blocks"].append(reserve)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        report = self.preflight(bundle)
        findings = [error for error in report["errors"] if error["code"] == "LAYOUT_UNUSED_FIT" and error.get("block_id") == reserve["block_id"]]
        self.assertTrue(findings, report["errors"])
        self.assertEqual(findings[0].get("page"), source_block["page"])


if __name__ == "__main__":
    unittest.main()
