"""Regression coverage for requested outputs and registered item renderers."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_assessment.py"
SPEC = importlib.util.spec_from_file_location("render_assessment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def base_item(item_type: str) -> dict:
    return {
        "item_id": "item-" + item_type,
        "item_type": item_type,
        "score": 2,
        "canonical_item_ids": ["g7s2-unit-01-topic-001"],
        "answer": {"primary": "example", "accepted": ["example"]},
        "rationale": "Teacher explanation.",
        "validation": {"answer_unique": True},
    }


def every_registered_item() -> list[dict]:
    listening = base_item("listening_blueprint")
    listening.pop("answer")
    listening.pop("rationale")
    listening.update({
        "script_outline": "Two students discuss a school club.",
        "speaker_roles": [{"role": "Host", "purpose": "asks questions"}],
        "task_sequence": [{"step": 1, "task_kind": "choose", "item_count": 1, "score": 2}],
        "target_skills": ["main idea"],
    })
    choice = base_item("single_choice")
    choice.update({"stem": "Choose.", "options": [{"option_id": "A", "text": "One"}, {"option_id": "B", "text": "Two"}]})
    cloze = base_item("cloze")
    cloze.update({"passage": "A [b1] passage.", "blanks": [{"blank_id": "b1", "position": 1}], "options": [{"blank_id": "b1", "options": [{"option_id": "A", "text": "short"}, {"option_id": "B", "text": "long"}]}]})
    reading_choice = base_item("reading_multiple_choice")
    reading_choice.update({"passage": "Reading passage.", "stem": "Choose.", "options": [{"option_id": "A", "text": "One"}, {"option_id": "B", "text": "Two"}]})
    matching = base_item("reading_matching")
    matching.update({"passage": "Matching passage.", "prompts": [{"prompt_id": "p1", "text": "Prompt one"}], "options": [{"option_id": "A", "text": "Match one"}]})
    task = base_item("task_based_reading")
    task.update({"passage": "Task passage.", "tasks": [{"task_id": "t1", "prompt": "State the idea.", "response_format": "short answer", "score": 2}]})
    vocabulary = base_item("vocabulary_in_context")
    vocabulary.update({"context": "Vocabulary context.", "stem": "What does it mean?", "options": [{"option_id": "A", "text": "Meaning"}, {"option_id": "B", "text": "Other"}]})
    grammar = base_item("grammar_fill")
    grammar["stem"] = "Fill the form."
    completion = base_item("sentence_completion")
    completion["stem"] = "Complete this sentence."
    word_bank = base_item("word_bank_fill")
    word_bank.update({"stem": "A [b1] frame.", "blanks": [{"blank_id": "b1", "position": 1}], "word_bank": ["word", "other"]})
    writing = base_item("practical_writing")
    writing.update({"prompt": "Write a note.", "rubric": [{"criterion": "purpose", "points": 2, "descriptor": "Clear purpose."}]})
    return [listening, choice, cloze, reading_choice, matching, task, vocabulary, grammar, completion, word_bank, writing]


class RendererTest(unittest.TestCase):
    def assessment(self, outputs: list[str]) -> dict:
        return {"assessment_id": "render-fixture", "request": {"outputs": outputs}, "blueprint": {"blueprint_id": "bp"}, "items": every_registered_item()}

    def test_registered_types_render_required_student_fields_without_metadata(self) -> None:
        student = RENDERER.render(self.assessment(["student"]), teacher=False)
        for expected in ("Script outline", "Speaker roles", "Task sequence", "Target skills", "Options by blank", "Prompts", "**Tasks**", "**Rubric**", "**Word bank:**"):
            self.assertIn(expected, student)
        for forbidden in ("Teacher explanation.", "g7s2-unit-01-topic-001", "answer_unique", "**Answer:**", "**Rationale:**"):
            self.assertNotIn(forbidden, student)
        self.assertNotIn("audio", student.casefold())

    def test_requested_outputs_are_the_only_files_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "assessment.json"
            source.write_text(json.dumps(self.assessment(["student"])), encoding="utf-8")
            out_dir = Path(temp_dir) / "out"
            self.assertEqual(0, RENDERER.main(["--input", str(source), "--out-dir", str(out_dir)]))
            self.assertEqual([path.name for path in out_dir.iterdir()], ["student.md"])

    def test_teacher_and_answer_sheet_keep_machine_answers(self) -> None:
        data = self.assessment(["teacher", "answer_sheet"])
        teacher = RENDERER.render(data, teacher=True)
        sheet = RENDERER.answer_sheet(data)
        self.assertIn("Teacher explanation.", teacher)
        self.assertIn("g7s2-unit-01-topic-001", teacher)
        self.assertNotIn("**Answer:** `null`", teacher)
        self.assertEqual(sheet["items"][1]["answer"], data["items"][1]["answer"])
        self.assertNotIn("answer", sheet["items"][0])


if __name__ == "__main__":
    unittest.main()
