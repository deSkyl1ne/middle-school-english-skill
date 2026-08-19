#!/usr/bin/env python3
"""Validate a clean Skill package without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"
FORBIDDEN_KEY_NAMES = {"uncertain", "uncertain_items", "unresolved_items"}
FORBIDDEN_ASSESSMENT_KEYS = {"duration_minutes", "total_score", "sections", "question_numbers", "answer_card_structure"}
ITEM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RELATION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_catalog_data_file(data_file: Any) -> Path:
    """Return a catalog data file only when it is a real file inside references."""
    if not isinstance(data_file, str) or not data_file:
        raise ValueError("data_file must be a non-empty string")
    relative = Path(data_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("data_file must be a relative path inside references")
    candidate = REFERENCES / relative
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("data_file must resolve to a file") from exc
    if not resolved.is_file():
        raise ValueError("data_file must name a regular file")
    return resolved


def walk(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")


def collect_global_violations(value: Any, location: str) -> list[str]:
    errors: list[str] = []
    for path, key, item in walk(value):
        lowered_key = str(key).casefold()
        if lowered_key in FORBIDDEN_KEY_NAMES or "conflict" in lowered_key:
            errors.append(f"{location}:{path}: forbidden key {key}")
        if key == "level" and item == "E":
            errors.append(f"{location}:{path}: level E is not publishable")
        if isinstance(item, str):
            lowered = item.casefold()
            if item.startswith(("/private/", "/tmp/")) or item.startswith("../") or item == "..":
                errors.append(f"{location}:{path}: local or traversal path found")
    return errors


def validate_book(data: dict[str, Any], location: str, source_ids: set[str]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    required = {"schema_version", "dataset_version", "book", "units", "items", "progressions", "cross_unit_links", "assessment_boundaries", "generation_boundaries"}
    missing = sorted(required - set(data))
    if missing:
        errors.append(f"{location}: missing top-level fields {missing}")
        return errors, {"book_id": None, "item_ids": []}
    if data.get("schema_version") != "1.0.0":
        errors.append(f"{location}: schema_version must be 1.0.0")
    book = data.get("book", {})
    book_id = book.get("book_id")
    if not re.fullmatch(r"grade-0[789]-semester-[12]", str(book_id)):
        errors.append(f"{location}: invalid book_id {book_id!r}")
    unit_ids = [unit.get("unit_id") for unit in data.get("units", []) if isinstance(unit, dict)]
    if len(unit_ids) != len(set(unit_ids)):
        errors.append(f"{location}: duplicate unit IDs")
    for unit_id in unit_ids:
        if not re.fullmatch(r"(?:starter|unit)-\d{2}", str(unit_id)):
            errors.append(f"{location}: invalid unit ID {unit_id!r}")
    for index, unit in enumerate(data.get("units", [])):
        pointer = f"{location}:units[{index}]"
        for page_range in unit.get("source_page_ranges", []):
            if page_range.get("source_id") not in source_ids:
                errors.append(f"{pointer}: unknown source_id {page_range.get('source_id')!r}")
            pages = page_range.get("pages")
            if not isinstance(pages, list) or any(not isinstance(page, int) or page < 0 for page in pages):
                errors.append(f"{pointer}: pages must be non-negative integers")
    unit_set = set(unit_ids)
    item_ids: list[str] = []
    for index, item in enumerate(data.get("items", [])):
        pointer = f"{location}:items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{pointer}: item is not an object")
            continue
        item_id = item.get("id")
        item_ids.append(str(item_id))
        if not isinstance(item_id, str) or not ITEM_ID_PATTERN.fullmatch(item_id):
            errors.append(f"{pointer}: item id must match ^[a-z0-9][a-z0-9-]*$: {item_id!r}")
        required_item = {"id", "unit_id", "domain", "label", "level", "evidence"}
        missing_item = sorted(required_item - set(item))
        if missing_item:
            errors.append(f"{pointer}: missing {missing_item}")
        if item.get("unit_id") not in unit_set:
            errors.append(f"{pointer}: unit reference is invalid: {item.get('unit_id')!r}")
        if item.get("level") not in {"A", "B", "C", "D"}:
            errors.append(f"{pointer}: invalid level {item.get('level')!r}")
        if not isinstance(item.get("evidence"), list) or not item.get("evidence"):
            errors.append(f"{pointer}: evidence must be non-empty")
        for evidence_index, entry in enumerate(item.get("evidence", [])):
            evidence_pointer = f"{pointer}.evidence[{evidence_index}]"
            if entry.get("source_id") not in source_ids:
                errors.append(f"{evidence_pointer}: unknown source_id {entry.get('source_id')!r}")
            pages = entry.get("pages")
            if not isinstance(pages, list) or any(not isinstance(page, int) or page < 0 for page in pages):
                errors.append(f"{evidence_pointer}: pages must be non-negative integers")
        mapping = item.get("official_mapping")
        if mapping is not None and mapping.get("status") == "mapped" and not mapping.get("topic_ids"):
            errors.append(f"{pointer}: mapped official_mapping needs topic_ids")
    relation_ids: set[str] = set()
    relation_signatures: set[str] = set()
    for relation_group in ("progressions", "cross_unit_links"):
        for index, relation in enumerate(data.get(relation_group, [])):
            pointer = f"{location}:{relation_group}[{index}]"
            relation_id = relation.get("id")
            if not isinstance(relation_id, str) or not RELATION_ID_PATTERN.fullmatch(relation_id):
                errors.append(f"{pointer}: relation id must match ^[a-z0-9][a-z0-9-]*$: {relation_id!r}")
            elif relation_id in relation_ids:
                errors.append(f"{pointer}: duplicate relation id {relation_id!r}")
            else:
                relation_ids.add(relation_id)
            signature = json.dumps({key: value for key, value in relation.items() if key != "id"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if signature in relation_signatures:
                errors.append(f"{pointer}: duplicate relation object across progression/link arrays")
            relation_signatures.add(signature)
            if relation.get("from_unit_id") not in unit_set or relation.get("to_unit_id") not in unit_set:
                errors.append(f"{pointer}: invalid unit reference")
            for item_id in relation.get("item_ids", []):
                if item_id not in item_ids:
                    errors.append(f"{pointer}: invalid item reference {item_id!r}")
    for index, boundary in enumerate(data.get("assessment_boundaries", [])):
        pointer = f"{location}:assessment_boundaries[{index}]"
        if set(boundary) & FORBIDDEN_ASSESSMENT_KEYS:
            errors.append(f"{pointer}: sample-paper structure leaked into assessment boundary")
        if any(unit_id not in unit_set for unit_id in boundary.get("covered_unit_ids", [])):
            errors.append(f"{pointer}: invalid covered unit")
    for index, boundary in enumerate(data.get("generation_boundaries", [])):
        if boundary.get("unit_id") not in unit_set:
            errors.append(f"{location}:generation_boundaries[{index}]: invalid unit")
    return errors, {"book_id": book_id, "item_ids": item_ids, "unit_ids": unit_ids}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-released", action="store_true", help="also require a released catalog and a selected LICENSE")
    args = parser.parse_args()
    errors: list[str] = []
    warnings: list[str] = []
    catalog_path = REFERENCES / "catalog.json"
    manifest_path = REFERENCES / "source-manifest.json"
    if not catalog_path.exists() or not manifest_path.exists():
        print(json.dumps({"status": "FAIL", "errors": ["catalog.json and source-manifest.json are required"]}, ensure_ascii=False, indent=2))
        return 1
    catalog = load(catalog_path)
    manifest = load(manifest_path)
    errors.extend(collect_global_violations(catalog, "catalog.json"))
    errors.extend(collect_global_violations(manifest, "source-manifest.json"))
    source_ids = {source.get("source_id") for source in manifest.get("sources", [])}
    supported = catalog.get("supported_books", [])
    candidates = catalog.get("candidate_books", [])
    if not isinstance(supported, list) or not isinstance(candidates, list):
        errors.append("catalog supported_books and candidate_books must be arrays")
        supported = []
        candidates = []
    if args.require_released:
        if catalog.get("status") != "released" or not supported:
            errors.append("catalog is not released")
        if not (SKILL_ROOT.parent.parent / "LICENSE").exists():
            errors.append("LICENSE is not selected")
    else:
        if catalog.get("status") != "released":
            warnings.append("candidate catalog accepted for structural validation; public release remains blocked")
    listed = supported + candidates
    if not listed:
        errors.append("catalog has no book entries")
    all_item_ids: set[str] = set()
    book_reports: list[dict[str, Any]] = []
    seen_books: set[str] = set()
    for entry in listed:
        book_id = entry.get("book_id")
        if book_id in seen_books:
            errors.append(f"catalog repeats book {book_id}")
        seen_books.add(book_id)
        if str(book_id).startswith("grade-09"):
            errors.append("grade 9 must not be listed in this package")
        data_file = entry.get("data_file")
        try:
            path = resolve_catalog_data_file(data_file)
        except ValueError as exc:
            errors.append(f"catalog data_file {data_file!r}: {exc}")
            continue
        try:
            data = load(path)
        except Exception as exc:
            errors.append(f"{data_file}: invalid JSON: {exc}")
            continue
        errors.extend(collect_global_violations(data, data_file))
        book_errors, report = validate_book(data, data_file, source_ids)
        errors.extend(book_errors)
        if report.get("book_id") != book_id:
            errors.append(f"{data_file}: book_id does not match catalog")
        for item_id in report.get("item_ids", []):
            if item_id in all_item_ids:
                errors.append(f"duplicate canonical item ID: {item_id}")
            all_item_ids.add(item_id)
        book_reports.append({"book_id": report.get("book_id"), "items": len(report.get("item_ids", [])), "units": len(report.get("unit_ids", [])), "status": entry.get("status")})
    status = "RELEASE_VALIDATION_PASS" if not errors and args.require_released else "CANDIDATE_VALIDATION_PASS" if not errors else "FAIL"
    output = {"status": status, "errors": errors, "warnings": warnings, "books": book_reports, "unique_item_count": len(all_item_ids)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
