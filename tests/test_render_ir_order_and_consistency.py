from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / "scripts" / "compile_render_ir.py"


class RenderIROrderConsistencyTest(unittest.TestCase):
    def test_student_teacher_share_item_order_and_preserve_content_projection(self):
        source = json.loads((ROOT / "tests/fixtures/print-positive/assessment.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="mse-ir-order-") as td:
            assessment = Path(td) / "assessment.json"
            assessment.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            outputs = {}
            for view in ("student", "teacher"):
                output = Path(td) / f"{view}-ir.json"
                result = subprocess.run([sys.executable, str(COMPILER), "--assessment", str(assessment), "--output", str(output), "--view", view], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outputs[view] = json.loads(output.read_text(encoding="utf-8"))
            expected = [item["item_id"] for item in source["items"]]
            self.assertEqual([item["item_id"] for item in outputs["student"]["items"]], expected)
            self.assertEqual([item["item_id"] for item in outputs["teacher"]["items"]], expected)
            self.assertNotIn("answer", outputs["student"]["items"][0])
            self.assertIn("answer", outputs["teacher"]["items"][0])

    def test_registered_content_fields_are_emitted_to_ir(self):
        item = {
            "schema_version": "1.0.0", "assessment_id": "ir-fields", "request": {"book_id": "grade-07-semester-2", "unit_ids": ["unit-01"], "purpose": "test", "item_type_plan": [{"item_type": "task_based_reading", "item_count": 1, "score_each": 2}], "outputs": ["student", "teacher", "answer_sheet"]},
            "blueprint": {"blueprint_id": "ir-fields-bp", "request": {"book_id": "grade-07-semester-2", "unit_ids": ["unit-01"], "purpose": "test", "item_type_plan": [{"item_type": "task_based_reading", "item_count": 1, "score_each": 2}], "outputs": ["student", "teacher", "answer_sheet"]}, "resolved_unit_ids": ["unit-01"], "sections": [{"item_type": "task_based_reading", "item_count": 1, "score_each": 2, "score_total": 2}], "coverage_targets": [{"canonical_item_id": "g7s2-unit-01-text-type-001", "target_role": "primary", "planned_item_count": 1}], "score_check": {"expected_total": 2, "computed_total": 2}},
            "items": [{"item_id": "ir-task", "item_type": "task_based_reading", "passage": "A passage", "instruction": "Read carefully", "tasks": [{"task_id": "t1", "prompt": "State the idea", "response_format": "short answer", "score": 2}], "answer": {"responses": [{"task_id": "t1", "response": "A"}]}, "rationale": "The response is supported.", "score": 2, "canonical_item_ids": ["g7s2-unit-01-text-type-001"]}],
        }
        with tempfile.TemporaryDirectory(prefix="mse-ir-fields-") as td:
            assessment = Path(td) / "assessment.json"
            assessment.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
            output = Path(td) / "ir.json"
            result = subprocess.run([sys.executable, str(COMPILER), "--assessment", str(assessment), "--output", str(output), "--view", "student"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            ir = json.loads(output.read_text(encoding="utf-8"))
            kinds = {block["kind"] for block in ir["items"][0]["blocks"]}
            self.assertIn("ResponseArea", kinds)
            self.assertTrue(any(block.get("source_task_id") == "t1" for block in ir["items"][0]["blocks"]))

    def test_each_task_is_immediately_followed_by_its_bound_response_area(self):
        source = {
            "schema_version": "1.0.0",
            "assessment_id": "ir-task-pairs",
            "request": {"book_id": "grade-07-semester-2", "unit_ids": ["unit-01"], "purpose": "test", "outputs": ["student", "teacher", "answer_sheet"]},
            "blueprint": {"blueprint_id": "ir-task-pairs-bp"},
            "items": [{
                "item_id": "ir-task-pairs-item",
                "item_type": "task_based_reading",
                "passage": "A passage",
                "tasks": [
                    {"task_id": "t1", "prompt": "State the idea", "response_format": "short answer", "score": 1},
                    {"task_id": "t2", "prompt": "State one detail", "response_format": "short answer", "score": 1},
                ],
                "answer": {"responses": [{"task_id": "t1", "response": "A"}, {"task_id": "t2", "response": "B"}]},
                "rationale": "The responses are supported.",
                "score": 2,
                "canonical_item_ids": ["g7s2-unit-01-text-type-001"],
            }],
        }
        with tempfile.TemporaryDirectory(prefix="mse-ir-task-pairs-") as td:
            assessment = Path(td) / "assessment.json"
            assessment.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            output = Path(td) / "ir.json"
            result = subprocess.run([sys.executable, str(COMPILER), "--assessment", str(assessment), "--output", str(output), "--view", "student"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            blocks = json.loads(output.read_text(encoding="utf-8"))["items"][0]["blocks"]
            for index, block in enumerate(blocks):
                if block.get("role") != "task":
                    continue
                self.assertLess(index + 1, len(blocks))
                response = blocks[index + 1]
                self.assertEqual(response.get("role"), "response_area")
                self.assertEqual(response.get("source_task_id"), block.get("source_task_id"))

    def test_student_text_projection_hides_machine_labels_and_teacher_keeps_binding(self):
        source = {
            "assessment_id": "ir-visible-projection",
            "blueprint": {"blueprint_id": "ir-visible-projection-bp"},
            "items": [
                {
                    "item_id": "q46",
                    "item_type": "single_choice",
                    "stem": "Question 46: Choose the correct sentence.",
                    "options": [{"option_id": letter, "text": f"Choice {letter}."} for letter in "ABCD"],
                    "answer": {"option_ids": ["A"]},
                    "rationale": "Choice A is correct.",
                    "score": 2,
                    "canonical_item_ids": ["canonical-46"],
                },
                {
                    "item_id": "cloze-46",
                    "item_type": "cloze",
                    "passage": "Use [b11] in the sentence.",
                    "blanks": [{"blank_id": "b11", "position": 1}],
                    "options": [{"blank_id": "b11", "options": [{"option_id": "A", "text": "one"}, {"option_id": "B", "text": "two"}]}],
                    "answer": {"blank_answers": [{"blank_id": "b11", "option_id": "A"}]},
                    "rationale": "A completes the blank.",
                    "score": 2,
                    "canonical_item_ids": ["canonical-cloze"],
                },
                {
                    "item_id": "matching-46",
                    "item_type": "reading_matching",
                    "passage": "Match each description.",
                    "prompts": [{"prompt_id": "p46", "text": "Question 48: The first description."}],
                    "options": [{"option_id": "A", "text": "The first option."}],
                    "answer": {"matches": [{"prompt_id": "p46", "option_id": "A"}]},
                    "rationale": "The prompt has one matching option.",
                    "score": 2,
                    "canonical_item_ids": ["canonical-matching"],
                },
                {
                    "item_id": "task-46",
                    "item_type": "task_based_reading",
                    "passage": "Read the notice.",
                    "tasks": [{"task_id": "t46", "prompt": "Question 47: State one detail.", "response_format": "short answer", "score": 2}],
                    "answer": {"responses": [{"task_id": "t46", "response": "The detail."}]},
                    "rationale": "The response is supported.",
                    "score": 2,
                    "canonical_item_ids": ["canonical-task"],
                },
                {
                    "item_id": "writing-46",
                    "item_type": "practical_writing",
                    "prompt": "Write a short note.",
                    "rubric": [{"criterion": "purpose", "points": 2, "descriptor": "Clear purpose."}],
                    "answer": {"text": "Teacher answer."},
                    "rationale": "The note meets the task.",
                    "score": 2,
                    "canonical_item_ids": ["canonical-writing"],
                },
            ],
        }
        with tempfile.TemporaryDirectory(prefix="mse-ir-visible-projection-") as td:
            assessment = Path(td) / "assessment.json"
            assessment.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            outputs = {}
            for view in ("student", "teacher"):
                output = Path(td) / f"{view}-ir.json"
                result = subprocess.run([sys.executable, str(COMPILER), "--assessment", str(assessment), "--output", str(output), "--view", view], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outputs[view] = json.loads(output.read_text(encoding="utf-8"))

            student = outputs["student"]
            student_text = "\n".join(str(block.get("text", "")) for item in student["items"] for block in item["blocks"])
            self.assertEqual(re.findall(r"Question|Blank\s+b|Response format:|Score:|Answer:|Rationale|canonical|\b[qpb]\d+\b", student_text, re.IGNORECASE), [])
            self.assertIn("46. Choose the correct sentence.", student_text)
            self.assertIn("47. State one detail.", student_text)
            for letter in "ABCD":
                self.assertIn(f"{letter}.", student_text)
            self.assertIn("(   )", student_text)
            matching_grid = next(block for block in student["items"][2]["blocks"] if block.get("kind") == "MatchingGrid")
            self.assertEqual(matching_grid.get("text"), "")
            self.assertNotIn("answer", student["items"][0])
            self.assertNotIn("canonical_item_ids", student["items"][0])

            teacher = outputs["teacher"]
            for item in teacher["items"]:
                self.assertIn("answer", item)
                self.assertIn("rationale", item)
                self.assertIn("canonical_item_ids", item)
            teacher_grid = next(block for block in teacher["items"][2]["blocks"] if block.get("kind") == "MatchingGrid")
            self.assertIn("p46", teacher_grid["grid"]["prompt_ids"])
            teacher_text = "\n".join(str(block.get("text", "")) for item in teacher["items"] for block in item["blocks"])
            self.assertIn("t46", teacher_text)
            self.assertIn("Response format: short answer", teacher_text)
            self.assertIn("Score: 2", teacher_text)


if __name__ == "__main__":
    unittest.main()
