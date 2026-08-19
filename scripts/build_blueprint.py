#!/usr/bin/env python3
"""Build and validate an assessment blueprint before item writing."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def emit(value: dict[str, Any], output: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def resolve_book(catalog: dict[str, Any], book_id: str, include_candidates: bool) -> tuple[dict[str, Any] | None, str, str | None]:
    for entry in catalog.get("supported_books", []):
        if entry.get("book_id") == book_id:
            return entry, "released", None
    for entry in catalog.get("candidate_books", []):
        if entry.get("book_id") == book_id and include_candidates:
            return entry, "candidate", None
    if book_id.startswith("grade-09"):
        return None, "unpublished", "Grade 9 is not currently published."
    return None, "unpublished", "The requested book is not released in the catalog."


def clarification(missing: list[str], messages: list[str]) -> dict[str, Any]:
    return {"status": "NEEDS_CLARIFICATION", "missing": missing, "questions": messages}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, help="JSON request path, or - for stdin")
    parser.add_argument("--output")
    parser.add_argument("--include-candidates", action="store_true")
    args = parser.parse_args()
    if args.request == "-":
        request = json.load(sys.stdin)
    else:
        request = load(Path(args.request))
    missing: list[str] = []
    questions: list[str] = []
    if not request.get("book_id"):
        missing.append("book_id")
        questions.append("Which published book should be used?")
    if not request.get("unit_ids") and not request.get("assessment_scope"):
        missing.append("unit_ids_or_assessment_scope")
        questions.append("Which unit IDs or named assessment scope should be covered?")
    if not request.get("purpose"):
        missing.append("purpose")
        questions.append("Is this lesson practice, unit practice, midterm practice, final practice, or another purpose?")
    if not request.get("item_type_plan"):
        missing.append("item_type_plan")
        questions.append("Which registered item types, counts, and per-item scores should be used?")
    if not request.get("outputs"):
        missing.append("outputs")
        questions.append("Should the result include student, teacher, and/or answer-sheet output?")
    if missing:
        result = clarification(missing, questions)
        emit(result, args.output)
        return 2
    catalog = load(REFERENCES / "catalog.json")
    entry, catalog_status, reason = resolve_book(catalog, request["book_id"], args.include_candidates)
    if not entry:
        emit({"status": "UNPUBLISHED_BOOK", "book_id": request["book_id"], "message": reason}, args.output)
        return 2
    data = load(REFERENCES / str(entry["data_file"]))
    units_by_id = {unit["unit_id"]: unit for unit in data.get("units", [])}
    if request.get("unit_ids"):
        unit_ids = list(dict.fromkeys(request["unit_ids"]))
    else:
        boundaries = [boundary for boundary in data.get("assessment_boundaries", []) if boundary.get("assessment_type") == request["assessment_scope"]]
        if not boundaries:
            emit({"status": "NO_SUCH_ASSESSMENT_SCOPE", "book_id": request["book_id"], "assessment_scope": request["assessment_scope"]}, args.output)
            return 2
        boundary = boundaries[0]
        if boundary.get("scope_status") != "confirmed":
            emit({
                "status": "ASSESSMENT_SCOPE_UNCONFIRMED",
                "book_id": request["book_id"],
                "assessment_scope": request["assessment_scope"],
                "scope_status": boundary.get("scope_status"),
                "message": "This scope is only a profile and cannot drive a formal assessment.",
            }, args.output)
            return 2
        unit_ids = boundary.get("covered_unit_ids", [])
    invalid_units = [unit_id for unit_id in unit_ids if unit_id not in units_by_id]
    if invalid_units:
        emit({"status": "INVALID_SCOPE", "invalid_unit_ids": invalid_units}, args.output)
        return 2
    registry = load(REFERENCES / "authoring" / "registry.json")
    registered = {item["item_type"] for item in registry.get("item_types", [])}
    invalid_types = [plan.get("item_type") for plan in request["item_type_plan"] if plan.get("item_type") not in registered]
    if invalid_types:
        emit({"status": "UNREGISTERED_ITEM_TYPE", "item_types": invalid_types}, args.output)
        return 2
    score_lines: list[dict[str, Any]] = []
    computed_total = 0
    for plan in request["item_type_plan"]:
        count = plan.get("item_count", 0)
        score_each = plan.get("score_each", 0)
        if not isinstance(count, int) or count <= 0 or not isinstance(score_each, (int, float)) or score_each <= 0:
            emit({"status": "INVALID_ITEM_PLAN", "plan": plan}, args.output)
            return 2
        total = count * score_each
        computed_total += total
        score_lines.append({"item_type": plan["item_type"], "item_count": count, "score_each": score_each, "score_total": total})
    expected_total = request.get("total_score", computed_total)
    if expected_total != computed_total:
        emit({"status": "SCORE_ARITHMETIC_FAIL", "expected_total": expected_total, "computed_total": computed_total}, args.output)
        return 2
    scope_items = [item for item in data.get("items", []) if item.get("unit_id") in unit_ids]
    primary_items = [item for item in scope_items if item.get("level") in {"A", "B"}]
    if not primary_items:
        emit({"status": "NO_PRIMARY_COVERAGE", "unit_ids": unit_ids}, args.output)
        return 2
    primary_items.sort(key=lambda item: (item["unit_id"], item["domain"], item["id"]))
    planned_count = sum(line["item_count"] for line in score_lines)
    coverage_targets: list[dict[str, Any]] = []
    for item in primary_items[:planned_count]:
        coverage_targets.append({"canonical_item_id": item["id"], "target_role": "primary", "planned_item_count": 1})
    if len(coverage_targets) < planned_count:
        coverage_targets[0]["planned_item_count"] += planned_count - len(coverage_targets)
    canonical_request = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    blueprint_id = "generated-" + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()[:16]
    blueprint = {
        "blueprint_id": blueprint_id,
        "request": request,
        "catalog_status": catalog_status,
        "resolved_unit_ids": unit_ids,
        "sections": score_lines,
        "coverage_targets": coverage_targets,
        "score_check": {"expected_total": expected_total, "computed_total": computed_total},
        "boundary_check": {"allowed_primary_levels": ["A", "B"], "reinforcement": bool(request.get("reinforcement", False)), "context_only_level": "D"},
    }
    emit({"status": "BLUEPRINT_OK", "blueprint": blueprint}, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
