from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from test_print_support import PRINT_POSITIVE, ROOT


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _render_manifest(mutator=None) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="mse-response-contract-")
    root = Path(temp.name)
    source = root / "source"
    shutil.copytree(PRINT_POSITIVE, source)
    if mutator is not None:
        assessment = json.loads((source / "assessment.json").read_text(encoding="utf-8"))
        mutator(assessment)
        _write_json(source / "assessment.json", assessment)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render_pdf.py"),
            "--request",
            str(source / "render-request.json"),
            "--bundle-out",
            str(root / "bundle"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return temp, root / "bundle"


def _matching_assessment(assessment: dict) -> None:
    item = copy.deepcopy(assessment["items"][0])
    item.update(
        {
            "item_type": "reading_matching",
            "passage": "Read the descriptions and match each prompt with the best option.",
            "prompts": [
                {"prompt_id": f"p{index}", "text": f"Prompt {index} describes a complete school activity."}
                for index in range(1, 6)
            ],
            "options": [
                {"option_id": chr(65 + index), "text": f"Option {chr(65 + index)} describes a complete school activity."}
                for index in range(7)
            ],
            "answer": {
                "matches": [
                    {"prompt_id": f"p{index}", "option_id": chr(65 + index - 1)}
                    for index in range(1, 6)
                ]
            },
            "score": 10,
            "canonical_item_ids": ["g7s2-unit-01-text-type-001"],
        }
    )
    assessment["items"] = [item]
    assessment["request"]["item_type_plan"] = [{"item_type": "reading_matching", "item_count": 1, "score_each": 10}]
    assessment["request"]["total_score"] = 10
    assessment["blueprint"]["request"] = copy.deepcopy(assessment["request"])
    assessment["blueprint"]["sections"] = [{"item_type": "reading_matching", "item_count": 1, "score_each": 10, "score_total": 10}]
    assessment["blueprint"]["score_check"] = {"expected_total": 10, "computed_total": 10}
    assessment["blueprint"]["coverage_targets"] = [
        {"canonical_item_id": "g7s2-unit-01-text-type-001", "target_role": "primary", "planned_item_count": 1}
    ]


class ResponseAreaSemanticsTest(unittest.TestCase):
    def test_answer_sheet_requires_typed_rows(self):
        schema = json.loads((ROOT / "schema" / "answer-sheet.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        invalid = {
            "assessment_id": "assessment-01",
            "blueprint_id": "blueprint-01",
            "items": [{"item_number": 1, "item_id": "item-01", "score": 2}],
        }
        errors = list(validator.iter_errors(invalid))
        self.assertTrue(any(error.validator == "required" for error in errors))
        valid = {
            "schema_version": "1.0.0",
            "assessment_id": "assessment-01",
            "blueprint_id": "blueprint-01",
            "items": [
                {
                    "item_number": 1,
                    "item_id": "item-01",
                    "score": 2,
                    "response_type": "one_option",
                    "answer": {"option_ids": ["A"]},
                }
            ],
        }
        self.assertEqual(list(validator.iter_errors(valid)), [])
        empty_answer = copy.deepcopy(valid)
        empty_answer["items"][0]["answer"] = {}
        self.assertTrue(list(validator.iter_errors(empty_answer)))
        listening = copy.deepcopy(valid)
        listening["items"][0]["response_type"] = "listening_blueprint"
        listening["items"][0].pop("answer")
        self.assertEqual(list(validator.iter_errors(listening)), [])

    def test_choice_fixture_has_no_extra_full_response_line(self):
        temp, bundle = _render_manifest()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["response_areas"], [])

    def test_matching_response_contract_is_inline_and_prompt_bound(self):
        temp, bundle = _render_manifest(_matching_assessment)
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["response_areas"]), 10)
        expected_prompt_ids = {f"p{index}" for index in range(1, 6)}
        for document in ("student", "teacher"):
            records = [row for row in manifest["response_areas"] if row["document"] == document]
            self.assertEqual({row["source_prompt_id"] for row in records}, expected_prompt_ids)
            for row in records:
                contract = row["response_contract"]
                self.assertEqual(contract["response_kind"], "letter")
                self.assertEqual(contract["line_policy"], "inline")
                self.assertEqual(row["actual_line_count"], 0)
                prompt = next(
                    block
                    for block in manifest["blocks"]
                    if block["document"] == document
                    and block["role"] == "prompt"
                    and block["source_prompt_id"] == row["source_prompt_id"]
                )
                self.assertEqual(row["page"], prompt["page"])
                self.assertEqual(row["bbox_pt"], prompt["bbox_pt"])


if __name__ == "__main__":
    unittest.main()
