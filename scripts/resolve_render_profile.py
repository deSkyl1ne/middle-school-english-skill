#!/usr/bin/env python3
"""Resolve and verify a deterministic print profile.

The profile resolver is intentionally independent from the PDF renderer.  It
does two related jobs:

* resolve a base profile and request overrides using a closed, hardening-only
  allow-list; and
* provide the font-resolution contract used by the compiler/renderer and
  preflight.  The font helper parses every face in a TTC collection, checks
  family/PostScript name, weight and glyph coverage together, and returns the
  auditable record required by PRD FR-6.

No font is silently substituted.  A profile can be resolved without touching
the host font installation, but an explicit font resolution request must
provide a real file and positive embedding evidence.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "render-profile.schema.json"

WEIGHT_NAMES = ("thin", "light", "regular", "medium", "semibold", "bold", "black", "heavy")
FORBIDDEN_REGULAR_WEIGHTS = frozenset(("bold", "black", "heavy"))
STYLE_SUFFIXES = {
    "thin": "thin",
    "hairline": "thin",
    "extralight": "light",
    "ultralight": "light",
    "light": "light",
    "book": "regular",
    "normal": "regular",
    "regular": "regular",
    "roman": "regular",
    "medium": "medium",
    "demi": "semibold",
    "demibold": "semibold",
    "semibold": "semibold",
    "bold": "bold",
    "heavy": "heavy",
    "black": "black",
    "extrabold": "black",
    "ultrabold": "black",
    "ultrablack": "black",
}


class ProfileResolutionError(ValueError):
    """A hard profile or font contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FontResolutionError(ProfileResolutionError):
    """A hard FR-6 font-resolution failure."""


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_stable_file(path: Path) -> bytes:
    """Read a regular runtime file used by profile resolution."""
    return path.read_bytes()


def load_stable_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(read_stable_file(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        raise ProfileResolutionError("PROFILE_BINDING_INVALID", f"{label} could not be read as JSON: {exc}") from exc


def parse_json_bytes(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileResolutionError("PROFILE_BINDING_INVALID", f"{label} is invalid JSON: {exc}") from exc


def resolve_cli_file(value: str, *, label: str) -> tuple[Path, bytes]:
    """Resolve a CLI-owned input file."""
    raw = Path(value).expanduser()
    if not value or "\x00" in value:
        raise ProfileResolutionError("PROFILE_BINDING_INVALID", f"{label} must be a regular file")
    try:
        path = raw.resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return path, read_stable_file(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProfileResolutionError("PROFILE_BINDING_INVALID", f"{label} is not a readable regular file: {exc}") from exc


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("truncated font table")
    return struct.unpack_from(">H", data, offset)[0]


def _i16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("truncated font table")
    return struct.unpack_from(">h", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("truncated font table")
    return struct.unpack_from(">I", data, offset)[0]


def _bounded_slice(data: bytes, start: int, length: int) -> bytes:
    if start < 0 or length < 0 or start + length > len(data):
        raise ValueError("font table points outside the font file")
    return data[start : start + length]


def _collection_offsets(data: bytes) -> list[int]:
    """Return *all* sfnt offsets in a TTC, or ``[0]`` for a single font."""
    if data[:4] != b"ttcf":
        if len(data) < 12:
            raise ValueError("font file is not a complete sfnt")
        return [0]
    if len(data) < 12:
        raise ValueError("truncated TTC header")
    count = _u32(data, 8)
    if count == 0 or count > 4096:
        raise ValueError("invalid TTC face count")
    end = 12 + count * 4
    if end > len(data):
        raise ValueError("truncated TTC offset table")
    offsets = [_u32(data, 12 + index * 4) for index in range(count)]
    if any(offset >= len(data) for offset in offsets):
        raise ValueError("TTC face offset is outside the file")
    return offsets


def _table_map(data: bytes, sfnt_offset: int) -> dict[str, tuple[int, int]]:
    if sfnt_offset + 12 > len(data):
        raise ValueError("truncated sfnt header")
    number = _u16(data, sfnt_offset + 4)
    directory_end = sfnt_offset + 12 + number * 16
    if directory_end > len(data):
        raise ValueError("truncated sfnt directory")
    tables: dict[str, tuple[int, int]] = {}
    for index in range(number):
        record = sfnt_offset + 12 + index * 16
        tag = data[record : record + 4].decode("ascii", errors="replace")
        table_offset = _u32(data, record + 8)
        table_length = _u32(data, record + 12)
        _bounded_slice(data, table_offset, table_length)
        tables[tag] = (table_offset, table_length)
    return tables


def _decode_name(raw: bytes, platform_id: int, encoding_id: int) -> str:
    if platform_id in (0, 3) or encoding_id in (1, 10):
        return raw.decode("utf-16-be", errors="replace").replace("\x00", "").strip()
    if platform_id == 1:
        return raw.decode("mac_roman", errors="replace").replace("\x00", "").strip()
    return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()


def _name_values(data: bytes, tables: Mapping[str, tuple[int, int]], name_id: int) -> list[str]:
    if "name" not in tables:
        return []
    offset, length = tables["name"]
    table = _bounded_slice(data, offset, length)
    if len(table) < 6:
        return []
    count = _u16(table, 2)
    string_offset = _u16(table, 4)
    values: list[str] = []
    for index in range(count):
        record = 6 + index * 12
        if record + 12 > len(table):
            break
        platform = _u16(table, record)
        encoding = _u16(table, record + 2)
        current_name_id = _u16(table, record + 6)
        value_length = _u16(table, record + 8)
        value_offset = _u16(table, record + 10)
        if current_name_id != name_id:
            continue
        raw_start = string_offset + value_offset
        try:
            raw = _bounded_slice(table, raw_start, value_length)
        except ValueError:
            continue
        value = _decode_name(raw, platform, encoding)
        if value and value not in values:
            values.append(value)
    return values


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _strip_style_suffix(value: str) -> tuple[str, str | None]:
    words = re.split(r"[\s_-]+", value.strip()) if value.strip() else []
    if words:
        suffix = STYLE_SUFFIXES.get(words[-1].casefold())
        if suffix:
            return " ".join(words[:-1]).strip(), suffix
    return value.strip(), None


def _weight_from_face(data: bytes, tables: Mapping[str, tuple[int, int]], subfamily: str) -> str:
    lower = re.sub(r"[^a-z]+", " ", subfamily.casefold()).split()
    for word in reversed(lower):
        if word in STYLE_SUFFIXES:
            return STYLE_SUFFIXES[word]
    if "OS/2" in tables:
        offset, length = tables["OS/2"]
        if length >= 6:
            numeric = _u16(data, offset + 4)
            if numeric <= 150:
                return "thin"
            if numeric <= 350:
                return "light"
            if numeric <= 450:
                return "regular"
            if numeric <= 550:
                return "medium"
            if numeric <= 650:
                return "semibold"
            if numeric <= 750:
                return "bold"
            if numeric <= 850:
                return "heavy"
            return "black"
    return "unknown"


def _format4_coverage(table: bytes, subtable_offset: int) -> set[int]:
    """Read a format-4 cmap subtable into the actual non-zero glyph set."""
    start = subtable_offset
    if start + 16 > len(table):
        return set()
    length = _u16(table, start + 2)
    end = min(len(table), start + length)
    if end < start + 16:
        return set()
    seg_count = _u16(table, start + 6) // 2
    end_codes = start + 14
    start_codes = end_codes + seg_count * 2 + 2
    id_deltas = start_codes + seg_count * 2
    id_range_offsets = id_deltas + seg_count * 2
    if id_range_offsets + seg_count * 2 > end:
        return set()
    covered: set[int] = set()
    for index in range(seg_count):
        first = _u16(table, start_codes + index * 2)
        last = _u16(table, end_codes + index * 2)
        if first == 0xFFFF and last == 0xFFFF:
            continue
        delta = _i16(table, id_deltas + index * 2)
        range_offset = _u16(table, id_range_offsets + index * 2)
        for codepoint in range(first, min(last, 0xFFFF) + 1):
            if range_offset == 0:
                glyph = (codepoint + delta) & 0xFFFF
            else:
                glyph_address = id_range_offsets + index * 2 + range_offset + (codepoint - first) * 2
                if glyph_address + 2 > end:
                    glyph = 0
                else:
                    glyph = _u16(table, glyph_address)
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
            if glyph:
                covered.add(codepoint)
    return covered


def _cmap_coverage(data: bytes, tables: Mapping[str, tuple[int, int]]) -> tuple[frozenset[int], tuple[tuple[int, int], ...]]:
    if "cmap" not in tables:
        return frozenset(), ()
    offset, length = tables["cmap"]
    table = _bounded_slice(data, offset, length)
    if len(table) < 4:
        return frozenset(), ()
    count = _u16(table, 2)
    subtables: list[tuple[int, int, int]] = []
    for index in range(count):
        record = 4 + index * 8
        if record + 8 > len(table):
            break
        platform = _u16(table, record)
        encoding = _u16(table, record + 2)
        sub_offset = _u32(table, record + 4)
        if sub_offset + 2 <= len(table):
            subtables.append((platform, encoding, sub_offset))

    covered: set[int] = set()
    ranges: list[tuple[int, int]] = []
    # Prefer Unicode format 12/13.  They can represent supplementary planes
    # without forcing a huge set into the face record.
    for _platform, _encoding, sub_offset in subtables:
        fmt = _u16(table, sub_offset)
        if fmt not in (12, 13) or sub_offset + 16 > len(table):
            continue
        groups = _u32(table, sub_offset + 12)
        group_start = sub_offset + 16
        for index in range(groups):
            record = group_start + index * 12
            if record + 12 > len(table):
                break
            first = _u32(table, record)
            last = _u32(table, record + 4)
            glyph = _u32(table, record + 8)
            if first <= last and glyph:
                ranges.append((first, last))
        if ranges:
            break

    if not ranges:
        for _platform, _encoding, sub_offset in subtables:
            fmt = _u16(table, sub_offset)
            if fmt == 4:
                covered.update(_format4_coverage(table, sub_offset))
            elif fmt == 6 and sub_offset + 10 <= len(table):
                first = _u16(table, sub_offset + 6)
                count = _u16(table, sub_offset + 8)
                for codepoint in range(first, first + count):
                    if sub_offset + 10 + (codepoint - first) * 2 + 2 <= len(table) and _u16(table, sub_offset + 10 + (codepoint - first) * 2):
                        covered.add(codepoint)
            elif fmt == 0 and sub_offset + 262 <= len(table):
                for codepoint, glyph in enumerate(table[sub_offset + 6 : sub_offset + 262]):
                    if glyph:
                        covered.add(codepoint)
        return frozenset(covered), ()
    return frozenset(covered), tuple(sorted(set(ranges)))


@dataclass(frozen=True)
class FontFace:
    """A single face from a TTF/OTF file or one TTC subfont."""

    source_file: str
    subfont_index: int
    family: str
    postscript_name: str
    subfamily: str
    weight: str
    coverage_codepoints: frozenset[int]
    coverage_ranges: tuple[tuple[int, int], ...]

    def covers(self, value: str | int) -> bool:
        codepoint = ord(value) if isinstance(value, str) else value
        if codepoint in self.coverage_codepoints:
            return True
        return any(first <= codepoint <= last for first, last in self.coverage_ranges)

    def coverage_ratio(self, values: Sequence[str | int]) -> float:
        if not values:
            return 1.0
        return sum(1 for value in values if self.covers(value)) / len(values)


def enumerate_font_faces(font_path: str | Path) -> list[FontFace]:
    """Enumerate every face in ``font_path`` (including every TTC subfont).

    This parser deliberately does not stop after the first three collection
    indexes.  It reads the sfnt ``name``, ``OS/2`` and Unicode ``cmap`` tables
    needed for deterministic selection and works without the optional
    fontTools package.
    """
    path = Path(font_path).expanduser()
    try:
        data = read_stable_file(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FontResolutionError("FONT_UNRESOLVED", f"cannot read font file {path}: {exc}") from exc
    try:
        offsets = _collection_offsets(data)
        faces: list[FontFace] = []
        for index, sfnt_offset in enumerate(offsets):
            tables = _table_map(data, sfnt_offset)
            family_values = _name_values(data, tables, 16) or _name_values(data, tables, 1)
            subfamily_values = _name_values(data, tables, 17) or _name_values(data, tables, 2)
            postscript_values = _name_values(data, tables, 6)
            family = family_values[0] if family_values else ""
            subfamily = subfamily_values[0] if subfamily_values else ""
            postscript = postscript_values[0] if postscript_values else ""
            if not family or not postscript:
                raise FontResolutionError(
                    "FONT_METADATA_MISSING",
                    f"font face {path}#{index} lacks family or PostScript metadata",
                )
            codepoints, ranges = _cmap_coverage(data, tables)
            faces.append(
                FontFace(
                    source_file=str(path.resolve()),
                    subfont_index=index,
                    family=family,
                    postscript_name=postscript,
                    subfamily=subfamily,
                    weight=_weight_from_face(data, tables, subfamily),
                    coverage_codepoints=codepoints,
                    coverage_ranges=ranges,
                )
            )
        if not faces:
            raise FontResolutionError("FONT_UNRESOLVED", f"font file {path} contains no faces")
        return faces
    except ProfileResolutionError:
        raise
    except (ValueError, IndexError, struct.error) as exc:
        raise FontResolutionError("FONT_UNRESOLVED", f"cannot parse font file {path}: {exc}") from exc


# Descriptive alias for callers that need to make the TTC requirement visible.
enumerate_ttc_faces = enumerate_font_faces


def _requested_family_and_weight(requested_family: str, requested_weight: str | None) -> tuple[str, str]:
    family, suffix_weight = _strip_style_suffix(requested_family)
    desired = requested_weight or suffix_weight or "regular"
    desired = STYLE_SUFFIXES.get(desired.casefold(), desired.casefold())
    if desired not in WEIGHT_NAMES:
        raise FontResolutionError("FONT_WEIGHT_INVALID", f"unsupported requested font weight: {desired}")
    return family or requested_family.strip(), desired


def _required_characters(token: Mapping[str, Any], required_chars: Iterable[str | int] | None) -> list[str | int]:
    if required_chars is not None:
        values = list(required_chars)
    else:
        coverage = token.get("required_coverage", {})
        values = list(coverage.get("characters", [])) if isinstance(coverage, Mapping) else []
    normalised: list[str | int] = []
    for value in values:
        if isinstance(value, int) and 0 <= value <= 0x10FFFF:
            normalised.append(value)
        elif isinstance(value, str) and value:
            normalised.extend(value if len(value) > 1 else [value])
        else:
            raise FontResolutionError("FONT_COVERAGE_INVALID", f"invalid required character in token {token.get('token')!r}")
    return normalised


def _font_paths_for_token(font_files: Any, token_name: str) -> list[Path]:
    if isinstance(font_files, Mapping):
        selected = font_files.get(token_name)
        if selected is None:
            selected = font_files.get("*")
    else:
        selected = font_files
    if isinstance(selected, (str, Path)):
        selected = [selected]
    if not isinstance(selected, Sequence) or isinstance(selected, (bytes, bytearray)):
        raise FontResolutionError("FONT_UNRESOLVED", f"no font file candidates supplied for token {token_name!r}")
    paths = [Path(value).expanduser() for value in selected]
    if not paths:
        raise FontResolutionError("FONT_UNRESOLVED", f"no font file candidates supplied for token {token_name!r}")
    return paths


def resolve_font_token(
    token: Mapping[str, Any],
    font_files: str | Path | Sequence[str | Path],
    *,
    required_chars: Iterable[str | int] | None = None,
    embedded: bool | None = None,
) -> dict[str, Any]:
    """Resolve one profile font token into a manifest-ready record.

    ``embedded`` is deliberately not inferred from a successful file parse:
    a renderer must pass positive evidence that the selected face was actually
    registered and embedded in the PDF.  Missing evidence is a hard error.
    """
    token_name = str(token.get("token", ""))
    if not token_name:
        raise FontResolutionError("FONT_RESOLUTION_INVALID", "font token has no token name")
    requested_family = str(token.get("requested_family", "")).strip()
    if not requested_family:
        raise FontResolutionError("FONT_RESOLUTION_INVALID", f"font token {token_name!r} has no requested family")
    requested_weight = token.get("requested_weight")
    family_name, desired_weight = _requested_family_and_weight(requested_family, requested_weight if isinstance(requested_weight, str) else None)
    allowed_weights = token.get("weights", [desired_weight])
    if not isinstance(allowed_weights, list) or desired_weight not in allowed_weights:
        raise FontResolutionError("FONT_WEIGHT_INVALID", f"token {token_name!r} does not permit requested weight {desired_weight!r}")
    fallback_families = token.get("fallback_families", [])
    if not isinstance(fallback_families, list) or any(not isinstance(value, str) or not value.strip() for value in fallback_families):
        raise FontResolutionError("FONT_FALLBACK_INVALID", f"token {token_name!r} has invalid fallback families")
    requested_names = [family_name, requested_family]
    fallback_names: list[str] = []
    for value in fallback_families:
        stripped, _ = _strip_style_suffix(value)
        fallback_names.extend((stripped, value))
    name_ranks: dict[str, int] = {}
    for rank, value in enumerate(requested_names + fallback_names):
        normalised = _normalise_name(value)
        if normalised:
            name_ranks.setdefault(normalised, 0 if rank < len(requested_names) else rank - len(requested_names) + 1)

    required = _required_characters(token, required_chars)
    candidates: list[tuple[tuple[int, int, int, int], FontFace, bool]] = []
    saw_weight_mismatch = False
    saw_coverage_mismatch = False
    saw_family = False
    for candidate_path in _font_paths_for_token(font_files, token_name):
        try:
            path = candidate_path.resolve(strict=True)
            faces = enumerate_font_faces(path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FontResolutionError("FONT_UNRESOLVED", f"font file could not be read: {path}: {exc}") from exc
        for face in faces:
            names = {_normalise_name(face.family), _normalise_name(face.postscript_name)}
            ranks = [name_ranks[name] for name in names if name in name_ranks]
            if not ranks:
                continue
            saw_family = True
            family_rank = min(ranks)
            if face.weight != desired_weight:
                saw_weight_mismatch = True
                continue
            if required and face.coverage_ratio(required) < 1.0:
                saw_coverage_mismatch = True
                continue
            postscript_rank = 0 if _normalise_name(face.postscript_name) in name_ranks else 1
            candidates.append(((family_rank, postscript_rank, -int(face.coverage_ratio(required) * 1000000), face.subfont_index), face, family_rank > 0))

    if not candidates:
        if saw_family and saw_weight_mismatch and desired_weight == "regular":
            raise FontResolutionError(
                "FONT_WEIGHT_INVALID",
                f"regular request for token {token_name!r} resolved only to a non-Regular face; Black/Bold/Heavy fallback is forbidden",
            )
        if saw_family and saw_coverage_mismatch:
            raise FontResolutionError("FONT_COVERAGE_INSUFFICIENT", f"font token {token_name!r} does not cover all required characters")
        if saw_family and saw_weight_mismatch:
            raise FontResolutionError("FONT_WEIGHT_INVALID", f"no allowed weight for font token {token_name!r}")
        raise FontResolutionError("FONT_FALLBACK_INVALID", f"font token {token_name!r} has no known requested/fallback family")

    _score, face, fallback_used = sorted(candidates, key=lambda item: item[0])[0]
    if fallback_used and face.family not in fallback_families and _normalise_name(face.family) not in {_normalise_name(value) for value in fallback_families}:
        raise FontResolutionError("FONT_FALLBACK_INVALID", f"font token {token_name!r} selected an unknown fallback family")
    if desired_weight == "regular" and face.weight in FORBIDDEN_REGULAR_WEIGHTS:
        raise FontResolutionError("FONT_WEIGHT_INVALID", f"font token {token_name!r} selected forbidden weight {face.weight}")
    actual_embedded = embedded
    if actual_embedded is None:
        resolved = token.get("resolved")
        actual_embedded = resolved.get("embedded") if isinstance(resolved, Mapping) else None
    if actual_embedded is not True:
        raise FontResolutionError("FONT_NOT_EMBEDDED", f"font token {token_name!r} has no positive embedding evidence")
    return {
        "token": token_name,
        "requested_family": requested_family,
        "resolved_family": face.family,
        "postscript_name": face.postscript_name,
        "resolved_file": face.source_file,
        "subfont_index": face.subfont_index,
        "weight": face.weight,
        "embedded": True,
        "fallback_used": fallback_used,
        "fallback_families": list(fallback_families),
        "coverage": {
            "required_count": len(required),
            "covered_count": len(required),
            "ratio": 1.0 if required else 1.0,
        },
    }


def resolve_fonts(
    profile: Mapping[str, Any],
    font_files: Any,
    *,
    required_chars_by_token: Mapping[str, Iterable[str | int]] | None = None,
    embedding_status: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Resolve all profile tokens and return auditable manifest records."""
    fonts = profile.get("fonts", {})
    tokens = fonts.get("tokens") if isinstance(fonts, Mapping) else None
    if not isinstance(tokens, list) or not tokens:
        raise FontResolutionError("FONT_RESOLUTION_INVALID", "profile has no font tokens")
    if embedding_status is None:
        embedding_status = {}
    records: list[dict[str, Any]] = []
    for token in tokens:
        if not isinstance(token, Mapping):
            raise FontResolutionError("FONT_RESOLUTION_INVALID", "font token must be an object")
        name = str(token.get("token", ""))
        required = required_chars_by_token.get(name) if required_chars_by_token else None
        records.append(resolve_font_token(token, _font_paths_for_token(font_files, name), required_chars=required, embedded=embedding_status.get(name)))
    return records


# Each entry is an explicit leaf path.  Unknown keys, including convenient
# renderer knobs, are rejected instead of being copied into a supposedly
# resolved profile.
OVERRIDE_RULES: dict[tuple[str, ...], str] = {
    ("page", "safe_padding_pt"): "increase",
    ("typography", "body_min_font_size_pt"): "increase",
    ("typography", "chinese_min_font_size_pt"): "increase",
    ("typography", "option_min_font_size_pt"): "increase",
    ("typography", "reading_matching_min_font_size_pt"): "increase",
    ("typography", "response_min_font_size_pt"): "increase",
    ("typography", "box_text_min_font_size_pt"): "increase",
    ("typography", "annotation_min_font_size_pt"): "increase",
    ("typography", "min_leading_multiplier"): "increase",
    ("hard_gates", "max_non_response_empty_ratio"): "decrease",
    ("hard_gates", "max_hole_width_ratio"): "decrease",
    ("hard_gates", "max_hole_height_ratio"): "decrease",
    ("hard_gates", "min_stimulus_dpi"): "increase",
    ("hard_gates", "min_photo_dpi"): "increase",
    ("hard_gates", "box_center_tolerance_pt"): "decrease",
    ("hard_gates", "box_padding_min_pt"): "increase",
    ("hard_gates", "box_padding_symmetry_tolerance_pt"): "decrease",
    ("response_areas", "min_height_mm"): "increase",
    ("response_areas", "writing_space_line_count_ceiling"): "decrease",
    ("box_geometry", "min_padding_pt"): "increase",
    ("box_geometry", "center_delta_tolerance_pt"): "decrease",
    ("box_geometry", "padding_symmetry_tolerance_pt"): "decrease",
    ("box_geometry", "min_font_size_pt"): "increase",
    ("layout", "tie_break_order"): "tie_break_order",
}
OVERRIDE_OBJECTS = {
    "page",
    "typography",
    "hard_gates",
    "response_areas",
    "box_geometry",
    "layout",
}


def _override_errors(value: Any, prefix: tuple[str, ...] = ()) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["PROFILE_OVERRIDE_UNKNOWN_PATH:" + ".".join(prefix)]
    for key, child in value.items():
        if not isinstance(key, str):
            errors.append("PROFILE_OVERRIDE_UNKNOWN_PATH:" + ".".join(prefix))
            continue
        path = prefix + (key,)
        if path in OVERRIDE_RULES:
            rule = OVERRIDE_RULES[path]
            if rule in ("increase", "decrease") and not isinstance(child, (int, float)):
                errors.append("PROFILE_OVERRIDE_TYPE:" + ".".join(path))
            elif rule == "tie_break_order":
                if child != ["card-grid", "stacked", "dual-independent-flow"] and not (
                    isinstance(child, list) and set(child) == {"card-grid", "stacked", "dual-independent-flow"} and len(child) == 3
                ):
                    errors.append("PROFILE_OVERRIDE_INVALID_TIE_BREAK:" + ".".join(path))
            continue
        if len(path) == 1 and key in OVERRIDE_OBJECTS and isinstance(child, Mapping):
            errors.extend(_override_errors(child, path))
        else:
            errors.append("PROFILE_OVERRIDE_UNKNOWN_PATH:" + ".".join(path))
    return errors


def validate_override_document(value: Any, *, section: bool = False) -> list[str]:
    """Validate a request override before it can enter the profile merge."""
    if not isinstance(value, Mapping):
        return ["PROFILE_OVERRIDE_INVALID:override must be an object"]
    if not section:
        return _override_errors(value)
    errors: list[str] = []
    for section_id, section_value in value.items():
        if not isinstance(section_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", section_id):
            errors.append(f"PROFILE_SECTION_OVERRIDE_INVALID:{section_id!r}")
        else:
            errors.extend(_override_errors(section_value))
    return errors


def deep_merge(base: Any, override: Any) -> Any:
    """Deterministically merge dictionaries while copying all input values."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key in sorted(override):
            out[key] = deep_merge(out[key], override[key]) if key in out else copy.deepcopy(override[key])
        return out
    return copy.deepcopy(override)


def _get_path(document: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = document
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def validate_hardening(base: Mapping[str, Any], resolved: Mapping[str, Any]) -> list[str]:
    """Ensure every allowed override preserves or tightens every hard gate."""
    violations: list[str] = []
    for path, rule in OVERRIDE_RULES.items():
        if rule not in ("increase", "decrease"):
            continue
        before = _get_path(base, path)
        after = _get_path(resolved, path)
        if before is None or after is None or not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue
        if rule == "increase" and after < before:
            violations.append("PROFILE_OVERRIDE_WEAKENS:" + ".".join(path))
        if rule == "decrease" and after > before:
            violations.append("PROFILE_OVERRIDE_WEAKENS:" + ".".join(path))

    hard_gates = resolved.get("hard_gates", {})
    typography = resolved.get("typography", {})
    illustrations = resolved.get("illustrations", {})
    layout = resolved.get("layout", {})
    font_resolution = resolved.get("font_resolution", {})
    response_areas = resolved.get("response_areas", {})
    box_geometry = resolved.get("box_geometry", {})
    if typography.get("english_body_weight") != "regular":
        violations.append("FONT_WEIGHT_INVALID")
    if hard_gates.get("orphan_hard_error") is not True:
        violations.append("PROFILE_OVERRIDE_WEAKENS:hard_gates.orphan_hard_error")
    if illustrations.get("embed_required") is not True:
        violations.append("PROFILE_OVERRIDE_WEAKENS:illustrations.embed_required")
    if illustrations.get("allow_placeholder_fallback", False) is not False:
        violations.append("PROFILE_OVERRIDE_WEAKENS:illustrations.allow_placeholder_fallback")
    for key in ("enumerate_all_ttc_faces", "require_embedded", "unknown_fallback_is_error", "content_required"):
        if key in font_resolution and font_resolution[key] is not True:
            violations.append("PROFILE_OVERRIDE_WEAKENS:font_resolution." + key)
    if response_areas.get("line_count_source", "answer_contract") != "answer_contract":
        violations.append("PROFILE_OVERRIDE_WEAKENS:response_areas.line_count_source")
    if response_areas.get("choice_extra_full_line", False) is not False:
        violations.append("PROFILE_OVERRIDE_WEAKENS:response_areas.choice_extra_full_line")
    if layout.get("keep_with_next") is not True:
        violations.append("PROFILE_OVERRIDE_WEAKENS:layout.keep_with_next")
    if set(layout.get("reading_matching_candidates", [])) != {"card-grid", "stacked", "dual-independent-flow"}:
        violations.append("MATCHING_LAYOUT_CANDIDATES_INVALID")
    if box_geometry and box_geometry.get("default_alignment") not in (None, "center"):
        # A prose card may opt into left alignment in IR; the profile default
        # for the box components covered by FR-15 remains centered.
        violations.append("DIAGRAM_ALIGNMENT")
    return violations


def schema_check(doc: Any) -> list[dict[str, Any]]:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from validate_json_schema import load_json, normalized_errors

        schema = load_json(SCHEMA)
        return normalized_errors(doc, schema)
    except Exception as exc:  # schema runtime failures are hard failures
        return [{"path": "$", "message": str(exc), "validator": "runtime"}]


def _font_files_from_roots(roots: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    suffixes = {".ttc", ".ttf", ".otf"}
    seen_real: set[Path] = set()
    for root in roots:
        path = Path(root).expanduser()
        if path.is_file() and path.suffix.casefold() in suffixes:
            try:
                real = path.resolve(strict=True)
                if real not in seen_real and not real.is_symlink():
                    files.append(real)
                    seen_real.add(real)
            except (OSError, RuntimeError):
                # Broken/cyclic symlinks are not font candidates.  Ignore
                # them deterministically and continue searching trusted roots.
                continue
        elif path.is_dir():
            try:
                candidates = sorted(path.rglob("*"))
            except (OSError, RuntimeError):
                candidates = []
            for candidate in candidates:
                if not candidate.is_file() or candidate.suffix.casefold() not in suffixes:
                    continue
                try:
                    real = candidate.resolve(strict=True)
                    if real.is_symlink() or real in seen_real:
                        continue
                    files.append(real)
                    seen_real.add(real)
                except (OSError, RuntimeError):
                    continue
    return sorted(set(files), key=lambda item: str(item))


def _records_from_file(path: Path, raw: bytes | None = None) -> tuple[dict[str, list[Path]], dict[str, bool], list[dict[str, Any]]]:
    if raw is None:
        raw = read_stable_file(path)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileResolutionError("FONT_RECORD_INVALID", f"font record document is invalid JSON: {exc}") from exc
    values = document.get("fonts", document) if isinstance(document, Mapping) else document
    if isinstance(values, Mapping):
        values = list(values.values())
    if not isinstance(values, list):
        raise ProfileResolutionError("FONT_RECORD_INVALID", "font record document must be an array or a fonts map")
    paths: dict[str, list[Path]] = {}
    embedding: dict[str, bool] = {}
    for record in values:
        if not isinstance(record, Mapping):
            raise ProfileResolutionError("FONT_RECORD_INVALID", "font record must be an object")
        token = str(record.get("token", ""))
        resolved_file = record.get("resolved_file")
        if not token or not isinstance(resolved_file, str):
            raise ProfileResolutionError("FONT_RECORD_INVALID", f"font record for {token!r} lacks resolved_file")
        try:
            resolved_font = Path(resolved_file).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProfileResolutionError("FONT_UNRESOLVED", f"font record for {token!r} points to an unavailable file: {exc}") from exc
        if record.get("embedded") is not True:
            raise ProfileResolutionError("FONT_NOT_EMBEDDED", f"font record for {token!r} is not embedded")
        paths[token] = [resolved_font]
        embedding[token] = True
    return paths, embedding, [dict(record) for record in values]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a hardening-only print profile and its optional font records.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--overrides")
    parser.add_argument("--section-overrides")
    parser.add_argument("--font-root", action="append", default=[], help="read-only font root; may be repeated")
    parser.add_argument("--font-records", help="JSON manifest of already resolved and embedded fonts")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        base_path, base_bytes = resolve_cli_file(args.base, label="base profile")
        raw_output = Path(args.output).expanduser()
        if not args.output or "\x00" in args.output:
            raise ProfileResolutionError("PROFILE_BINDING_INVALID", "output profile must be a regular file path")
        out = raw_output.resolve()
        if out == base_path:
            raise ProfileResolutionError("PROFILE_BINDING_INVALID", "output profile must differ from base profile")
        out.parent.mkdir(parents=True, exist_ok=True)
        base = parse_json_bytes(base_bytes, label="base profile")
        if not isinstance(base, Mapping):
            raise ProfileResolutionError("PROFILE_BINDING_INVALID", "base profile must be an object")
        base_schema_errors = schema_check(base)
        if base_schema_errors:
            print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": base_schema_errors}, ensure_ascii=False))
            return 1
        if args.overrides:
            _override_path, override_raw = resolve_cli_file(args.overrides, label="profile overrides")
            overrides = parse_json_bytes(override_raw, label="profile overrides")
        else:
            overrides = {}
        if args.section_overrides:
            _section_path, section_raw = resolve_cli_file(args.section_overrides, label="profile section overrides")
            section = parse_json_bytes(section_raw, label="profile section overrides")
        else:
            section = {}
        override_errors = validate_override_document(overrides)
        override_errors.extend(validate_override_document(section, section=True))
        if override_errors:
            print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": [{"code": error} for error in override_errors]}, ensure_ascii=False))
            return 1
        merged: Any = deep_merge(dict(base), overrides)
        if section:
            merged["sections"] = deep_merge(merged.get("sections", {}), {name: value for name, value in sorted(section.items())})
        if not isinstance(merged, Mapping):
            raise ProfileResolutionError("PROFILE_BINDING_INVALID", "resolved profile must be an object")
        errors = schema_check(merged)
        errors.extend({"path": "$", "message": error, "validator": "policy"} for error in validate_hardening(base, merged))
        # Section overrides are not a second policy language.  Evaluate each
        # section against the same base profile so a locally scoped change
        # cannot lower a font/DPI/embedding gate merely because the root
        # profile has no pre-existing ``sections`` object.
        if isinstance(section, Mapping):
            for section_id, section_value in section.items():
                section_profile = deep_merge(dict(base), section_value)
                errors.extend({"path": f"sections.{section_id}", "message": error, "validator": "policy"} for error in validate_hardening(base, section_profile))
        if errors:
            print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": errors}, ensure_ascii=False))
            return 1

        font_records: list[dict[str, Any]] = []
        if args.font_records:
            record_path, record_raw = resolve_cli_file(args.font_records, label="font records")
            record_paths, embedding, supplied_records = _records_from_file(record_path, record_raw)
            # Re-resolve metadata against the actual face rather than trusting
            # names supplied by a caller; then retain the supplied record only
            # as an additional audit input in stdout.
            font_records = resolve_fonts(merged, record_paths, embedding_status=embedding)
            for record in supplied_records:
                if not any(item["token"] == record.get("token") for item in font_records):
                    raise ProfileResolutionError("FONT_RECORD_INVALID", f"font record token {record.get('token')!r} is not in the profile")
        elif args.font_root:
            candidates = _font_files_from_roots(args.font_root)
            if not candidates:
                raise ProfileResolutionError("FONT_UNRESOLVED", "font roots contain no TTF, OTF or TTC files")
            # A renderer must provide embedding evidence.  The resolver does
            # not pretend that discovering a file means that a PDF embedded it.
            font_records = resolve_fonts(merged, candidates, embedding_status={})

        atomic_write_json(out, merged)
        result: dict[str, Any] = {"status": "PROFILE_RESOLVED", "profile_id": merged["profile_id"]}
        if font_records:
            result["fonts"] = font_records
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except FontResolutionError as exc:
        print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": [{"code": exc.code, "message": str(exc)}]}, ensure_ascii=False))
        return 1
    except ProfileResolutionError as exc:
        print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": [{"code": exc.code, "message": str(exc)}]}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "PROFILE_BINDING_INVALID", "errors": [{"code": "PROFILE_BINDING_INVALID", "message": str(exc)}]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
