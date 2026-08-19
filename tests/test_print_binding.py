"""Assessment schema and content contract tests.

These cover only the contracts implemented in Phase 0/1 (PRD FR-1/FR-2 and the
``stimulus_assets`` placement contract).  They deliberately avoid the full print
fixtures that later phases add.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = PACKAGE_ROOT / "schema"
FIXTURES = PACKAGE_ROOT / "tests" / "fixtures"

SPEC = importlib.util.spec_from_file_location("validate_assessment", PACKAGE_ROOT / "scripts" / "validate_assessment.py")
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

try:
    import jsonschema  # noqa: F401
    from jsonschema import Draft202012Validator  # noqa: F401

    JSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    JSCHEMA_AVAILABLE = False


class Draft2020SchemaGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assessment = json.loads((FIXTURES / "assessment-positive.json").read_text(encoding="utf-8"))

    def test_invalid_stimulus_asset_placement_is_schema_fail(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        assessment = json.loads(json.dumps(self.assessment))
        assessment["items"][0]["stimulus_assets"] = [
            {
                "asset_id": "img-weather-map-01",
                "semantic_role": "stimulus",
                "placement": "inline_block",
                "required_for_answer": True,
            }
        ]
        report = VALIDATOR.validate_assessment(
            assessment,
            canonical_root=PACKAGE_ROOT / "references",
            allow_candidate=True,
        )
        self.assertEqual("ASSESSMENT_VALIDATOR_FAIL", report["status"])
        self.assertTrue(
            any(error["code"] == "SCHEMA_INVALID" for error in report["errors"]),
            report["errors"],
        )

    def test_valid_stimulus_asset_contract_passes(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        assessment = json.loads(json.dumps(self.assessment))
        assessment["items"][0]["stimulus_assets"] = [
            {
                "asset_id": "img-weather-map-01",
                "semantic_role": "stimulus",
                "placement": "after_passage",
                "required_for_answer": True,
                "caption": "Weather map",
            }
        ]
        report = VALIDATOR.validate_assessment(
            assessment,
            canonical_root=PACKAGE_ROOT / "references",
            allow_candidate=True,
        )
        self.assertEqual("ASSESSMENT_VALIDATOR_PASS", report["status"], report["errors"])

    def test_draft2020_schema_gate_rejects_unknown_item_field(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        assessment = json.loads(json.dumps(self.assessment))
        assessment["items"][0]["unexpected_metadata"] = True
        report = VALIDATOR.validate_assessment(
            assessment,
            canonical_root=PACKAGE_ROOT / "references",
            allow_candidate=True,
        )
        self.assertEqual("ASSESSMENT_VALIDATOR_FAIL", report["status"])
        self.assertTrue(any(error["code"] == "SCHEMA_INVALID" for error in report["errors"]), report["errors"])

    def test_schema_invalid_input_fails_from_cli(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        with tempfile.TemporaryDirectory(prefix="print-binding-schema-") as temp_dir:
            assessment = json.loads(json.dumps(self.assessment))
            assessment["items"][0]["stimulus_assets"] = [
                {"asset_id": "img-weather-map-01", "semantic_role": "stimulus", "placement": "inline_block", "required_for_answer": True}
            ]
            path = Path(temp_dir) / "bad.json"
            path.write_text(json.dumps(assessment, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_ROOT / "scripts" / "validate_assessment.py"),
                    "--assessment",
                    str(path),
                    "--include-candidates",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            report = json.loads(completed.stdout)
            self.assertEqual("ASSESSMENT_VALIDATOR_FAIL", report["status"])
            self.assertTrue(any(error["code"] == "SCHEMA_INVALID" for error in report["errors"]))


class NegativeFixtureRegressionTest(unittest.TestCase):
    def test_double_answer_fixture_still_reports_answer_not_unique(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_ROOT / "scripts" / "validate_assessment.py"),
                "--assessment",
                str(FIXTURES / "assessment-double-answer.json"),
                "--include-candidates",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        report = json.loads(completed.stdout)
        self.assertEqual("ASSESSMENT_VALIDATOR_FAIL", report["status"])
        self.assertTrue(any(error["code"] == "ANSWER_NOT_UNIQUE" for error in report["errors"]), report["errors"])


if __name__ == "__main__":
    unittest.main()
