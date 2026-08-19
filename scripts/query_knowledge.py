#!/usr/bin/env python3
"""Deterministically query one canonical book."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def unit_sort_key(unit_id: str) -> tuple[int, int]:
    prefix, number = unit_id.split("-")
    return (0 if prefix == "starter" else 1, int(number))


def resolve_entry(catalog: dict[str, Any], book_id: str, include_candidates: bool) -> tuple[dict[str, Any] | None, str, str | None]:
    for entry in catalog.get("supported_books", []):
        if entry.get("book_id") == book_id:
            return entry, "released", None
    for entry in catalog.get("candidate_books", []):
        if entry.get("book_id") == book_id:
            if include_candidates:
                return entry, "candidate", None
            return None, "unpublished", "This book is staged as a candidate and is not released."
    if book_id.startswith("grade-09"):
        return None, "unpublished", "Grade 9 is not currently published."
    return None, "unpublished", "The requested book is not in the catalog."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", required=True)
    parser.add_argument("--unit", action="append", dest="units")
    parser.add_argument("--domain", action="append", dest="domains")
    parser.add_argument("--level", action="append", dest="levels")
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument("--assessment-scope")
    parser.add_argument("--include-candidates", action="store_true")
    args = parser.parse_args()

    catalog = load(REFERENCES / "catalog.json")
    entry, status, reason = resolve_entry(catalog, args.book, args.include_candidates)
    if not entry:
        print(json.dumps({"status": "UNPUBLISHED_BOOK", "book_id": args.book, "message": reason}, ensure_ascii=False, indent=2))
        return 2
    path = REFERENCES / str(entry["data_file"])
    if not path.exists():
        print(json.dumps({"status": "FAIL", "message": f"Missing catalog data file: {path.name}"}, ensure_ascii=False, indent=2))
        return 1
    data = load(path)
    unit_filter = set(args.units or [])
    domain_filter = set(args.domains or [])
    level_filter = set(args.levels or [])
    tag_filter = set(args.tags or [])
    scope_units: set[str] | None = None
    selected_boundary: dict[str, Any] | None = None
    if args.assessment_scope:
        for boundary in data.get("assessment_boundaries", []):
            if boundary.get("assessment_type") == args.assessment_scope:
                selected_boundary = boundary
                scope_units = set(boundary.get("covered_unit_ids", []))
                break
        if selected_boundary is None:
            print(json.dumps({"status": "NO_SUCH_ASSESSMENT_SCOPE", "book_id": args.book, "assessment_scope": args.assessment_scope}, ensure_ascii=False, indent=2))
            return 2
        if selected_boundary.get("scope_status") != "confirmed":
            print(json.dumps({
                "status": "ASSESSMENT_SCOPE_UNCONFIRMED",
                "book_id": args.book,
                "assessment_scope": args.assessment_scope,
                "scope_status": selected_boundary.get("scope_status"),
                "message": "This scope is only a profile and cannot drive a formal assessment.",
            }, ensure_ascii=False, indent=2))
            return 2
    if scope_units is not None:
        unit_filter = scope_units if not unit_filter else unit_filter & scope_units
    items: list[dict[str, Any]] = []
    for item in data.get("items", []):
        if unit_filter and item.get("unit_id") not in unit_filter:
            continue
        if domain_filter and item.get("domain") not in domain_filter:
            continue
        if level_filter and item.get("level") not in level_filter:
            continue
        if tag_filter and not tag_filter.intersection(item.get("tags", [])):
            continue
        items.append(item)
    items.sort(key=lambda item: (unit_sort_key(item["unit_id"]), item["domain"], item["id"]))
    units = [unit for unit in data.get("units", []) if not unit_filter or unit.get("unit_id") in unit_filter]
    units.sort(key=lambda unit: unit_sort_key(unit["unit_id"]))
    output = {
        "status": "OK",
        "catalog_status": status,
        "book_id": args.book,
        "units": units,
        "assessment_scope": selected_boundary,
        "count": len(items),
        "items": items,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
