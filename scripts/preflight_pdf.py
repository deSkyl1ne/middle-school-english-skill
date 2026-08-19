#!/usr/bin/env python3
"""Run the hard PDF preflight gate over a rendered bundle.

The gate is intentionally evidence based.  A valid header, a parseable JSON
file, or an empty manifest is never sufficient: every bound input/output is
read, every PDF is parsed with PyMuPDF, and the semantic IR is checked against
the extracted student/teacher text.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import fitz
except ImportError as exc:  # pragma: no cover - dependency gate
    print(json.dumps({"status": "PRINT_PREFLIGHT_FAIL", "error_code": "PRINT_RUNTIME_DEPENDENCY", "message": str(exc)}))
    raise SystemExit(3)


ROOT = Path(__file__).resolve().parents[1]
PAGE_W = 595.276
PAGE_H = 841.890
# A final page is only eligible for the compatibility tail treatment when
# observed content spans most of the printable region.  A truthy text block
# alone is not evidence that a sparse tail is intentional.
MIN_SUBSTANTIAL_TAIL_SPAN_RATIO = 0.75
PAGINATION_FIT_TOLERANCE_PT = 2.0


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_bytes(data: bytes, *, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: {exc}") from exc


def issue(code: str, message: str, **fields: Any) -> dict[str, Any]:
    value = {"code": code, "message": message}
    value.update({key: field for key, field in fields.items() if field is not None})
    return value


def bound_path(bundle: Path, value: Any, errors: list[dict[str, Any]], *, label: str) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(issue("INPUT_MISSING", f"{label} path is missing", path=label))
        return None
    try:
        raw = Path(value)
        if raw.is_absolute() or ".." in raw.parts or "\x00" in value:
            raise ValueError("BUNDLE_PATH_ESCAPE")
        current = bundle
        for part in raw.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("BUNDLE_PATH_ESCAPE")
        resolved = (bundle / raw).resolve(strict=True)
        resolved.relative_to(bundle.resolve(strict=True))
        if not resolved.is_file():
            raise OSError("not a regular bundle file")
    except FileNotFoundError:
        errors.append(issue("INPUT_MISSING", f"{label} path is missing from the bundle", path=value))
        return None
    except (OSError, RuntimeError, ValueError) as exc:
        code = "INPUT_INVALID"
        message = f"{label} path is not a safe regular bundle file: {exc}"
        errors.append(issue(code, message, path=value))
        return None
    return resolved


def bound_snapshot(
    bundle: Path,
    value: Any,
    errors: list[dict[str, Any]],
    *,
    label: str,
) -> tuple[Path | None, bytes | None]:
    """Resolve and read one bundle file for the current validation run."""
    path = bound_path(bundle, value, errors, label=label)
    if path is None or not isinstance(value, str):
        return path, None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        errors.append(issue("INPUT_MISSING", f"{label} path is missing from the bundle", path=value))
        return None, None
    except OSError as exc:
        errors.append(issue("INPUT_INVALID", f"{label} path could not be read: {exc}", path=value))
        return None, None
    return path, data


def validate_schema(name: str, document: Any, errors: list[dict[str, Any]], *, path: str) -> None:
    try:
        from jsonschema import Draft202012Validator
        schema = load(ROOT / "schema" / name)
        found = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.absolute_path))
        for error in found:
            errors.append(issue("SCHEMA_INVALID", error.message, path=f"{path}{error.json_path}"))
    except ImportError:
        errors.append(issue("SCHEMA_RUNTIME_DEPENDENCY", "jsonschema Draft 2020-12 is required", path=name))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(issue("SCHEMA_INVALID", str(exc), path=name))


def normalise(value: str) -> str:
    value = re.sub(r"\s+", " ", value.replace("\u00ad", "").replace("\n", " ")).strip().casefold()
    # PDF text extraction may insert a break-space between adjacent Han
    # characters when a mixed-script line wraps. It is not source content.
    value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value)
    return value


def horizontal_line_intersects(expected: Any, line: Any) -> bool:
    """Return true for a horizontal PDF drawing inside a response bbox.

    PyMuPDF reports a ReportLab line as a zero-height rectangle.  ``Rect``
    intersection is intentionally not used here because some versions treat a
    zero-area rectangle as non-intersecting even when its point lies inside.
    """
    if expected is None or line is None:
        return False
    y = float(line.y0)
    return (
        float(line.width) >= max(20.0, float(expected.width) * 0.5)
        and abs(float(line.height)) <= 2.0
        and float(line.x1) > float(expected.x0)
        and float(line.x0) < float(expected.x1)
        and float(expected.y0) - 1.5 <= y <= float(expected.y1) + 1.5
    )


def response_geometry_evidence(
    response: Mapping[str, Any],
    expected: Any,
    entries: list[tuple[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Require contract-sized geometry backed by the parsed PDF.

    The vertical size is derived from the response contract.  A one-line
    answer cannot legitimately claim the entire page as its bbox, and a
    multi-line area must contain the declared number of actual line drawings.
    Inline areas must have text in their small declared region.
    """
    contract = response.get("response_contract") or {}
    policy = contract.get("line_policy")
    expected_lines = int(response.get("actual_line_count", 0))
    height = float(expected.height)
    if policy in {"one-line", "multi-line"}:
        max_height = max(32.0, expected_lines * 16.0 + 16.0)
        lines = [
            rect for kind, rect in entries
            if kind == "drawing" and horizontal_line_intersects(expected, rect)
        ]
        return height <= max_height and len(lines) >= expected_lines, {
            "expected_line_count": expected_lines,
            "actual_line_geometry_count": len(lines),
            "max_contract_height_pt": max_height,
            "actual_height_pt": height,
        }
    if policy == "inline":
        nearby = fitz.Rect(expected.x0 - 4, expected.y0 - 4, expected.x1 + 4, expected.y1 + 4)
        text_rects = [rect for kind, rect in entries if kind == "text" and nearby.intersects(rect)]
        # Inline matching prompts may wrap, but they still cannot claim most
        # of a page as response geometry.
        return height <= 120.0 and bool(text_rects), {
            "nearby_text_geometry_count": len(text_rects),
            "max_contract_height_pt": 120.0,
            "actual_height_pt": height,
        }
    return False, {"line_policy": policy, "actual_height_pt": height}


def pdf_text(document: Any) -> str:
    return "\n".join(page.get_text("text") for page in document)


def _manifest_rect(bbox: Any) -> fitz.Rect | None:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return fitz.Rect(x0, PAGE_H - y1, x1, PAGE_H - y0)


def _same_layout_flow(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if str(left.get("layout_region", "body")) != str(right.get("layout_region", "body")):
        return False
    left_column = str(left.get("layout_column", "full"))
    right_column = str(right.get("layout_column", "full"))
    return left_column == "full" or right_column == "full" or left_column == right_column


def _semantic_geometry_records(
    manifest: Mapping[str, Any],
    actual_block_rects: Mapping[tuple[str, str], Any],
    document: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw_blocks = manifest.get("blocks") if isinstance(manifest.get("blocks"), list) else []
    counts: dict[str, int] = {}
    for block in raw_blocks:
        if isinstance(block, Mapping) and block.get("document") == document:
            counts[str(block.get("block_id"))] = counts.get(str(block.get("block_id")), 0) + 1
    for manifest_index, block in enumerate(raw_blocks):
        if not isinstance(block, Mapping) or block.get("document") != document:
            continue
        role = str(block.get("role", ""))
        if role in {"response_area", "footer", "declared_page_reserve"}:
            continue
        block_id = str(block.get("block_id", ""))
        rect = actual_block_rects.get((document, block_id))
        try:
            page = int(block.get("page", 0))
        except (TypeError, ValueError):
            continue
        if not block_id or rect is None or page < 1 or counts.get(block_id) != 1:
            # A repeated block id has no reliable one-to-one actual rectangle;
            # it must not become a pagination claim.
            continue
        records.append({"block": block, "block_id": block_id, "page": page, "rect": rect, "manifest_index": manifest_index})
    return records


def check_cross_page_pagination(
    manifest: Mapping[str, Any],
    actual_geometry: Mapping[str, Mapping[int, list[tuple[str, Any]]]],
    actual_block_rects: Mapping[tuple[str, str], Any],
    profile: Mapping[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    """Find a complete first block that demonstrably fits the previous page.

    This deliberately uses measured semantic rectangles.  A manifest bbox can
    identify a block and its intended region, but it cannot create occupied
    geometry or waive the page's remaining space.
    """
    max_empty = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
    margins = profile.get("page", {}).get("margins_pt", {})
    region_top = float(margins.get("top", 0))
    region_bottom = PAGE_H - float(margins.get("bottom", 0))
    usable_height = max(1.0, region_bottom - region_top)
    response_rects: dict[tuple[str, int], list[fitz.Rect]] = {}
    response_records = manifest.get("response_areas") if isinstance(manifest.get("response_areas"), list) else []
    for response in response_records:
        if not isinstance(response, Mapping):
            continue
        document = str(response.get("document", ""))
        try:
            page = int(response.get("page", 0))
        except (TypeError, ValueError):
            continue
        expected = _manifest_rect(response.get("bbox_pt"))
        candidates = actual_geometry.get(document, {}).get(page)
        if expected is None or candidates is None:
            continue
        evidence_ok, _evidence = response_geometry_evidence(response, expected, candidates)
        if evidence_ok:
            response_rects.setdefault((document, page), []).append(expected)

    matching_layouts = {
        (str(diagnostic.get("document")), str(diagnostic.get("item_id"))): str(diagnostic.get("selected_layout"))
        for diagnostic in manifest.get("matching", []) or []
        if isinstance(diagnostic, Mapping)
    }

    for document in ("student", "teacher"):
        records = _semantic_geometry_records(manifest, actual_block_rects, document)
        if not records:
            continue
        by_page: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            by_page.setdefault(record["page"], []).append(record)

        def complete_group(candidate: dict[str, Any]) -> list[fitz.Rect] | None:
            block = candidate["block"]
            role = str(block.get("role", ""))
            region = str(block.get("layout_region", "body"))
            if role == "content" and region == "matching":
                # The matching container is a measurement envelope, not a
                # movable row.  Its option/prompt children are the boundaries.
                return None
            group = [candidate["rect"]]
            if role == "heading":
                following = [
                    record for record in records
                    if record["manifest_index"] > candidate["manifest_index"]
                    and record["page"] == candidate["page"]
                    and record["block"].get("source_item_id") == block.get("source_item_id")
                ]
                following.sort(key=lambda record: (float(record["rect"].y0), record["manifest_index"]))
                if not following:
                    return None
                group.append(following[0]["rect"])
                writing_responses = [
                    response for response in response_records
                    if isinstance(response, Mapping)
                    and response.get("document") == document
                    and response.get("source_item_id") == block.get("source_item_id")
                    and not response.get("source_task_id")
                    and not response.get("source_prompt_id")
                ]
                if writing_responses:
                    # The writing response is a multi-line semantic unit. Its
                    # page-local prompt/response binding is checked below;
                    # this generic heading fit check cannot safely reduce it
                    # to the heading and first text block alone.
                    return None
            if role in {"task", "prompt"}:
                source_key = "source_task_id" if role == "task" else "source_prompt_id"
                source_id = block.get(source_key)
                bound = [
                    response for response in response_records
                    if isinstance(response, Mapping)
                    and response.get("document") == document
                    and int(response.get("page", 0)) == candidate["page"]
                    and response.get("source_item_id") == block.get("source_item_id")
                    and response.get(source_key) == source_id
                ]
                if not bound:
                    return None
                group.extend(response_rects.get((document, candidate["page"]), []))
            if role == "option" and region == "matching" and matching_layouts.get((document, str(block.get("source_item_id")))) == "card-grid":
                # card-grid keeps a pair of options as one row.  Include a
                # same-row sibling when the first block is such a row.
                for sibling in by_page.get(candidate["page"], []):
                    if sibling is candidate or sibling["block"].get("role") != "option":
                        continue
                    if sibling["block"].get("source_item_id") != block.get("source_item_id"):
                        continue
                    if abs(float(sibling["rect"].y0) - float(candidate["rect"].y0)) <= PAGINATION_FIT_TOLERANCE_PT:
                        group.append(sibling["rect"])
            return group

        page_count = max(max(by_page, default=0), max(actual_geometry.get(document, {}), default=0))
        for page in range(2, page_count + 1):
            observed_entries = actual_geometry.get(document, {}).get(page - 1, [])
            observed_rects = [
                rect for _kind, rect in observed_entries
                if rect is not None and float(rect.y1) > region_top and float(rect.y0) < region_bottom
            ]
            if observed_rects:
                actual_tail = max(0.0, region_bottom - max(float(rect.y1) for rect in observed_rects))
                if actual_tail / usable_height <= max_empty:
                    # The semantic ledger may end above a response line or a
                    # non-text drawing. The parsed page geometry is the source
                    # of truth for whether the previous page has an avoidable
                    # tail; page-level whitespace is still checked separately.
                    continue
            next_records = sorted(by_page.get(page, []), key=lambda record: (float(record["rect"].y0), record["manifest_index"]))
            if not next_records:
                continue
            candidate = next_records[0]
            group = complete_group(candidate)
            if not group:
                continue
            candidate_block = candidate["block"]
            prior_rects = [
                record["rect"]
                for record in by_page.get(page - 1, [])
                if _same_layout_flow(candidate_block, record["block"])
            ]
            # A verified response line is real occupied geometry even though
            # it is intentionally absent from actual_block_rects.
            prior_rects.extend(response_rects.get((document, page - 1), []))
            if not prior_rects:
                continue
            remaining = region_bottom - max(float(rect.y1) for rect in prior_rects)
            group_height = max(float(rect.y1) for rect in group) - min(float(rect.y0) for rect in group)
            remaining_ratio = max(0.0, remaining) / usable_height
            if remaining_ratio <= max_empty or remaining + PAGINATION_FIT_TOLERANCE_PT < group_height:
                continue
            candidate_id = candidate["block_id"]
            errors.append(issue(
                "LAYOUT_UNUSED_FIT",
                f"first complete block {candidate_id} on page {page} fits the remaining space on page {page - 1}",
                page=page - 1,
                ratio=remaining_ratio,
                block_id=candidate_id,
                details={
                    "document": document,
                    "region": candidate_block.get("layout_region", "body"),
                    "column": candidate_block.get("layout_column", "full"),
                    "remaining_pt": round(max(0.0, remaining), 3),
                    "complete_block_height_pt": round(group_height, 3),
                    "complete_block_ids": [candidate_id],
                    "actual_block_rect_pt": [float(candidate["rect"].x0), float(candidate["rect"].y0), float(candidate["rect"].x1), float(candidate["rect"].y1)],
                },
            ))


def check_obvious_orphans(
    manifest: Mapping[str, Any],
    actual_block_rects: Mapping[tuple[str, str], Any],
    profile: Mapping[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    """Report only page relationships that the manifest can prove."""
    margins = profile.get("page", {}).get("margins_pt", {})
    usable_height = max(1.0, PAGE_H - float(margins.get("top", 0)) - float(margins.get("bottom", 0)))
    blocks = manifest.get("blocks") if isinstance(manifest.get("blocks"), list) else []
    for document in ("student", "teacher"):
        document_blocks = [block for block in blocks if isinstance(block, Mapping) and block.get("document") == document]
        by_item: dict[str, list[Mapping[str, Any]]] = {}
        for block in document_blocks:
            by_item.setdefault(str(block.get("source_item_id", "")), []).append(block)
        for item_id, item_blocks in by_item.items():
            visible = [
                block for block in item_blocks
                if block.get("role") not in {"heading", "response_area", "footer", "declared_page_reserve"}
            ]
            page_item_blocks: dict[int, list[Mapping[str, Any]]] = {}
            for block in item_blocks:
                if block.get("role") in {"response_area", "footer", "declared_page_reserve"}:
                    continue
                page_item_blocks.setdefault(int(block.get("page", 0)), []).append(block)
            for block in visible:
                role = str(block.get("role", ""))
                if role not in {"option", "prompt", "content", "instruction", "passage", "stem"}:
                    continue
                region = str(block.get("layout_region", "body"))
                if region == "matching" and role in {"option", "prompt"}:
                    # Matching rows/flows have their own candidate diagnostics;
                    # a single child is not enough to prove an orphan here.
                    continue
                page = int(block.get("page", 0))
                same_page = page_item_blocks.get(page, [])
                rect = actual_block_rects.get((document, str(block.get("block_id"))))
                short = rect is not None and float(rect.height) <= usable_height * 0.25
                if len(same_page) != 1 or not short:
                    continue
                if role == "prompt":
                    orphan_type = "prompt"
                elif role == "option":
                    orphan_type = "option"
                else:
                    orphan_type = "paragraph"
                errors.append(issue(
                    "LAYOUT_ORPHAN",
                    f"{orphan_type} block {block.get('block_id')} is the only short content block for {item_id} on page {page}",
                    page=page or None,
                    block_id=str(block.get("block_id")),
                    item_id=item_id,
                    details={
                        "document": document,
                        "orphan_type": orphan_type,
                        "region": region,
                        "column": block.get("layout_column", "full"),
                        "block_height_pt": round(float(rect.height), 3) if rect is not None else None,
                    },
                ))


def page_geometry(page: Any) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    """Return observed page geometry and the text subset.

    The manifest is produced in ReportLab's bottom-left coordinate system;
    these rectangles are the post-render PyMuPDF geometry in top-left
    coordinates. Image rectangles come from ``get_image_rects`` because one
    xref can be placed more than once.
    """
    entries: list[tuple[str, Any]] = []
    text_entries: list[tuple[str, Any]] = []
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if str(span.get("text", "")).strip()]
            if not spans:
                continue
            rect = fitz.Rect(*line.get("bbox", (0, 0, 0, 0)))
            entries.append(("text", rect))
            text_entries.append(("".join(str(span.get("text", "")) for span in spans), rect))
    for xref, *_rest in page.get_images(full=True):
        try:
            for rect in page.get_image_rects(xref):
                entries.append(("image", fitz.Rect(rect)))
        except Exception:
            continue
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None and rect.width > 0 and rect.height >= 0:
            entries.append(("drawing", fitz.Rect(rect)))
    return entries, text_entries


def used_fonts(page: Any) -> list[dict[str, Any]]:
    names = {str(span.get("font")) for block in page.get_text("dict").get("blocks", []) if block.get("type") == 0 for line in block.get("lines", []) for span in line.get("spans", []) if span.get("text", "").strip()}
    result: list[dict[str, Any]] = []
    for record in page.get_fonts(full=True):
        resource_name = str(record[3]) if len(record) >= 4 else ""
        candidates = {resource_name, resource_name.split("+", 1)[-1]}
        if any(name in candidates for name in names):
            result.append({"xref": record[0], "type": record[2], "name": record[3], "embedded": record[1] != "n/a"})
    return result


def inspect_pdf(
    path: Path,
    document: str,
    manifest: Mapping[str, Any],
    profile: Mapping[str, Any],
    errors: list[dict[str, Any]],
    *,
    data: bytes | None = None,
) -> tuple[dict[str, Any], str]:
    record: dict[str, Any] = {"document": document, "path": path.name, "page_count": 0, "pages": []}
    if data is None:
        try:
            if path.is_symlink():
                raise OSError("PDF path is a symlink")
            data = path.read_bytes()
        except FileNotFoundError:
            errors.append(issue("PDF_MISSING", f"missing {document}.pdf", path=path.name))
            return record, ""
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(issue("PDF_PARSE_INVALID", f"could not read {document}.pdf safely: {exc}", path=path.name))
            return record, ""
    if not data:
        errors.append(issue("PDF_PARSE_INVALID", f"{document}.pdf is empty", path=path.name))
        return record, ""
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        errors.append(issue("PDF_PARSE_INVALID", f"PyMuPDF failed to open {document}.pdf: {exc}", path=path.name))
        return record, ""
    try:
        if getattr(pdf, "is_encrypted", False):
            errors.append(issue("PDF_PARSE_INVALID", "encrypted PDF is forbidden", path=path.name))
        if getattr(pdf, "is_repaired", False):
            errors.append(issue("PDF_PARSE_INVALID", "PyMuPDF opened a repaired PDF", path=path.name))
        if pdf.page_count < 1:
            errors.append(issue("PDF_PARSE_INVALID", "PDF has zero pages", path=path.name))
        record["page_count"] = pdf.page_count
        text_parts: list[str] = []
        max_empty = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
        for index, page in enumerate(pdf, 1):
            rect = page.rect
            if abs(rect.width - PAGE_W) > 1.5 or abs(rect.height - PAGE_H) > 1.5:
                errors.append(issue("PDF_PAGE_GEOMETRY_INVALID", "page is not A4 portrait", path=path.name, page=index))
            text = page.get_text("text")
            text_parts.append(text)
            drawings = page.get_drawings()
            images = page.get_images(full=True)
            if not text.strip() and not drawings and not images:
                errors.append(issue("PDF_PARSE_INVALID", "blank or unreadable page", path=path.name, page=index))
            blocks = [block for block in page.get_text("blocks") if len(block) >= 5 and str(block[4]).strip()]
            # Whitespace is a layout-region gate, not a crude ink-area ratio.
            # A short line has little ink area but can still be correctly
            # placed.  Conversely, a page with content stranded at the top
            # must fail even when its few glyphs are wide.  Measure the
            # largest continuous vertical gap in the usable A4 region and
            # retain the value as the page's auditable empty ratio.
            margin = profile.get("page", {}).get("margins_pt", {})
            region_top = float(margin.get("top", 0))
            region_bottom = float(rect.height) - float(margin.get("bottom", 0))
            intervals = [
                (max(region_top, float(block[1])), min(region_bottom, float(block[3])))
                for block in blocks
                if float(block[3]) > region_top and float(block[1]) < region_bottom
            ]
            # Text blocks alone miss image-only stimulus pages and leave a
            # forged semantic/reserve bbox able to mask a real vertical hole.
            # Count every observed placement of every image xref.
            for xref, *_rest in images:
                try:
                    image_rects = page.get_image_rects(xref)
                except Exception:
                    image_rects = []
                for image_rect in image_rects:
                    if image_rect.y1 > region_top and image_rect.y0 < region_bottom:
                        intervals.append((max(region_top, float(image_rect.y0)), min(region_bottom, float(image_rect.y1))))
            # Response areas are intentionally drawn lines rather than text.
            # Only geometry observed in the parsed PDF may contribute to the
            # occupied intervals.  The manifest is an audit claim, not a
            # waiver: a forged giant bbox must not hide a blank page.
            response_areas = manifest.get("response_areas") if isinstance(manifest.get("response_areas"), list) else []
            verified_writing_response = False
            for response in response_areas:
                if not isinstance(response, Mapping):
                    continue
                if response.get("document") != document or int(response.get("page", 0)) != index:
                    continue
                bbox = response.get("bbox_pt")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                response_top = float(rect.height) - float(bbox[3])
                response_bottom = float(rect.height) - float(bbox[1])
                expected_response = fitz.Rect(float(bbox[0]), response_top, float(bbox[2]), response_bottom)
                contract = response.get("response_contract") or {}
                policy = contract.get("line_policy")
                line_count = int(response.get("actual_line_count", 0))
                evidence_ok, _evidence = response_geometry_evidence(
                    response,
                    expected_response,
                    [("drawing", drawing.get("rect")) for drawing in drawings if drawing.get("rect") is not None]
                    + [("text", fitz.Rect(*block[:4])) for block in blocks],
                )
                if evidence_ok and response_bottom > region_top and response_top < region_bottom:
                    intervals.append((max(region_top, response_top), min(region_bottom, response_bottom)))
                    # A practical-writing response intentionally reserves
                    # substantial lower-page space. Its line geometry is
                    # audited above and below, so the remaining tail is not
                    # the same defect as a sparse task-reading continuation.
                    if (
                        contract.get("response_kind") == "paragraph"
                        and not response.get("source_task_id")
                        and not response.get("source_prompt_id")
                    ):
                        verified_writing_response = True
            intervals.sort()
            merged: list[list[float]] = []
            for start, end in intervals:
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            gaps = [region_bottom - region_top] if not merged else [merged[0][0] - region_top] + [merged[index][0] - merged[index - 1][1] for index in range(1, len(merged))] + [region_bottom - merged[-1][1]]
            empty_ratio = max(0.0, min(1.0, max(gaps, default=region_bottom - region_top) / max(1.0, region_bottom - region_top)))
            # A final page may legitimately contain the closing item(s) and a
            # natural tail.  It is not a waiver for blank/intermediate pages:
            # real page geometry must exist, and forged reserve/response claims
            # are checked independently below.  Keep the reported metric at
            # the unchanged 15% gate for compatibility with the print report.
            tail_span = 0.0
            if intervals:
                tail_span = max(end for _start, end in intervals) - min(start for start, _end in intervals)
            tail_span_ratio = tail_span / max(1.0, region_bottom - region_top)
            natural_tail = (
                index == pdf.page_count
                and bool(blocks)
                and bool(text.strip())
                and tail_span_ratio >= MIN_SUBSTANTIAL_TAIL_SPAN_RATIO
            )
            natural_tail = natural_tail or (verified_writing_response and index == pdf.page_count)
            reported_empty_ratio = min(empty_ratio, max_empty) if natural_tail else empty_ratio
            page_record: dict[str, Any] = {"page": index, "width_pt": float(rect.width), "height_pt": float(rect.height), "empty_ratio": reported_empty_ratio}
            record["pages"].append(page_record)
            # A source fixture may intentionally be a compact smoke paper.  It
            # is still invalid when the declared semantic geometry proves a
            # large avoidable hole; the physical page ratio remains a hard
            # signal for documents with enough content to occupy the page.
            if empty_ratio > max_empty and not natural_tail:
                errors.append(issue("LAYOUT_EXCESSIVE_WHITESPACE", f"non-response page empty ratio {empty_ratio:.4f} exceeds {max_empty:.4f}", path=path.name, page=index, ratio=empty_ratio))
            page_fonts = used_fonts(page)
            if not page_fonts and text.strip():
                errors.append(issue("FONT_NOT_EMBEDDED", "text spans have no resolvable PDF font resource", path=path.name, page=index))
            for font in page_fonts:
                if not font["embedded"]:
                    errors.append(issue("FONT_NOT_EMBEDDED", f"used font {font['name']} is not embedded", path=path.name, page=index))
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size = float(span.get("size", 0))
                        value = str(span.get("text", ""))
                        if value.strip() and size < 10.5:
                            errors.append(issue("TYPOGRAPHY_TOO_SMALL", f"rendered text is {size:.2f}pt below 10.5pt", path=path.name, page=index, ratio=size, details={"text": value}))
        return record, "\n".join(text_parts)
    except Exception as exc:
        errors.append(issue("PDF_PARSE_INVALID", f"page read failed: {exc}", path=path.name))
        return record, ""
    finally:
        pdf.close()


def source_content(assessment: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for item in assessment.get("items", []):
        if not isinstance(item, Mapping):
            continue
        for key in ("passage", "instruction", "context", "stem", "prompt", "script_outline"):
            if item.get(key):
                values.append(str(item[key]))
        for key in ("options", "prompts", "tasks"):
            for value in item.get(key, []) or []:
                if isinstance(value, Mapping):
                    for field in ("option_id", "prompt_id", "task_id", "text", "prompt"):
                        if value.get(field):
                            values.append(str(value[field]))
                    if isinstance(value.get("options"), list):
                        for option in value["options"]:
                            if isinstance(option, Mapping):
                                for field in ("option_id", "text"):
                                    if option.get(field):
                                        values.append(str(option[field]))
        for key in ("word_bank", "target_skills"):
            values.extend(str(value) for value in item.get(key, []) or [] if value)
        for key in ("speaker_roles", "task_sequence", "blanks", "rubric"):
            for value in item.get(key, []) or []:
                if isinstance(value, Mapping):
                    values.extend(str(value[field]) for field in value if field in {"role", "purpose", "step", "task_kind", "item_count", "score", "blank_id", "criterion", "descriptor", "points"} and value.get(field) is not None)
        for asset in item.get("stimulus_assets", []) or []:
            if isinstance(asset, Mapping) and asset.get("caption"):
                values.append(str(asset["caption"]))
    return values


def student_source_content(assessment: Mapping[str, Any]) -> list[str]:
    """Collect content expected in the cleaned student projection."""
    from compile_render_ir import compile_views, option_text, prompt_text

    student_ir, _teacher_ir = compile_views(assessment)
    values: list[str] = []
    for item in student_ir.get("items", []):
        if not isinstance(item, Mapping):
            continue
        for block in item.get("blocks", []) or []:
            if isinstance(block, Mapping) and block.get("text"):
                values.append(str(block["text"]))
    for item in assessment.get("items", []):
        if not isinstance(item, Mapping) or item.get("item_type") != "reading_matching":
            continue
        for prompt in item.get("prompts", []) or []:
            if isinstance(prompt, Mapping):
                value = prompt_text(prompt, "student")
                if value:
                    values.append(value)
        for option in item.get("options", []) or []:
            if isinstance(option, Mapping):
                value = option_text(option, "student")
                if value:
                    values.append(value)
    return values


def validate_semantics(
    bundle: Path,
    assessment: Mapping[str, Any],
    manifest: Mapping[str, Any],
    profile: Mapping[str, Any],
    errors: list[dict[str, Any]],
    student_text: str,
    teacher_text: str,
    snapshots: Mapping[str, bytes],
    pdf_snapshots: Mapping[str, bytes],
) -> None:
    if not isinstance(assessment.get("items"), list):
        errors.append(issue("SCHEMA_INVALID", "assessment items must be an array", path="assessment.json:items"))
        return
    if any(not isinstance(item, Mapping) for item in assessment.get("items", [])):
        errors.append(issue("SCHEMA_INVALID", "assessment items must be objects", path="assessment.json:items"))
        return
    output_records = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
    def output_document(name: str) -> tuple[Path | None, bytes | None]:
        record = output_records.get(name) if isinstance(output_records, Mapping) else None
        value = record.get("path") if isinstance(record, Mapping) else None
        if not isinstance(value, str):
            return None, None
        return bundle / value, snapshots.get(value)

    student_ir_path, student_ir_bytes = output_document("student_ir")
    teacher_ir_path, teacher_ir_bytes = output_document("teacher_ir")
    answer_path, answer_bytes = output_document("answer_sheet")
    if student_ir_bytes is None or teacher_ir_bytes is None or answer_bytes is None:
        return
    try:
        student_ir = load_bytes(student_ir_bytes, label="student-ir.json")
        teacher_ir = load_bytes(teacher_ir_bytes, label="teacher-ir.json")
        answer_sheet = load_bytes(answer_bytes, label="answer-sheet.json")
    except ValueError as exc:
        errors.append(issue("IR_INVALID", str(exc)))
        return
    validate_schema("render-ir.schema.json", student_ir, errors, path=str(student_ir_path or "student-ir.json"))
    validate_schema("render-ir.schema.json", teacher_ir, errors, path=str(teacher_ir_path or "teacher-ir.json"))
    validate_schema("answer-sheet.schema.json", answer_sheet, errors, path="answer-sheet.json")
    if not isinstance(student_ir, Mapping) or not isinstance(teacher_ir, Mapping) or not isinstance(answer_sheet, Mapping):
        errors.append(issue("IR_INVALID", "student IR, teacher IR, and answer sheet must be JSON objects"))
        return
    if any(not isinstance(document.get("items"), list) for document in (student_ir, teacher_ir, answer_sheet)):
        errors.append(issue("IR_INVALID", "student IR, teacher IR, and answer sheet items must be arrays"))
        return
    if any(
        any(not isinstance(item, Mapping) for item in document.get("items", []))
        for document in (student_ir, teacher_ir, answer_sheet)
    ):
        errors.append(issue("IR_INVALID", "student IR, teacher IR, and answer sheet items must be objects"))
        return
    for field in ("blocks", "response_areas", "pages", "matching", "assets"):
        values = manifest.get(field)
        if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
            errors.append(issue("SCHEMA_INVALID", f"render manifest {field} must be an array", path=f"render-manifest.json:{field}"))
            return
    if [item.get("item_id") for item in student_ir.get("items", [])] != [item.get("item_id") for item in teacher_ir.get("items", [])]:
        errors.append(issue("TEACHER_CONTENT_MISMATCH", "student and teacher IR do not share one item order"))
    expected_items = list(assessment.get("items", []))
    student_items = student_ir.get("items", [])
    teacher_items = teacher_ir.get("items", [])
    answer_items = answer_sheet.get("items", [])
    expected_ids = [item.get("item_id") for item in expected_items]
    if [item.get("item_id") for item in student_items] != expected_ids:
        errors.append(issue("STUDENT_CONTENT_MISMATCH", "student IR item order or coverage differs from assessment"))
    if [item.get("item_id") for item in teacher_items] != expected_ids:
        errors.append(issue("TEACHER_CONTENT_MISMATCH", "teacher IR item order or coverage differs from assessment"))
    if [item.get("item_id") for item in answer_items] != expected_ids:
        errors.append(issue("ANSWER_SHEET_MISMATCH", "answer sheet item order or coverage differs from assessment"))
    for expected, row in zip(expected_items, answer_items):
        if expected.get("score") != row.get("score"):
            errors.append(issue("ANSWER_SHEET_MISMATCH", f"answer sheet score differs for {expected.get('item_id')}"))
        if expected.get("answer") != row.get("answer"):
            errors.append(issue("ANSWER_SHEET_MISMATCH", f"answer sheet typed answer differs for {expected.get('item_id')}"))
    student_norm = normalise(student_text)
    teacher_norm = normalise(teacher_text)
    for value in source_content(assessment):
        normalized = normalise(value)
        if normalized and normalized not in teacher_norm:
            errors.append(issue("TEACHER_CONTENT_MISMATCH", f"source content is absent from teacher PDF: {value[:80]}"))
    for value in student_source_content(assessment):
        normalized = normalise(value)
        if normalized and normalized not in student_norm:
            errors.append(issue("STUDENT_CONTENT_MISMATCH", f"student projection content is absent from student PDF: {value[:80]}"))
    for item in expected_items:
        item_id = str(item.get("item_id", ""))
        if item_id and item_id.casefold() in student_norm:
            errors.append(issue("STUDENT_ANSWER_LEAK", f"student PDF contains internal item ID {item_id}"))
        answer_text = normalise(json.dumps(item.get("answer"), ensure_ascii=False, sort_keys=True))
        answer_values: list[str] = []
        def collect_answer(value: Any) -> None:
            if isinstance(value, Mapping):
                for child in value.values():
                    collect_answer(child)
            elif isinstance(value, list):
                for child in value:
                    collect_answer(child)
            elif value is not None:
                answer_values.append(normalise(str(value)))
        collect_answer(item.get("answer"))
        if answer_text and answer_values and not any(value and value in teacher_norm for value in answer_values):
            errors.append(issue("TEACHER_CONTENT_MISMATCH", f"teacher PDF answer is absent or differs for {item_id}", item_id=item_id))
        rationale = normalise(str(item.get("rationale", "")))
        if rationale and rationale not in teacher_norm:
            errors.append(issue("TEACHER_CONTENT_MISMATCH", f"teacher PDF rationale is absent or differs for {item_id}", item_id=item_id))
    if re.search(r"(?:^|\n)\s*(?:Answer|Rationale)\s*:", student_text, flags=re.I) or re.search(r"\b(?:canonical_item_ids|teacher_answer_binding|validation_report)\b", student_text, flags=re.I):
        errors.append(issue("STUDENT_ANSWER_LEAK", "student PDF contains answer/rationale/validation metadata"))

    # Cross-check every semantic bbox against the actual PDF geometry.
    # ReportLab uses a bottom-left origin while PyMuPDF
    # reports text/image rectangles from the top-left origin, so convert the
    # ledger rectangle before checking overlap.
    actual_geometry: dict[str, dict[int, list[tuple[str, Any]]]] = {"student": {}, "teacher": {}}
    actual_block_rects: dict[tuple[str, str], fitz.Rect] = {}
    max_empty = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
    for document, raw_pdf in pdf_snapshots.items():
        try:
            parsed = fitz.open(stream=raw_pdf, filetype="pdf")
            for page_number, page in enumerate(parsed, 1):
                entries, _text_entries = page_geometry(page)
                actual_geometry[document][page_number] = entries
            parsed.close()
        except Exception as exc:
            errors.append(issue("PDF_PARSE_INVALID", f"could not inspect actual PDF geometry for {document}: {exc}"))
    for document in ("student", "teacher"):
        for block in [value for value in manifest.get("blocks", []) or [] if value.get("document") == document]:
            role = str(block.get("role", ""))
            bbox = block.get("bbox_pt")
            page_number = int(block.get("page", 0))
            if role == "declared_page_reserve":
                if isinstance(bbox, list) and len(bbox) == 4 and page_number in actual_geometry.get(document, {}):
                    reserve_height = max(0.0, float(bbox[3]) - float(bbox[1]))
                    usable_height = max(1.0, PAGE_H - float(profile.get("page", {}).get("margins_pt", {}).get("top", 0)) - float(profile.get("page", {}).get("margins_pt", {}).get("bottom", 0)))
                    reserve_ratio = reserve_height / usable_height
                    if reserve_ratio > float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15)):
                        errors.append(issue(
                            "LAYOUT_UNUSED_FIT",
                            f"declared reserve {block.get('block_id')} claims {reserve_ratio:.4f} unused fit",
                            page=page_number,
                            ratio=reserve_ratio,
                            block_id=block.get("block_id"),
                            details={"document": document, "bbox_pt": bbox, "region": block.get("layout_region", "body"), "column": block.get("layout_column", "full")},
                        ))
                continue
            if role in {"response_area", "footer"}:
                continue
            if not isinstance(bbox, list) or len(bbox) != 4 or page_number not in actual_geometry.get(document, {}):
                continue
            expected_rect = fitz.Rect(float(bbox[0]), PAGE_H - float(bbox[3]), float(bbox[2]), PAGE_H - float(bbox[1]))
            candidates = actual_geometry[document][page_number]
            sibling_boxes = []
            for sibling in manifest.get("blocks", []) or []:
                if sibling is block or sibling.get("document") != document or int(sibling.get("page", 0)) != page_number:
                    continue
                if sibling.get("role") in {"response_area", "footer", "declared_page_reserve", "content"}:
                    continue
                sibling_bbox = sibling.get("bbox_pt")
                if isinstance(sibling_bbox, list) and len(sibling_bbox) == 4:
                    sibling_boxes.append(fitz.Rect(float(sibling_bbox[0]), PAGE_H - float(sibling_bbox[3]), float(sibling_bbox[2]), PAGE_H - float(sibling_bbox[1])))
            sibling_overlap_count = sum(expected_rect.intersects(sibling_box) for sibling_box in sibling_boxes)
            if sibling_overlap_count >= 2 and expected_rect.height > 120.0 and str(block.get("layout_region", "body")) != "matching":
                errors.append(issue(
                    "LAYOUT_UNUSED_FIT",
                    f"semantic block {block.get('block_id')} overlaps {sibling_overlap_count} sibling blocks",
                    block_id=block.get("block_id"),
                    page=page_number,
                    ratio=max_empty + 1e-6,
                    details={"document": document, "bbox_pt": bbox, "sibling_overlap_count": sibling_overlap_count, "region": block.get("layout_region", "body"), "column": block.get("layout_column", "full")},
                ))
            if role == "asset":
                backed = any(kind == "image" and expected_rect.intersects(rect) for kind, rect in candidates)
            else:
                observed = []
                for kind, rect in candidates:
                    if kind not in {"text", "drawing", "image"} or not expected_rect.intersects(rect):
                        continue
                    center_y = (float(rect.y0) + float(rect.y1)) / 2.0
                    if expected_rect.y0 - 6.0 <= center_y <= expected_rect.y1 + 6.0:
                        observed.append(rect)
                if observed:
                    envelope = fitz.Rect(
                        min(rect.x0 for rect in observed),
                        min(rect.y0 for rect in observed),
                        max(rect.x1 for rect in observed),
                        max(rect.y1 for rect in observed),
                    )
                    # Paragraph declarations may include horizontal slack,
                    # but their vertical envelope must remain contract-sized.
                    tolerance = max(12.0, min(24.0, envelope.height * 0.5))
                    unused_height = max(0.0, expected_rect.height - envelope.height - tolerance)
                    unused_ratio = unused_height / max(expected_rect.height, 1.0)
                    boundary_ok = expected_rect.height <= envelope.height + tolerance
                    backed = boundary_ok
                    actual_block_rects[(document, str(block.get("block_id")))] = envelope
                    if unused_ratio > float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15)):
                        errors.append(issue(
                            "LAYOUT_UNUSED_FIT",
                            f"semantic block {block.get('block_id')} claims {unused_ratio:.4f} unused vertical fit",
                            block_id=block.get("block_id"),
                            page=page_number,
                            ratio=unused_ratio,
                            details={"document": document, "bbox_pt": bbox, "region": block.get("layout_region", "body"), "column": block.get("layout_column", "full"), "actual_envelope_pt": [envelope.x0, envelope.y0, envelope.x1, envelope.y1]},
                        ))
                else:
                    backed = False
            if not backed:
                errors.append(issue("LAYOUT_GEOMETRY_MISMATCH", f"semantic block {block.get('block_id')} has no overlapping PDF geometry", block_id=block.get("block_id"), page=page_number, details={"document": document, "bbox_pt": bbox}))
    check_cross_page_pagination(manifest, actual_geometry, actual_block_rects, profile, errors)
    check_obvious_orphans(manifest, actual_block_rects, profile, errors)
    # Written response contracts must be backed by the declared number of
    # actual line drawings; inline contracts must have nearby text geometry.
    for response in manifest.get("response_areas", []) or []:
        if not isinstance(response, Mapping):
            continue
        document = str(response.get("document", ""))
        page_number = int(response.get("page", 0))
        bbox = response.get("bbox_pt")
        if document not in actual_geometry or page_number not in actual_geometry[document] or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        expected = fitz.Rect(float(bbox[0]), PAGE_H - float(bbox[3]), float(bbox[2]), PAGE_H - float(bbox[1]))
        ok, evidence = response_geometry_evidence(response, expected, actual_geometry[document][page_number])
        if not ok:
            errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "response contract is not backed by actual PDF geometry", response_id=response.get("response_id"), page=page_number, details={"document": document, **evidence}))
    for block in manifest.get("blocks", []) or []:
        if block.get("role") not in {"footer", "declared_page_reserve", "response_area"} and not block.get("bbox_pt"):
            errors.append(issue("LAYOUT_OVERFLOW", f"semantic block {block.get('block_id')} has no geometry", block_id=block.get("block_id")))
        bbox = block.get("bbox_pt")
        if isinstance(bbox, list) and len(bbox) == 4:
            x0, y0, x1, y1 = [float(value) for value in bbox]
            if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0 or x1 > PAGE_W + 1 or y1 > PAGE_H + 1:
                errors.append(issue("LAYOUT_OVERFLOW", f"semantic block {block.get('block_id')} has invalid page geometry", block_id=block.get("block_id"), details={"bbox_pt": bbox}))
            box = block.get("box")
            if isinstance(box, Mapping):
                box_bbox = box.get("box_bbox_pt")
                content_bbox = box.get("content_bbox_pt")
                padding = box.get("padding_pt") or {}
                if not isinstance(box_bbox, list) or not isinstance(content_bbox, list) or len(box_bbox) != 4 or len(content_bbox) != 4:
                    errors.append(issue("BOX_GEOMETRY_INVALID", f"box geometry is incomplete for {block.get('block_id')}"))
                else:
                    bx0, by0, bx1, by1 = [float(value) for value in box_bbox]
                    cx0, cy0, cx1, cy1 = [float(value) for value in content_bbox]
                    if not (bx0 <= cx0 <= cx1 <= bx1 and by0 <= cy0 <= cy1 <= by1):
                        errors.append(issue("BOX_GEOMETRY_INVALID", f"content escapes box for {block.get('block_id')}"))
                    minimum = float(profile.get("box_geometry", {}).get("min_padding_pt", 8))
                    if any(float(padding.get(side, 0)) < minimum for side in ("top", "right", "bottom", "left")):
                        errors.append(issue("BOX_GEOMETRY_INVALID", f"box padding is below {minimum}pt for {block.get('block_id')}"))
                    tolerance = float(profile.get("box_geometry", {}).get("center_delta_tolerance_pt", profile.get("hard_gates", {}).get("box_center_tolerance_pt", 2)))
                    if float(box.get("horizontal_center_delta_pt", tolerance + 1)) > tolerance or float(box.get("vertical_center_delta_pt", tolerance + 1)) > tolerance:
                        errors.append(issue("BOX_OFF_CENTER", f"box content is off-center for {block.get('block_id')}"))
                    if float(box.get("font_size_pt", 0)) < 10.5:
                        errors.append(issue("TYPOGRAPHY_TOO_SMALL", f"box font is below 10.5pt for {block.get('block_id')}"))
    for document in ("student", "teacher"):
        document_blocks = [block for block in manifest.get("blocks", []) or [] if block.get("document") == document]
        manifest_block_ids = {str(block.get("block_id")) for block in document_blocks}
        ir = student_ir if document == "student" else teacher_ir
        for ir_item in ir.get("items", []) or []:
            if not isinstance(ir_item, Mapping):
                continue
            for ir_block in ir_item.get("blocks", []) or []:
                if not isinstance(ir_block, Mapping):
                    continue
                role = ir_block.get("role")
                if role == "response_area":
                    continue
                if role == "asset":
                    asset_id = str((ir_block.get("asset") or {}).get("asset_id", ""))
                    if not any(str(block.get("asset_id", "")) == asset_id and block.get("document") == document for block in manifest.get("blocks", []) or []):
                        errors.append(issue("MANIFEST_BLOCK_MISSING", f"asset block {asset_id} is missing from {document} manifest"))
                    continue
                if role == "content" and ir_block.get("kind") == "MatchingGrid":
                    matching_id = f"{ir_item.get('item_id')}-matching-grid"
                    if not any(block_id == matching_id or block_id.startswith(matching_id + "-") for block_id in manifest_block_ids):
                        errors.append(issue("MANIFEST_BLOCK_MISSING", f"matching grid {matching_id} is missing from {document} manifest"))
                    continue
                if ir_block.get("text") and str(ir_block.get("block_id")) not in manifest_block_ids:
                    errors.append(issue("MANIFEST_BLOCK_MISSING", f"IR block {ir_block.get('block_id')} is missing from {document} manifest"))
        page_keys = {int(page.get("page")) for page in manifest.get("pages", []) or [] if page.get("document") == document}
        by_item: dict[str, list[Mapping[str, Any]]] = {}
        for block in document_blocks:
            by_item.setdefault(str(block.get("source_item_id", "")), []).append(block)
        for item_id, blocks in by_item.items():
            ordered = sorted(blocks, key=lambda value: (int(value.get("page", 0)), -float((value.get("bbox_pt") or [0, 0, 0, 0])[1])))
            headings = [value for value in blocks if value.get("role") == "heading"]
            visible = [value for value in ordered if value.get("role") not in {"heading", "response_area", "footer", "declared_page_reserve"}]
            if headings and visible and int(headings[0].get("page", 0)) != int(visible[0].get("page", 0)):
                errors.append(issue("LAYOUT_ORPHAN", f"heading is separated from its first content block in {document}", block_id=headings[0].get("block_id"), page=int(headings[0].get("page", 0)) or None, item_id=item_id, details={"document": document, "orphan_type": "heading"}))
            stems = [value for value in blocks if value.get("role") in {"content", "instruction", "stem", "passage", "prompt"}]
            options = [value for value in blocks if value.get("role") == "option"]
            if options and stems and min(int(value.get("page", 0)) for value in options) < min(int(value.get("page", 0)) for value in stems):
                errors.append(issue("CONTENT_ORDER_MISMATCH", f"option precedes its stem in {document}", item_id=item_id))
            for value in blocks:
                page = int(value.get("page", 0))
                if page not in page_keys:
                    errors.append(issue("LAYOUT_OVERFLOW", f"semantic block page is not declared in {document}", block_id=value.get("block_id"), page=page))
        response_ids = [record.get("response_id") for record in manifest.get("response_areas", []) or [] if record.get("document") == document]
        if len(response_ids) != len(set(response_ids)):
            errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"response IDs are duplicated in {document}"))
        for response in [record for record in manifest.get("response_areas", []) or [] if record.get("document") == document]:
            bbox = response.get("bbox_pt")
            if not isinstance(bbox, list) or len(bbox) != 4 or float(bbox[2]) <= float(bbox[0]) or float(bbox[3]) <= float(bbox[1]):
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"response area has invalid geometry in {document}", response_id=response.get("response_id")))
            contract = response.get("response_contract") or {}
            actual = int(response.get("actual_line_count", 0))
            contract_line_count = contract.get("line_count")
            if contract_line_count is not None and actual != int(contract_line_count):
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "actual response line count differs from its answer contract", response_id=response.get("response_id"), details={"actual_line_count": actual, "contract_line_count": int(contract_line_count)}))
            policy = contract.get("line_policy")
            if policy == "inline" and actual != 0:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "inline response area has non-zero line count", response_id=response.get("response_id")))
            if policy in {"one-line", "multi-line"} and actual < 1:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "written response area has no line", response_id=response.get("response_id")))
            if int(response.get("page", 0)) not in page_keys:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "response area page is not declared", response_id=response.get("response_id")))
            prompt_id = response.get("source_prompt_id")
            if prompt_id:
                prompts = [block for block in document_blocks if block.get("source_item_id") == response.get("source_item_id") and block.get("role") == "prompt" and block.get("source_prompt_id") == prompt_id]
                if not prompts or any(int(prompt.get("page", 0)) != int(response.get("page", 0)) for prompt in prompts):
                    errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "matching response is detached from its prompt", response_id=response.get("response_id")))
        matching_item_ids = {str(item.get("item_id")): item for item in expected_items if item.get("item_type") == "reading_matching"}
        for item_id, item in matching_item_ids.items():
            prompt_ids = {str(prompt.get("prompt_id")) for prompt in item.get("prompts", []) if isinstance(prompt, Mapping)}
            response_ids_for_item = {
                str(record.get("source_prompt_id")) for record in manifest.get("response_areas", []) or []
                if record.get("document") == document and record.get("source_item_id") == item_id
            }
            if response_ids_for_item != prompt_ids:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"matching response IDs do not cover prompts in {document}", item_id=item_id))
        for item in expected_items:
            item_id = str(item.get("item_id", ""))
            item_type = str(item.get("item_type", ""))
            item_responses = [record for record in manifest.get("response_areas", []) or [] if record.get("document") == document and record.get("source_item_id") == item_id]
            if item_type in {"single_choice", "reading_multiple_choice"} and item_responses:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"choice item has an extra response line in {document}", item_id=item_id))
            if item_type == "vocabulary_in_context" and item.get("options") and item_responses:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"choice vocabulary item has an extra response line in {document}", item_id=item_id))
            if item_type == "task_based_reading":
                expected_tasks = {str(task.get("task_id")) for task in item.get("tasks", []) if isinstance(task, Mapping)}
                actual_tasks = {str(record.get("source_task_id")) for record in item_responses}
                if actual_tasks != expected_tasks:
                    errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"task response areas do not match tasks in {document}", item_id=item_id))
                for response in item_responses:
                    task_id = response.get("source_task_id")
                    task_blocks = [block for block in document_blocks if block.get("source_item_id") == item_id and block.get("role") == "task" and block.get("source_task_id") == task_id]
                    if not task_blocks or any(int(task.get("page", 0)) != int(response.get("page", 0)) for task in task_blocks):
                        errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "task response area is detached from its task", response_id=response.get("response_id"), item_id=item_id))
                    else:
                        task = task_blocks[0]
                        task_bbox = task.get("bbox_pt") or []
                        response_bbox = response.get("bbox_pt") or []
                        if len(task_bbox) == 4 and len(response_bbox) == 4:
                            # ReportLab and the manifest use a bottom-left
                            # origin.  A response must follow its own task,
                            # not merely share the same page with it.
                            gap = float(task_bbox[1]) - float(response_bbox[3])
                            if gap < -1.0 or gap > 24.0:
                                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "task response area is not immediately after its task", response_id=response.get("response_id"), item_id=item_id, details={"task_id": task_id, "gap_pt": round(gap, 3)}))
            if item_type == "practical_writing" and len(item_responses) != 1:
                errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"writing item must have one response area in {document}", item_id=item_id))
            if item_type == "practical_writing" and item_responses:
                prompt_blocks = [block for block in document_blocks if block.get("source_item_id") == item_id and block.get("role") == "prompt"]
                if not prompt_blocks or any(int(prompt.get("page", 0)) != int(item_responses[0].get("page", 0)) for prompt in prompt_blocks):
                    errors.append(issue("RESPONSE_LAYOUT_MISMATCH", "writing response area is detached from its prompt", item_id=item_id, response_id=item_responses[0].get("response_id")))
            if item_type == "cloze":
                blank_count = len([blank for blank in item.get("blanks", []) if isinstance(blank, Mapping)])
                if len(item_responses) != blank_count:
                    errors.append(issue("RESPONSE_LAYOUT_MISMATCH", f"cloze response areas do not match blanks in {document}", item_id=item_id))
    for diagnostic in manifest.get("matching", []) or []:
        candidates = [candidate for candidate in diagnostic.get("candidates", []) if isinstance(candidate, Mapping)]
        layouts = [candidate.get("layout") for candidate in candidates]
        if set(layouts) != {"card-grid", "stacked", "dual-independent-flow"}:
            errors.append(issue("MATCHING_LAYOUT_CANDIDATES_INVALID", f"matching item {diagnostic.get('item_id')} lacks three candidates"))
        if diagnostic.get("selected_layout") not in layouts:
            errors.append(issue("MATCHING_LAYOUT_CANDIDATES_INVALID", "selected matching layout is not measured"))
        if len(layouts) != len(set(layouts)) or len(layouts) != 3:
            errors.append(issue("MATCHING_LAYOUT_CANDIDATES_INVALID", "matching layout candidates are not exactly three distinct layouts"))
        metrics = {(candidate.get("page_count"), candidate.get("break_count"), candidate.get("hard_violation_count"), candidate.get("max_non_response_empty_ratio")) for candidate in candidates}
        if len(metrics) < 2:
            errors.append(issue("MATCHING_LAYOUT_CANDIDATES_INVALID", "matching candidates are not materially different measurements", item_id=diagnostic.get("item_id")))
        if diagnostic.get("document") not in {"student", "teacher"}:
            errors.append(issue("MATCHING_LAYOUT_CANDIDATES_INVALID", "matching diagnostic is not bound to a document", item_id=diagnostic.get("item_id")))
        if any(int(candidate.get("isolated_item_count", 0)) > 0 for candidate in candidates):
            errors.append(issue("LAYOUT_ORPHAN", "matching candidate contains an isolated item", item_id=diagnostic.get("item_id")))
        max_empty = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
        selected_layout = diagnostic.get("selected_layout")
        for candidate in candidates:
            candidate_empty = float(candidate.get("max_non_response_empty_ratio", 1.0))
            imbalance = float(candidate.get("column_imbalance_ratio", candidate_empty))
            is_selected = candidate.get("layout") == selected_layout
            if is_selected and (candidate_empty > max_empty or int(candidate.get("hard_violation_count", 0)) > 0):
                errors.append(issue(
                    "LAYOUT_EXCESSIVE_WHITESPACE",
                    f"matching {candidate.get('layout')} candidate empty ratio {candidate_empty:.4f} exceeds {max_empty:.4f}",
                    item_id=diagnostic.get("item_id"),
                    ratio=candidate_empty,
                    details={"layout": candidate.get("layout"), "region": "matching"},
                ))
            if is_selected and imbalance > max_empty:
                errors.append(issue(
                    "LAYOUT_COLUMN_HOLE",
                    f"matching {candidate.get('layout')} columns are imbalanced by {imbalance:.4f}",
                    item_id=diagnostic.get("item_id"),
                    ratio=imbalance,
                    details={"layout": candidate.get("layout"), "region": "matching", "hole_width_ratio": 0.5, "hole_height_ratio": imbalance},
                ))

    # Cross-check the semantic page geometry for real two-dimensional holes.
    # A vertical-gap-only check can miss a blank half-column beside content in
    # the other half.  For each page, partition body blocks by their geometric
    # center, then flag a column whose occupied height is less than the hard
    # gate while its sibling has same-region content.
    max_empty = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
    max_hole_width = float(profile.get("hard_gates", {}).get("max_hole_width_ratio", 0.25))
    max_hole_height = float(profile.get("hard_gates", {}).get("max_hole_height_ratio", 0.25))
    matching_layout_by_document_item = {
        (str(diagnostic.get("document")), str(diagnostic.get("item_id"))): str(diagnostic.get("selected_layout"))
        for diagnostic in manifest.get("matching", []) or []
        if isinstance(diagnostic, Mapping)
    }
    for document in ("student", "teacher"):
        blocks = [block for block in manifest.get("blocks", []) or [] if block.get("document") == document and block.get("role") not in {"response_area", "footer", "declared_page_reserve"}]
        for page in sorted({int(block.get("page", 0)) for block in blocks if block.get("page")}):
            page_blocks: list[dict[str, Any]] = []
            for block in blocks:
                if int(block.get("page", 0)) != page:
                    continue
                manifest_bbox = block.get("bbox_pt")
                measured = actual_block_rects.get((document, str(block.get("block_id"))))
                if not isinstance(manifest_bbox, list) or len(manifest_bbox) != 4 or measured is None:
                    continue
                # Convert the measured PyMuPDF top-left envelope back to the
                # manifest's bottom-left coordinates. Every 2-D calculation
                # below therefore uses rendered geometry, never a claimed box.
                measured_block = dict(block)
                measured_block["bbox_pt"] = [
                    float(measured.x0),
                    PAGE_H - float(measured.y1),
                    float(measured.x1),
                    PAGE_H - float(measured.y0),
                ]
                page_blocks.append(measured_block)
            # Only compare blocks from one semantic region.  Full-width
            # passage/heading blocks are containers, not a second column.
            regions = sorted({str(block.get("layout_region", "body")) for block in page_blocks})
            for region in regions:
                region_blocks = [
                    block for block in page_blocks
                    if str(block.get("layout_region", "body")) == region
                    and block.get("layout_column") in {"left", "right"}
                ]
                if len(region_blocks) < 2:
                    continue
                # Card-grid has a full-width prompt flow after the two-column
                # option cards.  Its prompt blocks are semantically distinct
                # from the option columns and must not be compared as a
                # second column by the generic geometry detector.
                matching_items = {str(block.get("source_item_id")) for block in page_blocks}
                selected_matching_layouts = {matching_layout_by_document_item.get((document, item_id)) for item_id in matching_items}
                if region == "matching" and selected_matching_layouts and selected_matching_layouts != {"dual-independent-flow"}:
                    # card-grid has a two-column option-card subregion but a
                    # deliberately independent full-width prompt flow; its
                    # unequal final row is not a column hole.  stacked has no
                    # columns.  Only the actual dual-independent-flow needs
                    # this geometric cross-check.
                    continue
                if region == "matching" and any(float(block["bbox_pt"][2]) - float(block["bbox_pt"][0]) > PAGE_W * 0.75 for block in page_blocks):
                    region_blocks = [block for block in region_blocks if block.get("role") == "option"]
                    if len(region_blocks) < 2:
                        continue
                left = [block for block in region_blocks if float(block["bbox_pt"][0]) < PAGE_W / 2]
                right = [block for block in region_blocks if float(block["bbox_pt"][2]) > PAGE_W / 2]
                if not left or not right:
                    continue
                union_top = max(float(block["bbox_pt"][3]) for block in region_blocks)
                union_bottom = min(float(block["bbox_pt"][1]) for block in region_blocks)
                region_height = max(union_top - union_bottom, 1.0)
                left_span = max(float(block["bbox_pt"][3]) for block in left) - min(float(block["bbox_pt"][1]) for block in left)
                right_span = max(float(block["bbox_pt"][3]) for block in right) - min(float(block["bbox_pt"][1]) for block in right)
                shorter_gap = max(0.0, region_height - min(left_span, right_span))
                imbalance = shorter_gap / region_height
                if imbalance > max_empty:
                    errors.append(issue("LAYOUT_COLUMN_HOLE", f"{document} page {page} region {region} has a two-column hole {imbalance:.4f}", page=page, ratio=imbalance, details={"document": document, "region": region, "hole_width_ratio": 0.5, "hole_height_ratio": imbalance}))
                for column_name, column in (("left", left), ("right", right)):
                    x0 = min(float(block["bbox_pt"][0]) for block in column)
                    x1 = max(float(block["bbox_pt"][2]) for block in column)
                    column_gap = max(0.0, region_height - (max(float(block["bbox_pt"][3]) for block in column) - min(float(block["bbox_pt"][1]) for block in column)))
                    if x1 - x0 >= max_hole_width * PAGE_W and column_gap / region_height > max_hole_height:
                        errors.append(issue("LAYOUT_2D_HOLE", f"{document} page {page} region {region} {column_name} column contains a two-dimensional hole", page=page, ratio=column_gap / region_height, details={"document": document, "region": region, "column": column_name, "hole_width_ratio": (x1 - x0) / PAGE_W, "hole_height_ratio": column_gap / region_height}))

    declared_assets = {str(item.get("asset_id")): item for item in manifest.get("assets", []) or []}
    for item in assessment.get("items", []) or []:
        for reference in item.get("stimulus_assets", []) or []:
            if not isinstance(reference, Mapping):
                continue
            asset_id = str(reference.get("asset_id", ""))
            if reference.get("required_for_answer") and asset_id not in declared_assets:
                errors.append(issue("ASSET_UNRESOLVED", f"required asset {asset_id} is absent from the manifest"))
    for asset_id, asset in declared_assets.items():
        asset_file = asset.get("file")
        if isinstance(asset_file, str) and asset_file in snapshots:
            path, asset_bytes = bundle / asset_file, snapshots[asset_file]
        else:
            path, asset_bytes = bound_snapshot(bundle, asset_file, errors, label=f"asset:{asset_id}")
            if isinstance(asset_file, str) and asset_bytes is not None:
                snapshots[asset_file] = asset_bytes
        if asset.get("rights_status") not in {"granted", "cc_public_domain", "school_license"}:
            errors.append(issue("GRAPHICS_RIGHTS_INVALID", "asset rights are not permitted", asset_id=asset_id))
        if asset.get("cropped") is not False:
            errors.append(issue("GRAPHICS_CROPPED", "asset is marked cropped", asset_id=asset_id))
        if float(asset.get("measured_dpi", 0)) < float(profile.get("hard_gates", {}).get("min_stimulus_dpi", 300)):
            errors.append(issue("GRAPHICS_LOW_DPI", "asset measured DPI is below profile gate", asset_id=asset_id))
        placement = asset.get("placement_bbox_pt")
        if isinstance(placement, list) and len(placement) == 4:
            display_width_pt = float(placement[2]) - float(placement[0])
            display_height_pt = float(placement[3]) - float(placement[1])
            if display_width_pt > 0 and display_height_pt > 0:
                actual_dpi = min(
                    float(asset.get("pixel_width", 0)) * 72.0 / display_width_pt,
                    float(asset.get("pixel_height", 0)) * 72.0 / display_height_pt,
                )
                if actual_dpi + 1e-6 < float(profile.get("hard_gates", {}).get("min_stimulus_dpi", 300)):
                    errors.append(issue("GRAPHICS_LOW_DPI", "asset actual placement DPI is below profile gate", asset_id=asset_id, details={"actual_dpi": round(actual_dpi, 3)}))
        if float(asset.get("contrast_ratio", 0)) < float(profile.get("illustrations", {}).get("min_contrast_ratio", 1.4)):
            errors.append(issue("GRAPHICS_LOW_CONTRAST", "asset contrast is below profile gate", asset_id=asset_id))
        if int(asset.get("pixel_width", 0)) < 1 or int(asset.get("pixel_height", 0)) < 1:
            errors.append(issue("ASSET_METADATA_INVALID", "asset pixel dimensions are invalid", asset_id=asset_id))
        if asset.get("color_mode") not in {"1", "L", "LA", "RGB", "RGBA", "CMYK"}:
            errors.append(issue("ASSET_METADATA_INVALID", "asset color mode is invalid", asset_id=asset_id))
        linked_items = {
            str(item.get("item_id"))
            for item in assessment.get("items", []) or []
            for reference in item.get("stimulus_assets", []) or []
            if isinstance(reference, Mapping) and str(reference.get("asset_id")) == asset_id
        }
        declared_links = {str(value) for value in asset.get("linked_item_ids", []) or []}
        if not linked_items.issubset(declared_links):
            errors.append(issue("ASSET_BINDING_INVALID", "asset linked_item_ids do not cover assessment references", asset_id=asset_id, details={"missing_item_ids": sorted(linked_items - declared_links)}))
        for document in ("student", "teacher"):
            pdf_bytes = pdf_snapshots.get(document)
            if pdf_bytes is not None:
                try:
                    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
                        xrefs = asset.get("pdf_xrefs") if isinstance(asset.get("pdf_xrefs"), Mapping) else {}
                        expected_xref = xrefs.get(document, asset.get("pdf_xref"))
                        if not any(record[0] == expected_xref for page in pdf for record in page.get_images(full=True)):
                            errors.append(issue("ASSET_NOT_EMBEDDED", f"asset {asset_id} is not embedded in {document}.pdf", asset_id=asset_id, path=f"{document}.pdf"))
                except Exception as exc:
                    errors.append(issue("PDF_PARSE_INVALID", f"could not inspect {document}.pdf asset embedding: {exc}", path=f"{document}.pdf"))
    if re.search(r"!\[[^\]]*\]\(|AssetBlock|ASSET_PLACEHOLDER", student_text + "\n" + teacher_text):
        errors.append(issue("ASSET_PLACEHOLDER", "PDF contains an asset markdown or placeholder literal"))
    font_records = manifest.get("fonts") if isinstance(manifest.get("fonts"), list) else []
    for record in font_records:
        if not isinstance(record, Mapping):
            continue
        if record.get("token") == "body" and record.get("weight") != "regular":
            errors.append(issue("FONT_WEIGHT_INVALID", "body font is not a verified Regular face"))


def manifest_record_bytes(
    manifest: Mapping[str, Any],
    snapshots: Mapping[str, bytes],
    section: str,
    name: str,
) -> bytes | None:
    records = manifest.get(section)
    record = records.get(name) if isinstance(records, Mapping) else None
    path = record.get("path") if isinstance(record, Mapping) else None
    return snapshots.get(path) if isinstance(path, str) else None


def validate_request_contract(
    manifest: Mapping[str, Any],
    snapshots: Mapping[str, bytes],
    errors: list[dict[str, Any]],
) -> None:
    """Validate the request schema and required output selection."""
    request_bytes = manifest_record_bytes(manifest, snapshots, "inputs", "request")
    if request_bytes is None:
        return
    try:
        request = load_bytes(request_bytes, label="render-request.json")
    except ValueError as exc:
        errors.append(issue("SCHEMA_INVALID", str(exc), path="render-request.json"))
        return
    validate_schema("render-request.schema.json", request, errors, path="render-request.json")
    if not isinstance(request, Mapping):
        return
    requested_outputs = request.get("outputs")
    if not isinstance(requested_outputs, list) or set(requested_outputs) != {"student_pdf", "teacher_pdf", "answer_sheet"}:
        errors.append(issue("OUTPUTS_INVALID", "render request must require student PDF, teacher PDF, and answer sheet"))


def validate_asset_manifest_binding(
    manifest: Mapping[str, Any],
    snapshots: Mapping[str, bytes],
    errors: list[dict[str, Any]],
) -> None:
    raw = manifest_record_bytes(manifest, snapshots, "inputs", "asset_manifest")
    if raw is None:
        return
    try:
        document = load_bytes(raw, label="asset-manifest.json")
    except ValueError as exc:
        errors.append(issue("SCHEMA_INVALID", str(exc), path="asset-manifest.json"))
        return
    validate_schema("asset-manifest.schema.json", document, errors, path="asset-manifest.json")
    if not isinstance(document, Mapping):
        return
    source_assets = document.get("assets") if isinstance(document.get("assets"), list) else []
    rendered_assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    declared = {str(item.get("asset_id")): item for item in source_assets if isinstance(item, Mapping)}
    rendered = {str(item.get("asset_id")): item for item in rendered_assets if isinstance(item, Mapping)}
    if set(declared) != set(rendered):
        errors.append(issue("ASSET_BINDING_INVALID", "render manifest assets do not cover the bound asset manifest"))
        return
    for asset_id, source in declared.items():
        output = rendered[asset_id]
        for key in ("file", "semantic_role", "rights_status", "linked_item_ids"):
            if source.get(key) != output.get(key):
                errors.append(issue("ASSET_BINDING_INVALID", f"asset {asset_id} field {key} differs from the bound asset manifest", asset_id=asset_id))


FONT_ROOTS = (
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)


def active_font_roots() -> tuple[Path, ...]:
    if sys.platform == "win32":
        windows_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        return (Path(windows_root) / "Fonts", *FONT_ROOTS)
    return FONT_ROOTS


def validate_font_file_records(manifest: Mapping[str, Any], errors: list[dict[str, Any]]) -> None:
    """Ensure resolved font records point to available installed files."""
    resolved_roots: list[Path] = []
    for root in active_font_roots():
        try:
            if root.is_dir():
                resolved_roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    font_records = manifest.get("fonts") if isinstance(manifest.get("fonts"), list) else []
    for record in font_records:
        if not isinstance(record, Mapping):
            continue
        value = record.get("resolved_file") if isinstance(record, Mapping) else None
        if not isinstance(value, str) or not Path(value).is_absolute():
            errors.append(issue("FONT_RECORD_INVALID", "font record lacks an absolute file path"))
            continue
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in resolved_roots):
                errors.append(issue("FONT_FALLBACK_INVALID", "resolved font is outside the installed font allowlist", path=value))
                continue
        except FileNotFoundError:
            errors.append(issue("FONT_UNRESOLVED", "resolved font file is missing", path=value))
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(issue("FONT_UNRESOLVED", f"resolved font file is unavailable: {exc}", path=value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict real-PDF print preflight")
    parser.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    raw_bundle = Path(args.bundle).expanduser()
    try:
        bundle = raw_bundle.resolve(strict=True)
    except (OSError, RuntimeError):
        print(json.dumps({"status": "PRINT_PREFLIGHT_FAIL", "error_code": "INPUT_MISSING", "message": "bundle is missing"}, ensure_ascii=False))
        return 2
    if not bundle.is_dir():
        print(json.dumps({"status": "PRINT_PREFLIGHT_FAIL", "error_code": "INPUT_INVALID", "message": "bundle is not a directory"}, ensure_ascii=False))
        return 2
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    snapshots: dict[str, bytes] = {}
    manifest_path, manifest_bytes = bound_snapshot(bundle, "render-manifest.json", errors, label="render-manifest")
    try:
        manifest = load_bytes(manifest_bytes, label="render-manifest.json") if manifest_bytes is not None else {}
        if not isinstance(manifest, Mapping):
            errors.append(issue("SCHEMA_INVALID", "render manifest must be an object", path="render-manifest.json"))
            manifest = {}
    except ValueError as exc:
        manifest = {}
        errors.append(issue("SCHEMA_INVALID", str(exc), path="render-manifest.json"))
    validate_schema("render-manifest.schema.json", manifest, errors, path="render-manifest.json")
    input_records = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    profile_records = manifest.get("profiles") if isinstance(manifest.get("profiles"), Mapping) else {}
    for section in ("inputs", "profiles", "outputs"):
        records = manifest.get(section) if isinstance(manifest.get(section), Mapping) else {}
        for name, value in records.items():
            if not isinstance(value, Mapping):
                continue
            path_value = value.get("path")
            path, data = bound_snapshot(bundle, path_value, errors, label=f"{section}.{name}")
            if path is not None and data is not None and isinstance(path_value, str):
                snapshots[path_value] = data
    if isinstance(input_records, Mapping) and isinstance(profile_records, Mapping):
        if input_records.get("base_profile") != profile_records.get("base") or input_records.get("resolved_profile") != profile_records.get("resolved"):
            errors.append(issue("PROFILE_BINDING_INVALID", "input and profile ledger records disagree"))
    validate_request_contract(manifest, snapshots, errors)
    validate_asset_manifest_binding(manifest, snapshots, errors)

    def input_record(name: str) -> tuple[Path | None, bytes | None]:
        records = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
        record = records.get(name) if isinstance(records, Mapping) else None
        value = record.get("path") if isinstance(record, Mapping) else None
        if not isinstance(value, str) or value not in snapshots:
            return None, None
        return bundle / value, snapshots[value]

    assessment_path, assessment_bytes = input_record("assessment")
    report_path, report_bytes = input_record("validation_report")
    resolved_profile_record = profile_records.get("resolved") if isinstance(profile_records, Mapping) else None
    resolved_profile_value = resolved_profile_record.get("path") if isinstance(resolved_profile_record, Mapping) else None
    resolved_profile_path = bundle / resolved_profile_value if isinstance(resolved_profile_value, str) and resolved_profile_value in snapshots else None
    resolved_profile_bytes = snapshots.get(resolved_profile_value) if isinstance(resolved_profile_value, str) else None
    assessment: Mapping[str, Any] = {}
    profile: Mapping[str, Any] = {}
    base_profile_record = input_records.get("base_profile") if isinstance(input_records, Mapping) else None
    base_profile_value = base_profile_record.get("path") if isinstance(base_profile_record, Mapping) else None
    base_profile_bytes = snapshots.get(base_profile_value) if isinstance(base_profile_value, str) else None
    if base_profile_bytes is not None:
        try:
            base_profile = load_bytes(base_profile_bytes, label="base-profile.json")
            validate_schema("render-profile.schema.json", base_profile, errors, path="base-profile.json")
        except ValueError as exc:
            errors.append(issue("PROFILE_BINDING_INVALID", str(exc), path="base-profile.json"))
    if assessment_bytes is not None:
        try:
            loaded_assessment = load_bytes(assessment_bytes, label="assessment.json")
            assessment = loaded_assessment if isinstance(loaded_assessment, Mapping) else {}
            validate_schema("assessment.schema.json", assessment, errors, path="assessment.json")
            if assessment.get("assessment_id") != manifest.get("assessment_id"):
                errors.append(issue("CONTENT_REPORT_BINDING_INVALID", "assessment_id differs from render manifest", path="assessment.json:assessment_id"))
        except ValueError as exc:
            errors.append(issue("SCHEMA_INVALID", str(exc), path="assessment.json"))
    if report_bytes is not None:
        try:
            report = load_bytes(report_bytes, label="content-validation-report.json")
            validate_schema("assessment-validation.schema.json", report, errors, path="content-validation-report.json")
            if not isinstance(report, Mapping):
                errors.append(issue("CONTENT_REPORT_BINDING_INVALID", "content report must be an object"))
            elif report.get("status") != "ASSESSMENT_VALIDATOR_PASS":
                errors.append(issue("CONTENT_REPORT_STATUS_INVALID", "content report is not ASSESSMENT_VALIDATOR_PASS"))
            if isinstance(report, Mapping) and report.get("assessment_id") != manifest.get("assessment_id"):
                errors.append(issue("CONTENT_REPORT_BINDING_INVALID", "content report is not bound to the actual assessment"))
        except ValueError as exc:
            errors.append(issue("CONTENT_REPORT_BINDING_INVALID", str(exc)))
    if resolved_profile_bytes is not None:
        try:
            loaded_profile = load_bytes(resolved_profile_bytes, label="resolved-profile.json")
            profile = loaded_profile if isinstance(loaded_profile, Mapping) else {}
            validate_schema("render-profile.schema.json", profile, errors, path="resolved-profile.json")
        except ValueError as exc:
            errors.append(issue("PROFILE_BINDING_INVALID", str(exc)))
    pdf_records: list[dict[str, Any]] = []
    pdf_snapshots: dict[str, bytes] = {}

    def output_pdf(document: str) -> tuple[Path, bytes | None]:
        outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
        record = outputs.get(f"{document}_pdf") if isinstance(outputs, Mapping) else None
        value = record.get("path") if isinstance(record, Mapping) else None
        if isinstance(value, str) and value in snapshots:
            return bundle / value, snapshots[value]
        return bundle / f"{document}.pdf", None

    for document in ("student", "teacher"):
        path, data = output_pdf(document)
        if data is not None:
            pdf_snapshots[document] = data
        if document == "student":
            student_record, student_text = inspect_pdf(path, document, manifest, profile, errors, data=data)
        else:
            teacher_record, teacher_text = inspect_pdf(path, document, manifest, profile, errors, data=data)
    pdf_records.extend((student_record, teacher_record))
    validate_semantics(bundle, assessment, manifest, profile, errors, student_text, teacher_text, snapshots, pdf_snapshots)
    # The manifest font records must agree with every used PDF font record.
    font_records = manifest.get("fonts") if isinstance(manifest.get("fonts"), list) else []
    declared_fonts = {record.get("resolved_family") for record in font_records if isinstance(record, Mapping)}
    if not declared_fonts:
        errors.append(issue("FONT_NOT_EMBEDDED", "manifest contains no resolved font record"))
    for record in font_records:
        if not isinstance(record, Mapping):
            continue
        if record.get("embedded") is not True:
            errors.append(issue("FONT_NOT_EMBEDDED", "font manifest violates embedding gate"))
        if record.get("fallback_used") is True:
            fallback_families = {str(value).casefold() for value in record.get("fallback_families", []) or []}
            if str(record.get("resolved_family", "")).casefold() not in fallback_families:
                errors.append(issue("FONT_FALLBACK_INVALID", "font manifest selected a fallback family not declared by the profile"))
    validate_font_file_records(manifest, errors)
    # Asset metadata and image placement are checked when the renderer emits
    # an AssetBlock.
    asset_records = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    for asset in asset_records:
        if not isinstance(asset, Mapping):
            continue
        if float(asset.get("measured_dpi", 0)) + 1e-6 < float(profile.get("hard_gates", {}).get("min_stimulus_dpi", 300)):
            errors.append(issue("GRAPHICS_LOW_DPI", "asset placement DPI is below profile gate", details={"asset_id": asset.get("asset_id")}))
    report = {
        "schema_version": "1.0.0",
        "status": "PRINT_PREFLIGHT_PASS" if not errors else "PRINT_PREFLIGHT_FAIL",
        "assessment_id": manifest.get("assessment_id", "unknown-assessment"),
        "errors": errors,
        "warnings": warnings,
        "pdfs": pdf_records,
        "summary": {"errors": len(errors), "warnings": len(warnings), "pdf_count": 2},
    }
    validate_schema("print-validation.schema.json", report, errors, path="print-validation-report.json")
    if errors:
        report["status"] = "PRINT_PREFLIGHT_FAIL"
        report["errors"] = errors
        report["summary"]["errors"] = len(errors)
    try:
        (bundle / "print-validation-report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "PRINT_PREFLIGHT_FAIL", "error_code": "REPORT_WRITE_INVALID", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PRINT_PREFLIGHT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
