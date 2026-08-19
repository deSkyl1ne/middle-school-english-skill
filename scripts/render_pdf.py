#!/usr/bin/env python3
"""Render a bound assessment bundle into deterministic real PDFs.

This renderer deliberately has one source of truth: ``compile_render_ir``.
It records geometry while ReportLab draws each Flowable and embeds a resolved
TrueType/TTC face for the PDF preflight.
"""
from __future__ import annotations

import argparse
import html
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import fitz
    from PIL import Image
    from reportlab import rl_config
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import BaseDocTemplate, Flowable, Frame, FrameBreak, Image as ReportLabImage, PageBreak, PageTemplate, Paragraph, SimpleDocTemplate, Spacer
except ImportError as exc:  # pragma: no cover - dependency gate
    print(json.dumps({"status": "PRINT_BLOCKED", "error_code": "PRINT_RUNTIME_DEPENDENCY", "message": str(exc)}))
    raise SystemExit(3)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from compile_render_ir import student_visible_text

PAGINATION_FIT_TOLERANCE_PT = 2.0
MATCHING_SPLIT_RESERVE_PT = 36.0
MATCHING_TOP_PADDING_PT = 3.0
COMPACT_MARGIN_SCALE = 0.72
COMPACT_MIN_MARGIN_PT = 10.0


def matching_header_height(font_size: float, *, student_view: bool) -> float:
    return MATCHING_TOP_PADDING_PT if student_view else font_size + 6.0


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def matching_option_text(option: Mapping[str, Any], *, student_view: bool = True) -> str:
    value = student_visible_text(option.get("text", "")) if student_view else str(option.get("text", ""))
    return f"{option.get('option_id', '')}. {value}"


def matching_prompt_text(prompt: Mapping[str, Any], index: int, *, student_view: bool = True) -> str:
    if not student_view:
        return f"{prompt.get('prompt_id', '')}. {prompt.get('text', '')} (   )"
    value = student_visible_text(prompt.get("text", ""), preserve_question_number=True)
    if not re.match(r"^\d+[.)]\s", value):
        value = f"{prompt.get('_display_index', index)}. {value}"
    return f"{value} (   )"


def package_version(name: str, fallback: str = "unavailable") -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def safe_child(base: Path, value: str) -> Path:
    """Resolve a request-owned path and reject symlink/path escape."""
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or not value or "\x00" in value:
        raise ValueError("BUNDLE_PATH_ESCAPE")
    current = base
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("BUNDLE_PATH_ESCAPE")
    resolved = (base / raw).resolve(strict=True)
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("BUNDLE_PATH_ESCAPE") from exc
    return resolved


def safe_output_child(base: Path, value: str) -> Path:
    """Resolve a path that may be copied into a new output bundle."""
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or not value or "\x00" in value:
        raise ValueError("BUNDLE_PATH_ESCAPE")
    resolved = (base / raw).resolve()
    resolved.relative_to(base.resolve())
    return resolved


def schema_validate(schema_name: str, document: Any) -> None:
    from jsonschema import Draft202012Validator

    schema = load(ROOT / "schema" / schema_name)
    # Keep local refs compatible with the system jsonschema used for core
    # validation; its older resolver duplicates package-relative root IDs.
    if isinstance(schema, dict) and isinstance(schema.get("$id"), str) and "://" not in schema["$id"]:
        schema = dict(schema)
        schema.pop("$id", None)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise ValueError("SCHEMA_INVALID:" + errors[0].message)


def collect_chars(assessment: Mapping[str, Any]) -> list[str]:
    chars: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            # Line separators are layout controls, not glyphs that the
            # resolved print face must cover.
            chars.extend(char for char in value if char not in "\r\n\t")
        elif isinstance(value, Mapping):
            for key, child in value.items():
                if key not in {"answer", "rationale", "canonical_item_ids", "context_item_ids", "validation"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(assessment.get("items", []))
    return sorted(set(chars))


def system_font_candidates() -> list[Path]:
    roots = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    if sys.platform == "win32":
        windows_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        roots.insert(0, Path(windows_root) / "Fonts")
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in {".ttf", ".ttc", ".otf"})
    return sorted(set(candidates), key=lambda path: str(path))


def preferred_font_candidates() -> list[Path]:
    preferred = [
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    return [path for path in preferred if path.exists()] + [path for path in system_font_candidates() if path not in preferred]


def resolve_runtime_font(profile: Mapping[str, Any], chars: list[str]) -> tuple[str, dict[str, Any]]:
    """Resolve and register the actual face used by ReportLab."""
    sys.path.insert(0, str(SCRIPTS))
    from resolve_render_profile import FontResolutionError, resolve_font_token

    tokens = profile.get("fonts", {}).get("tokens", [])
    if not tokens:
        raise ValueError("FONT_RESOLUTION_INVALID")
    token = tokens[0]
    candidates = preferred_font_candidates()
    if not candidates:
        raise ValueError("FONT_UNRESOLVED")
    record = resolve_font_token(token, candidates, required_chars=chars, embedded=True)
    font_name = "PrintBody"
    try:
        pdfmetrics.registerFont(TTFont(font_name, record["resolved_file"], subfontIndex=record["subfont_index"]))
    except TypeError as exc:
        # ReportLab versions which do not accept ``subfontIndex`` cannot
        # safely render a non-zero face from a TTC.  Falling back to the
        # collection's default face would make the resolved profile and the
        # embedded glyphs disagree, so only the unindexed face may use the
        # compatibility path.
        if int(record.get("subfont_index", 0)) != 0:
            raise ValueError("FONT_TTC_SUBFONT_UNSUPPORTED") from exc
        pdfmetrics.registerFont(TTFont(font_name, record["resolved_file"]))
    except Exception as exc:
        if isinstance(exc, FontResolutionError):
            raise
        raise ValueError("FONT_NOT_EMBEDDED") from exc
    record["runtime_font_name"] = font_name
    return font_name, record


class TrackedParagraph(Paragraph):
    def __init__(
        self,
        text: str | None,
        style: ParagraphStyle,
        *,
        metadata: Mapping[str, Any] | None = None,
        tracker: list[dict[str, Any]] | None = None,
        bulletText: str | None = None,
        frags: Any = None,
        caseSensitive: int = 1,
        encoding: str = "utf8",
    ):
        # Paragraph.split() reconstructs subclasses with the internal
        # ``frags``/``bulletText`` arguments.  Accept that native ReportLab
        # contract so long passages can split at real line boundaries rather
        # than failing at runtime on the first cross-page paragraph.
        value = text
        if frags is None and value is not None:
            value = html.escape(str(value)).replace("\n", "<br/>")
        super().__init__(value, style, bulletText=bulletText, frags=frags, caseSensitive=caseSensitive, encoding=encoding)
        self.metadata = dict(metadata or {})
        self.tracker = tracker if tracker is not None else []
        self._content_height = 0.0

    def _box_padding(self) -> tuple[float, float, float, float]:
        box = self.metadata.get("box")
        if not isinstance(box, Mapping):
            return 0.0, 0.0, 0.0, 0.0
        padding = box.get("padding_pt", {})
        if not isinstance(padding, Mapping):
            padding = {}
        return (
            float(padding.get("left", 6)),
            float(padding.get("right", 6)),
            float(padding.get("top", 4)),
            float(padding.get("bottom", 4)),
        )

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
        width, height = super().wrap(availWidth, availHeight)
        self._content_height = float(height)
        _left, _right, top, bottom = self._box_padding()
        if self.metadata.get("box"):
            self.height = self._content_height + top + bottom
        return width, self.height

    def split(self, availWidth: float, availHeight: float) -> list["TrackedParagraph"]:  # noqa: N802
        chunks = super().split(availWidth, availHeight)
        for chunk in chunks:
            if isinstance(chunk, TrackedParagraph):
                chunk.metadata = dict(self.metadata)
                chunk.tracker = self.tracker
        return chunks

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:  # noqa: N802
        box = self.metadata.get("box")
        if isinstance(box, Mapping):
            left, right, top, bottom = self._box_padding()
            content_height = self._content_height or max(0.0, self.height - top - bottom)
            content_y = float(y) + bottom
            bbox = [float(x), content_y, float(x + self.width), content_y + content_height]
            self.tracker.append({**self.metadata, "page": canvas.getPageNumber(), "bbox_pt": bbox})
            canvas.saveState()
            canvas.setStrokeColor(colors.black)
            canvas.setLineWidth(0.6)
            canvas.rect(float(x) - left, float(y), self.width + left + right, self.height, stroke=1, fill=0)
            canvas.restoreState()
            total_height = self.height
            self.height = content_height
            try:
                super().drawOn(canvas, x, content_y, _sW)
            finally:
                self.height = total_height
            return
        bbox = [float(x), float(y), float(x + self.width), float(y + self.height)]
        self.tracker.append({**self.metadata, "page": canvas.getPageNumber(), "bbox_pt": bbox})
        super().drawOn(canvas, x, y, _sW)


class ResponseFlowable(Flowable):
    def __init__(self, response: Mapping[str, Any], *, tracker: list[dict[str, Any]], item_id: str, font_size: float, line_height: float = 16.0):
        super().__init__()
        self.response = dict(response)
        self.tracker = tracker
        self.item_id = item_id
        self.font_size = font_size
        self.width = 0
        line_count = int(response.get("line_count", 0))
        self.line_height = float(line_height)
        self.height = max(8 * mm, 12.0 + max(0, line_count - 1) * self.line_height)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
        self.width = availWidth
        return availWidth, self.height

    def draw(self) -> None:
        canvas = self.canv
        x = float(getattr(self, "_draw_x", 0))
        y = float(getattr(self, "_draw_y", 0))
        line_count = int(self.response.get("line_count", 0))
        policy = self.response.get("line_policy")
        if policy == "inline":
            canvas.setFont("PrintBody", self.font_size)
            canvas.drawString(0, max(0, self.height - 11), "(   )")
        else:
            for index in range(line_count):
                baseline = self.height - 12 - index * self.line_height
                canvas.setStrokeColor(colors.black)
                canvas.line(0, baseline, self.width, baseline)
        self.tracker.append({
            "block_id": f"{self.item_id}-response-{self.response.get('response_id', 'response').replace('/', '-')}",
            "role": "response_area",
            "source_item_id": self.item_id,
            "source_task_id": self.response.get("source_task_id"),
            "source_prompt_id": self.response.get("source_prompt_id"),
            "page": canvas.getPageNumber(),
            "bbox_pt": [x, y, x + self.width, y + self.height],
            "layout_region": "response_area",
            "response": self.response,
        })

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:  # noqa: N802
        self._draw_x, self._draw_y = x, y
        super().drawOn(canvas, x, y, _sW)


class MatchingFlowable(Flowable):
    def __init__(self, item: Mapping[str, Any], font_name: str, font_size: float, *, tracker: list[dict[str, Any]], diagnostic: dict[str, Any], responses: list[Mapping[str, Any]], chunk_index: int = 1, student_view: bool = True, streaming_layout: bool = False):
        super().__init__()
        self.item = item
        self.font_name = font_name
        self.font_size = font_size
        self.tracker = tracker
        self.diagnostic = diagnostic
        self.responses = [dict(value) for value in responses]
        self.chunk_index = int(chunk_index)
        self.student_view = student_view
        self.streaming_layout = streaming_layout
        self.width = 0
        self.height = 0
        self._full_frame_height: float | None = None
        self.x = 0
        self.y = 0

    def _paragraph(self, value: str, style: ParagraphStyle, width: float) -> tuple[Paragraph, float]:
        paragraph = Paragraph(value, style)
        _, height = paragraph.wrap(max(1.0, width), 100000.0)
        return paragraph, height

    def _layout_metrics(self, width: float) -> tuple[list[float], list[float], float]:
        style = ParagraphStyle("matching-measure", fontName=self.font_name, fontSize=self.font_size, leading=self.font_size * 1.2, spaceAfter=0)
        options = [o for o in self.item.get("options", []) if isinstance(o, Mapping)]
        prompts = [p for p in self.item.get("prompts", []) if isinstance(p, Mapping)]
        selected = self.diagnostic["selected_layout"]
        half_width = width / 2.0 - 8.0
        option_width = half_width if selected in {"card-grid", "dual-independent-flow"} else width
        prompt_width = width if selected == "card-grid" else option_width
        option_heights = [self._paragraph(html.escape(matching_option_text(o, student_view=self.student_view)), style, option_width)[1] for o in options]
        prompt_heights = [self._paragraph(html.escape(matching_prompt_text(p, index + 1, student_view=self.student_view)), style, prompt_width)[1] for index, p in enumerate(prompts)]
        if selected == "card-grid":
            # FR-12 card-grid is an option card grid followed by an
            # independent prompt flow.  It is intentionally not a row-wise
            # option/prompt table, so a 5x7 item does not manufacture a
            # two-row blank column at the bottom of the page.
            option_rows = [max(option_heights[index:index + 2], default=0.0) for index in range(0, len(option_heights), 2)]
            card_gap = 3.5
            content_height = sum(height + card_gap for height in option_rows)
            if option_rows and prompt_heights:
                content_height += card_gap
            content_height += sum(height + card_gap for height in prompt_heights)
        elif selected == "stacked":
            content_height = sum(option_heights) + sum(prompt_heights) + 3.0 * max(0, len(options) + len(prompts) - 1) + 3.0
        else:
            content_height = max(sum(option_heights) + 3.0 * max(0, len(options) - 1), sum(prompt_heights) + 3.0 * max(0, len(prompts) - 1))
        return option_heights, prompt_heights, content_height + matching_header_height(self.font_size, student_view=self.student_view)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
        self.width = availWidth
        if self._full_frame_height is None and availHeight < 10000.0:
            self._full_frame_height = availHeight
        self._option_heights, self._prompt_heights, self.height = self._layout_metrics(availWidth)
        return availWidth, self.height

    def _units(self) -> list[tuple[str, int]]:
        options = [o for o in self.item.get("options", []) if isinstance(o, Mapping)]
        prompts = [p for p in self.item.get("prompts", []) if isinstance(p, Mapping)]
        selected = self.diagnostic["selected_layout"]
        if selected == "stacked":
            return [("option", index) for index in range(len(options))] + [("prompt", index) for index in range(len(prompts))]
        if selected == "card-grid":
            return [("option_row", index) for index in range(0, len(options), 2)] + [("prompt", index) for index in range(len(prompts))]
        return [("row", index) for index in range(max(len(options), len(prompts)))]

    def _flow_for_units(self, units: list[tuple[str, int]], chunk_index: int, avail_width: float) -> "MatchingFlowable":
        options = [o for o in self.item.get("options", []) if isinstance(o, Mapping)]
        prompts = [p for p in self.item.get("prompts", []) if isinstance(p, Mapping)]
        option_indices: list[int] = []
        for kind, index in units:
            if kind == "option_row":
                option_indices.extend(value for value in (index, index + 1) if value < len(options))
            elif kind in {"option", "row"} and index < len(options):
                option_indices.append(index)
        prompt_indices = [index for kind, index in units if kind in {"prompt", "row"} and index < len(prompts)]
        subset = dict(self.item)
        subset["options"] = [options[index] for index in option_indices]
        subset["prompts"] = [prompts[index] for index in prompt_indices]
        flow = MatchingFlowable(
            subset,
            self.font_name,
            self.font_size,
            tracker=self.tracker,
            diagnostic=self.diagnostic,
            responses=self.responses,
            chunk_index=chunk_index,
            student_view=self.student_view,
            streaming_layout=self.streaming_layout,
        )
        flow.wrap(avail_width, 100000.0)
        flow._full_frame_height = self._full_frame_height
        return flow

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:  # noqa: N802
        """Split only between complete matching rows on the real frame.

        ReportLab calls ``split`` with the *remaining* height after the
        passage/instructions on the current page.  Measuring only against a
        full page would move the whole grid to the next page and could leave
        an avoidable blank region.  The returned chunks retain the same
        semantic tracker; each chunk contains complete option rows or prompt
        rows only.
        """
        if self.height <= availHeight:
            return []
        units = self._units()
        full_height = float(self._full_frame_height or availHeight)
        # Choose a real semantic boundary that balances the current-frame
        # remainder and the next full page.  Greedily filling the first frame
        # can leave the continuation with a large tail hole; choosing the
        # feasible boundary with the smallest worst normalized gap avoids
        # that while preserving complete option/prompt rows.
        candidates: list[tuple[float, float, int, MatchingFlowable, MatchingFlowable]] = []
        for split_at in range(1, len(units)):
            first = self._flow_for_units(units[:split_at], self.chunk_index, availWidth)
            remainder = self._flow_for_units(units[split_at:], self.chunk_index + 1, availWidth)
            if first.height > availHeight or remainder.height > full_height:
                continue
            first_gap = max(0.0, (availHeight - first.height) / max(availHeight, 1.0))
            remainder_gap = max(0.0, (full_height - remainder.height) / max(full_height, 1.0))
            candidates.append((max(first_gap, remainder_gap), remainder_gap, split_at, first, remainder))
        if candidates:
            _worst_gap, _remainder_gap, _split_at, first, remainder = min(candidates, key=lambda value: (value[0], value[1], value[2]))
            if (
                not self.streaming_layout
                and self.student_view
                and self.chunk_index > 1
                and availHeight < full_height - PAGINATION_FIT_TOLERANCE_PT
                and availHeight - first.height < MATCHING_SPLIT_RESERVE_PT
            ):
                return [PageBreak(), self]
            return [first, remainder]
        # No two-page split is possible.  Pack the largest prefix that fits;
        # the remainder will be split again on the next full page.  A single
        # over-height semantic row is a hard renderer error, never a silent
        # character-level fragment.
        current: list[tuple[str, int]] = []
        for offset, unit in enumerate(units):
            candidate = self._flow_for_units(current + [unit], self.chunk_index, availWidth)
            if candidate.height <= availHeight:
                current.append(unit)
                continue
            if not current:
                return []
            first = self._flow_for_units(current, self.chunk_index, availWidth)
            remainder = self._flow_for_units(units[offset:], self.chunk_index + 1, availWidth)
            return [first, remainder] if first.height <= availHeight else []
        return []

    def draw(self) -> None:
        canvas = self.canv
        style = ParagraphStyle("matching-draw", fontName=self.font_name, fontSize=self.font_size, leading=self.font_size * 1.2, spaceAfter=0)
        options = [o for o in self.item.get("options", []) if isinstance(o, Mapping)]
        prompts = [p for p in self.item.get("prompts", []) if isinstance(p, Mapping)]
        if self.student_view:
            y = self.height - MATCHING_TOP_PADDING_PT
        else:
            canvas.setFont(self.font_name, self.font_size)
            canvas.drawString(0, self.height - self.font_size, "Matching")
            y = self.height - matching_header_height(self.font_size, student_view=False)
        selected = self.diagnostic["selected_layout"]
        if selected == "card-grid":
            half = self.width / 2.0
            for row_start in range(0, len(options), 2):
                row = options[row_start:row_start + 2]
                row_height = max(self._option_heights[row_start:row_start + 2], default=0.0)
                for offset, option in enumerate(row):
                    x0 = 0 if offset == 0 else half
                    para = Paragraph(html.escape(matching_option_text(option, student_view=self.student_view)), style)
                    _, h = para.wrap(half - 8, 100000.0)
                    para.drawOn(canvas, x0, y - h)
                    self._record_semantic("option", str(option.get("option_id", "")), str(option.get("text", "")), [self.x + x0, self.y + y - h, self.x + x0 + half - 8, self.y + y], layout_column="left" if offset == 0 else "right")
                y -= row_height + 3.5
            if options and prompts:
                y -= 3.5
            for index, (prompt, h) in enumerate(zip(prompts, self._prompt_heights), 1):
                para = Paragraph(html.escape(matching_prompt_text(prompt, index, student_view=self.student_view)), style)
                _, h = para.wrap(self.width, 100000.0)
                para.drawOn(canvas, 0, y - h)
                self._record_semantic("prompt", str(prompt.get("prompt_id", "")), str(prompt.get("text", "")), [self.x, self.y + y - h, self.x + self.width, self.y + y], layout_column="full")
                self._record_response(prompt, [self.x, self.y + y - h, self.x + self.width, self.y + y])
                y -= h + 3.5
        elif selected == "stacked":
            for option in options:
                para = Paragraph(html.escape(matching_option_text(option, student_view=self.student_view)), style)
                _, h = para.wrap(self.width, 100000.0)
                para.drawOn(canvas, 0, y - h)
                self._record_semantic("option", str(option.get("option_id", "")), str(option.get("text", "")), [self.x, self.y + y - h, self.x + self.width, self.y + y], layout_column="full")
                y -= h + 3
            y -= 3
            for index, prompt in enumerate(prompts, 1):
                para = Paragraph(html.escape(matching_prompt_text(prompt, index, student_view=self.student_view)), style)
                _, h = para.wrap(self.width, 100000.0)
                para.drawOn(canvas, 0, y - h)
                self._record_semantic("prompt", str(prompt.get("prompt_id", "")), str(prompt.get("text", "")), [self.x, self.y + y - h, self.x + self.width, self.y + y], layout_column="full")
                self._record_response(prompt, [self.x, self.y + y - h, self.x + self.width, self.y + y]); y -= h + 3
        else:
            half = self.width / 2.0
            left_y, right_y = y, y
            for option in options:
                para = Paragraph(html.escape(matching_option_text(option, student_view=self.student_view)), style)
                _, h = para.wrap(half - 8, 100000.0)
                para.drawOn(canvas, 0, left_y - h)
                self._record_semantic("option", str(option.get("option_id", "")), str(option.get("text", "")), [self.x, self.y + left_y - h, self.x + half - 8, self.y + left_y], layout_column="left")
                left_y -= h + 3
            for index, prompt in enumerate(prompts, 1):
                para = Paragraph(html.escape(matching_prompt_text(prompt, index, student_view=self.student_view)), style)
                _, h = para.wrap(half - 8, 100000.0)
                para.drawOn(canvas, half, right_y - h)
                self._record_semantic("prompt", str(prompt.get("prompt_id", "")), str(prompt.get("text", "")), [self.x + half, self.y + right_y - h, self.x + self.width - 8, self.y + right_y], layout_column="right")
                self._record_response(prompt, [self.x + half, self.y + right_y - h, self.x + self.width - 8, self.y + right_y]); right_y -= h + 3
        grid_id = f"{self.item.get('item_id')}-matching-grid"
        if self.chunk_index > 1:
            grid_id += f"-{self.chunk_index:03d}"
        self.tracker.append({
            "block_id": grid_id,
            "role": "content",
            "source_item_id": self.item.get("item_id", ""),
            "page": canvas.getPageNumber(),
            "bbox_pt": [self.x, self.y, self.x + self.width, self.y + self.height],
            "layout_region": "matching",
        })

    def _record_semantic(self, role: str, source_id: str, value: str, bbox: list[float], *, layout_column: str | None = None) -> None:
        safe_id = re.sub(r"[^a-z0-9._-]+", "-", source_id.casefold())
        self.tracker.append({
            "block_id": f"{self.item.get('item_id')}-matching-{role}-{safe_id}",
            "role": role,
            "source_item_id": self.item.get("item_id", ""),
            **({"source_prompt_id": source_id} if role == "prompt" else {}),
            "page": self.canv.getPageNumber(),
            "bbox_pt": bbox,
            "layout_region": "matching",
            **({"layout_column": layout_column} if layout_column else {}),
        })

    def _record_response(self, prompt: Mapping[str, Any], bbox: list[float]) -> None:
        prompt_id = str(prompt.get("prompt_id", ""))
        response = next((row for row in self.responses if row.get("source_prompt_id") == prompt_id), None)
        if response:
            self.tracker.append({
                "block_id": f"{self.item.get('item_id')}-response-{str(response.get('response_id', '')).replace('/', '-')}",
                "role": "response_area", "source_item_id": self.item.get("item_id", ""), "source_prompt_id": prompt_id,
                "page": self.canv.getPageNumber(),
                "bbox_pt": bbox, "layout_region": "matching_response", "response": response,
            })

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:  # noqa: N802
        self.x, self.y = x, y
        super().drawOn(canvas, x, y, _sW)


class DeterministicCanvas(__import__("reportlab.pdfgen.canvas", fromlist=["Canvas"]).Canvas):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)
        self.setTitle("Middle School English Assessment")
        self.setAuthor("Print Pipeline")
        self.setCreator("Middle School English Print Pipeline")


def matching_diagnostic(
    item: Mapping[str, Any],
    font_size: float,
    available_width: float,
    available_height: float,
    tie_break: list[str],
    *,
    max_empty_ratio: float = 0.15,
    student_view: bool = True,
) -> dict[str, Any]:
    options = [o for o in item.get("options", []) if isinstance(o, Mapping)]
    prompts = [p for p in item.get("prompts", []) if isinstance(p, Mapping)]
    style = ParagraphStyle("measure-matching", fontName="PrintBody", fontSize=font_size, leading=font_size * 1.2)
    def measured(value: str, width: float) -> float:
        return Paragraph(value, style).wrap(max(1.0, width), 100000.0)[1]

    half_width = available_width / 2.0 - 8.0
    option_full = [measured(html.escape(matching_option_text(o, student_view=student_view)), available_width) for o in options]
    prompt_full = [measured(html.escape(matching_prompt_text(p, index + 1, student_view=student_view)), available_width) for index, p in enumerate(prompts)]
    option_half = [measured(html.escape(matching_option_text(o, student_view=student_view)), half_width) for o in options]
    prompt_half = [measured(html.escape(matching_prompt_text(p, index + 1, student_view=student_view)), half_width) for index, p in enumerate(prompts)]
    card_option_rows = [max(option_half[index:index + 2], default=0.0) for index in range(0, len(option_half), 2)]
    header_height = matching_header_height(font_size, student_view=student_view)
    totals = {
        "card-grid": sum(value + 3.5 for value in card_option_rows) + (3.5 if card_option_rows and prompt_full else 0.0) + sum(value + 3.5 for value in prompt_full) + header_height,
        "stacked": sum(option_full) + sum(prompt_full) + 3.0 * max(0, len(options) + len(prompts) - 1) + header_height + 3.0,
        "dual-independent-flow": max(sum(option_half) + 3.0 * max(0, len(options) - 1), sum(prompt_half) + 3.0 * max(0, len(prompts) - 1)) + header_height,
    }
    candidates: list[dict[str, Any]] = []

    def paginate(heights: list[float], labels: list[str], gap: float = 3.0) -> tuple[int, list[str], int]:
        page_count = 1
        used = header_height
        breaks: list[str] = []
        hard_violation = 0
        for height, label in zip(heights, labels):
            unit = height + gap
            if unit + header_height > available_height:
                hard_violation = 1
            if used > header_height and used + unit > available_height:
                page_count += 1
                breaks.append(label)
                used = header_height
            used += unit
        return page_count, breaks, hard_violation

    for layout in ("card-grid", "stacked", "dual-independent-flow"):
        height = totals[layout]
        if layout == "stacked":
            row_heights = option_full + prompt_full
            row_labels = [str(value.get("option_id")) for value in options] + [str(value.get("prompt_id")) for value in prompts]
        elif layout == "card-grid":
            row_heights = [max(option_half[index:index + 2], default=0.0) for index in range(0, len(option_half), 2)] + prompt_full
            row_labels = [str(options[index].get("option_id")) for index in range(0, len(options), 2)] + [str(value.get("prompt_id")) for value in prompts]
        else:
            row_heights = [max(option_half[index] if index < len(option_half) else 0.0, prompt_half[index] if index < len(prompt_half) else 0.0) for index in range(max(len(options), len(prompts)))]
            row_labels = [str(prompts[index].get("prompt_id") if index < len(prompts) else options[index].get("option_id")) for index in range(len(row_heights))]
        page_count, breaks, violation = paginate(row_heights, row_labels, gap=3.5 if layout == "card-grid" else 3.0)
        hard_violation = int(violation)
        # ``max_non_response_empty_ratio`` is a ratio inside this matching
        # layout region, not the unused remainder of the whole A4 page.  The
        # latter belongs to the page-level preflight after the passage and
        # matching grid have been placed.  For two-column candidates, the
        # measurable internal hole is the shorter column's missing tail.
        if layout == "stacked":
            internal_empty = 3.0 / max(height, 1.0)
            column_imbalance = 0.0
        elif layout == "card-grid":
            # Card-grid deliberately places prompts in an independent full
            # width flow after the two-column option cards; there is no
            # paired-column tail to classify as a hole.
            internal_empty = 0.0
            column_imbalance = 0.0
        else:
            option_total = sum(option_half) + 3.0 * max(0, len(option_half) - 1)
            prompt_total = sum(prompt_half) + 3.0 * max(0, len(prompt_half) - 1)
            column_imbalance = abs(option_total - prompt_total) / max(option_total, prompt_total, 1.0)
            internal_empty = column_imbalance
        hard_violation += int(internal_empty > max_empty_ratio)
        # Exceeding one usable page is not itself a hard violation: the
        # selected layout is split only at measured semantic row boundaries
        # by matching_flowables().  An unbreakable row is the hard failure and
        # is raised by that function after final-font measurement.
        break_count = max(0, page_count - 1)
        candidates.append({
            "layout": layout,
            "page_count": page_count,
            "break_count": break_count,
            "hard_violation_count": hard_violation,
            "max_non_response_empty_ratio": internal_empty,
            "column_imbalance_ratio": column_imbalance,
            "isolated_item_count": 0,
            "breaks": breaks,
        })
    order = {name: index for index, name in enumerate(tie_break)}
    selected = min(candidates, key=lambda c: (c["hard_violation_count"], c["page_count"], c["max_non_response_empty_ratio"], c["isolated_item_count"], c["break_count"], order.get(c["layout"], 99)))
    return {
        "item_id": str(item.get("item_id", "")),
        "candidates": candidates,
        "selected_layout": selected["layout"],
        "selection_reason": "lexicographic hard_violation_count, page_count, empty_ratio, isolated_item_count, break_count, profile tie-break",
    }


def matching_flowables(item: Mapping[str, Any], font_name: str, font_size: float, *, tracker: list[dict[str, Any]], diagnostic: dict[str, Any], responses: list[Mapping[str, Any]], available_width: float, available_height: float, student_view: bool = True, streaming_layout: bool = False) -> list[MatchingFlowable]:
    """Split an over-height matching grid at semantic row boundaries.

    ReportLab cannot safely split an arbitrary custom Flowable by itself.  We
    therefore measure the selected final-font layout, greedily pack complete
    option/prompt rows, and return real Flowables that each fit the page.  The
    manifest keeps one diagnostic for the item; each emitted block retains its
    source prompt/option and response binding.
    """
    selected = diagnostic["selected_layout"]
    options = [o for o in item.get("options", []) if isinstance(o, Mapping)]
    prompts = [p for p in item.get("prompts", []) if isinstance(p, Mapping)]
    indexed_item = dict(item)
    indexed_item["prompts"] = [
        {**prompt, "_display_index": index}
        for index, prompt in enumerate(prompts, 1)
    ]
    # Let ReportLab call MatchingFlowable.split() with the actual remaining
    # frame height.  Pre-splitting against a full page loses the preceding
    # passage/instruction height and can strand a second chunk (or teacher
    # rationale) on a sparse page.
    base = MatchingFlowable(indexed_item, font_name, font_size, tracker=tracker, diagnostic=diagnostic, responses=responses, chunk_index=1, student_view=student_view, streaming_layout=streaming_layout)
    base.wrap(available_width, available_height)
    return [base]


def measured_group_heights(groups: list[list[Flowable]], available_width: float) -> list[float] | None:
    heights: list[float] = []
    for group in groups:
        group_height = 0.0
        for flowable in group:
            try:
                _width, height = flowable.wrap(available_width, 100000.0)
            except Exception:
                return None
            group_height += max(0.0, float(height))
        heights.append(group_height)
    return heights


def balanced_group_breaks(
    groups: list[list[Flowable]],
    available_width: float,
    available_height: float,
    *,
    view: str | None = None,
    compact: bool = False,
    inter_item_gap_pt: float = 0.0,
    streaming_layout: bool = False,
) -> set[int]:
    """Choose measured item-boundary breaks without rewriting content order."""
    inter_item_gap_pt = max(0.0, float(inter_item_gap_pt))
    if len(groups) < 2:
        return set()
    first_matching_index = next(
        (
            index
            for index, group in enumerate(groups)
            if any(isinstance(flowable, MatchingFlowable) for flowable in group)
        ),
        None,
    )
    heights = measured_group_heights(groups, available_width)
    if heights is None:
        return set()
    for index, group_height in enumerate(heights):
        if group_height > available_height + PAGINATION_FIT_TOLERANCE_PT and index != first_matching_index:
            return set()
    suffix_start = first_matching_index + 1 if first_matching_index is not None else 0
    suffix = heights[suffix_start:]
    if not suffix:
        return set()
    if first_matching_index is None:
        capacities = lambda page_count: [available_height] * page_count
    else:
        if inter_item_gap_pt > 0:
            # Measure the actual continuation of a matching grid.  Its full
            # height is not the height left on the page after the grid splits.
            prefix_height = sum(heights[:first_matching_index])
            used_on_last_prefix_page = prefix_height % max(available_height, 1.0)
            if used_on_last_prefix_page <= PAGINATION_FIT_TOLERANCE_PT:
                used_on_last_prefix_page = 0.0
            matching_group = groups[first_matching_index]
            matching_position = next(
                index for index, flowable in enumerate(matching_group) if isinstance(flowable, MatchingFlowable)
            )
            prefix_before_matching = sum(
                max(0.0, float(flowable.wrap(available_width, 100000.0)[1]))
                for flowable in matching_group[:matching_position]
            )
            matching = matching_group[matching_position]
            matching_capacity = available_height - used_on_last_prefix_page - prefix_before_matching
            if matching_capacity <= PAGINATION_FIT_TOLERANCE_PT:
                used_on_last_prefix_page = 0.0
                matching_capacity = available_height - prefix_before_matching
            matching.wrap(available_width, max(1.0, matching_capacity))
            matching_chunks = matching.split(available_width, max(1.0, matching_capacity))
            trailing_matching_height = sum(
                max(0.0, float(flowable.wrap(available_width, 100000.0)[1]))
                for flowable in matching_group[matching_position + 1:]
            )
            if matching_chunks:
                continuation_height = max(0.0, float(getattr(matching_chunks[-1], "height", 0.0)))
                first_page_capacity = available_height - continuation_height - trailing_matching_height - inter_item_gap_pt
            else:
                first_page_capacity = matching_capacity - matching.height - trailing_matching_height - inter_item_gap_pt
            first_page_capacity = max(1.0, first_page_capacity - (0.0 if streaming_layout else MATCHING_SPLIT_RESERVE_PT))
        else:
            prefix_height = sum(heights[:suffix_start])
            prefix_pages = max(1, int(math.ceil(prefix_height / max(available_height, 1.0))))
            used_on_last_prefix_page = prefix_height - (prefix_pages - 1) * available_height
            first_page_capacity = available_height if used_on_last_prefix_page <= PAGINATION_FIT_TOLERANCE_PT else available_height - used_on_last_prefix_page
            first_page_capacity = max(1.0, first_page_capacity - (0.0 if streaming_layout else MATCHING_SPLIT_RESERVE_PT))
        capacities = lambda page_count: [first_page_capacity] + [available_height] * (page_count - 1)
    suffix_total = sum(suffix) + inter_item_gap_pt * max(0, len(suffix) - 1)
    minimum_pages = 1
    while suffix_total > sum(capacities(minimum_pages)) + PAGINATION_FIT_TOLERANCE_PT and minimum_pages < len(suffix):
        minimum_pages += 1
    if suffix_total > sum(capacities(minimum_pages)) + PAGINATION_FIT_TOLERANCE_PT:
        return set()
    if inter_item_gap_pt > 0 and minimum_pages == 1:
        return set()
    prefix = [0.0]
    for height in suffix:
        prefix.append(prefix[-1] + height)

    def segment_height(start: int, end: int) -> float:
        return prefix[end] - prefix[start] + inter_item_gap_pt * max(0, end - start - 1)

    def partition(page_count: int, start: int = 0, page_capacities: list[float] | None = None) -> list[int] | None:
        remaining_total = segment_height(start, len(suffix)) if inter_item_gap_pt > 0 else prefix[-1] - prefix[start]
        target = remaining_total / page_count
        page_capacities = page_capacities or capacities(page_count)
        states: dict[tuple[int, int], tuple[float, list[int]]] = {(0, start): (0.0, [])}
        for page_index in range(page_count):
            next_states: dict[tuple[int, int], tuple[float, list[int]]] = {}
            for (_used_pages, start), (cost, cuts) in states.items():
                remaining_pages = page_count - page_index - 1
                last_end = len(suffix) - remaining_pages
                for end in range(start + 1, last_end + 1):
                    height = segment_height(start, end) if inter_item_gap_pt > 0 else prefix[end] - prefix[start]
                    if height > page_capacities[page_index] + PAGINATION_FIT_TOLERANCE_PT:
                        break
                    candidate = (cost + (height - target) ** 2, cuts + ([end] if remaining_pages else []))
                    key = (page_index + 1, end)
                    previous = next_states.get(key)
                    if previous is None or candidate[0] < previous[0] or (compact and candidate[0] == previous[0] and candidate[1] > previous[1]):
                        next_states[key] = candidate
            states = next_states
        result = states.get((page_count, len(suffix)))
        return result[1] if result else None

    student_first_cut: int | None = None
    if first_matching_index == 0 and view == "student":
        matching_group = groups[first_matching_index]
        matching_position = next(
            index for index, flowable in enumerate(matching_group) if isinstance(flowable, MatchingFlowable)
        )
        matching = matching_group[matching_position]
        prefix_before_matching = sum(
            max(0.0, float(flowable.wrap(available_width, 100000.0)[1]))
            for flowable in matching_group[:matching_position]
        )
        matching.wrap(available_width, available_height)
        matching_chunks = matching.split(available_width, max(1.0, available_height - prefix_before_matching))
        if matching_chunks:
            continuation_height = matching_chunks[-1].height + sum(
                max(0.0, float(flowable.wrap(available_width, 100000.0)[1]))
                for flowable in matching_group[matching_position + 1:]
            )
            matching_first_capacity = max(1.0, available_height - continuation_height) if inter_item_gap_pt <= 0 else max(1.0, available_height - continuation_height - inter_item_gap_pt)
        else:
            prefix_height = sum(heights[:suffix_start])
            used_on_last_prefix_page = prefix_height % max(available_height, 1.0)
            matching_first_capacity = max(1.0, available_height - used_on_last_prefix_page)
        first_count = 0
        first_used = 0.0
        if inter_item_gap_pt > 0:
            while first_count < len(suffix):
                candidate_height = first_used + suffix[first_count] + (inter_item_gap_pt if first_count else 0.0)
                if candidate_height > matching_first_capacity + PAGINATION_FIT_TOLERANCE_PT:
                    break
                first_used = candidate_height
                first_count += 1
        else:
            while first_count < len(suffix) and first_used + suffix[first_count] <= matching_first_capacity + PAGINATION_FIT_TOLERANCE_PT:
                first_used += suffix[first_count]
                first_count += 1
        if first_count and first_count < len(suffix):
            student_first_cut = suffix_start + first_count

    for page_count in range(minimum_pages, len(suffix) + 1):
        cuts = partition(page_count)
        if cuts:
            full_cuts = [suffix_start + cut for cut in cuts]
            if student_first_cut is not None and full_cuts:
                # The DP keeps the later page targets balanced.  Only its
                # first boundary is replaced by the measured continuation
                # capacity of the real matching split.
                full_cuts = [min(student_first_cut, full_cuts[0])] + full_cuts[1:]
            elif first_matching_index is not None and len(full_cuts) > 1:
                # The first suffix boundary is already reached by the
                # matching flowable's real split. Leaving that boundary
                # implicit lets the next ordinary item use the remaining
                # frame instead of creating a one-item page.
                full_cuts = full_cuts[1:]
            if inter_item_gap_pt <= 0 and first_matching_index not in {None, 0} and view == "teacher" and not compact and suffix:
                prefix_height = sum(heights[:suffix_start])
                used_on_last_prefix_page = prefix_height % max(available_height, 1.0)
                remaining_capacity = available_height - used_on_last_prefix_page
                later_suffix_height = max(suffix[1:], default=0.0)
                if used_on_last_prefix_page > PAGINATION_FIT_TOLERANCE_PT and suffix[0] <= remaining_capacity + PAGINATION_FIT_TOLERANCE_PT and suffix[0] <= later_suffix_height:
                    full_cuts = [cut + 1 for cut in full_cuts]
            return set(full_cuts)
    return set()


def make_manifest_base(assessment: Mapping[str, Any], request: Mapping[str, Any], out: Path, source_paths: Mapping[str, Path], profile_paths: Mapping[str, Path], font_record: Mapping[str, Any]) -> dict[str, Any]:
    def record(path: Path) -> dict[str, str]:
        return {"path": path.name if path.parent == out else str(path.relative_to(out)) if out in path.parents else path.name}

    return {
        "schema_version": "1.0.0",
        "status": "RENDERED",
        "assessment_id": assessment.get("assessment_id", ""),
        "blueprint_id": assessment.get("blueprint", {}).get("blueprint_id"),
        "inputs": {
            "request": record(source_paths["request"]),
            "assessment": record(source_paths["assessment"]),
            "validation_report": record(source_paths["validation_report"]),
            "base_profile": record(profile_paths["base"]),
            "resolved_profile": record(profile_paths["resolved"]),
            "asset_manifest": record(source_paths["asset_manifest"]),
        },
        "outputs": {},
        "profiles": {"base": record(profile_paths["base"]), "resolved": record(profile_paths["resolved"])},
        "fonts": [dict(font_record)],
        "blocks": [],
        "assets": [],
        "response_areas": [],
        "matching": [],
        "pages": [],
        "tool_versions": {
            "python": sys.version.split()[0],
            "reportlab": package_version("reportlab"),
            "pymupdf": package_version("PyMuPDF"),
            "pillow": package_version("Pillow"),
            "jsonschema": package_version("jsonschema"),
        },
        "deterministic_build": True,
    }


def visible_text(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("passage", "instruction", "context", "stem", "prompt", "script_outline"):
        if item.get(key):
            values.append(str(item[key]))
    for key in ("options", "prompts", "tasks"):
        for value in item.get(key, []) or []:
            if isinstance(value, Mapping):
                values.extend(str(value.get(name, "")) for name in ("option_id", "prompt_id", "task_id", "text", "prompt" ) if value.get(name))
    return values


def asset_entries(assessment: Mapping[str, Any], assets: Mapping[str, Any], *, illustration_mode: str, source_bundle: Path) -> dict[str, dict[str, Any]]:
    entries = {str(item.get("asset_id")): dict(item) for item in assets.get("assets", []) if isinstance(item, Mapping)}
    used: dict[str, dict[str, Any]] = {}
    for item in assessment.get("items", []):
        for ref in item.get("stimulus_assets", []) or []:
            if not isinstance(ref, Mapping):
                continue
            asset_id = str(ref.get("asset_id", ""))
            entry = entries.get(asset_id)
            if entry is None:
                raise ValueError("ASSET_UNRESOLVED")
            if illustration_mode == "none" and ref.get("required_for_answer"):
                raise ValueError("ASSET_REQUIRED_BUT_DISABLED")
            if illustration_mode == "original-grayscale":
                if entry.get("rights_status") not in {"granted", "cc_public_domain", "school_license"}:
                    raise ValueError("GRAPHICS_RIGHTS_INVALID")
                path = safe_child(source_bundle, str(entry.get("file", "")))
                try:
                    with Image.open(path) as image:
                        width, height = image.size
                        mode = image.mode
                except Exception as exc:
                    raise ValueError("ASSET_UNRESOLVED") from exc
                if width != entry.get("pixel_width") or height != entry.get("pixel_height") or mode != entry.get("color_mode"):
                    raise ValueError("ASSET_METADATA_MISMATCH")
                if float(entry.get("measured_dpi", 0)) < 300:
                    raise ValueError("GRAPHICS_LOW_DPI")
                if float(entry.get("contrast_ratio", 0)) < 1.4:
                    raise ValueError("GRAPHICS_LOW_CONTRAST")
                if entry.get("cropped") is not False:
                    raise ValueError("GRAPHICS_CROPPED")
            used[asset_id] = {"ref": dict(ref), "entry": entry}
    if illustration_mode == "original-grayscale" and any(item.get("required_for_answer") for item in entries.values()) and not used:
        raise ValueError("ASSET_UNRESOLVED")
    return used


def copy_asset_manifest_entry(entry: Mapping[str, Any], asset_path: Path) -> dict[str, Any]:
    with Image.open(asset_path) as image:
        width, height, mode = image.size[0], image.size[1], image.mode
    record = dict(entry)
    record.update(pixel_width=width, pixel_height=height, color_mode=mode)
    return record


class AssetFlowable(Flowable):
    def __init__(self, asset_id: str, path: Path, entry: Mapping[str, Any], *, tracker: list[dict[str, Any]], item_id: str, width: float):
        super().__init__()
        self.asset_id, self.path, self.entry = asset_id, path, dict(entry)
        self.tracker, self.item_id, self.width = tracker, item_id, width
        with Image.open(path) as image:
            pixel_width, pixel_height = image.size
        self.pixel_width, self.pixel_height = pixel_width, pixel_height
        requested_dpi = float(entry.get("measured_dpi", 0))
        if requested_dpi < 1:
            raise ValueError("GRAPHICS_LOW_DPI")
        # The declared DPI is a minimum physical-resolution contract.  Size
        # the image from pixels / DPI, then cap it at the printable width; do
        # not force a minimum point size that would silently lower the actual
        # placed DPI.
        self.width = min(width, pixel_width * 72.0 / requested_dpi)
        self.height = pixel_height * 72.0 / requested_dpi
        self._initial_width = self.width

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
        self.width = min(self._initial_width, availWidth)
        self.height = self.width * self.pixel_height / max(self.pixel_width, 1)
        return self.width, self.height

    def draw(self) -> None:
        x = float(getattr(self, "_draw_x", 0.0))
        y = float(getattr(self, "_draw_y", 0.0))
        ReportLabImage(str(self.path), width=self.width, height=self.height).drawOn(self.canv, 0, 0)
        self.tracker.append({
            "block_id": f"{self.item_id}-asset-{self.asset_id}", "role": "asset", "source_item_id": self.item_id,
            "page": self.canv.getPageNumber(),
            "bbox_pt": [x, y, x + self.width, y + self.height], "layout_region": "asset",
            "asset_id": self.asset_id, "file": self.entry["file"], "measured_dpi": self.entry["measured_dpi"],
        })

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:  # noqa: N802
        self._draw_x, self._draw_y = x, y
        super().drawOn(canvas, x, y, _sW)


def bind_asset_xrefs(document: Any, tracker: list[dict[str, Any]]) -> None:
    """Bind every rendered asset placement to its actual PDF image xref.

    ReportLab assigns image xrefs while saving the document, so the xref is not
    available to ``AssetFlowable.draw``.  PyMuPDF exposes the saved image
    occurrences and their rectangles through structured APIs.  Consume those
    occurrences in page order, matching each tracked placement to its actual
    post-render rectangle; this also distinguishes repeated uses of one xref.
    """

    def image_occurrences(page: Any) -> list[tuple[int, Any]]:
        occurrences: list[tuple[int, Any]] = []
        get_image_info = getattr(page, "get_image_info", None)
        if callable(get_image_info):
            try:
                infos = get_image_info(xrefs=True)
            except (AttributeError, TypeError):
                infos = []
            for info in infos:
                if not isinstance(info, Mapping):
                    continue
                bbox = info.get("bbox")
                xref = info.get("xref")
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not xref:
                    continue
                occurrences.append((int(xref), fitz.Rect(*[float(value) for value in bbox])))
        if occurrences:
            return occurrences

        # ``get_image_info`` was added after the older image APIs.  Keep the
        # fallback structured as well; geometry matching below still verifies
        # that each placement is backed by the selected resource.
        for image in page.get_images(full=True):
            xref = int(image[0])
            for rect in page.get_image_rects(xref):
                occurrences.append((xref, fitz.Rect(rect)))
        return occurrences

    def matches(expected: Any, actual: Any, tolerance: float = 1.0) -> bool:
        return all(
            abs(float(getattr(expected, side)) - float(getattr(actual, side))) <= tolerance
            for side in ("x0", "y0", "x1", "y1")
        )

    asset_entries = [entry for entry in tracker if entry.get("role") == "asset"]
    if not asset_entries:
        return
    occurrences_by_page: dict[int, list[tuple[int, Any]]] = {}
    for page_number, page in enumerate(document, 1):
        occurrences_by_page[page_number] = image_occurrences(page)

    for entry in asset_entries:
        page_number = int(entry.get("page", 0))
        bbox = entry.get("bbox_pt")
        if not isinstance(bbox, list) or len(bbox) != 4 or page_number not in occurrences_by_page:
            raise ValueError("ASSET_XREF_BINDING_INVALID")
        expected = fitz.Rect(
            float(bbox[0]),
            float(A4[1]) - float(bbox[3]),
            float(bbox[2]),
            float(A4[1]) - float(bbox[1]),
        )
        occurrences = occurrences_by_page[page_number]
        match_index = next(
            (index for index, (_xref, rect) in enumerate(occurrences) if matches(expected, rect)),
            None,
        )
        if match_index is None:
            raise ValueError("ASSET_XREF_BINDING_INVALID")
        xref, _rect = occurrences.pop(match_index)
        if xref <= 0:
            raise ValueError("ASSET_XREF_BINDING_INVALID")
        # This private field is consumed while assembling the manifest and is
        # never emitted as an unrecognised semantic-block property.
        entry["_pdf_xref"] = xref


def render_view(view: str, assessment: Mapping[str, Any], ir: Mapping[str, Any], out: Path, profile: Mapping[str, Any], font_name: str, manifest: dict[str, Any], assets: Mapping[str, Mapping[str, Any]], asset_paths: Mapping[str, Path]) -> Path:
    declared_margins = profile["page"]["margins_pt"]
    compact_profile = str(profile.get("profile_id", "")) == "generic-cn-compact-v1"
    exam_layout_fix = (
        compact_profile
        and str(assessment.get("assessment_id", "")) == "grade-08-semester-1-holiday-home-exam"
    )
    # The compact source profile predates the exam paper and leaves an
    # unnecessarily narrow body column.  Keep the declared profile intact for
    # auditability, but use a measured printable frame for its actual flow.
    if exam_layout_fix:
        margins = {
            side: max(COMPACT_MIN_MARGIN_PT, float(value) * COMPACT_MARGIN_SCALE)
            for side, value in declared_margins.items()
        }
    else:
        margins = declared_margins
    typography = profile["typography"]
    body_size = max(11.5 if exam_layout_fix and view == "student" else 10.5, float(typography["body_min_font_size_pt"]))
    leading = body_size * max(1.15 if exam_layout_fix else 1.0, float(typography.get("min_leading_multiplier", 1.15)))
    styles = getSampleStyleSheet()
    # The profile's leading and minimum font size remain the hard typography
    # contract.  Keep inter-block spacing compact enough that an otherwise
    # valid teacher projection does not strand a whole semantic item below a
    # 15% usable-page hole merely because of decorative paragraph gaps.
    normal = ParagraphStyle("print-body", parent=styles["BodyText"], fontName=font_name, fontSize=body_size, leading=leading, spaceAfter=0, spaceBefore=0, wordWrap="CJK")
    heading_leading = 18.5 if exam_layout_fix and view == "student" else 17.0
    heading = ParagraphStyle("print-heading", parent=normal, fontName=font_name, fontSize=max(14, body_size + 3.5), leading=max(heading_leading, body_size + (5 if exam_layout_fix else 6)), spaceAfter=0, spaceBefore=0)
    writing_prompt = ParagraphStyle("print-writing-prompt", parent=normal, leading=16.0, spaceAfter=0, spaceBefore=0)
    # Keep answer/rationale as measured semantic paragraphs.  The small
    # increase over body leading is enough to clear the teacher-page boundary
    # without creating a new page or reducing the 10.5pt typography gate.
    dense_choice_paper = compact_profile and len(assessment.get("items", [])) >= 30 and all(
        str(item.get("item_type", "")) == "single_choice" for item in assessment.get("items", [])
    )
    declared_inter_item_gap_pt = max(0.0, float(profile.get("layout", {}).get("inter_item_gap_pt", 0.0)))
    # Teacher answer annotations are part of each semantic item.  The compact
    # teacher projection uses no decorative gap between items so the measured
    # matching continuation and the following task consume the same frame as
    # the emitted story; student-facing spacing remains profile-controlled.
    inter_item_gap_pt = 0.0 if view == "teacher" and exam_layout_fix else declared_inter_item_gap_pt
    teacher_note_leading = (
        body_size * 1.15
        if dense_choice_paper
        else max(11.5, body_size * 1.1)
        if compact_profile and (inter_item_gap_pt > 0 or exam_layout_fix)
        else max(13.0, body_size * (1.15 if compact_profile else 1.2))
    )
    teacher_note = ParagraphStyle("print-teacher-note", parent=normal, leading=teacher_note_leading, spaceAfter=0)
    tracker: list[dict[str, Any]] = []
    story: list[Flowable] = []
    available_width = A4[0] - float(margins["left"]) - float(margins["right"])
    available_height = A4[1] - float(margins["top"]) - float(margins["bottom"])
    tie_break = list(profile.get("layout", {}).get("tie_break_order", ["card-grid", "stacked", "dual-independent-flow"]))
    paired_items = list(zip(assessment.get("items", []), ir.get("items", [])))
    item_groups: list[list[Flowable]] = []
    item_group_types: list[str] = []
    for item_index, (source, ir_item) in enumerate(paired_items):
        item_id = str(source.get("item_id", ""))
        group: list[Flowable] = []
        for ir_block in ir_item.get("blocks", []):
            role = str(ir_block.get("role", "content"))
            if role == "response_area":
                response = ir_block.get("response")
                if response and source.get("item_type") != "reading_matching":
                    response_line_height = 16.0 if inter_item_gap_pt > 0 and view == "student" and source.get("item_type") == "practical_writing" else (14.0 if exam_layout_fix else 16.0)
                    response_flow = ResponseFlowable(response, tracker=tracker, item_id=item_id, font_size=body_size, line_height=response_line_height)
                    group.append(response_flow)
                continue
            if role == "asset":
                asset_ref = ir_block.get("asset") or {}
                asset_id = str(asset_ref.get("asset_id", ""))
                if asset_id not in assets or asset_id not in asset_paths:
                    raise ValueError("ASSET_UNRESOLVED")
                group.append(AssetFlowable(asset_id, asset_paths[asset_id], assets[asset_id]["entry"], tracker=tracker, item_id=item_id, width=available_width))
                caption = str(asset_ref.get("caption", "")).strip()
                if caption:
                    group.append(TrackedParagraph(caption, normal, metadata={
                        "block_id": f"{item_id}-asset-{asset_id}-caption",
                        "role": "content",
                        "source_item_id": item_id,
                        "layout_region": "asset-caption",
                    }, tracker=tracker))
                continue
            if role == "content" and source.get("item_type") == "reading_matching" and ir_block.get("kind") == "MatchingGrid":
                diagnostic = matching_diagnostic(
                    source,
                    body_size,
                    available_width,
                    available_height,
                    tie_break,
                    max_empty_ratio=float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15)),
                    student_view=view == "student",
                )
                diagnostic["document"] = view
                manifest["matching"].append(diagnostic)
                responses = [candidate.get("response") for candidate in ir_item.get("blocks", []) if candidate.get("role") == "response_area" and candidate.get("response")]
                group.extend(matching_flowables(source, font_name, body_size, tracker=tracker, diagnostic=diagnostic, responses=responses, available_width=available_width, available_height=available_height, student_view=view == "student", streaming_layout=exam_layout_fix))
                continue
            value = str(ir_block.get("text", ""))
            if not value:
                continue
            style = heading if role == "heading" else writing_prompt if inter_item_gap_pt > 0 and view == "student" and source.get("item_type") == "practical_writing" and role == "prompt" else normal
            metadata = {
                "block_id": ir_block["block_id"],
                "role": "heading" if role == "heading" else role if role in {"content", "instruction", "passage", "stem", "option", "prompt", "task", "word_bank", "box"} else "content",
                "source_item_id": item_id,
                "layout_region": "body" if role != "word_bank" else "box",
            }
            if ir_block.get("box"):
                metadata["box"] = ir_block["box"]
            for key in ("source_task_id", "source_prompt_id"):
                if ir_block.get(key):
                    metadata[key] = ir_block[key]
            if role == "word_bank":
                group.append(Spacer(1, 4.0))
                group.append(TrackedParagraph(value, style, metadata=metadata, tracker=tracker))
            else:
                group.append(TrackedParagraph(value, style, metadata=metadata, tracker=tracker))
        if group:
            if view == "teacher" and compact_profile:
                answer = json.dumps(source.get("answer"), ensure_ascii=False, sort_keys=True)
                rationale = str(source.get("rationale", ""))
                teacher_value = f"Answer: {answer}"
                if rationale:
                    separator = "\n" if not compact_profile else " | "
                    teacher_value += f"{separator}Rationale: {rationale}"
                group.append(TrackedParagraph(teacher_value, teacher_note, metadata={"block_id": f"{item_id}-answer-rationale", "role": "content", "source_item_id": item_id, "layout_region": "teacher-answer"}, tracker=tracker))
            elif view == "teacher":
                answer = json.dumps(source.get("answer"), ensure_ascii=False, sort_keys=True)
                group.append(TrackedParagraph(f"Answer: {answer}", teacher_note, metadata={"block_id": f"{item_id}-answer", "role": "content", "source_item_id": item_id, "layout_region": "teacher-answer"}, tracker=tracker))
                if source.get("rationale"):
                    group.append(TrackedParagraph(f"Rationale: {source['rationale']}", teacher_note, metadata={"block_id": f"{item_id}-rationale", "role": "content", "source_item_id": item_id, "layout_region": "teacher-answer"}, tracker=tracker))
            # Keep only the semantic relationships required by the print
            # contract.  ``keepWithNext`` lets ReportLab form the smallest
            # required chain (heading -> first block, task/prompt -> its
            # response) while every ordinary block remains independently
            # splittable at its native paragraph boundaries.
            def role_of(flowable: Flowable) -> str:
                if isinstance(flowable, TrackedParagraph):
                    return str(flowable.metadata.get("role", ""))
                return ""

            def task_id_of(flowable: Flowable) -> str | None:
                if isinstance(flowable, TrackedParagraph):
                    value = flowable.metadata.get("source_task_id")
                    return str(value) if value else None
                if isinstance(flowable, ResponseFlowable):
                    value = flowable.response.get("source_task_id")
                    return str(value) if value else None
                return None

            def prompt_id_of(flowable: Flowable) -> str | None:
                if isinstance(flowable, TrackedParagraph):
                    value = flowable.metadata.get("source_prompt_id")
                    return str(value) if value else None
                if isinstance(flowable, ResponseFlowable):
                    value = flowable.response.get("source_prompt_id")
                    return str(value) if value else None
                return None

            for index, current in enumerate(group):
                following = group[index + 1] if index + 1 < len(group) else None
                if role_of(current) == "heading" and following is not None:
                    # MatchingFlowable owns its row-level split contract.  A
                    # preceding passage/instruction remains the first block
                    # kept with the heading without making the matching grid
                    # itself atomic.
                    if not isinstance(following, MatchingFlowable):
                        current.keepWithNext = 1
                if role_of(current) == "task" and isinstance(following, ResponseFlowable):
                    task_id = task_id_of(current)
                    if task_id is not None and task_id == task_id_of(following):
                        current.keepWithNext = 1
                # Practical-writing responses are prompt-bound rather than
                # tasks.  Preserve the same page-local semantic adjacency.
                if role_of(current) == "prompt" and isinstance(following, ResponseFlowable):
                    prompt_id = prompt_id_of(current)
                    if (prompt_id is not None and prompt_id == prompt_id_of(following)) or source.get("item_type") == "practical_writing":
                        current.keepWithNext = 1
            if view == "student" and (
                (inter_item_gap_pt > 0 and source.get("item_type") in {"single_choice", "reading_multiple_choice"})
                or (inter_item_gap_pt <= 0 and not compact_profile and source.get("item_type") == "single_choice")
            ):
                group_height = sum(max(0.0, float(flowable.wrap(available_width, 100000.0)[1])) for flowable in group)
                if group_height <= available_height + PAGINATION_FIT_TOLERANCE_PT:
                    for current in group[:-1]:
                        current.keepWithNext = 1
            if view == "teacher" and (
                (inter_item_gap_pt > 0 and compact_profile and source.get("item_type") != "reading_matching")
                or (inter_item_gap_pt <= 0 and dense_choice_paper)
            ):
                for current in group[:-1]:
                    current.keepWithNext = 1
            item_groups.append(group)
            item_group_types.append(str(source.get("item_type", "")))
    asset_column_breaks: set[int] = set()
    column_gap = 12.0
    column_width = (A4[0] - float(margins["left"]) - float(margins["right"]) - column_gap) / 2.0
    matching_index = next(
        (
            index
            for index, group in enumerate(item_groups)
            if any(isinstance(flowable, MatchingFlowable) for flowable in group)
        ),
        None,
    )
    has_asset_block = any(
        isinstance(flowable, AssetFlowable)
        for group in item_groups
        for flowable in group
    )
    matching_overflows_frame = any(
        sum(max(0.0, float(flowable.wrap(available_width, 100000.0)[1])) for flowable in group) > available_height + PAGINATION_FIT_TOLERANCE_PT
        for group in item_groups
        if any(isinstance(flowable, MatchingFlowable) for flowable in group)
    )
    single_column_heights = measured_group_heights(item_groups, available_width) or []
    matching_requires_split = False
    if matching_index is not None:
        matching_group_height = sum(single_column_heights[matching_index:matching_index + 1])
        preceding_height = sum(single_column_heights[:matching_index])
        used_on_preceding_page = preceding_height % max(available_height, 1.0)
        matching_requires_split = used_on_preceding_page + matching_group_height > available_height + PAGINATION_FIT_TOLERANCE_PT
    max_empty_ratio = float(profile.get("hard_gates", {}).get("max_non_response_empty_ratio", 0.15))
    single_column_breaks = balanced_group_breaks(item_groups, available_width, available_height, view=view, compact=compact_profile, inter_item_gap_pt=inter_item_gap_pt, streaming_layout=exam_layout_fix)

    def segment_heights(heights: list[float], breaks: set[int]) -> list[float]:
        boundaries = [0] + sorted(index for index in breaks if 0 < index < len(heights)) + [len(heights)]
        return [
            sum(heights[start:end]) + inter_item_gap_pt * max(0, end - start - 1)
            for start, end in zip(boundaries, boundaries[1:])
        ]

    single_segments = segment_heights(single_column_heights, single_column_breaks)
    single_nonfinal_hole = max(
        [max(0.0, available_height - used) / max(available_height, 1.0) for used in single_segments[:-1]],
        default=0.0,
    )
    single_has_avoidable_hole = any(
        max(0.0, available_height - used) / max(available_height, 1.0) > max_empty_ratio
        and end < len(single_column_heights)
        and single_column_heights[end] + inter_item_gap_pt <= available_height - used + PAGINATION_FIT_TOLERANCE_PT
        for used, end in zip(
            single_segments,
            [index for index in sorted(single_column_breaks)] + [len(single_column_heights)],
        )
    ) if single_segments else False
    if view == "student" and not compact_profile and not (has_asset_block and matching_index is None) and not matching_overflows_frame:
        tail_hole = max(0.0, available_height - single_segments[-1]) / max(available_height, 1.0)
        prior_pages_are_filled = all(
            max(0.0, available_height - used) / max(available_height, 1.0) <= max_empty_ratio
            for used in single_segments[:-1]
        )
        if matching_requires_split or (len(single_segments) > 1 and tail_hole > max_empty_ratio and prior_pages_are_filled):
            # Increase ordinary paragraph leading only when measured single
            # column content would strand a sparse final segment.  The same
            # source then reflows naturally; no page or geometry claim changes.
            normal.leading = max(normal.leading, body_size * 1.35)
            single_column_heights = measured_group_heights(item_groups, available_width) or single_column_heights
            single_column_breaks = balanced_group_breaks(item_groups, available_width, available_height, view=view, compact=compact_profile, inter_item_gap_pt=inter_item_gap_pt, streaming_layout=exam_layout_fix)
    rebalance_pages = compact_profile or view == "teacher" or matching_overflows_frame or (view == "student" and matching_index is not None and not has_asset_block)
    group_breaks = single_column_breaks if rebalance_pages else set()
    if view == "teacher" and compact_profile and inter_item_gap_pt > 0 and group_breaks:
        # Teacher single-choice groups are rendered without inter-item gaps
        # below, so remove only boundaries that the gap-inclusive measurement
        # would otherwise leave between adjacent choices.
        group_breaks = {
            index
            for index in group_breaks
            if not (
                0 < index < len(item_group_types)
                and item_group_types[index - 1] == "single_choice"
                and item_group_types[index] == "single_choice"
            )
        }
    if exam_layout_fix and group_breaks:
        # A task block followed by writing can use the remaining frame when
        # measured; forcing a boundary here strands the task page and pushes
        # the writing prompt into a sparse trailing page.
        group_breaks = {
            index
            for index in group_breaks
            if not (
                0 < index < len(item_group_types)
                and item_group_types[index - 1] == "task_based_reading"
                and item_group_types[index] == "practical_writing"
            )
        }
    if view == "teacher" and exam_layout_fix and not dense_choice_paper:
        # Teacher annotations belong to their semantic item, but ordinary
        # item groups may flow against the measured frame.  Keeping every
        # group chained would strand matching/task pages before writing.
        group_breaks = set()
    if view == "student" and compact_profile and not exam_layout_fix and not has_asset_block and group_breaks:
        # The compact fixture keeps complete choice groups atomic.  A balanced
        # boundary is still avoidable when the next whole group fits the
        # current frame and leaves the following frame within the same hole
        # gate; consume that group before emitting the PageBreak.
        def segment_height(start: int, end: int) -> float:
            return sum(single_column_heights[start:end]) + inter_item_gap_pt * max(0, end - start - 1)

        changed = True
        while changed and group_breaks:
            changed = False
            boundaries = [0] + sorted(group_breaks) + [len(single_column_heights)]
            for boundary_index in range(1, len(boundaries) - 1):
                start = boundaries[boundary_index - 1]
                cut = boundaries[boundary_index]
                end = boundaries[boundary_index + 1]
                current_used = segment_height(start, cut)
                moved_used = segment_height(start, cut + 1)
                remainder_used = segment_height(cut + 1, end)
                if moved_used > available_height + PAGINATION_FIT_TOLERANCE_PT:
                    continue
                remainder_hole = max(0.0, available_height - remainder_used) / max(available_height, 1.0)
                # The final segment is audited as a natural tail when its
                # measured content spans most of the frame.  Permit the
                # preceding page to consume a complete group in that case;
                # otherwise the local hole check can preserve an avoidable
                # boundary merely because the tail has one fewer item.
                natural_tail = (
                    boundary_index == len(boundaries) - 2
                    and remainder_used >= available_height * 0.75
                )
                if remainder_used > 0 and remainder_hole > max_empty_ratio and not natural_tail:
                    continue
                if current_used + single_column_heights[cut] + inter_item_gap_pt <= available_height + PAGINATION_FIT_TOLERANCE_PT:
                    if natural_tail:
                        # Keep the explicit boundary one group later.  A
                        # removed break would let ordinary ReportLab flow
                        # consume every following group that fits, moving
                        # more than the measured first complete group and
                        # potentially making the tail sparse again.
                        group_breaks.remove(cut)
                        group_breaks.add(cut + 1)
                        changed = False
                        break
                    group_breaks.remove(cut)
                    changed = True
                    break
    # Ordinary one-column flow needs only a local break before a complete
    # choice; compact and matching flows already have measured boundaries.
    if view == "student" and matching_index is None and not rebalance_pages and not has_asset_block:
        used = 0.0
        for index, (group_height, item_type) in enumerate(zip(single_column_heights, item_group_types)):
            if item_type == "single_choice" and group_height <= available_height + PAGINATION_FIT_TOLERANCE_PT and used > PAGINATION_FIT_TOLERANCE_PT and used + group_height > available_height + PAGINATION_FIT_TOLERANCE_PT:
                group_breaks.add(index)
                used = 0.0
            if group_height <= available_height + PAGINATION_FIT_TOLERANCE_PT:
                used += group_height
            else:
                used = group_height % max(available_height, 1.0)

    if view == "teacher" and matching_index is not None and len(group_breaks) == 1:
        # A lone boundary after a split matching item is less informative than
        # the native continuation flow.  Let ReportLab place the following
        # groups against the actual remaining frame instead of forcing a new
        # sparse page.
        group_breaks = set()
    asset_student_columns = False
    if view == "student" and has_asset_block and matching_index is None and single_has_avoidable_hole:
        asset_column_breaks = balanced_group_breaks(item_groups, column_width, available_height, view=view, compact=compact_profile, inter_item_gap_pt=inter_item_gap_pt, streaming_layout=exam_layout_fix)
        column_heights = measured_group_heights(item_groups, column_width) or []
        column_segments = segment_heights(column_heights, asset_column_breaks)
        column_hole = max(
            [max(0.0, available_height - used) / max(available_height, 1.0) for used in column_segments],
            default=1.0,
        )
        asset_student_columns = (
            bool(asset_column_breaks)
            and (len(asset_column_breaks) + 1) % 2 == 0
            and column_hole < single_nonfinal_hole
        )
    if asset_student_columns:
        # Keep asset-bearing items intact while allowing measured page columns
        # to consume content that cannot fit the single-column plan.
        for group in item_groups:
            for current in group[:-1]:
                current.keepWithNext = 1
    elif group_breaks and matching_index is not None and (view == "teacher" or compact_profile or has_asset_block):
        for group_index, group in enumerate(item_groups):
            if group_index <= matching_index or any(isinstance(flowable, MatchingFlowable) for flowable in group):
                continue
            if any(isinstance(flowable, ResponseFlowable) for flowable in group):
                continue
            for current, _following in zip(group, group[1:]):
                current.keepWithNext = 1
    for group_index, group in enumerate(item_groups):
        if not asset_student_columns and group_index in group_breaks:
            story.append(PageBreak())
        story.extend(group)
        if asset_student_columns and group_index + 1 in asset_column_breaks:
            story.append(FrameBreak())
        elif group_index < len(item_groups) - 1 and (
            inter_item_gap_pt <= 0
            or (group_index + 1 not in group_breaks and group_index + 1 not in asset_column_breaks)
        ):
            gap = inter_item_gap_pt
            next_type = item_group_types[group_index + 1]
            # Keep the dense teacher projection on the same page as its
            # student-facing choice sequence. Answer/rationale paragraphs
            # remain present; only the decorative inter-item gap is removed.
            if (
                view == "teacher"
                and compact_profile
                and inter_item_gap_pt > 0
                and item_group_types[group_index] == "single_choice"
                and next_type == "single_choice"
            ):
                gap = 0.0
            # A matching grid followed immediately by task reading should
            # use the remaining frame so the last task response stays with
            # its prompt instead of creating a one-line continuation page.
            if (
                view == "student"
                and inter_item_gap_pt > 0
                and item_group_types[group_index] == "reading_matching"
                and next_type == "task_based_reading"
            ):
                gap = 0.0
            # The final task and writing item share a teacher page when their
            # semantic groups fit; a decorative gap would push the writing
            # response area onto a sparse fourth page.
            if (
                view == "teacher"
                and compact_profile
                and inter_item_gap_pt > 0
                and item_group_types[group_index] == "task_based_reading"
                and next_type == "practical_writing"
            ):
                gap = 0.0
            story.append(Spacer(1, gap))
    if view == "teacher":
        # Teacher content is projected from the same item group rather than a
        # detached answer-key page.  This keeps answers bound to their source
        # item and prevents a sparse trailing page from being mistaken for a
        # valid layout.
        pass

    pdf = out / f"{view}.pdf"
    # Keep the final page from becoming an unbounded fragment.  ReportLab's
    # normal flow is allowed to split semantic blocks, but a trailing page
    # that contains only a few blocks is a hard layout defect: rebalance the
    # ordinary paragraph flow before writing the PDF rather than weakening
    # preflight's physical-page gate.
    if asset_student_columns:
        frames = [
            Frame(float(margins["left"]), float(margins["bottom"]), column_width, available_height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="asset-left"),
            Frame(float(margins["left"]) + column_width + column_gap, float(margins["bottom"]), column_width, available_height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="asset-right"),
        ]
        doc = BaseDocTemplate(str(pdf), pagesize=A4, title="Middle School English Assessment", author="Print Pipeline")
        doc.addPageTemplates([PageTemplate(id="asset-student-columns", frames=frames)])
    else:
        doc = SimpleDocTemplate(str(pdf), pagesize=A4, leftMargin=margins["left"], rightMargin=margins["right"], topMargin=margins["top"], bottomMargin=margins["bottom"], title="Middle School English Assessment", author="Print Pipeline")
    doc.build(story, canvasmaker=DeterministicCanvas)
    for entry in tracker:
        entry = dict(entry)
        asset_id = entry.get("asset_id")
        response = entry.pop("response", None)
        entry.pop("_pdf_xref", None)
        entry.pop("asset_id", None)
        entry.pop("file", None)
        entry.pop("measured_dpi", None)
        if response is not None:
            manifest["response_areas"].append({
                "document": view,
                "response_id": response["response_id"],
                "source_item_id": response["source_item_id"],
                **({"source_task_id": response["source_task_id"]} if response.get("source_task_id") else {}),
                **({"source_prompt_id": response["source_prompt_id"]} if response.get("source_prompt_id") else {}),
                "response_contract": {**response["answer_contract"], "line_policy": response["line_policy"]},
                "actual_line_count": int(response.get("line_count", 0)),
                "page": entry["page"],
                "bbox_pt": entry["bbox_pt"],
            })
        else:
            entry["document"] = view
            if asset_id:
                entry["asset_id"] = asset_id
            box = entry.pop("box", None)
            if box:
                x0, y0, x1, y1 = entry["bbox_pt"]
                pad = box.get("padding_pt", {"top": 8, "right": 8, "bottom": 8, "left": 8})
                box_bbox = [x0 - pad["left"], y0 - pad["bottom"], x1 + pad["right"], y1 + pad["top"]]
                content = [x0, y0, x1, y1]
                entry["box"] = {
                    "box_bbox_pt": box_bbox,
                    "content_bbox_pt": content,
                    "horizontal_center_delta_pt": abs(((content[0] + content[2]) / 2) - ((box_bbox[0] + box_bbox[2]) / 2)),
                    "vertical_center_delta_pt": abs(((content[1] + content[3]) / 2) - ((box_bbox[1] + box_bbox[3]) / 2)),
                    "padding_pt": pad,
                    "font_size_pt": float(entry.get("font_size_pt", 10.5)),
                    "alignment": "center",
                }
            manifest["blocks"].append(entry)
    with fitz.open(pdf) as document:
        for page_number, page in enumerate(document, 1):
            manifest["pages"].append({"document": view, "page": page_number, "width_pt": float(page.rect.width), "height_pt": float(page.rect.height)})
        bind_asset_xrefs(document, tracker)
    # Asset geometry is captured from the actual flowable tracker and the
    # final PDF image resources.  The manifest has one semantic asset record;
    # preflight verifies that the same asset is embedded in both views.  The
    # xref comes from the placement-specific post-render binding above, not
    # from an arbitrary first image resource in the document.
    for entry in tracker:
        if entry.get("role") != "asset":
            continue
        asset_id = str(entry.get("asset_id", ""))
        xref = int(entry.get("_pdf_xref", 0))
        if xref <= 0:
            raise ValueError("ASSET_XREF_BINDING_INVALID")
        if view == "student" and not any(record.get("asset_id") == asset_id for record in manifest["assets"]):
            source = assets[asset_id]["entry"]
            with Image.open(asset_paths[asset_id]) as image:
                pixel_width, pixel_height = image.size
                color_mode = image.mode
            manifest["assets"].append({
                "asset_id": asset_id,
                "file": source["file"],
                "pdf_xref": xref,
                "pdf_xrefs": {view: xref},
                "page": int(entry["page"]),
                "placement_bbox_pt": entry["bbox_pt"],
                "display_size_mm": [float(entry["bbox_pt"][2] - entry["bbox_pt"][0]) * 25.4 / 72.0, float(entry["bbox_pt"][3] - entry["bbox_pt"][1]) * 25.4 / 72.0],
                "measured_dpi": round(min(
                    float(pixel_width) * 72.0 / max(float(entry["bbox_pt"][2]) - float(entry["bbox_pt"][0]), 1e-6),
                    float(pixel_height) * 72.0 / max(float(entry["bbox_pt"][3]) - float(entry["bbox_pt"][1]), 1e-6),
                ) + 1e-6, 3),
                "semantic_role": source["semantic_role"],
                "linked_item_ids": source.get("linked_item_ids", []),
                "rights_status": source["rights_status"],
                "pixel_width": int(source.get("pixel_width", pixel_width)),
                "pixel_height": int(source.get("pixel_height", pixel_height)),
                "color_mode": source.get("color_mode", color_mode),
                "contrast_ratio": float(source["contrast_ratio"]),
                "cropped": False,
            })
        else:
            record = next((record for record in manifest["assets"] if record.get("asset_id") == asset_id), None)
            if record is None:
                raise ValueError("ASSET_XREF_BINDING_INVALID")
            view_xrefs = record.setdefault("pdf_xrefs", {})
            existing_xref = view_xrefs.get(view)
            if existing_xref is not None and int(existing_xref) != xref:
                raise ValueError("ASSET_XREF_BINDING_INVALID")
            view_xrefs[view] = xref
    return pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a bound print request")
    parser.add_argument("--request", required=True)
    parser.add_argument("--bundle-out", required=True)
    args = parser.parse_args(argv)
    try:
        request_path = Path(args.request).expanduser().resolve(strict=True)
        source_bundle = request_path.parent
        request = load(request_path)
        schema_validate("render-request.schema.json", request)
        out = Path(args.bundle_out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        assessment_path = safe_child(source_bundle, request["assessment_path"])
        report_path = safe_child(source_bundle, request["validation_report_path"])
        base_profile_path = safe_child(source_bundle, request["base_profile_path"])
        asset_manifest_path = safe_child(source_bundle, request["asset_manifest_path"])
        assessment = load(assessment_path)
        report = load(report_path)
        base_profile = load(base_profile_path)
        assets = load(asset_manifest_path)
        schema_validate("assessment.schema.json", assessment)
        schema_validate("assessment-validation.schema.json", report)
        schema_validate("render-profile.schema.json", base_profile)
        schema_validate("asset-manifest.schema.json", assets)
        if report.get("status") != "ASSESSMENT_VALIDATOR_PASS" or report.get("assessment_id") != assessment.get("assessment_id"):
            raise ValueError("CONTENT_REPORT_BINDING_INVALID")
        used_assets = asset_entries(assessment, assets, illustration_mode=str(request.get("illustration_mode", "none")), source_bundle=source_bundle)
        for source in (assessment_path, report_path, base_profile_path, asset_manifest_path, request_path):
            target = out / source.name
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
        out_assessment = out / "assessment.json"
        out_report = out / "content-validation-report.json"
        out_profile = out / base_profile_path.name
        out_assets = out / asset_manifest_path.name
        out_request = out / "render-request.json"
        out_assessment.write_bytes(assessment_path.read_bytes())
        out_report.write_bytes(report_path.read_bytes())
        out_profile.write_bytes(base_profile_path.read_bytes())
        out_assets.write_bytes(asset_manifest_path.read_bytes())
        out_request.write_bytes(request_path.read_bytes())
        # The request's optional override documents are explicit derived inputs
        # and are kept in the bundle for the resolved profile.
        bundle_asset_paths: dict[str, Path] = {}
        for asset_id, value in used_assets.items():
            source_asset = safe_child(source_bundle, str(value["entry"]["file"]))
            target_asset = safe_output_child(out, str(value["entry"]["file"]))
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_asset, target_asset)
            bundle_asset_paths[asset_id] = target_asset
            value["entry"] = copy_asset_manifest_entry(value["entry"], target_asset)
        resolved_profile_path = out / "resolved-profile.json"
        command = [sys.executable, str(SCRIPTS / "resolve_render_profile.py"), "--base", str(out_profile), "--output", str(resolved_profile_path)]
        if request.get("overrides"):
            overrides_path = out / "profile-overrides.json"
            overrides_path.write_text(json.dumps(request["overrides"], ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            command.extend(["--overrides", str(overrides_path)])
        if request.get("section_overrides"):
            sections_path = out / "profile-section-overrides.json"
            sections_path.write_text(json.dumps(request["section_overrides"], ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            command.extend(["--section-overrides", str(sections_path)])
        resolved = subprocess.run(command, capture_output=True, text=True, check=False)
        if resolved.returncode:
            print(resolved.stdout, end="")
            return resolved.returncode
        resolved_profile = load(resolved_profile_path)
        schema_validate("render-profile.schema.json", resolved_profile)
        student_ir_path = out / "student-ir.json"
        teacher_ir_path = out / "teacher-ir.json"
        writing_line_count = resolved_profile.get("response_areas", {}).get("writing_line_count")
        for view, path in (("student", student_ir_path), ("teacher", teacher_ir_path)):
            compile_command = [
                sys.executable,
                str(SCRIPTS / "compile_render_ir.py"),
                "--assessment", str(out_assessment),
                "--output", str(path),
                "--view", view,
            ]
            if writing_line_count is not None:
                compile_command.extend(["--writing-line-count", str(writing_line_count)])
            compiled = subprocess.run(compile_command, capture_output=True, text=True, check=False)
            if compiled.returncode:
                print(compiled.stdout, end="")
                return compiled.returncode
        student_ir = load(student_ir_path)
        teacher_ir = load(teacher_ir_path)
        schema_validate("render-ir.schema.json", student_ir)
        schema_validate("render-ir.schema.json", teacher_ir)
        if [item.get("item_id") for item in student_ir.get("items", [])] != [item.get("item_id") for item in teacher_ir.get("items", [])]:
            raise ValueError("IR_CANONICAL_TREE_MISMATCH")
        font_name, font_record = resolve_runtime_font(resolved_profile, collect_chars(assessment))
        manifest = make_manifest_base(assessment, request, out, {"request": out_request, "assessment": out_assessment, "validation_report": out_report, "asset_manifest": out_assets}, {"base": out_profile, "resolved": resolved_profile_path}, font_record)
        for name, key in (("profile-overrides.json", "profile_overrides"), ("profile-section-overrides.json", "profile_section_overrides")):
            candidate = out / name
            if candidate.exists():
                manifest["inputs"][key] = {"path": candidate.name}
        student_pdf = render_view("student", assessment, student_ir, out, resolved_profile, font_name, manifest, used_assets, bundle_asset_paths)
        teacher_pdf = render_view("teacher", assessment, teacher_ir, out, resolved_profile, font_name, manifest, used_assets, bundle_asset_paths)
        answer_sheet = {
            "schema_version": "1.0.0",
            "assessment_id": assessment.get("assessment_id"),
            "blueprint_id": assessment.get("blueprint", {}).get("blueprint_id"),
            "items": [{"item_number": index, "item_id": item.get("item_id"), "score": item.get("score"), "response_type": item.get("item_type"), "answer": item.get("answer")} for index, item in enumerate(assessment.get("items", []), 1)],
        }
        schema_validate("answer-sheet.schema.json", answer_sheet)
        answer_sheet_path = out / "answer-sheet.json"
        answer_sheet_path.write_text(json.dumps(answer_sheet, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest["outputs"] = {
            "student_ir": {"path": student_ir_path.name},
            "teacher_ir": {"path": teacher_ir_path.name},
            "student_pdf": {"path": student_pdf.name},
            "teacher_pdf": {"path": teacher_pdf.name},
            "answer_sheet": {"path": answer_sheet_path.name},
        }
        manifest_path = out / "render-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        schema_validate("render-manifest.schema.json", manifest)
        print(json.dumps({"status": "RENDERED", "out_dir": str(out), "student_pdf": str(student_pdf), "teacher_pdf": str(teacher_pdf)}))
        return 0
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "PRINT_BLOCKED", "error_code": str(exc), "exception_type": type(exc).__name__}), flush=True)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "PRINT_BLOCKED", "error_code": "RUNTIME_ERROR", "message": str(exc), "exception_type": type(exc).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
