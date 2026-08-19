"""End-to-end contract coverage for every registered assessment item type."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PACKAGE_ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDERER = load_module("assessment_contract_renderer", "render_assessment.py")
VALIDATOR = load_module("assessment_contract_validator", "validate_assessment.py")


def option_pair() -> list[dict[str, str]]:
    return [{"option_id": "A", "text": "Correct"}, {"option_id": "B", "text": "Other"}]


def item(item_type: str, canonical_id: str, number: int) -> dict:
    value = {
        "item_id": f"contract-item-{number:02d}",
        "item_type": item_type,
        "score": 2,
        "canonical_item_ids": [canonical_id],
    }
    if item_type == "listening_blueprint":
        value.update({
            "script_outline": "A host and a guest discuss a school club.",
            "speaker_roles": [{"role": "Host", "purpose": "asks a question"}],
            "task_sequence": [{"step": 1, "task_kind": "choose", "item_count": 1, "score": 2}],
            "target_skills": ["main idea"],
        })
    elif item_type == "cloze":
        value.update({
            "passage": "A [b1] passage.",
            "blanks": [{"blank_id": "b1", "position": 1}],
            "options": [{"blank_id": "b1", "options": option_pair()}],
            "answer": {"blank_answers": [{"blank_id": "b1", "value": "Correct", "option_id": "A"}]},
            "rationale": "The selected option completes the blank.",
        })
    elif item_type == "reading_matching":
        value.update({
            "passage": "A matching passage.",
            "prompts": [{"prompt_id": "p1", "text": "Match this prompt."}],
            "options": [{"option_id": "A", "text": "Matching answer"}],
            "answer": {"matches": [{"prompt_id": "p1", "option_id": "A"}]},
            "rationale": "The match is unique.",
        })
    elif item_type == "task_based_reading":
        value.update({
            "passage": "A task-based reading passage.",
            "tasks": [{"task_id": "t1", "prompt": "State the main idea.", "response_format": "short answer", "score": 2}],
            "answer": {"responses": [{"task_id": "t1", "response": "A school club"}]},
            "rationale": "The response addresses the stated task.",
        })
    elif item_type == "vocabulary_in_context":
        value.update({
            "context": "The word is used in context.",
            "stem": "What does the word mean?",
            "options": option_pair(),
            "answer": {"value": "Correct"},
            "rationale": "The context supports the word answer.",
        })
    elif item_type == "word_bank_fill":
        value.update({
            "stem": "Use [b1] in the sentence.",
            "blanks": [{"blank_id": "b1", "position": 1}],
            "word_bank": ["Correct", "Other"],
            "answer": {"blank_answers": [{"blank_id": "b1", "value": "Correct"}]},
            "rationale": "The word-bank answer is unique.",
        })
    elif item_type == "practical_writing":
        value.update({
            "prompt": "Write a short school notice.",
            "rubric": [{"criterion": "purpose", "points": 2, "descriptor": "The purpose is clear."}],
            "answer": {"response": "A clear school notice."},
            "rationale": "The response satisfies the stated criterion.",
        })
    elif item_type in {"single_choice", "reading_multiple_choice"}:
        value.update({
            "stem": "Choose the correct answer.",
            "options": option_pair(),
            "answer": {"option_ids": ["A"]},
            "rationale": "Option A is the only correct answer.",
        })
        if item_type == "reading_multiple_choice":
            value["passage"] = "A reading passage."
    elif item_type in {"grammar_fill", "sentence_completion"}:
        value.update({
            "stem": "Complete the sentence correctly.",
            "answer": {"primary": "Correct", "accepted": ["Correct"]},
            "rationale": "The accepted form is grammatically correct.",
        })
    else:
        raise AssertionError(f"missing fixture for {item_type}")
    return value


class AssessmentContractTest(unittest.TestCase):
    def assessment(self) -> dict:
        item_types = [
            "listening_blueprint", "single_choice", "cloze", "reading_multiple_choice",
            "reading_matching", "task_based_reading", "vocabulary_in_context", "grammar_fill",
            "sentence_completion", "word_bank_fill", "practical_writing",
        ]
        canonical_ids = [f"g7s2-unit-01-vocabulary-{index:03d}" for index in range(1, len(item_types) + 1)]
        plan = [{"item_type": item_type, "item_count": 1, "score_each": 2} for item_type in item_types]
        request = {
            "book_id": "grade-07-semester-2",
            "unit_ids": ["unit-01"],
            "purpose": "unit_practice",
            "total_score": 22,
            "item_type_plan": plan,
            "outputs": ["student", "teacher", "answer_sheet"],
        }
        return {
            "schema_version": "1.0.0",
            "assessment_id": "contract-all-types",
            "request": request,
            "blueprint": {
                "blueprint_id": "contract-blueprint",
                "request": request,
                "resolved_unit_ids": ["unit-01"],
                "sections": [{**line, "score_total": 2} for line in plan],
                "coverage_targets": [
                    {"canonical_item_id": canonical_id, "target_role": "primary", "planned_item_count": 1}
                    for canonical_id in canonical_ids
                ],
                "score_check": {"expected_total": 22, "computed_total": 22},
                "boundary_check": {"allowed_primary_levels": ["A", "B"], "reinforcement": False, "context_only_level": "D"},
            },
            "items": [item(item_type, canonical_id, number) for number, (item_type, canonical_id) in enumerate(zip(item_types, canonical_ids), 1)],
        }

    def test_all_registered_types_validate_and_render(self) -> None:
        assessment = self.assessment()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "assessment.json"
            source.write_text(json.dumps(assessment), encoding="utf-8")
            rendered = root / "rendered"
            self.assertEqual(0, RENDERER.main(["--input", str(source), "--out-dir", str(rendered)]))
            report = VALIDATOR.validate_assessment(
                assessment,
                canonical_root=PACKAGE_ROOT / "references",
                output_paths={
                    "student": rendered / "student.md",
                    "teacher": rendered / "teacher.md",
                    "answer_sheet": rendered / "answer-sheet.json",
                },
                allow_candidate=True,
            )
            self.assertEqual("ASSESSMENT_VALIDATOR_PASS", report["status"], report["errors"])
            self.assertNotIn("**Answer:**", (rendered / "student.md").read_text(encoding="utf-8"))

    def test_student_only_all_types_neither_leaks_nor_writes_teacher_files(self) -> None:
        assessment = self.assessment()
        assessment["request"]["outputs"] = ["student"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "assessment.json"
            source.write_text(json.dumps(assessment), encoding="utf-8")
            rendered = root / "rendered"
            self.assertEqual(0, RENDERER.main(["--input", str(source), "--out-dir", str(rendered)]))
            self.assertEqual(["student.md"], [path.name for path in rendered.iterdir()])
            report = VALIDATOR.validate_assessment(
                assessment,
                canonical_root=PACKAGE_ROOT / "references",
                output_paths={"student": rendered / "student.md"},
                allow_candidate=True,
            )
            self.assertEqual("ASSESSMENT_VALIDATOR_PASS", report["status"], report["errors"])

    def test_nested_schema_extra_field_is_rejected(self) -> None:
        assessment = self.assessment()
        assessment["blueprint"]["sections"][0]["unexpected"] = True
        report = VALIDATOR.validate_assessment(
            assessment,
            canonical_root=PACKAGE_ROOT / "references",
            allow_candidate=True,
        )
        self.assertEqual("ASSESSMENT_VALIDATOR_FAIL", report["status"])
        self.assertTrue(any(error["path"] == "blueprint.sections[0]" for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
