#!/usr/bin/env python3
"""Render a validated assessment machine source into requested output views."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


OUTPUT_FILENAMES = {
    "student": "student.md",
    "teacher": "teacher.md",
    "answer_sheet": "answer-sheet.json",
}

COMPACT_CHOICE_TYPES = {
    "single_choice": None,
    "reading_multiple_choice": "passage",
    "vocabulary_in_context": "context",
}


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def text_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def option_lines(options: Any) -> list[str]:
    if not isinstance(options, list):
        return []
    lines: list[str] = []
    for index, option in enumerate(options):
        if isinstance(option, dict):
            option_id = option.get("option_id", chr(65 + index))
            option_text = option.get("text", "")
        else:
            option_id = chr(65 + index)
            option_text = option
        lines.append(f"{option_id}. {option_text}")
    return lines


def compact_option_line(options: Any) -> str:
    return " ".join(option_lines(options))


def labeled_text(label: str, value: Any) -> list[str]:
    value = text_value(value)
    return [f"**{label}**\n\n{value}"] if value else []


def blank_lines(blanks: Any) -> list[str]:
    if not isinstance(blanks, list):
        return []
    lines = ["**Blanks**"]
    for index, blank in enumerate(blanks, 1):
        if isinstance(blank, dict):
            blank_id = blank.get("blank_id", index)
            position = blank.get("position", index)
            target = text_value(blank.get("target"))
            suffix = f" — {target}" if target else ""
            lines.append(f"{position}. [{blank_id}]{suffix}")
        else:
            lines.append(f"{index}. {blank}")
    return lines


def cloze_option_lines(options: Any) -> list[str]:
    if not isinstance(options, list):
        return []
    lines = ["**Options by blank**"]
    for index, option_set in enumerate(options, 1):
        if isinstance(option_set, dict):
            blank_id = option_set.get("blank_id", index)
            lines.append(f"Blank {blank_id}:")
            lines.extend(option_lines(option_set.get("options")))
        else:
            lines.append(f"Blank {index}: {option_set}")
    return lines


def matching_prompt_lines(prompts: Any) -> list[str]:
    if not isinstance(prompts, list):
        return []
    lines = ["**Prompts**"]
    for index, prompt in enumerate(prompts, 1):
        if isinstance(prompt, dict):
            prompt_id = prompt.get("prompt_id", index)
            prompt_text = prompt.get("text", "")
            lines.append(f"{prompt_id}. {prompt_text}")
        else:
            lines.append(f"{index}. {prompt}")
    return lines


def task_lines(tasks: Any) -> list[str]:
    if not isinstance(tasks, list):
        return []
    lines = ["**Tasks**"]
    for index, task in enumerate(tasks, 1):
        if isinstance(task, dict):
            task_id = task.get("task_id", index)
            prompt = task.get("prompt", "")
            response_format = text_value(task.get("response_format"))
            score = task.get("score")
            lines.append(f"{task_id}. {prompt}")
            details = []
            if response_format:
                details.append(f"Response format: {response_format}")
            if score is not None:
                details.append(f"Score: {score}")
            if details:
                lines.append("   " + " | ".join(details))
        else:
            lines.append(f"{index}. {task}")
    return lines


def rubric_lines(rubric: Any) -> list[str]:
    if not isinstance(rubric, list):
        return []
    lines = ["**Rubric**"]
    for index, criterion in enumerate(rubric, 1):
        if isinstance(criterion, dict):
            name = criterion.get("criterion", index)
            points = criterion.get("points", "")
            descriptor = criterion.get("descriptor", "")
            lines.append(f"{index}. {name} ({points}): {descriptor}")
        else:
            lines.append(f"{index}. {criterion}")
    return lines


def listening_lines(item: dict[str, Any]) -> list[str]:
    lines = labeled_text("Script outline", item.get("script_outline"))
    roles = item.get("speaker_roles")
    if isinstance(roles, list):
        lines.append("**Speaker roles**")
        for role in roles:
            if isinstance(role, dict):
                lines.append(f"- {role.get('role', '')}: {role.get('purpose', '')}")
    sequence = item.get("task_sequence")
    if isinstance(sequence, list):
        lines.append("**Task sequence**")
        for step in sequence:
            if isinstance(step, dict):
                lines.append(
                    f"{step.get('step', '')}. {step.get('task_kind', '')} "
                    f"({step.get('item_count', '')} item(s), {step.get('score', '')} point(s))"
                )
    skills = item.get("target_skills")
    if isinstance(skills, list):
        lines.append("**Target skills:** " + ", ".join(str(skill) for skill in skills))
    return lines


def single_choice_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Question", item.get("stem")) + option_lines(item.get("options"))


def cloze_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Passage", item.get("passage")) + blank_lines(item.get("blanks")) + cloze_option_lines(item.get("options"))


def reading_multiple_choice_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Passage", item.get("passage")) + labeled_text("Question", item.get("stem")) + option_lines(item.get("options"))


def reading_matching_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Passage", item.get("passage")) + matching_prompt_lines(item.get("prompts")) + ["**Options**"] + option_lines(item.get("options"))


def task_based_reading_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Passage", item.get("passage")) + task_lines(item.get("tasks"))


def vocabulary_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Context", item.get("context")) + labeled_text("Question", item.get("stem")) + option_lines(item.get("options"))


def compact_choice_lines(item: dict[str, Any]) -> list[str]:
    context_field = COMPACT_CHOICE_TYPES[str(item.get("item_type"))]
    context = text_value(item.get(context_field)) if context_field else ""
    question = " ".join(value for value in (text_value(item.get("stem")), compact_option_line(item.get("options"))) if value)
    return [line for line in (context, question) if line]


def grammar_or_completion_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Question", item.get("stem"))


def word_bank_lines(item: dict[str, Any]) -> list[str]:
    lines = labeled_text("Question", item.get("stem")) + blank_lines(item.get("blanks"))
    word_bank = item.get("word_bank")
    if isinstance(word_bank, list):
        lines.append("**Word bank:** " + ", ".join(str(word) for word in word_bank))
    return lines


def practical_writing_lines(item: dict[str, Any]) -> list[str]:
    return labeled_text("Writing task", item.get("prompt")) + rubric_lines(item.get("rubric"))


STUDENT_RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "listening_blueprint": listening_lines,
    "single_choice": single_choice_lines,
    "cloze": cloze_lines,
    "reading_multiple_choice": reading_multiple_choice_lines,
    "reading_matching": reading_matching_lines,
    "task_based_reading": task_based_reading_lines,
    "vocabulary_in_context": vocabulary_lines,
    "grammar_fill": grammar_or_completion_lines,
    "sentence_completion": grammar_or_completion_lines,
    "word_bank_fill": word_bank_lines,
    "practical_writing": practical_writing_lines,
}


def item_content(item: dict[str, Any], teacher: bool, number: int) -> list[str]:
    item_type = str(item.get("item_type", "item"))
    lines = [f"### {number}. {item_type} ({item.get('score', '')} point(s))"]
    renderer = STUDENT_RENDERERS.get(item_type)
    if renderer is not None:
        lines.extend(compact_choice_lines(item) if not teacher and item_type in COMPACT_CHOICE_TYPES else renderer(item))
    if teacher:
        # A listening blueprint has no answer key or rationale by schema contract.
        if "answer" in item:
            lines.append(f"**Answer:** `{json.dumps(item['answer'], ensure_ascii=False, sort_keys=True)}`")
        if text_value(item.get("rationale")):
            lines.append(f"**Rationale:** {text_value(item['rationale'])}")
        lines.append("**Canonical items:** " + ", ".join(str(value) for value in item.get("canonical_item_ids", [])))
        if item.get("validation") is not None:
            lines.append("**Validation:** " + json.dumps(item["validation"], ensure_ascii=False, sort_keys=True))
    elif item_type not in COMPACT_CHOICE_TYPES:
        lines.append("Response: ____________________")
    return lines


def render(data: dict[str, Any], teacher: bool) -> str:
    title = "Teacher Answer and Analysis" if teacher else "Student Practice"
    lines = [f"# {title}", "", f"Assessment: {data.get('assessment_id', '')}", ""]
    for number, item in enumerate(data.get("items", []), 1):
        if isinstance(item, dict):
            lines.extend(item_content(item, teacher, number))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def response_type(item: dict[str, Any]) -> str:
    return {
        "listening_blueprint": "listening_blueprint",
        "single_choice": "one_option",
        "cloze": "one_option_per_blank",
        "reading_multiple_choice": "one_option",
        "reading_matching": "one_match_per_prompt",
        "task_based_reading": "structured_response",
        "vocabulary_in_context": "one_option_or_word",
        "grammar_fill": "free_response",
        "sentence_completion": "free_response",
        "word_bank_fill": "one_word_per_blank",
        "practical_writing": "rubric_scored",
    }.get(str(item.get("item_type")), "unknown")


def answer_sheet(data: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for index, item in enumerate(data.get("items", []), 1):
        if not isinstance(item, dict):
            continue
        row = {
            "item_number": index,
            "item_id": item.get("item_id"),
            "score": item.get("score"),
            "response_type": response_type(item),
        }
        if "answer" in item:
            row["answer"] = item["answer"]
        rows.append(row)
    return {
        "assessment_id": data.get("assessment_id"),
        "blueprint_id": data.get("blueprint", {}).get("blueprint_id"),
        "items": rows,
    }


def requested_outputs(data: dict[str, Any]) -> list[str]:
    request = data.get("request")
    outputs = request.get("outputs") if isinstance(request, dict) else None
    if not isinstance(outputs, list):
        raise ValueError("assessment request must contain an outputs array")
    unknown = [output for output in outputs if output not in OUTPUT_FILENAMES]
    if unknown:
        raise ValueError(f"assessment request contains unknown outputs: {unknown}")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        data = load(Path(args.input))
        outputs = requested_outputs(data)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if "student" in outputs:
            (out_dir / OUTPUT_FILENAMES["student"]).write_text(render(data, teacher=False), encoding="utf-8")
        if "teacher" in outputs:
            (out_dir / OUTPUT_FILENAMES["teacher"]).write_text(render(data, teacher=True), encoding="utf-8")
        if "answer_sheet" in outputs:
            (out_dir / OUTPUT_FILENAMES["answer_sheet"]).write_text(
                json.dumps(answer_sheet(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps({"status": "RENDERED", "out_dir": str(out_dir), "files": [OUTPUT_FILENAMES[output] for output in outputs]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "RENDER_FAILED", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
