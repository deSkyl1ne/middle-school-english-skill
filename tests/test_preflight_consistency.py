from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prd_fixture_runtime import create_source, last_json, run

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight_pdf.py"
FIXTURE = ROOT / "tests" / "fixtures" / "print-positive" / "render-request.json"
RENDER = ROOT / "scripts" / "render_pdf.py"


class PreflightConsistencyTest(unittest.TestCase):
    def prepared_bundle(self) -> Path:
        td = tempfile.TemporaryDirectory(prefix="mse-preflight-consistency-")
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        source = create_source("print-task-reading-three-short-answers", root)
        out = root / "bundle"
        result = run([sys.executable, str(RENDER), "--request", str(source / "render-request.json"), "--bundle-out", str(out)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return out

    def test_missing_bound_assessment_is_hard_error(self):
        bundle = self.prepared_bundle()
        (bundle / "assessment.json").unlink()
        result = subprocess.run([sys.executable, str(PREFLIGHT), "--bundle", str(bundle)], capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(error["code"] == "INPUT_MISSING" for error in report["errors"]))

    def test_invalid_response_bbox_fails_geometry_preflight(self):
        bundle = self.prepared_bundle()
        manifest_path = bundle / "render-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        responses = [record for record in manifest["response_areas"] if record.get("document") == "student"]
        self.assertTrue(responses)
        for response in responses:
            response["bbox_pt"] = [0, 0, 0, 0]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        result = subprocess.run([sys.executable, str(PREFLIGHT), "--bundle", str(bundle)], capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        codes = {error["code"] for error in report["errors"]}
        self.assertTrue({"RESPONSE_LAYOUT_MISMATCH", "LAYOUT_EXCESSIVE_WHITESPACE"}.intersection(codes))


if __name__ == "__main__":
    unittest.main()
