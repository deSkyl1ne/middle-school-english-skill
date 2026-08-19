from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency gate
    Draft202012Validator = None


ROOT = Path(__file__).resolve().parents[1]
COMPILER_PATH = ROOT / "scripts" / "compile_render_ir.py"
SCHEMA_PATH = ROOT / "schema" / "render-ir.schema.json"


def load_compiler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compile_render_ir_asset_test", COMPILER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load render IR compiler")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPILER = load_compiler()


def assessment_with_all_placements() -> dict:
    item_id = "asset-placement-item"
    return {
        "schema_version": "1.0.0",
        "assessment_id": "asset-placement-assessment",
        "blueprint": {"blueprint_id": "asset-placement-blueprint"},
        "items": [
            {
                "item_id": item_id,
                "item_type": "single_choice",
                "passage": "A short passage.",
                "stem": "Choose the correct answer.",
                "options": [{"option_id": "A", "text": "The first option."}],
                "answer": {"option_ids": ["A"]},
                "rationale": "The first option is correct.",
                "score": 1,
                "canonical_item_ids": ["g7s2-unit-01-topic-001"],
                "stimulus_assets": [
                    {
                        "asset_id": "asset-before-passage",
                        "semantic_role": "stimulus",
                        "placement": "before_passage",
                        "required_for_answer": True,
                    },
                    {
                        "asset_id": "asset-after-passage",
                        "semantic_role": "stimulus",
                        "placement": "after_passage",
                        "required_for_answer": True,
                    },
                    {
                        "asset_id": "asset-before-stem",
                        "semantic_role": "required_context",
                        "placement": "before_stem",
                        "required_for_answer": True,
                    },
                    {
                        "asset_id": "asset-after-stem",
                        "semantic_role": "required_context",
                        "placement": "after_stem",
                        "required_for_answer": True,
                    },
                    {
                        "asset_id": "asset-inline",
                        "semantic_role": "required_context",
                        "placement": "inline_block",
                        "after_block_id": f"{item_id}-block-003",
                        "required_for_answer": True,
                    },
                ],
            }
        ],
    }


class AssetPlacementCompileTest(unittest.TestCase):
    @unittest.skipIf(Draft202012Validator is None, "jsonschema not installed")
    def test_all_asset_placements_are_ordered_and_schema_valid(self) -> None:
        assessment = assessment_with_all_placements()
        student, teacher = COMPILER.compile_views(assessment)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

        for view in (student, teacher):
            errors = list(Draft202012Validator(schema).iter_errors(view))
            self.assertEqual([], errors, view)

        expected_asset_ids = [
            "asset-before-passage",
            "asset-after-passage",
            "asset-before-stem",
            "asset-after-stem",
            "asset-inline",
        ]
        for ir in (student, teacher):
            blocks = ir["items"][0]["blocks"]
            self.assertEqual(
                expected_asset_ids,
                [block["asset"]["asset_id"] for block in blocks if block["role"] == "asset"],
            )
            self.assertEqual(
                [
                    "asset-placement-item-block-001",
                    "asset-placement-item-asset-001",
                    "asset-placement-item-block-002",
                    "asset-placement-item-asset-002",
                    "asset-placement-item-asset-003",
                    "asset-placement-item-block-003",
                    "asset-placement-item-asset-004",
                    "asset-placement-item-asset-005",
                    "asset-placement-item-block-004",
                ],
                [block["block_id"] for block in blocks],
            )
            inline = next(block for block in blocks if block.get("asset", {}).get("asset_id") == "asset-inline")
            self.assertEqual("asset-placement-item-block-003", inline["asset"]["after_block_id"])

        self.assertEqual(
            [block["block_id"] for block in student["items"][0]["blocks"]],
            [block["block_id"] for block in teacher["items"][0]["blocks"]],
        )


if __name__ == "__main__":
    unittest.main()
