#!/usr/bin/env python3
"""Compile the shared canonical assessment tree into student/teacher IR.

The print renderer consumes this module rather than reconstructing questions
from ad-hoc fields.  The canonical tree is built once; the two public views
are projections of that tree.  Keeping the compiler small and deterministic is
intentional: the manifest can then bind every visible block to its source
item, task or prompt.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
QUESTION_PREFIX = re.compile(r"^\s*Question\s+(\d+)\s*[:.)-]\s*", re.IGNORECASE)
BLANK_LABEL = re.compile(r"\bBlank\s+b\d+\s*:?[ \t]*", re.IGNORECASE)
BLANK_MARKER = re.compile(r"\[\s*b\d+\s*\]", re.IGNORECASE)
INTERNAL_ID = re.compile(r"\b[qpb]\d+\b", re.IGNORECASE)
WRITING_SECTION_LABEL = re.compile(r"(?<!\n)(?=(?:思维导图|要求|给定开头)[：:])")
DEFAULT_WRITING_LINE_COUNT = 6


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def student_visible_text(value: Any, *, preserve_question_number: bool = False) -> str:
    """Remove renderer labels while retaining source wording and blank space."""
    value_text = BLANK_MARKER.sub("(   )", text(value))
    value_text = BLANK_LABEL.sub("", value_text)
    question = QUESTION_PREFIX.match(value_text)
    if question:
        remainder = value_text[question.end():]
        value_text = f"{question.group(1)}. {remainder}" if preserve_question_number else remainder
    value_text = INTERNAL_ID.sub("", value_text)
    value_text = re.sub(r"[ \t]+([,.;:!?])", r"\1", value_text)
    return value_text.strip()


def writing_prompt_text(value: Any, view: str = "student") -> str:
    """Keep source semantic lines and split only at known writing labels."""
    visible = student_visible_text(value, preserve_question_number=True) if view == "student" else text(value)
    if view != "student" or not any("\u4e00" <= char <= "\u9fff" for char in visible):
        return visible
    # Source newlines are authoritative.  The labels are the only safe
    # fallback boundary when an upstream source keeps sections inline.
    visible = re.sub(r"[ \t]*\r?\n[ \t]*", "\n", visible).strip()
    return WRITING_SECTION_LABEL.sub("\n", visible).strip()


def block(
    item_id: str,
    ordinal: int,
    role: str,
    kind: str,
    value: str,
    *,
    task_id: str | None = None,
    prompt_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
    block_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "block_id": block_id or f"{item_id}-block-{ordinal:03d}",
        "role": role,
        "kind": kind,
        "source_item_id": item_id,
        "text": value,
        "font_size_pt": 10.5,
    }
    if task_id:
        record["source_task_id"] = task_id
    if prompt_id:
        record["source_prompt_id"] = prompt_id
    if extra:
        record.update(extra)
    return record


def response_contract(
    item: Mapping[str, Any],
    task: Mapping[str, Any] | None = None,
    *,
    writing_line_count: int = DEFAULT_WRITING_LINE_COUNT,
) -> dict[str, Any]:
    item_type = str(item.get("item_type", ""))
    score = item.get("score", 1)
    if item_type in {"single_choice", "reading_multiple_choice", "cloze"}:
        if item_type == "cloze":
            return {
                "response_kind": "choice",
                "line_policy": "inline",
                "line_count": 0,
                "score": score,
            }
        return {
            "response_kind": "choice",
            "line_policy": "none",
            "line_count": 0,
            "score": score,
        }
    if item_type == "vocabulary_in_context" and item.get("options"):
        return {
            "response_kind": "choice",
            "line_policy": "none",
            "line_count": 0,
            "score": score,
        }
    if item_type == "reading_matching":
        return {
            "response_kind": "letter",
            "line_policy": "inline",
            "line_count": 0,
            "score": score,
        }
    if task is not None:
        response_format = str(task.get("response_format", "sentence")).casefold()
        kind = "paragraph" if "paragraph" in response_format or "writing" in response_format else "sentence"
        return {
            "response_kind": kind,
            "line_policy": "multi-line" if kind == "paragraph" else "one-line",
            "line_count": 4 if kind == "paragraph" else 1,
            "expected_max_chars": 160 if kind == "paragraph" else 80,
            "expected_words": 60 if kind == "paragraph" else 12,
            "score": task.get("score", score),
        }
    if item_type in {"practical_writing"}:
        return {
            "response_kind": "paragraph",
            "line_policy": "multi-line",
            "line_count": writing_line_count,
            "expected_max_chars": 240,
            "expected_words": 80,
            "score": score,
        }
    return {
        "response_kind": "word" if item_type in {"grammar_fill", "word_bank_fill", "vocabulary_in_context"} else "sentence",
        "line_policy": "one-line",
        "line_count": 1,
        "expected_max_chars": 80,
        "score": score,
    }


def option_text(option: Mapping[str, Any], view: str = "student") -> str:
    value = student_visible_text(option.get("text", "")) if view == "student" else text(option.get("text", ""))
    return f"{option.get('option_id', '')}. {value}"


def prompt_text(prompt: Mapping[str, Any], view: str = "student") -> str:
    if view == "student":
        return student_visible_text(prompt.get("text", ""), preserve_question_number=True)
    return f"{prompt.get('prompt_id', '')}. {text(prompt.get('text', ''))}"


def task_text(task: Mapping[str, Any], view: str = "student") -> str:
    if view == "student":
        return student_visible_text(task.get("prompt", ""), preserve_question_number=True)
    value = f"{task.get('task_id', '')}. {text(task.get('prompt', ''))}"
    details: list[str] = []
    if task.get("response_format") is not None:
        details.append(f"Response format: {task.get('response_format')}")
    if task.get("score") is not None:
        details.append(f"Score: {task.get('score')}")
    return value + (" | " + " | ".join(details) if details else "")


def matching_grid_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete prompt/option payload owned by a MatchingGrid.

    MatchingGrid is rendered by one specialised flowable. Keeping its full
    source payload in the IR (rather than only prompt/option IDs) prevents the
    renderer from having to reconstruct content from a second ad-hoc source,
    while still avoiding duplicate ordinary Paragraph blocks in the PDF.
    """
    return {
        "options": [dict(option) for option in source.get("options", []) or [] if isinstance(option, Mapping)],
        "prompts": [dict(prompt) for prompt in source.get("prompts", []) or [] if isinstance(prompt, Mapping)],
    }


def compile_canonical(assessment: Mapping[str, Any]) -> dict[str, Any]:
    """Return the private, answer-bearing canonical tree."""
    items: list[dict[str, Any]] = []
    for index, source in enumerate(assessment.get("items", []), 1):
        item = dict(source)
        item["item_index"] = index
        item["item_id"] = str(source.get("item_id", ""))
        item["score"] = source.get("score", 1)
        items.append(item)
    return {
        "assessment_id": assessment.get("assessment_id", ""),
        "blueprint_id": assessment.get("blueprint", {}).get("blueprint_id"),
        "items": items,
    }


def project(
    canonical: Mapping[str, Any],
    view: str,
    *,
    writing_line_count: int = DEFAULT_WRITING_LINE_COUNT,
) -> dict[str, Any]:
    if view not in {"student", "teacher"}:
        raise ValueError("view must be student or teacher")
    projected_items: list[dict[str, Any]] = []
    for source in canonical["items"]:
        item_id = str(source["item_id"])
        blocks: list[dict[str, Any]] = []
        base_block_ordinal = 0
        asset_block_ordinal = 0
        emitted_assets: set[str] = set()
        stimulus_assets = [
            asset for asset in source.get("stimulus_assets", []) or [] if isinstance(asset, Mapping)
        ]

        def asset_reference(asset: Mapping[str, Any]) -> dict[str, Any]:
            reference: dict[str, Any] = {"kind": "AssetBlock"}
            for key in ("asset_id", "semantic_role", "placement", "required_for_answer", "caption", "after_block_id"):
                if key in asset:
                    reference[key] = asset[key]
            return reference

        def append_asset(asset: Mapping[str, Any]) -> None:
            nonlocal asset_block_ordinal
            asset_id = str(asset.get("asset_id", ""))
            if not asset_id or asset_id in emitted_assets:
                return
            asset_block_ordinal += 1
            blocks.append(
                block(
                    item_id,
                    asset_block_ordinal,
                    "asset",
                    "AssetBlock",
                    text(asset.get("caption", "")),
                    extra={"asset": asset_reference(asset)},
                    block_id=f"{item_id}-asset-{asset_block_ordinal:03d}",
                )
            )
            emitted_assets.add(asset_id)

        def emit_assets_before(placement: str) -> None:
            for asset in stimulus_assets:
                if str(asset.get("asset_id", "")) in emitted_assets:
                    continue
                if asset.get("placement") == placement:
                    append_asset(asset)

        def emit_assets_at_boundary(anchor_block_id: str, after_placement: str | None = None) -> None:
            """Emit assets whose semantic boundary is this already-built block."""
            for asset in stimulus_assets:
                if str(asset.get("asset_id", "")) in emitted_assets:
                    continue
                placement = asset.get("placement")
                follows_anchor = placement == "inline_block" and asset.get("after_block_id") == anchor_block_id
                follows_semantic_field = after_placement is not None and placement == after_placement
                if follows_anchor or follows_semantic_field:
                    append_asset(asset)

        def add(
            role: str,
            kind: str,
            value: Any,
            *,
            allow_empty: bool = False,
            after_placement: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any] | None:
            nonlocal base_block_ordinal
            visible = student_visible_text(value, preserve_question_number=True) if view == "student" else text(value)
            if not visible and not allow_empty:
                return None
            task_id = kwargs.pop("task_id", None)
            prompt_id = kwargs.pop("prompt_id", None)
            base_block_ordinal += 1
            record = block(item_id, base_block_ordinal, role, kind, visible, task_id=task_id, prompt_id=prompt_id, extra=kwargs)
            blocks.append(record)
            emit_assets_at_boundary(record["block_id"], after_placement)
            return record

        heading_text = f"{source['item_index']}." if view == "student" else f"Question {source['item_index']} ({source['score']} point(s))"
        add("heading", "Paragraph", heading_text)

        for field, role in (("passage", "passage"), ("instruction", "instruction"), ("context", "content"), ("stem", "stem"), ("prompt", "prompt")):
            if source.get(field):
                if field == "passage":
                    emit_assets_before("before_passage")
                elif field == "stem":
                    emit_assets_before("before_stem")
                add(
                    role,
                    "Paragraph",
                    writing_prompt_text(source[field], view) if field == "prompt" and source.get("item_type") == "practical_writing" else source[field],
                    after_placement={"passage": "after_passage", "stem": "after_stem"}.get(field),
                )
                if field == "prompt" and source.get("item_type") == "practical_writing":
                    contract = response_contract(source, writing_line_count=writing_line_count)
                    add(
                        "response_area",
                        "ResponseArea",
                        "",
                        allow_empty=True,
                        response={
                            "kind": "ResponseArea",
                            "response_id": f"{item_id}/response",
                            "source_item_id": item_id,
                            "answer_contract": contract,
                            "line_policy": contract["line_policy"],
                            "line_count": contract["line_count"],
                            "min_height_mm": 8,
                        },
                    )
        if source.get("script_outline"):
            add("content", "Paragraph", source["script_outline"])
        for speaker in source.get("speaker_roles", []) or []:
            if isinstance(speaker, Mapping):
                add("content", "Paragraph", f"{speaker.get('role', '')}: {speaker.get('purpose', '')}")
        for step in source.get("task_sequence", []) or []:
            if isinstance(step, Mapping):
                step_text = f"Step {step.get('step', '')}: {step.get('task_kind', '')}"
                if view == "teacher":
                    step_text += f" ({step.get('item_count', '')} item(s), {step.get('score', '')} points)"
                add("content", "Paragraph", step_text)
        for skill in source.get("target_skills", []) or []:
            add("content", "Paragraph", f"Target skill: {skill}")
        if view == "teacher":
            for blank in source.get("blanks", []) or []:
                if isinstance(blank, Mapping):
                    add("content", "Paragraph", f"Blank {blank.get('blank_id', '')}")
        is_matching = source.get("item_type") == "reading_matching"
        tasks = [task for task in source.get("tasks", []) or [] if isinstance(task, Mapping)]
        if not is_matching:
            for prompt in source.get("prompts", []) or []:
                if isinstance(prompt, Mapping):
                    prompt_id = str(prompt.get("prompt_id", ""))
                    add("prompt", "Paragraph", prompt_text(prompt, view), prompt_id=prompt_id)
        # A task response is part of the task's semantic unit.  Emit the
        # task and its answer area as one pair so the renderer cannot collect
        # all task prompts and append anonymous lines at the end of the item.
        for task in tasks:
            task_id = str(task.get("task_id", ""))
            add("task", "Paragraph", task_text(task, view), task_id=task_id)
            contract = response_contract(source, task)
            add(
                "response_area",
                "ResponseArea",
                "",
                allow_empty=True,
                task_id=task_id,
                response={
                    "kind": "ResponseArea",
                    "response_id": f"{item_id}/{task_id}",
                    "source_item_id": item_id,
                    "source_task_id": task_id,
                    "answer_contract": contract,
                    "line_policy": contract["line_policy"],
                    "line_count": contract["line_count"],
                    "min_height_mm": 8,
                },
            )
        if not is_matching:
            for option in source.get("options", []) or []:
                if isinstance(option, Mapping):
                    if isinstance(option.get("options"), list):
                        nested = "; ".join(option_text(nested_option, view) for nested_option in option["options"] if isinstance(nested_option, Mapping))
                        value = nested if view == "student" else f"{option.get('blank_id', '')}: {nested}"
                        add("option", "Paragraph", value)
                    else:
                        add("option", "Paragraph", option_text(option, view))
        if source.get("word_bank"):
            word_bank_text = "Word bank: " + ", ".join(text(value) for value in source["word_bank"])
            add(
                "word_bank",
                "WordBankBox",
                word_bank_text,
                alignment="center",
                box={
                    "component_id": f"{item_id}-word-bank",
                    "component_role": "word_bank",
                    "alignment": "center",
                    "padding_pt": {"top": 8, "right": 10, "bottom": 8, "left": 10},
                },
            )
        if source.get("item_type") == "listening_blueprint":
            add("instruction", "Paragraph", "Listening blueprint: follow the script outline and task sequence.")
        if view == "teacher":
            for criterion in source.get("rubric", []) or []:
                if isinstance(criterion, Mapping):
                    add("instruction", "Paragraph", f"{criterion.get('criterion', '')}: {criterion.get('descriptor', '')} ({criterion.get('points', '')} points)")

        if is_matching:
            matching_payload = matching_grid_payload(source)
            add(
                "content",
                "MatchingGrid",
                "",
                allow_empty=True,
                grid={
                    "kind": "MatchingGrid",
                    "layout": "card-grid",
                    "prompt_ids": [str(prompt.get("prompt_id")) for prompt in matching_payload["prompts"]],
                    "option_ids": [str(option.get("option_id")) for option in matching_payload["options"]],
                },
            )

        if not tasks and source.get("prompts") and source.get("item_type") == "reading_matching":
            for prompt in source.get("prompts", []) or []:
                prompt_id = str(prompt.get("prompt_id", ""))
                contract = response_contract(source)
                add(
                    "response_area",
                    "ResponseArea",
                    "",
                    prompt_id=prompt_id,
                    allow_empty=True,
                    response={
                        "kind": "ResponseArea",
                        "response_id": f"{item_id}/{prompt_id}",
                        "source_item_id": item_id,
                        "source_prompt_id": prompt_id,
                        "answer_contract": contract,
                        "line_policy": "inline",
                        "line_count": 0,
                        "min_height_mm": 0,
                    },
                )
        elif source.get("item_type") == "cloze" and source.get("blanks"):
            contract = response_contract(source)
            for blank in source.get("blanks", []) or []:
                if not isinstance(blank, Mapping):
                    continue
                blank_id = str(blank.get("blank_id", ""))
                add(
                    "response_area",
                    "ResponseArea",
                    "(   )",
                    allow_empty=True,
                    response={
                        "kind": "ResponseArea",
                        "response_id": f"{item_id}/{blank_id}",
                        "source_item_id": item_id,
                        "answer_contract": contract,
                        "line_policy": "inline",
                        "line_count": 0,
                        "min_height_mm": 0,
                    },
                )
        elif not tasks and source.get("item_type") != "practical_writing":
            contract = response_contract(source)
            if contract["line_policy"] != "none":
                add(
                    "response_area",
                    "ResponseArea",
                    "",
                    allow_empty=True,
                    response={
                        "kind": "ResponseArea",
                        "response_id": f"{item_id}/response",
                        "source_item_id": item_id,
                        "answer_contract": contract,
                        "line_policy": contract["line_policy"],
                        "line_count": contract["line_count"],
                        "min_height_mm": 8,
                    },
                )

        unresolved_assets = [
            asset for asset in stimulus_assets if str(asset.get("asset_id", "")) not in emitted_assets
        ]
        if unresolved_assets:
            unresolved = ", ".join(
                f"{asset.get('asset_id', '')}:{asset.get('placement', '')}"
                for asset in unresolved_assets
            )
            raise ValueError(f"asset placement anchor not found: {unresolved}")

        projected: dict[str, Any] = {
            "item_id": item_id,
            "item_type": source.get("item_type", ""),
            "item_index": source["item_index"],
            "score": source["score"],
            "blocks": blocks,
        }
        if view == "teacher":
            projected["answer"] = source.get("answer")
            projected["rationale"] = source.get("rationale", "")
            projected["canonical_item_ids"] = list(source.get("canonical_item_ids", []))
        projected_items.append(projected)

    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "assessment_id": canonical["assessment_id"],
        "blueprint_id": canonical["blueprint_id"],
        "view": view,
        "items": projected_items,
    }
    return result


def compile_views(
    assessment: Mapping[str, Any],
    *,
    writing_line_count: int = DEFAULT_WRITING_LINE_COUNT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical = compile_canonical(assessment)
    return (
        project(canonical, "student", writing_line_count=writing_line_count),
        project(canonical, "teacher", writing_line_count=writing_line_count),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the shared canonical print IR")
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--view", choices=("student", "teacher"), required=True)
    parser.add_argument("--writing-line-count", type=int, default=DEFAULT_WRITING_LINE_COUNT)
    args = parser.parse_args(argv)
    try:
        assessment = json.loads(Path(args.assessment).read_text(encoding="utf-8"))
        if not isinstance(assessment, Mapping) or not assessment.get("items"):
            raise ValueError("assessment must contain items")
        if args.writing_line_count < 1:
            raise ValueError("writing line count must be positive")
        canonical = compile_canonical(assessment)
        result = project(canonical, args.view, writing_line_count=args.writing_line_count)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "IR_COMPILED", "view": args.view, "items": len(result["items"])}))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "IR_COMPILE_FAIL", "error_code": "IR_INVALID", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
