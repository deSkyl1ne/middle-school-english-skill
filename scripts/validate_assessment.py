#!/usr/bin/env python3
"""Validate a generated assessment against the published canonical references.

The validator intentionally uses only the Python standard library so that the
public Skill can run in a clean environment.  It validates the machine source
first and optionally checks rendered student, teacher, and answer-sheet files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


def _load_json_schema_module() -> Any | None:
    """Load the shared Draft 2020-12 runtime validator module.

    Prefers a plain ``import`` (scripts dir is on ``sys.path`` when running as
    a CLI) and falls back to loading the sibling script file so library callers
    that load this module via ``importlib`` still find it.
    """
    try:
        import validate_json_schema  # type: ignore

        return validate_json_schema
    except ImportError:
        pass
    try:
        candidate = Path(__file__).resolve().parent / "validate_json_schema.py"
        spec = importlib.util.spec_from_file_location("_skill_validate_json_schema", candidate)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError):
        return None


JSON_SCHEMA = _load_json_schema_module()
JSONSCHEMA_AVAILABLE = (
    JSON_SCHEMA is not None
    and getattr(JSON_SCHEMA, "Draft202012Validator", None) is not None
)


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCES = SKILL_ROOT / "references"
VALIDATOR_VERSION = "1.0.0"
MAX_UNPLANNED_PRIMARY_REUSE = 2
BOOK_ID_PATTERN = re.compile(r"^grade-\d{2}-semester-[12]$")
UNIT_ID_PATTERN = re.compile(r"^(?:starter|unit)-\d{2}$")
MACHINE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LEVELS = {"A", "B", "C", "D"}
PRIMARY_LEVELS = {"A", "B"}
OUTPUT_NAMES = {"student", "teacher", "answer_sheet"}
ASSESSMENT_ROOT_KEYS = {"schema_version", "assessment_id", "request", "blueprint", "items"}
REQUEST_KEYS = {
    "book_id",
    "unit_ids",
    "assessment_scope",
    "purpose",
    "duration_minutes",
    "total_score",
    "difficulty_target",
    "reinforcement",
    "item_type_plan",
    "outputs",
}
DIFFICULTIES = {"easy", "medium", "hard", "mixed"}
BLUEPRINT_TARGET_ROLES = {"primary", "context"}
ITEM_KEYS = {
    "item_id", "item_type", "stem", "passage", "prompt", "context", "script_outline",
    "speaker_roles", "task_sequence", "target_skills", "options", "blanks", "prompts",
    "tasks", "word_bank", "rubric", "answer", "rationale", "score", "difficulty",
    "canonical_item_ids", "context_item_ids", "stimulus_assets", "validation",
}
BLUEPRINT_KEYS = {"blueprint_id", "request", "catalog_status", "resolved_unit_ids", "sections", "coverage_targets", "score_check", "boundary_check"}

DEFAULT_ITEM_SPECS: dict[str, dict[str, Any]] = {
    "listening_blueprint": {
        "required": ["script_outline", "speaker_roles", "task_sequence", "target_skills", "score", "canonical_item_ids"],
        "answer_mode": "script_only",
    },
    "single_choice": {
        "required": ["stem", "options", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_option",
    },
    "cloze": {
        "required": ["passage", "blanks", "options", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_option_per_blank",
    },
    "reading_multiple_choice": {
        "required": ["passage", "stem", "options", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_option_per_question",
    },
    "reading_matching": {
        "required": ["passage", "prompts", "options", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_match_per_prompt",
    },
    "task_based_reading": {
        "required": ["passage", "tasks", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "structured_response",
    },
    "vocabulary_in_context": {
        "required": ["stem", "options", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_option_or_word",
    },
    "grammar_fill": {
        "required": ["stem", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "word_or_phrase",
    },
    "sentence_completion": {
        "required": ["stem", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "free_response",
    },
    "word_bank_fill": {
        "required": ["stem", "word_bank", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "one_word_per_blank",
    },
    "practical_writing": {
        "required": ["prompt", "rubric", "answer", "rationale", "score", "canonical_item_ids"],
        "answer_mode": "rubric_scored",
    },
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def to_decimal(value: Any) -> Decimal | None:
    if not is_number(value):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def number_for_json(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def normalize_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def normalized_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalized_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalized_json(item) for item in value]
    if isinstance(value, str):
        return normalize_text(value)
    return value


def nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def has_duplicates(values: list[Any]) -> bool:
    seen: set[str] = set()
    for value in values:
        try:
            marker = json.dumps(normalized_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            marker = repr(value)
        if marker in seen:
            return True
        seen.add(marker)
    return False


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def answer_values(answer: Any) -> list[Any]:
    """Flatten common answer representations while preserving sequence order."""

    if isinstance(answer, dict):
        for key in (
            "option_ids",
            "option_id",
            "selected_option_ids",
            "selected_option",
            "answers",
            "blank_answers",
            "match_answers",
            "responses",
            "response",
            "values",
            "matches",
        ):
            if key in answer:
                value = answer[key]
                if isinstance(value, dict):
                    return list(value.values())
                if isinstance(value, list):
                    return value
                return [value]
        return [answer] if answer else []
    if isinstance(answer, list):
        return answer
    if answer is None:
        return []
    return [answer]


def answer_option_ids(answer: Any) -> list[str]:
    values = answer_values(answer)
    return [str(value) for value in values if isinstance(value, (str, int)) and not isinstance(value, bool)]


def option_records(options: Any) -> list[tuple[str, str]]:
    if isinstance(options, dict):
        return [(str(key), str(value)) for key, value in options.items()]
    if not isinstance(options, list):
        return []
    records: list[tuple[str, str]] = []
    for index, option in enumerate(options):
        default_id = chr(65 + index)
        if isinstance(option, dict):
            option_id = option.get("option_id", default_id)
            text = option.get("text", "")
        else:
            option_id = default_id
            text = option
        records.append((str(option_id), str(text) if text is not None else ""))
    return records


def item_fingerprint(item: dict[str, Any]) -> str | None:
    content_keys = (
        "item_type",
        "passage",
        "stem",
        "prompt",
        "options",
        "blanks",
        "prompts",
        "tasks",
        "word_bank",
        "rubric",
    )
    content = {key: normalized_json(item.get(key)) for key in content_keys if key in item}
    if not any(nonempty(value) for value in content.values()):
        return None
    serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return serialized


def safe_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def issue(code: str, message: str, path: str | None = None, item_id: str | None = None, details: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if path:
        result["path"] = path
    if item_id:
        result["item_id"] = item_id
    if details is not None:
        result["details"] = details
    return result


class AssessmentValidator:
    def __init__(
        self,
        assessment: dict[str, Any],
        canonical_root: Path,
        output_paths: dict[str, Path] | None = None,
        allow_candidate: bool = False,
    ):
        self.assessment = assessment
        self.canonical_root = canonical_root
        self.output_paths = output_paths or {}
        self.allow_candidate = allow_candidate
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {
            "request": True,
            "scope": True,
            "blueprint": True,
            "canonical_references": True,
            "level_permissions": True,
            "answer_uniqueness": True,
            "score_arithmetic": True,
            "duplicates": True,
            "student_teacher_consistency": True,
        }
        self.request: dict[str, Any] = {}
        self.blueprint: dict[str, Any] = {}
        self.items: list[dict[str, Any]] = []
        self.book_id: str | None = None
        self.scope_unit_ids: list[str] = []
        self.reinforcement = False
        self.explicit_canonical_ids: set[str] = set()
        self.canonical: dict[str, Any] = {}
        self.registry: dict[str, dict[str, Any]] = dict(DEFAULT_ITEM_SPECS)
        self.actual_scores: list[Decimal] = []
        self.expected_slots: list[tuple[str, Decimal]] = []
        self.blueprint_slots: list[tuple[str, Decimal]] = []
        self.primary_reference_usage: Counter[str] = Counter()
        self.choice_positions: list[int] = []
        self.output_checked: list[str] = []

    def add_error(
        self,
        code: str,
        message: str,
        check: str,
        path: str | None = None,
        item_id: str | None = None,
        details: Any = None,
    ) -> None:
        self.errors.append(issue(code, message, path, item_id, details))
        normalized_check = check.replace(" ", "_")
        if normalized_check not in self.checks:
            normalized_check = "request"
        self.checks[normalized_check] = False

    def add_warning(self, code: str, message: str, path: str | None = None, details: Any = None) -> None:
        self.warnings.append(issue(code, message, path, details=details))

    def run(self) -> dict[str, Any]:
        self.unwrap_assessment()
        self.load_request_and_blueprint()
        self.validate_schema_shape()
        self.validate_draft2020_schema()
        self.load_registry()
        self.load_canonical_references()
        self.validate_request_and_scope()
        self.validate_blueprint()
        self.validate_items()
        self.validate_score_arithmetic()
        self.validate_coverage_targets()
        self.validate_answer_position_bias()
        self.validate_rendered_outputs()

        expected_score = self.expected_total()
        computed_score = sum(self.actual_scores, Decimal(0)) if self.actual_scores else Decimal(0)
        summary = {
            "assessment_id": self.assessment.get("assessment_id"),
            "book_id": self.book_id,
            "resolved_unit_ids": self.scope_unit_ids,
            "item_count": len(self.items),
            "canonical_reference_count": sum(len(list_value(item.get("canonical_item_ids"))) for item in self.items if isinstance(item, dict)),
            "primary_canonical_item_count": sum(
                1
                for item in self.items
                for canonical_id in list_value(item.get("canonical_item_ids"))
                if isinstance(canonical_id, str)
                and self.canonical.get("items_by_id", {}).get(canonical_id, {}).get("level") in PRIMARY_LEVELS
            ),
            "expected_score": number_for_json(expected_score),
            "computed_score": number_for_json(computed_score),
            "output_files_checked": self.output_checked,
            "checks": self.checks,
        }
        return {
            "schema_version": "1.0.0",
            "validator_version": VALIDATOR_VERSION,
            "status": "ASSESSMENT_VALIDATOR_PASS" if not self.errors else "ASSESSMENT_VALIDATOR_FAIL",
            "assessment_id": self.assessment.get("assessment_id"),
            "book_id": self.book_id,
            "errors": self.errors,
            "warnings": self.warnings,
            "summary": summary,
        }

    def validate_draft2020_schema(self) -> None:
        """Run the published Draft 2020-12 assessment schema (PRD FR-1).

        Schema violations are hard errors, never warnings or defaults.  When the
        jsonschema runtime is missing, the gate is skipped with a warning so the
        legacy API/CLI keeps working in dependency-free environments.
        """
        if not JSONSCHEMA_AVAILABLE:
            self.add_warning(
                "SCHEMA_RUNTIME_MISSING",
                "jsonschema Draft 2020-12 runtime is not installed; Draft 2020-12 schema gate skipped",
                "request",
            )
            return
        schema_path = JSON_SCHEMA.SCHEMA_ROOT / "assessment.schema.json"
        try:
            schema = JSON_SCHEMA.load_json(schema_path)
            meta_errors = JSON_SCHEMA.check_schema_meta(schema)
        except JSON_SCHEMA.SchemaRuntimeDependencyError:
            self.add_warning(
                "SCHEMA_RUNTIME_MISSING",
                "jsonschema Draft 2020-12 runtime is not installed; Draft 2020-12 schema gate skipped",
                "request",
            )
            return
        except OSError as exc:
            self.add_error("SCHEMA_INVALID", f"could not read assessment.schema.json: {exc}", "request", "$")
            return
        if meta_errors:
            self.add_error(
                "SCHEMA_INVALID",
                f"assessment.schema.json is not a valid Draft 2020-12 schema: {meta_errors[0]['message']}",
                "request",
                "$",
            )
            return
        try:
            violations = JSON_SCHEMA.normalized_errors(self.assessment, schema)
        except JSON_SCHEMA.SchemaRuntimeDependencyError:
            self.add_warning(
                "SCHEMA_RUNTIME_MISSING",
                "jsonschema Draft 2020-12 runtime is not installed; Draft 2020-12 schema gate skipped",
                "request",
            )
            return
        for error in violations:
            self.add_error(
                "SCHEMA_INVALID",
                f"Draft 2020-12 schema violation: {error['message']}",
                "request",
                JSON_SCHEMA.json_pointer_to_path(error["path"]),
            )

    def unwrap_assessment(self) -> None:
        if not isinstance(self.assessment, dict):
            self.assessment = {}
            self.add_error("ASSESSMENT_NOT_OBJECT", "assessment input must be a JSON object", "request", "$")
            return
        unknown_keys = sorted(set(self.assessment) - ASSESSMENT_ROOT_KEYS)
        if unknown_keys:
            self.add_error("ASSESSMENT_SCHEMA_FAIL", f"unknown assessment fields: {unknown_keys}", "request", "$")
        if "schema_version" in self.assessment and self.assessment.get("schema_version") != "1.0.0":
            self.add_error("ASSESSMENT_SCHEMA_FAIL", "schema_version must be 1.0.0", "request", "schema_version")
        assessment_id = self.assessment.get("assessment_id")
        if not isinstance(assessment_id, str) or not MACHINE_ID_PATTERN.fullmatch(assessment_id):
            self.add_error("ASSESSMENT_SCHEMA_FAIL", "assessment_id must match ^[a-z0-9][a-z0-9-]*$", "request", "assessment_id")
        if not isinstance(self.assessment.get("items"), list):
            self.add_error("REQUEST_MISSING_FIELD", "items must be an array", "request", "items")
            self.items = []
        elif not self.assessment["items"]:
            self.add_error("REQUEST_INVALID_FIELD", "items must contain at least one item", "request", "items")
            self.items = self.assessment["items"]
        else:
            self.items = self.assessment["items"]

    def load_request_and_blueprint(self) -> None:
        request = self.assessment.get("request")
        blueprint = self.assessment.get("blueprint")
        if not isinstance(request, dict):
            request = {}
            self.add_error("REQUEST_MISSING_FIELD", "request must be an object", "request", "request")
        if not isinstance(blueprint, dict):
            blueprint = {}
            self.add_error("BLUEPRINT_MISSING", "blueprint must be an object", "blueprint", "blueprint")
        self.request = request
        self.blueprint = blueprint
        self.book_id = request.get("book_id") if isinstance(request.get("book_id"), str) else None

    def schema_error(self, path: str, message: str) -> None:
        self.add_error("ASSESSMENT_SCHEMA_FAIL", message, "request", path)

    def schema_object(self, value: Any, required: set[str], allowed: set[str], path: str) -> bool:
        if not isinstance(value, dict):
            self.schema_error(path, "must be an object")
            return False
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - allowed)
        if missing:
            self.schema_error(path, f"is missing required fields: {missing}")
        if unknown:
            self.schema_error(path, f"contains unknown fields: {unknown}")
        return not missing and not unknown

    def schema_string(self, value: Any, path: str, pattern: re.Pattern[str] | None = None) -> None:
        if not isinstance(value, str) or not value:
            self.schema_error(path, "must be a non-empty string")
        elif pattern is not None and not pattern.fullmatch(value):
            self.schema_error(path, "does not match its required identifier pattern")

    def schema_number(self, value: Any, path: str) -> None:
        if to_decimal(value) is None or to_decimal(value) <= 0:
            self.schema_error(path, "must be a positive finite number")

    def schema_id_array(self, value: Any, path: str, pattern: re.Pattern[str] = MACHINE_ID_PATTERN) -> None:
        if not isinstance(value, list) or not value:
            self.schema_error(path, "must be a non-empty array")
            return
        if has_duplicates(value):
            self.schema_error(path, "must not contain duplicates")
        for index, item in enumerate(value):
            self.schema_string(item, f"{path}[{index}]", pattern)

    def schema_optional_id_array(self, value: Any, path: str, pattern: re.Pattern[str] = MACHINE_ID_PATTERN) -> None:
        if not isinstance(value, list):
            self.schema_error(path, "must be an array")
            return
        if has_duplicates(value):
            self.schema_error(path, "must not contain duplicates")
        for index, item in enumerate(value):
            self.schema_string(item, f"{path}[{index}]", pattern)

    def validate_request_schema(self, request: Any, path: str) -> None:
        if not self.schema_object(request, {"book_id", "purpose", "item_type_plan", "outputs"}, REQUEST_KEYS, path):
            return
        self.schema_string(request.get("book_id"), f"{path}.book_id", BOOK_ID_PATTERN)
        self.schema_string(request.get("purpose"), f"{path}.purpose")
        units, scope = request.get("unit_ids"), request.get("assessment_scope")
        if (units is None) == (scope is None):
            self.schema_error(path, "must contain exactly one of unit_ids or assessment_scope")
        if units is not None:
            self.schema_id_array(units, f"{path}.unit_ids", UNIT_ID_PATTERN)
        if scope is not None:
            self.schema_string(scope, f"{path}.assessment_scope")
        if "duration_minutes" in request and (not isinstance(request["duration_minutes"], int) or isinstance(request["duration_minutes"], bool) or request["duration_minutes"] < 1):
            self.schema_error(f"{path}.duration_minutes", "must be a positive integer")
        if "total_score" in request:
            self.schema_number(request["total_score"], f"{path}.total_score")
        if "difficulty_target" in request and request["difficulty_target"] not in DIFFICULTIES:
            self.schema_error(f"{path}.difficulty_target", "must be a registered difficulty")
        if "reinforcement" in request and not isinstance(request["reinforcement"], bool):
            self.schema_error(f"{path}.reinforcement", "must be boolean")
        outputs = request.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            self.schema_error(f"{path}.outputs", "must be a non-empty array")
        elif has_duplicates(outputs) or any(not isinstance(output, str) or output not in OUTPUT_NAMES for output in outputs):
            self.schema_error(f"{path}.outputs", "must contain unique registered outputs")
        plan = request.get("item_type_plan")
        if not isinstance(plan, list) or not plan:
            self.schema_error(f"{path}.item_type_plan", "must be a non-empty array")
        else:
            for index, line in enumerate(plan):
                line_path = f"{path}.item_type_plan[{index}]"
                if not self.schema_object(line, {"item_type", "item_count", "score_each"}, {"item_type", "item_count", "score_each"}, line_path):
                    continue
                if not isinstance(line.get("item_type"), str) or not line["item_type"]:
                    self.schema_error(f"{line_path}.item_type", "must be a non-empty string")
                if not isinstance(line.get("item_count"), int) or isinstance(line.get("item_count"), bool) or line["item_count"] < 1:
                    self.schema_error(f"{line_path}.item_count", "must be a positive integer")
                self.schema_number(line.get("score_each"), f"{line_path}.score_each")

    def validate_option(self, value: Any, path: str) -> None:
        if self.schema_object(value, {"option_id", "text"}, {"option_id", "text"}, path):
            self.schema_string(value.get("option_id"), f"{path}.option_id", re.compile(r"^[A-Z][A-Z0-9_-]*$"))
            self.schema_string(value.get("text"), f"{path}.text")

    def validate_options_shape(self, value: Any, path: str, minimum: int, maximum: int) -> None:
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            self.schema_error(path, f"must be an array containing {minimum} to {maximum} options")
            return
        for index, option in enumerate(value):
            self.validate_option(option, f"{path}[{index}]")

    def validate_blank(self, value: Any, path: str) -> None:
        if self.schema_object(value, {"blank_id", "position"}, {"blank_id", "position", "target"}, path):
            self.schema_string(value.get("blank_id"), f"{path}.blank_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
            if not isinstance(value.get("position"), int) or isinstance(value.get("position"), bool) or value["position"] < 1:
                self.schema_error(f"{path}.position", "must be a positive integer")
            if "target" in value:
                self.schema_string(value["target"], f"{path}.target")

    def validate_blank_answer(self, value: Any, path: str) -> None:
        if self.schema_object(value, {"blank_id", "value"}, {"blank_id", "value", "option_id"}, path):
            self.schema_string(value.get("blank_id"), f"{path}.blank_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
            self.schema_string(value.get("value"), f"{path}.value")
            if "option_id" in value:
                self.schema_string(value["option_id"], f"{path}.option_id", re.compile(r"^[A-Z][A-Z0-9_-]*$"))

    def validate_item_schema(self, item: Any, path: str) -> None:
        if not self.schema_object(item, {"item_id", "item_type", "score", "canonical_item_ids"}, ITEM_KEYS, path):
            return
        item_type = item.get("item_type")
        self.schema_string(item.get("item_id"), f"{path}.item_id", MACHINE_ID_PATTERN)
        if not isinstance(item_type, str) or item_type not in DEFAULT_ITEM_SPECS:
            self.schema_error(f"{path}.item_type", "must be a registered item type")
            return
        self.schema_number(item.get("score"), f"{path}.score")
        self.schema_id_array(item.get("canonical_item_ids"), f"{path}.canonical_item_ids")
        if "context_item_ids" in item:
            self.schema_optional_id_array(item["context_item_ids"], f"{path}.context_item_ids")
        if "difficulty" in item and item["difficulty"] not in {"easy", "medium", "hard"}:
            self.schema_error(f"{path}.difficulty", "must be a registered difficulty")
        if "validation" in item and not isinstance(item["validation"], dict):
            self.schema_error(f"{path}.validation", "must be an object")
        if item_type == "listening_blueprint":
            for field in ("answer", "rationale"):
                if field in item:
                    self.schema_error(f"{path}.{field}", "is forbidden for listening_blueprint")
            self.schema_string(item.get("script_outline"), f"{path}.script_outline")
            roles = item.get("speaker_roles")
            if not isinstance(roles, list) or not roles:
                self.schema_error(f"{path}.speaker_roles", "must be a non-empty array")
            else:
                for index, role in enumerate(roles):
                    role_path = f"{path}.speaker_roles[{index}]"
                    if self.schema_object(role, {"role", "purpose"}, {"role", "purpose"}, role_path):
                        self.schema_string(role.get("role"), f"{role_path}.role")
                        self.schema_string(role.get("purpose"), f"{role_path}.purpose")
            sequence = item.get("task_sequence")
            if not isinstance(sequence, list) or not sequence:
                self.schema_error(f"{path}.task_sequence", "must be a non-empty array")
            else:
                for index, step in enumerate(sequence):
                    step_path = f"{path}.task_sequence[{index}]"
                    if self.schema_object(step, {"step", "task_kind", "item_count", "score"}, {"step", "task_kind", "item_count", "score"}, step_path):
                        if not isinstance(step.get("step"), int) or isinstance(step.get("step"), bool) or step["step"] < 1:
                            self.schema_error(f"{step_path}.step", "must be a positive integer")
                        self.schema_string(step.get("task_kind"), f"{step_path}.task_kind")
                        if not isinstance(step.get("item_count"), int) or isinstance(step.get("item_count"), bool) or step["item_count"] < 1:
                            self.schema_error(f"{step_path}.item_count", "must be a positive integer")
                        self.schema_number(step.get("score"), f"{step_path}.score")
            skills = item.get("target_skills")
            if not isinstance(skills, list) or not skills or has_duplicates(skills):
                self.schema_error(f"{path}.target_skills", "must be a non-empty unique array")
            else:
                for index, skill in enumerate(skills):
                    self.schema_string(skill, f"{path}.target_skills[{index}]")
            return
        self.schema_string(item.get("rationale"), f"{path}.rationale")
        if item_type in {"single_choice", "reading_multiple_choice"}:
            if item_type == "reading_multiple_choice":
                self.schema_string(item.get("passage"), f"{path}.passage")
            self.schema_string(item.get("stem"), f"{path}.stem")
            self.validate_options_shape(item.get("options"), f"{path}.options", 2, 6)
            answer = item.get("answer")
            if self.schema_object(answer, {"option_ids"}, {"option_ids"}, f"{path}.answer"):
                values = answer.get("option_ids")
                if not isinstance(values, list) or len(values) != 1 or has_duplicates(values):
                    self.schema_error(f"{path}.answer.option_ids", "must contain exactly one unique option ID")
                else:
                    self.schema_string(values[0], f"{path}.answer.option_ids[0]", re.compile(r"^[A-Z][A-Z0-9_-]*$"))
        elif item_type == "cloze":
            self.schema_string(item.get("passage"), f"{path}.passage")
            blanks = item.get("blanks")
            if not isinstance(blanks, list) or not blanks:
                self.schema_error(f"{path}.blanks", "must be a non-empty array")
            else:
                for index, blank in enumerate(blanks):
                    self.validate_blank(blank, f"{path}.blanks[{index}]")
            option_sets = item.get("options")
            if not isinstance(option_sets, list) or not option_sets:
                self.schema_error(f"{path}.options", "must be a non-empty array")
            else:
                for index, option_set in enumerate(option_sets):
                    set_path = f"{path}.options[{index}]"
                    if self.schema_object(option_set, {"blank_id", "options"}, {"blank_id", "options"}, set_path):
                        self.schema_string(option_set.get("blank_id"), f"{set_path}.blank_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
                        self.validate_options_shape(option_set.get("options"), f"{set_path}.options", 2, 6)
            answer = item.get("answer")
            if self.schema_object(answer, {"blank_answers"}, {"blank_answers"}, f"{path}.answer"):
                values = answer.get("blank_answers")
                if not isinstance(values, list) or not values:
                    self.schema_error(f"{path}.answer.blank_answers", "must be a non-empty array")
                else:
                    for index, value in enumerate(values):
                        self.validate_blank_answer(value, f"{path}.answer.blank_answers[{index}]")
        elif item_type == "reading_matching":
            self.schema_string(item.get("passage"), f"{path}.passage")
            prompts = item.get("prompts")
            if not isinstance(prompts, list) or not prompts:
                self.schema_error(f"{path}.prompts", "must be a non-empty array")
            else:
                for index, prompt in enumerate(prompts):
                    prompt_path = f"{path}.prompts[{index}]"
                    if self.schema_object(prompt, {"prompt_id", "text"}, {"prompt_id", "text"}, prompt_path):
                        self.schema_string(prompt.get("prompt_id"), f"{prompt_path}.prompt_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
                        self.schema_string(prompt.get("text"), f"{prompt_path}.text")
            self.validate_options_shape(item.get("options"), f"{path}.options", 1, 20)
            answer = item.get("answer")
            if self.schema_object(answer, {"matches"}, {"matches"}, f"{path}.answer"):
                matches = answer.get("matches")
                if not isinstance(matches, list) or not matches:
                    self.schema_error(f"{path}.answer.matches", "must be a non-empty array")
                else:
                    for index, match in enumerate(matches):
                        match_path = f"{path}.answer.matches[{index}]"
                        if self.schema_object(match, {"prompt_id", "option_id"}, {"prompt_id", "option_id"}, match_path):
                            self.schema_string(match.get("prompt_id"), f"{match_path}.prompt_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
                            self.schema_string(match.get("option_id"), f"{match_path}.option_id", re.compile(r"^[A-Z][A-Z0-9_-]*$"))
        elif item_type == "task_based_reading":
            self.schema_string(item.get("passage"), f"{path}.passage")
            tasks = item.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                self.schema_error(f"{path}.tasks", "must be a non-empty array")
            else:
                for index, task in enumerate(tasks):
                    task_path = f"{path}.tasks[{index}]"
                    if self.schema_object(task, {"task_id", "prompt", "response_format", "score"}, {"task_id", "prompt", "response_format", "score", "evidence_requirements"}, task_path):
                        self.schema_string(task.get("task_id"), f"{task_path}.task_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
                        self.schema_string(task.get("prompt"), f"{task_path}.prompt")
                        self.schema_string(task.get("response_format"), f"{task_path}.response_format")
                        self.schema_number(task.get("score"), f"{task_path}.score")
                        if "evidence_requirements" in task:
                            requirements = task["evidence_requirements"]
                            if not isinstance(requirements, list):
                                self.schema_error(f"{task_path}.evidence_requirements", "must be an array")
                            else:
                                for requirement_index, requirement in enumerate(requirements):
                                    self.schema_string(requirement, f"{task_path}.evidence_requirements[{requirement_index}]")
            answer = item.get("answer")
            if self.schema_object(answer, {"responses"}, {"responses"}, f"{path}.answer"):
                responses = answer.get("responses")
                if not isinstance(responses, list) or not responses:
                    self.schema_error(f"{path}.answer.responses", "must be a non-empty array")
                else:
                    for index, response in enumerate(responses):
                        response_path = f"{path}.answer.responses[{index}]"
                        if self.schema_object(response, {"task_id", "response"}, {"task_id", "response"}, response_path):
                            self.schema_string(response.get("task_id"), f"{response_path}.task_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"))
        elif item_type == "vocabulary_in_context":
            self.schema_string(item.get("context"), f"{path}.context")
            self.schema_string(item.get("stem"), f"{path}.stem")
            self.validate_options_shape(item.get("options"), f"{path}.options", 2, 6)
            answer = item.get("answer")
            if isinstance(answer, dict) and set(answer) == {"value"}:
                self.schema_string(answer.get("value"), f"{path}.answer.value")
            elif self.schema_object(answer, {"option_ids"}, {"option_ids"}, f"{path}.answer"):
                values = answer.get("option_ids")
                if not isinstance(values, list) or len(values) != 1:
                    self.schema_error(f"{path}.answer.option_ids", "must contain exactly one option ID")
        elif item_type in {"grammar_fill", "sentence_completion"}:
            self.schema_string(item.get("stem"), f"{path}.stem")
            answer = item.get("answer")
            if self.schema_object(answer, {"primary", "accepted"}, {"primary", "accepted"}, f"{path}.answer"):
                self.schema_string(answer.get("primary"), f"{path}.answer.primary")
                accepted = answer.get("accepted")
                if not isinstance(accepted, list) or not accepted or has_duplicates(accepted):
                    self.schema_error(f"{path}.answer.accepted", "must be a non-empty unique array")
                else:
                    for index, value in enumerate(accepted):
                        self.schema_string(value, f"{path}.answer.accepted[{index}]")
        elif item_type == "word_bank_fill":
            self.schema_string(item.get("stem"), f"{path}.stem")
            blanks = item.get("blanks")
            if not isinstance(blanks, list) or not blanks:
                self.schema_error(f"{path}.blanks", "must be a non-empty array")
            else:
                for index, blank in enumerate(blanks):
                    self.validate_blank(blank, f"{path}.blanks[{index}]")
            bank = item.get("word_bank")
            if not isinstance(bank, list) or not bank or has_duplicates(bank):
                self.schema_error(f"{path}.word_bank", "must be a non-empty unique array")
            else:
                for index, word in enumerate(bank):
                    self.schema_string(word, f"{path}.word_bank[{index}]")
            answer = item.get("answer")
            if self.schema_object(answer, {"blank_answers"}, {"blank_answers"}, f"{path}.answer"):
                values = answer.get("blank_answers")
                if not isinstance(values, list) or not values:
                    self.schema_error(f"{path}.answer.blank_answers", "must be a non-empty array")
                else:
                    for index, value in enumerate(values):
                        self.validate_blank_answer(value, f"{path}.answer.blank_answers[{index}]")
        elif item_type == "practical_writing":
            self.schema_string(item.get("prompt"), f"{path}.prompt")
            rubric = item.get("rubric")
            if not isinstance(rubric, list) or not rubric:
                self.schema_error(f"{path}.rubric", "must be a non-empty array")
            else:
                for index, criterion in enumerate(rubric):
                    criterion_path = f"{path}.rubric[{index}]"
                    if self.schema_object(criterion, {"criterion", "points", "descriptor"}, {"criterion", "points", "descriptor"}, criterion_path):
                        self.schema_string(criterion.get("criterion"), f"{criterion_path}.criterion")
                        self.schema_number(criterion.get("points"), f"{criterion_path}.points")
                        self.schema_string(criterion.get("descriptor"), f"{criterion_path}.descriptor")
            answer = item.get("answer")
            if self.schema_object(answer, {"response"}, {"response", "accepted_elements"}, f"{path}.answer"):
                self.schema_string(answer.get("response"), f"{path}.answer.response")
                if "accepted_elements" in answer:
                    if not isinstance(answer["accepted_elements"], list):
                        self.schema_error(f"{path}.answer.accepted_elements", "must be an array")
                    else:
                        for index, value in enumerate(answer["accepted_elements"]):
                            self.schema_string(value, f"{path}.answer.accepted_elements[{index}]")

    def validate_schema_shape(self) -> None:
        self.validate_request_schema(self.request, "request")
        blueprint = self.blueprint
        if self.schema_object(
            blueprint,
            {"blueprint_id", "request", "resolved_unit_ids", "sections", "coverage_targets", "score_check"},
            BLUEPRINT_KEYS,
            "blueprint",
        ):
            self.schema_string(blueprint.get("blueprint_id"), "blueprint.blueprint_id", MACHINE_ID_PATTERN)
            if "catalog_status" in blueprint and blueprint["catalog_status"] not in {"released", "candidate"}:
                self.schema_error("blueprint.catalog_status", "must be released or candidate")
            self.validate_request_schema(blueprint.get("request"), "blueprint.request")
            self.schema_id_array(blueprint.get("resolved_unit_ids"), "blueprint.resolved_unit_ids", UNIT_ID_PATTERN)
            sections = blueprint.get("sections")
            if not isinstance(sections, list) or not sections:
                self.schema_error("blueprint.sections", "must be a non-empty array")
            else:
                for index, section in enumerate(sections):
                    section_path = f"blueprint.sections[{index}]"
                    if self.schema_object(section, {"item_type", "item_count", "score_each", "score_total"}, {"section_id", "item_type", "item_count", "score_each", "score_total"}, section_path):
                        if "section_id" in section:
                            self.schema_string(section["section_id"], f"{section_path}.section_id")
                        if not isinstance(section.get("item_type"), str) or not section["item_type"]:
                            self.schema_error(f"{section_path}.item_type", "must be a non-empty string")
                        if not isinstance(section.get("item_count"), int) or isinstance(section.get("item_count"), bool) or section["item_count"] < 1:
                            self.schema_error(f"{section_path}.item_count", "must be a positive integer")
                        self.schema_number(section.get("score_each"), f"{section_path}.score_each")
                        self.schema_number(section.get("score_total"), f"{section_path}.score_total")
            targets = blueprint.get("coverage_targets")
            if not isinstance(targets, list) or not targets:
                self.schema_error("blueprint.coverage_targets", "must be a non-empty array")
            else:
                for index, target in enumerate(targets):
                    target_path = f"blueprint.coverage_targets[{index}]"
                    if self.schema_object(target, {"canonical_item_id", "target_role", "planned_item_count"}, {"canonical_item_id", "target_role", "planned_item_count"}, target_path):
                        self.schema_string(target.get("canonical_item_id"), f"{target_path}.canonical_item_id")
                        if target.get("target_role") not in BLUEPRINT_TARGET_ROLES:
                            self.schema_error(f"{target_path}.target_role", "must be primary or context")
                        if not isinstance(target.get("planned_item_count"), int) or isinstance(target.get("planned_item_count"), bool) or target["planned_item_count"] < 1:
                            self.schema_error(f"{target_path}.planned_item_count", "must be a positive integer")
            score_check = blueprint.get("score_check")
            if self.schema_object(score_check, {"expected_total", "computed_total"}, {"expected_total", "computed_total"}, "blueprint.score_check"):
                self.schema_number(score_check.get("expected_total"), "blueprint.score_check.expected_total")
                self.schema_number(score_check.get("computed_total"), "blueprint.score_check.computed_total")
            if "boundary_check" in blueprint:
                boundary = blueprint["boundary_check"]
                if self.schema_object(boundary, {"allowed_primary_levels", "reinforcement", "context_only_level"}, {"allowed_primary_levels", "reinforcement", "context_only_level"}, "blueprint.boundary_check"):
                    levels = boundary.get("allowed_primary_levels")
                    if not isinstance(levels, list) or not levels or has_duplicates(levels) or any(level not in PRIMARY_LEVELS for level in levels):
                        self.schema_error("blueprint.boundary_check.allowed_primary_levels", "must be a non-empty unique A/B array")
                    if not isinstance(boundary.get("reinforcement"), bool):
                        self.schema_error("blueprint.boundary_check.reinforcement", "must be boolean")
                    if boundary.get("context_only_level") != "D":
                        self.schema_error("blueprint.boundary_check.context_only_level", "must be D")
        for index, item in enumerate(self.items):
            self.validate_item_schema(item, f"items[{index}]")

    def load_registry(self) -> None:
        registry_path = self.reference_dir() / "authoring" / "registry.json"
        if not registry_path.exists():
            return
        try:
            registry = load_json(registry_path)
        except (OSError, json.JSONDecodeError) as exc:
            self.add_warning("REGISTRY_UNREADABLE", f"could not read item registry: {exc}")
            return
        if not isinstance(registry, dict) or not isinstance(registry.get("item_types"), list):
            self.add_warning("REGISTRY_INVALID", "item registry is not an object with item_types")
            return
        loaded: dict[str, dict[str, Any]] = {}
        for entry in registry["item_types"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("item_type"), str):
                continue
            loaded[entry["item_type"]] = entry
        if loaded:
            self.registry = loaded

    def reference_dir(self) -> Path:
        root = self.canonical_root
        if (root / "references").is_dir():
            return root / "references"
        return root

    def load_canonical_references(self) -> None:
        root = self.reference_dir()
        catalog_path = root / "catalog.json"
        catalog: dict[str, Any] | None = None
        entries: list[dict[str, Any]] = []
        if catalog_path.exists():
            try:
                loaded_catalog = load_json(catalog_path)
                if isinstance(loaded_catalog, dict):
                    catalog = loaded_catalog
                    for key in ("supported_books", "candidate_books"):
                        if isinstance(catalog.get(key), list):
                            entries.extend(entry for entry in catalog[key] if isinstance(entry, dict))
            except (OSError, json.JSONDecodeError) as exc:
                self.add_error("CANONICAL_CATALOG_INVALID", f"could not read catalog.json: {exc}", "canonical_references", "catalog.json")
        if not self.book_id:
            return
        entry = next((candidate for candidate in entries if candidate.get("book_id") == self.book_id), None)
        data_path: Path | None = None
        catalog_status: str | None = None
        if entry:
            data_file = entry.get("data_file")
            if not isinstance(data_file, str):
                self.add_error("CANONICAL_CATALOG_INVALID", "catalog entry has no data_file", "canonical_references", self.book_id)
            else:
                data_path = root / data_file
                catalog_status = str(entry.get("status") or "")
                if not safe_path(data_path, root):
                    self.add_error("CANONICAL_PATH_INVALID", "catalog data_file escapes the reference directory", "canonical_references", data_file)
                    data_path = None
        else:
            fallback = root / f"{self.book_id}.json"
            if fallback.exists():
                data_path = fallback
                self.add_warning("CANONICAL_CATALOG_ENTRY_MISSING", "book was loaded from a direct canonical file without a catalog entry")
            else:
                self.add_error("CANONICAL_BOOK_NOT_FOUND", f"book {self.book_id!r} is not present in the canonical catalog", "canonical_references", self.book_id)
        if data_path is None or not data_path.exists():
            if data_path is not None:
                self.add_error("CANONICAL_BOOK_NOT_FOUND", f"canonical data file does not exist: {data_path.name}", "canonical_references", self.book_id)
            return
        try:
            data = load_json(data_path)
        except (OSError, json.JSONDecodeError) as exc:
            self.add_error("CANONICAL_DATA_INVALID", f"could not read canonical book: {exc}", "canonical_references", data_path.name)
            return
        if not isinstance(data, dict):
            self.add_error("CANONICAL_DATA_INVALID", "canonical book must be an object", "canonical_references", data_path.name)
            return
        book = data.get("book")
        actual_book_id = book.get("book_id") if isinstance(book, dict) else None
        if actual_book_id != self.book_id:
            self.add_error(
                "CANONICAL_BOOK_MISMATCH",
                f"canonical book_id {actual_book_id!r} does not match request {self.book_id!r}",
                "canonical_references",
                data_path.name,
            )
        if catalog_status == "candidate":
            if self.allow_candidate:
                self.add_warning("CANONICAL_CANDIDATE", "assessment is being checked against a candidate canonical book")
            else:
                self.add_error("CANONICAL_BOOK_NOT_RELEASED", "canonical book is a candidate and is not released", "canonical_references", self.book_id)
        units = data.get("units")
        items = data.get("items")
        if not isinstance(units, list) or not isinstance(items, list):
            self.add_error("CANONICAL_DATA_INVALID", "canonical book must contain units and items arrays", "canonical_references", data_path.name)
            return
        units_by_id: dict[str, dict[str, Any]] = {}
        for index, unit in enumerate(units):
            if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
                self.add_error("CANONICAL_DATA_INVALID", "canonical unit must have a string unit_id", "canonical_references", f"units[{index}]")
                continue
            unit_id = unit["unit_id"]
            if unit_id in units_by_id:
                self.add_error("CANONICAL_DUPLICATE_UNIT", f"duplicate canonical unit {unit_id}", "canonical_references", unit_id)
            units_by_id[unit_id] = unit
        items_by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(items):
            pointer = f"items[{index}]"
            if not isinstance(item, dict):
                self.add_error("CANONICAL_DATA_INVALID", "canonical item must be an object", "canonical_references", pointer)
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                self.add_error("CANONICAL_DATA_INVALID", "canonical item id must be a non-empty string", "canonical_references", pointer)
                continue
            if item_id in items_by_id:
                self.add_error("CANONICAL_DUPLICATE_ITEM", f"duplicate canonical item {item_id}", "canonical_references", item_id)
            items_by_id[item_id] = item
            unit_id = item.get("unit_id")
            if unit_id not in units_by_id:
                self.add_error("CANONICAL_DATA_INVALID", f"canonical item points to unknown unit {unit_id!r}", "canonical_references", pointer, item_id)
            if item.get("level") not in LEVELS:
                self.add_error("CANONICAL_DATA_INVALID", f"canonical item has invalid level {item.get('level')!r}", "canonical_references", pointer, item_id)
        self.canonical = {
            "data": data,
            "book": book if isinstance(book, dict) else {},
            "units_by_id": units_by_id,
            "items_by_id": items_by_id,
            "assessment_boundaries": data.get("assessment_boundaries", []),
            "data_path": data_path,
        }

    def validate_request_and_scope(self) -> None:
        request = self.request
        unknown_keys = sorted(set(request) - REQUEST_KEYS)
        if unknown_keys:
            self.add_error("ASSESSMENT_REQUEST_SCHEMA_FAIL", f"unknown request fields: {unknown_keys}", "request", "request")
        if not isinstance(request.get("book_id"), str):
            self.add_error("REQUEST_MISSING_FIELD", "request.book_id is required", "request", "request.book_id")
        elif not BOOK_ID_PATTERN.fullmatch(request["book_id"]):
            self.add_error("INVALID_BOOK_ID", f"invalid book_id {request['book_id']!r}", "scope", "request.book_id")
        elif self.book_id and request["book_id"] != self.book_id:
            self.add_error("BOOK_ID_MISMATCH", "request book_id does not match the resolved book", "scope", "request.book_id")
        for field in ("purpose", "outputs"):
            if field not in request:
                self.add_error("REQUEST_MISSING_FIELD", f"request.{field} is required", "request", f"request.{field}")
        if not isinstance(request.get("purpose"), str) or not request.get("purpose", "").strip():
            self.add_error("REQUEST_INVALID_FIELD", "request.purpose must be a non-empty string", "request", "request.purpose")
        request_total = to_decimal(request.get("total_score"))
        if "total_score" in request and (request_total is None or request_total <= 0):
            self.add_error("REQUEST_INVALID_FIELD", "request.total_score must be a positive finite number", "score arithmetic", "request.total_score")
        self.reinforcement = request.get("reinforcement", False)
        if not isinstance(self.reinforcement, bool):
            self.add_error("REQUEST_INVALID_FIELD", "request.reinforcement must be boolean", "request", "request.reinforcement")
            self.reinforcement = False
        duration = request.get("duration_minutes")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 1):
            self.add_error("REQUEST_INVALID_FIELD", "request.duration_minutes must be a positive integer", "request", "request.duration_minutes")
        difficulty = request.get("difficulty_target")
        if difficulty is not None and (not isinstance(difficulty, str) or difficulty not in DIFFICULTIES):
            self.add_error("REQUEST_INVALID_FIELD", f"unknown difficulty_target {difficulty!r}", "request", "request.difficulty_target")
        if isinstance(request.get("outputs"), list):
            if not request["outputs"]:
                self.add_error("REQUEST_INVALID_FIELD", "request.outputs must contain at least one output", "request", "request.outputs")
            if has_duplicates(request["outputs"]):
                self.add_error("REQUEST_INVALID_FIELD", "request.outputs must not contain duplicates", "request", "request.outputs")
            invalid_outputs = [name for name in request["outputs"] if not isinstance(name, str) or name not in OUTPUT_NAMES]
            if invalid_outputs:
                self.add_error("REQUEST_INVALID_FIELD", f"unknown output names: {invalid_outputs}", "request", "request.outputs")
        elif "outputs" in request:
            self.add_error("REQUEST_INVALID_FIELD", "request.outputs must be an array", "request", "request.outputs")

        unit_ids = request.get("unit_ids")
        assessment_scope = request.get("assessment_scope")
        if unit_ids is not None and assessment_scope is not None:
            self.add_error("INVALID_SCOPE", "request must use unit_ids or assessment_scope, not both", "scope", "request")
        if unit_ids is None and assessment_scope is None:
            self.add_error("INVALID_SCOPE", "request must include unit_ids or assessment_scope", "scope", "request")
        if unit_ids is not None:
            self.scope_unit_ids = self.validate_unit_ids(unit_ids, "request.unit_ids")
        elif assessment_scope is not None:
            self.scope_unit_ids = self.resolve_assessment_scope(assessment_scope)
        explicit_keys = (
            "canonical_item_ids",
            "named_canonical_item_ids",
            "explicit_canonical_item_ids",
            "target_canonical_item_ids",
        )
        for key in explicit_keys:
            values = request.get(key)
            if isinstance(values, list):
                self.explicit_canonical_ids.update(str(value) for value in values)
        plan = request.get("item_type_plan")
        if plan is None:
            self.add_error("REQUEST_MISSING_FIELD", "request.item_type_plan is required", "request", "request.item_type_plan")
        elif not isinstance(plan, list) or not plan:
            self.add_error("REQUEST_INVALID_FIELD", "request.item_type_plan must be a non-empty array", "request", "request.item_type_plan")
        else:
            self.expected_slots = self.slots_from_plan(plan, "request.item_type_plan")

    def validate_unit_ids(self, unit_ids: Any, path: str) -> list[str]:
        if not isinstance(unit_ids, list) or not unit_ids:
            self.add_error("INVALID_SCOPE", f"{path} must be a non-empty array", "scope", path)
            return []
        if has_duplicates(unit_ids):
            self.add_error("INVALID_SCOPE", f"{path} contains duplicate unit IDs", "scope", path)
        result: list[str] = []
        units_by_id = self.canonical.get("units_by_id", {})
        for index, unit_id in enumerate(unit_ids):
            if not isinstance(unit_id, str) or not UNIT_ID_PATTERN.fullmatch(unit_id):
                self.add_error("INVALID_SCOPE", f"invalid unit ID {unit_id!r}", "scope", f"{path}[{index}]")
                continue
            if units_by_id and unit_id not in units_by_id:
                self.add_error("INVALID_SCOPE", f"unit {unit_id!r} is not in the canonical book", "scope", f"{path}[{index}]")
                continue
            result.append(unit_id)
        return result

    def resolve_assessment_scope(self, assessment_scope: Any) -> list[str]:
        if not isinstance(assessment_scope, str) or not assessment_scope.strip():
            self.add_error("INVALID_SCOPE", "assessment_scope must be a non-empty string", "scope", "request.assessment_scope")
            return []
        boundaries = self.canonical.get("assessment_boundaries", [])
        matches = [
            boundary
            for boundary in boundaries
            if isinstance(boundary, dict) and boundary.get("assessment_type") == assessment_scope
        ]
        if not matches:
            self.add_error("ASSESSMENT_SCOPE_NOT_FOUND", f"no canonical assessment boundary named {assessment_scope!r}", "scope", "request.assessment_scope")
            return []
        boundary = next((entry for entry in matches if entry.get("scope_status") == "confirmed"), matches[0])
        if boundary.get("scope_status") != "confirmed":
            self.add_error("ASSESSMENT_SCOPE_UNCONFIRMED", f"assessment scope {assessment_scope!r} is not confirmed", "scope", "request.assessment_scope")
        return self.validate_unit_ids(boundary.get("covered_unit_ids"), "canonical.assessment_boundaries.covered_unit_ids")

    def slots_from_plan(self, plan: Any, path: str) -> list[tuple[str, Decimal]]:
        slots: list[tuple[str, Decimal]] = []
        if not isinstance(plan, list):
            return slots
        for index, entry in enumerate(plan):
            pointer = f"{path}[{index}]"
            if not isinstance(entry, dict):
                self.add_error("REQUEST_INVALID_FIELD", "item plan entry must be an object", "request", pointer)
                continue
            item_type = entry.get("item_type")
            count = entry.get("item_count")
            score_each = to_decimal(entry.get("score_each"))
            if not isinstance(item_type, str) or item_type not in self.registry:
                self.add_error("UNREGISTERED_ITEM_TYPE", f"unregistered item type {item_type!r}", "request", pointer)
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                self.add_error("REQUEST_INVALID_FIELD", "item_count must be a positive integer", "request", pointer)
                continue
            if score_each is None or score_each <= 0:
                self.add_error("REQUEST_INVALID_FIELD", "score_each must be a positive finite number", "score arithmetic", pointer)
                continue
            slots.extend((str(item_type), score_each) for _ in range(count))
        return slots

    def validate_blueprint(self) -> None:
        blueprint = self.blueprint
        if not blueprint:
            return
        unknown_keys = sorted(set(blueprint) - BLUEPRINT_KEYS)
        if unknown_keys:
            self.add_error("ASSESSMENT_SCHEMA_FAIL", f"unknown blueprint fields: {unknown_keys}", "blueprint", "blueprint")
        blueprint_id = blueprint.get("blueprint_id")
        if not isinstance(blueprint_id, str) or not MACHINE_ID_PATTERN.fullmatch(blueprint_id):
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint_id must match ^[a-z0-9][a-z0-9-]*$", "blueprint", "blueprint.blueprint_id")
        resolved = blueprint.get("resolved_unit_ids", blueprint.get("unit_ids"))
        if resolved is None:
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint.resolved_unit_ids is required", "scope", "blueprint.resolved_unit_ids")
        else:
            resolved_ids = self.validate_unit_ids(resolved, "blueprint.resolved_unit_ids")
            if resolved_ids != self.scope_unit_ids:
                self.add_error(
                    "BLUEPRINT_SCOPE_MISMATCH",
                    f"blueprint scope {resolved_ids!r} does not match request scope {self.scope_unit_ids!r}",
                    "scope",
                    "blueprint.resolved_unit_ids",
                )
        blueprint_request = blueprint.get("request")
        if not isinstance(blueprint_request, dict):
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint.request is required", "blueprint", "blueprint.request")
        elif normalized_json(blueprint_request) != normalized_json(self.request):
            self.add_error("BLUEPRINT_REQUEST_MISMATCH", "blueprint.request must exactly match root request", "blueprint", "blueprint.request")
        sections = blueprint.get("sections")
        if sections is None:
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint.sections is required", "blueprint", "blueprint.sections")
        else:
            if not isinstance(sections, list):
                self.add_error("BLUEPRINT_INVALID", "blueprint.sections must be an array", "blueprint", "blueprint.sections")
            elif not sections:
                self.add_error("BLUEPRINT_INVALID", "blueprint.sections must contain at least one section", "blueprint", "blueprint.sections")
            else:
                self.blueprint_slots = self.slots_from_sections(sections)
                if self.expected_slots and Counter(self.expected_slots) != Counter(self.blueprint_slots):
                    self.add_error("BLUEPRINT_PLAN_MISMATCH", "blueprint sections do not match request item_type_plan", "score arithmetic", "blueprint.sections")
                if not self.expected_slots:
                    self.expected_slots = list(self.blueprint_slots)
        score_check = blueprint.get("score_check")
        if score_check is None:
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint.score_check is required", "score arithmetic", "blueprint.score_check")
        elif not isinstance(score_check, dict):
            self.add_error("BLUEPRINT_INVALID", "blueprint.score_check must be an object", "blueprint", "blueprint.score_check")
        else:
            for field in ("expected_total", "computed_total"):
                if to_decimal(score_check.get(field)) is None:
                    self.add_error("BLUEPRINT_INVALID", f"blueprint.score_check.{field} must be a finite number", "score arithmetic", f"blueprint.score_check.{field}")
        coverage_targets = blueprint.get("coverage_targets")
        if coverage_targets is None:
            self.add_error("BLUEPRINT_MISSING_FIELD", "blueprint.coverage_targets is required", "blueprint", "blueprint.coverage_targets")
        elif not isinstance(coverage_targets, list):
            self.add_error("BLUEPRINT_INVALID", "blueprint.coverage_targets must be an array", "blueprint", "blueprint.coverage_targets")
        elif not coverage_targets:
            self.add_error("BLUEPRINT_INVALID", "blueprint.coverage_targets must contain at least one target", "blueprint", "blueprint.coverage_targets")
        else:
            self.explicit_canonical_ids.update(
                target.get("canonical_item_id")
                for target in coverage_targets
                if isinstance(target, dict) and isinstance(target.get("canonical_item_id"), str)
            )

    def slots_from_sections(self, sections: list[Any]) -> list[tuple[str, Decimal]]:
        slots: list[tuple[str, Decimal]] = []
        for index, section in enumerate(sections):
            pointer = f"blueprint.sections[{index}]"
            if not isinstance(section, dict):
                self.add_error("BLUEPRINT_INVALID", "section must be an object", "blueprint", pointer)
                continue
            item_type = section.get("item_type")
            count = section.get("item_count")
            score_each = to_decimal(section.get("score_each"))
            if not isinstance(item_type, str) or item_type not in self.registry:
                self.add_error("UNREGISTERED_ITEM_TYPE", f"unregistered item type {item_type!r}", "blueprint", pointer)
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or score_each is None or score_each <= 0:
                self.add_error("BLUEPRINT_INVALID", "section item_count and score_each must be positive", "score arithmetic", pointer)
                continue
            score_total = to_decimal(section.get("score_total"))
            expected_total = score_each * count
            if score_total is None:
                self.add_error("SCORE_ARITHMETIC_FAIL", "section score_total is required and must be finite", "score arithmetic", pointer)
            elif score_total != expected_total:
                self.add_error("SCORE_ARITHMETIC_FAIL", "section score_total does not equal item_count × score_each", "score arithmetic", pointer)
            slots.extend((str(item_type), score_each) for _ in range(count))
        return slots

    def validate_items(self) -> None:
        item_ids: set[str] = set()
        fingerprints: dict[str, str] = {}
        for index, item in enumerate(self.items):
            pointer = f"items[{index}]"
            if not isinstance(item, dict):
                self.add_error("ITEM_INVALID", "assessment item must be an object", "request", pointer)
                continue
            item_id = item.get("item_id")
            item_id_text = item_id if isinstance(item_id, str) else None
            if not item_id_text or not MACHINE_ID_PATTERN.fullmatch(item_id_text):
                self.add_error("ASSESSMENT_SCHEMA_FAIL", "item_id must match ^[a-z0-9][a-z0-9-]*$", "request", pointer)
            elif item_id_text in item_ids:
                self.add_error("DUPLICATE_ITEM_ID", f"duplicate item_id {item_id_text!r}", "duplicates", pointer, item_id_text)
            else:
                item_ids.add(item_id_text)
            item_type = item.get("item_type")
            if not isinstance(item_type, str) or item_type not in self.registry:
                self.add_error("UNREGISTERED_ITEM_TYPE", f"unregistered item type {item_type!r}", "request", pointer, item_id_text)
                spec: dict[str, Any] = {}
            else:
                spec = self.registry[item_type]
            unknown_item_keys = sorted(set(item) - ITEM_KEYS)
            if unknown_item_keys:
                self.add_error("ASSESSMENT_SCHEMA_FAIL", f"unknown item fields: {unknown_item_keys}", "request", pointer, item_id_text)
            for field in spec.get("required_fields", spec.get("required", [])):
                if field not in item:
                    self.add_error("ITEM_MISSING_FIELD", f"item is missing required field {field!r}", "request", f"{pointer}.{field}", item_id_text)
                elif field in {"stem", "passage", "prompt", "rationale"} and not nonempty(item.get(field)):
                    self.add_error("ITEM_INSUFFICIENT", f"item field {field!r} must be non-empty", "answer uniqueness", f"{pointer}.{field}", item_id_text)
            if item_type != "listening_blueprint" and ("answer" not in item or not nonempty(item.get("answer"))):
                self.add_error("ANSWER_MISSING", "formal item must contain a non-empty answer", "answer uniqueness", pointer, item_id_text)
            if item_type != "listening_blueprint" and not nonempty(item.get("rationale")):
                self.add_error("RATIONALE_MISSING", "formal item must contain a rationale", "answer uniqueness", pointer, item_id_text)
            score = to_decimal(item.get("score"))
            if score is None or score <= 0:
                self.add_error("SCORE_INVALID", "item score must be a positive finite number", "score arithmetic", pointer, item_id_text)
            else:
                self.actual_scores.append(score)
            canonical_ids = item.get("canonical_item_ids")
            context_ids = item.get("context_item_ids", [])
            if not isinstance(canonical_ids, list) or not canonical_ids:
                self.add_error("CANONICAL_REFERENCE_MISSING", "every formal item must cite canonical_item_ids", "canonical references", pointer, item_id_text)
                canonical_ids = []
            elif has_duplicates(canonical_ids):
                self.add_error("DUPLICATE_CANONICAL_REFERENCE", "canonical_item_ids must be unique within an item", "duplicates", pointer, item_id_text)
            if not isinstance(context_ids, list):
                self.add_error("CANONICAL_REFERENCE_INVALID", "context_item_ids must be an array", "canonical references", pointer, item_id_text)
                context_ids = []
            elif has_duplicates(context_ids):
                self.add_error("DUPLICATE_CANONICAL_REFERENCE", "context_item_ids must be unique within an item", "duplicates", pointer, item_id_text)
            if isinstance(canonical_ids, list) and isinstance(context_ids, list) and has_duplicates(canonical_ids + context_ids):
                self.add_error("DUPLICATE_CANONICAL_REFERENCE", "the same canonical item cannot be both primary and context for one item", "duplicates", pointer, item_id_text)
            self.validate_item_canonical_references(item, canonical_ids, context_ids, pointer, item_id_text)
            self.validate_item_answer(item, item_type, pointer, item_id_text)
            validation = item.get("validation")
            if isinstance(validation, dict):
                self.validate_claimed_flags(validation, pointer, item_id_text)
            fingerprint = item_fingerprint(item)
            if fingerprint:
                if fingerprint in fingerprints:
                    self.add_error(
                        "DUPLICATE_ITEM_CONTENT",
                        f"item duplicates {fingerprints[fingerprint]} after whitespace/case normalization",
                        "duplicates",
                        pointer,
                        item_id_text,
                    )
                else:
                    fingerprints[fingerprint] = item_id_text or pointer
        self.validate_canonical_reuse()

    def validate_item_canonical_references(
        self,
        item: dict[str, Any],
        canonical_ids: list[Any],
        context_ids: list[Any],
        pointer: str,
        item_id: str | None,
    ) -> None:
        items_by_id = self.canonical.get("items_by_id", {})
        primary_ab = 0
        for role, ids in (("primary", canonical_ids), ("context", context_ids)):
            ref_field = "canonical_item_ids" if role == "primary" else "context_item_ids"
            for ref_index, canonical_id in enumerate(ids):
                if not isinstance(canonical_id, str) or not canonical_id:
                    self.add_error("CANONICAL_REFERENCE_INVALID", "canonical references must be non-empty strings", "canonical references", f"{pointer}.{ref_field}[{ref_index}]", item_id)
                    continue
                reference = items_by_id.get(canonical_id)
                if reference is None:
                    self.add_error("INVALID_CANONICAL_ITEM", f"canonical item {canonical_id!r} does not exist", "canonical references", f"{pointer}.{ref_field}[{ref_index}]", item_id)
                    continue
                unit_id = reference.get("unit_id")
                if unit_id not in self.scope_unit_ids:
                    self.add_error("OUT_OF_SCOPE_CANONICAL_ITEM", f"canonical item {canonical_id!r} belongs to {unit_id!r}, outside the requested scope", "scope", f"{pointer}.{ref_field}[{ref_index}]", item_id)
                level = reference.get("level")
                if role == "primary":
                    self.primary_reference_usage[canonical_id] += 1
                    if level in PRIMARY_LEVELS and unit_id in self.scope_unit_ids:
                        primary_ab += 1
                    elif level == "C":
                        if not self.reinforcement and canonical_id not in self.explicit_canonical_ids:
                            self.add_error("LEVEL_PERMISSION_FAIL", f"C-level canonical item {canonical_id!r} requires reinforcement=true or an explicit named target", "level permissions", f"{pointer}.{ref_field}", item_id)
                    elif level == "D":
                        self.add_error("LEVEL_PERMISSION_FAIL", f"D-level canonical item {canonical_id!r} may be context only, not a primary target", "level permissions", f"{pointer}.{ref_field}", item_id)
                elif level not in LEVELS:
                    self.add_error("CANONICAL_REFERENCE_INVALID", f"canonical item {canonical_id!r} has no valid level", "canonical references", f"{pointer}.{ref_field}[{ref_index}]", item_id)
                if role == "context" and level == "C" and not self.reinforcement and canonical_id not in self.explicit_canonical_ids:
                    self.add_error("LEVEL_PERMISSION_FAIL", f"C-level context item {canonical_id!r} requires reinforcement=true or an explicit named target", "level permissions", f"{pointer}.{ref_field}", item_id)
        if canonical_ids and primary_ab == 0:
            self.add_error("NO_PRIMARY_CANONICAL", "each formal item must cite at least one in-scope A/B canonical item", "level permissions", pointer, item_id)
        declared_unit_id = item.get("unit_id")
        declared_unit_ids = item.get("unit_ids")
        if declared_unit_id is not None and declared_unit_id not in self.scope_unit_ids:
            self.add_error("OUT_OF_SCOPE_ITEM", f"item unit_id {declared_unit_id!r} is outside the requested scope", "scope", pointer, item_id)
        if isinstance(declared_unit_ids, list) and any(unit_id not in self.scope_unit_ids for unit_id in declared_unit_ids):
            self.add_error("OUT_OF_SCOPE_ITEM", "item unit_ids include a unit outside the requested scope", "scope", pointer, item_id)

    def validate_item_answer(self, item: dict[str, Any], item_type: Any, pointer: str, item_id: str | None) -> None:
        if not isinstance(item_type, str) or item_type not in self.registry or item_type == "listening_blueprint":
            return
        validation = item.get("validation")
        if isinstance(validation, dict) and validation.get("answer_unique") is False:
            self.add_error("ANSWER_NOT_UNIQUE", "item validation marks the answer as non-unique", "answer uniqueness", pointer, item_id)
        choice_types = {"single_choice", "reading_multiple_choice", "vocabulary_in_context"}
        if item_type in choice_types:
            records = option_records(item.get("options"))
            self.validate_options(records, pointer, item_id)
            if len(records) < 2:
                self.add_error("ANSWER_NOT_UNIQUE", "choice item must provide at least two options", "answer uniqueness", pointer, item_id)
            selected = answer_option_ids(item.get("answer"))
            if item_type == "vocabulary_in_context" and isinstance(item.get("answer"), dict) and isinstance(item["answer"].get("value"), str):
                answer_text = normalize_text(item["answer"]["value"])
                selected = [option_id for option_id, text in records if normalize_text(text) == answer_text]
                if not selected:
                    # The published Schema also permits a free word answer; it does
                    # not require that word to duplicate an option label.
                    return
            if len(selected) != 1:
                self.add_error("ANSWER_NOT_UNIQUE", "choice item must have exactly one selected option", "answer uniqueness", pointer, item_id, {"selected": selected})
            elif selected[0] not in {option_id for option_id, _ in records}:
                self.add_error("ANSWER_INVALID", f"answer option {selected[0]!r} is not present in options", "answer uniqueness", pointer, item_id)
            else:
                self.choice_positions.append(ord(selected[0][0].upper()) - 65 if selected[0] else -1)
            return
        if item_type == "cloze":
            blanks = item.get("blanks")
            blank_count = len(blanks) if isinstance(blanks, list) else 0
            option_sets = item.get("options")
            answers = item.get("answer", {}).get("blank_answers") if isinstance(item.get("answer"), dict) else None
            if not isinstance(option_sets, list) or not isinstance(answers, list):
                self.add_error("ANSWER_INVALID", "cloze options and answer must use Schema blank_id sets", "answer uniqueness", pointer, item_id)
                return
            options_by_blank: dict[str, set[str]] = {}
            for option_set in option_sets:
                if not isinstance(option_set, dict) or not isinstance(option_set.get("blank_id"), str):
                    self.add_error("OPTION_INVALID", "each cloze option set needs blank_id", "answer uniqueness", pointer, item_id)
                    continue
                records = option_records(option_set.get("options"))
                self.validate_options(records, pointer, item_id)
                options_by_blank[option_set["blank_id"]] = {option_id for option_id, _ in records}
            selected = [entry for entry in answers if isinstance(entry, dict)]
            if blank_count <= 0:
                self.add_error("ANSWER_NOT_UNIQUE", "cloze must declare at least one blank", "answer uniqueness", pointer, item_id)
            if len(selected) != blank_count:
                self.add_error("ANSWER_NOT_UNIQUE", "cloze must provide exactly one answer per blank", "answer uniqueness", pointer, item_id, {"blank_count": blank_count, "answer_count": len(selected)})
            blank_ids = {blank.get("blank_id") for blank in blanks if isinstance(blank, dict)}
            answer_ids = [entry.get("blank_id") for entry in selected]
            if len(answer_ids) != len(set(answer_ids)) or set(answer_ids) != blank_ids or set(options_by_blank) != blank_ids:
                self.add_error("ANSWER_INVALID", "cloze blanks, option sets, and answers must align by blank_id", "answer uniqueness", pointer, item_id)
            for entry in selected:
                option_id = entry.get("option_id")
                blank_id = entry.get("blank_id")
                if option_id is not None and option_id not in options_by_blank.get(blank_id, set()):
                    self.add_error("ANSWER_INVALID", "cloze answer option_id is not declared for its blank", "answer uniqueness", pointer, item_id)
            return
        if item_type == "reading_matching":
            records = option_records(item.get("options"))
            self.validate_options(records, pointer, item_id)
            prompts = item.get("prompts")
            prompt_count = len(prompts) if isinstance(prompts, list) else 0
            matches = item.get("answer", {}).get("matches") if isinstance(item.get("answer"), dict) else None
            if not isinstance(matches, list):
                self.add_error("ANSWER_INVALID", "reading_matching answer must contain matches", "answer uniqueness", pointer, item_id)
                return
            selected = [match.get("option_id") for match in matches if isinstance(match, dict)]
            prompt_ids = {prompt.get("prompt_id") for prompt in prompts if isinstance(prompt, dict)}
            answer_prompt_ids = [match.get("prompt_id") for match in matches if isinstance(match, dict)]
            if prompt_count <= 0 or len(selected) != prompt_count:
                self.add_error("ANSWER_NOT_UNIQUE", "reading matching must provide one answer per prompt", "answer uniqueness", pointer, item_id)
            if len(answer_prompt_ids) != len(set(answer_prompt_ids)) or set(answer_prompt_ids) != prompt_ids:
                self.add_error("ANSWER_INVALID", "reading matching answers must align with prompts by prompt_id", "answer uniqueness", pointer, item_id)
            if len(selected) != len(set(selected)):
                self.add_error("ANSWER_NOT_UNIQUE", "reading matching cannot reuse an option", "answer uniqueness", pointer, item_id)
            invalid = [value for value in selected if value not in {option_id for option_id, _ in records}]
            if invalid:
                self.add_error("ANSWER_INVALID", f"matching answer options are not declared: {invalid}", "answer uniqueness", pointer, item_id)
            return
        if item_type == "word_bank_fill":
            bank = item.get("word_bank")
            if not isinstance(bank, list) or not bank:
                self.add_error("ANSWER_NOT_UNIQUE", "word_bank_fill requires a non-empty word_bank", "answer uniqueness", pointer, item_id)
                return
            normalized_bank = [normalize_text(word) for word in bank]
            if len(normalized_bank) != len(set(normalized_bank)):
                self.add_error("DUPLICATE_OPTION_TEXT", "word_bank entries must be unique", "duplicates", pointer, item_id)
            blank_answers = item.get("answer", {}).get("blank_answers") if isinstance(item.get("answer"), dict) else None
            if not isinstance(blank_answers, list):
                self.add_error("ANSWER_INVALID", "word_bank_fill answer must contain blank_answers", "answer uniqueness", pointer, item_id)
                return
            selected = [normalize_text(entry.get("value")) for entry in blank_answers if isinstance(entry, dict)]
            blanks = item.get("blanks")
            expected_count = len(blanks) if isinstance(blanks, list) and blanks else 1
            if len(selected) != expected_count:
                self.add_error("ANSWER_NOT_UNIQUE", "word_bank_fill must provide one answer per blank", "answer uniqueness", pointer, item_id)
            blank_ids = {blank.get("blank_id") for blank in blanks if isinstance(blank, dict)}
            answer_ids = [entry.get("blank_id") for entry in blank_answers if isinstance(entry, dict)]
            if len(answer_ids) != len(set(answer_ids)) or set(answer_ids) != blank_ids:
                self.add_error("ANSWER_INVALID", "word_bank_fill answers must align with blanks by blank_id", "answer uniqueness", pointer, item_id)
            invalid = [value for value in selected if value not in normalized_bank]
            if invalid:
                self.add_error("ANSWER_INVALID", f"answers are not in the word bank: {invalid}", "answer uniqueness", pointer, item_id)
            return
        if item_type == "practical_writing":
            rubric = item.get("rubric")
            if not isinstance(rubric, (dict, list)) or not rubric:
                self.add_error("SCORING_RULE_INCOMPLETE", "practical_writing requires a non-empty executable rubric", "answer uniqueness", pointer, item_id)
            else:
                criteria = rubric.get("criteria", rubric.get("dimensions", [])) if isinstance(rubric, dict) else rubric
                if not isinstance(criteria, list) or not criteria:
                    self.add_error("SCORING_RULE_INCOMPLETE", "practical_writing rubric must contain criteria or dimensions", "answer uniqueness", pointer, item_id)
                else:
                    rubric_total = Decimal(0)
                    for criterion_index, criterion in enumerate(criteria):
                        criterion_pointer = f"{pointer}.rubric[{criterion_index}]"
                        if not isinstance(criterion, dict) or not nonempty(criterion.get("name", criterion.get("criterion"))):
                            self.add_error("SCORING_RULE_INCOMPLETE", "each rubric criterion needs a name", "answer uniqueness", criterion_pointer, item_id)
                            continue
                        maximum = to_decimal(criterion.get("points"))
                        if maximum is None or maximum <= 0:
                            self.add_error("SCORING_RULE_INCOMPLETE", "each rubric criterion needs positive points", "answer uniqueness", criterion_pointer, item_id)
                        else:
                            rubric_total += maximum
                    score = to_decimal(item.get("score"))
                    if score is not None and rubric_total and rubric_total != score:
                        self.add_error("SCORING_RULE_INCOMPLETE", "rubric maximum must equal the item score", "score arithmetic", pointer, item_id)
            return
        if item_type == "task_based_reading":
            tasks = item.get("tasks")
            answer = item.get("answer")
            if not isinstance(tasks, list) or not tasks:
                self.add_error("SCORING_RULE_INCOMPLETE", "task_based_reading requires at least one task", "answer uniqueness", pointer, item_id)
            if not isinstance(answer, (dict, list, str)):
                self.add_error("SCORING_RULE_INCOMPLETE", "task_based_reading answer must contain executable structured responses", "answer uniqueness", pointer, item_id)
            if isinstance(tasks, list) and tasks:
                task_ids: list[str] = []
                task_score = Decimal(0)
                for task_index, task in enumerate(tasks):
                    task_pointer = f"{pointer}.tasks[{task_index}]"
                    if not isinstance(task, dict):
                        self.add_error("SCORING_RULE_INCOMPLETE", "each task must be an object", "answer uniqueness", task_pointer, item_id)
                        continue
                    task_id = task.get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        self.add_error("SCORING_RULE_INCOMPLETE", "each task needs a non-empty task_id", "answer uniqueness", task_pointer, item_id)
                    else:
                        task_ids.append(task_id)
                    maximum = to_decimal(task.get("score", task.get("max_score")))
                    if maximum is None or maximum <= 0:
                        self.add_error("SCORING_RULE_INCOMPLETE", "each task needs a positive score", "score arithmetic", task_pointer, item_id)
                    else:
                        task_score += maximum
                if has_duplicates(task_ids):
                    self.add_error("DUPLICATE_TASK_ID", "task IDs must be unique", "duplicates", pointer, item_id)
                score = to_decimal(item.get("score"))
                if score is not None and task_score and task_score != score:
                    self.add_error("SCORING_RULE_INCOMPLETE", "task scores must add up to the item score", "score arithmetic", pointer, item_id)
                responses = answer.get("responses") if isinstance(answer, dict) else None
                if not isinstance(responses, list):
                    self.add_error("SCORING_RULE_INCOMPLETE", "structured task answers must contain responses", "answer uniqueness", pointer, item_id)
                else:
                    response_ids = [response.get("task_id") for response in responses if isinstance(response, dict)]
                    if len(response_ids) != len(set(response_ids)) or set(response_ids) != set(task_ids):
                        self.add_error("SCORING_RULE_INCOMPLETE", "structured task answers must cover every task_id exactly once", "answer uniqueness", pointer, item_id)

    def validate_canonical_reuse(self) -> None:
        planned: dict[str, int] = {}
        targets = self.blueprint.get("coverage_targets") if isinstance(self.blueprint, dict) else None
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, dict) or target.get("target_role", "primary") != "primary":
                    continue
                canonical_id = target.get("canonical_item_id")
                count = target.get("planned_item_count", 1)
                if isinstance(canonical_id, str) and isinstance(count, int) and count > 0:
                    planned[canonical_id] = count
        for canonical_id, usage in self.primary_reference_usage.items():
            limit = planned.get(canonical_id, MAX_UNPLANNED_PRIMARY_REUSE)
            if usage > limit:
                self.add_error(
                    "DUPLICATE_CANONICAL_OVERUSE",
                    f"primary canonical item {canonical_id!r} is cited {usage} times, above the planned limit {limit}",
                    "duplicates",
                    "canonical_item_ids",
                    details={"canonical_item_id": canonical_id, "usage": usage, "limit": limit},
                )

    def validate_options(self, records: list[tuple[str, str]], pointer: str, item_id: str | None) -> None:
        ids = [option_id for option_id, _ in records]
        texts = [normalize_text(text) for _, text in records]
        if len(ids) != len(set(ids)):
            self.add_error("DUPLICATE_OPTION_ID", "option IDs must be unique", "duplicates", pointer, item_id)
        if any(not text for text in texts):
            self.add_error("OPTION_INVALID", "option text must be non-empty", "answer uniqueness", pointer, item_id)
        if len(texts) != len(set(texts)):
            self.add_error("DUPLICATE_OPTION_TEXT", "option texts must be mutually distinct", "answer uniqueness", pointer, item_id)
        validation = self.items[int(pointer.split("[")[-1].split("]")[0])].get("validation") if pointer.startswith("items[") else None
        if isinstance(validation, dict) and validation.get("options_mutually_exclusive") is False:
            self.add_error("OPTIONS_NOT_MUTUALLY_EXCLUSIVE", "item validation marks options as non-exclusive", "answer uniqueness", pointer, item_id)

    def validate_claimed_flags(self, validation: dict[str, Any], pointer: str, item_id: str | None) -> None:
        flag_codes = {
            "stem_sufficient": ("STEM_INSUFFICIENT", "stem is marked insufficient"),
            "rationale_consistent": ("RATIONALE_INCONSISTENT", "rationale is marked inconsistent with the answer"),
            "originality": ("ORIGINALITY_FAIL", "item is marked as non-original"),
            "outputs_consistent": ("OUTPUT_CONSISTENCY_FAIL", "outputs are marked inconsistent"),
        }
        for flag, (code, message) in flag_codes.items():
            if validation.get(flag) is False:
                self.add_error(code, message, "answer uniqueness" if flag in {"stem_sufficient", "rationale_consistent"} else "student_teacher_consistency", pointer, item_id)

    def validate_score_arithmetic(self) -> None:
        if self.expected_slots:
            actual_slots = []
            for item in self.items:
                score = to_decimal(item.get("score"))
                if score is not None:
                    actual_slots.append((str(item.get("item_type")), score))
            if Counter(actual_slots) != Counter(self.expected_slots):
                self.add_error("SCORE_ARITHMETIC_FAIL", "item types, counts, or per-item scores do not match the request/blueprint plan", "score arithmetic", "items")
        expected_total = self.expected_total()
        computed_total = sum(self.actual_scores, Decimal(0)) if self.actual_scores else Decimal(0)
        if expected_total is None:
            self.add_error("SCORE_ARITHMETIC_FAIL", "no expected total score is available from request or blueprint", "score arithmetic", "score")
        elif computed_total != expected_total:
            self.add_error("SCORE_ARITHMETIC_FAIL", f"computed item score {computed_total} does not equal expected total {expected_total}", "score arithmetic", "items")
        score_check = self.blueprint.get("score_check") if isinstance(self.blueprint, dict) else None
        if isinstance(score_check, dict):
            declared_expected = to_decimal(score_check.get("expected_total"))
            declared_computed = to_decimal(score_check.get("computed_total"))
            if declared_expected is not None and expected_total is not None and declared_expected != expected_total:
                self.add_error("SCORE_ARITHMETIC_FAIL", "blueprint score_check.expected_total disagrees with the request", "score arithmetic", "blueprint.score_check.expected_total")
            if declared_computed is not None and declared_computed != computed_total:
                self.add_error("SCORE_ARITHMETIC_FAIL", "blueprint score_check.computed_total disagrees with item scores", "score arithmetic", "blueprint.score_check.computed_total")

    def expected_total(self) -> Decimal | None:
        request_total = to_decimal(self.request.get("total_score"))
        if request_total is not None:
            return request_total
        if self.expected_slots:
            return sum((score for _, score in self.expected_slots), Decimal(0))
        score_check = self.blueprint.get("score_check") if isinstance(self.blueprint, dict) else None
        if isinstance(score_check, dict):
            return to_decimal(score_check.get("expected_total"))
        return None

    def validate_coverage_targets(self) -> None:
        targets = self.blueprint.get("coverage_targets") if isinstance(self.blueprint, dict) else None
        if not isinstance(targets, list) or not targets:
            return
        items_by_id = self.canonical.get("items_by_id", {})
        seen_targets: set[str] = set()
        planned_total = 0
        for index, target in enumerate(targets):
            pointer = f"blueprint.coverage_targets[{index}]"
            if not isinstance(target, dict):
                self.add_error("BLUEPRINT_INVALID", "coverage target must be an object", "blueprint", pointer)
                continue
            canonical_id = target.get("canonical_item_id")
            if not isinstance(canonical_id, str) or not canonical_id:
                self.add_error("BLUEPRINT_INVALID", "coverage target canonical_item_id must be a non-empty string", "canonical references", pointer)
                continue
            if canonical_id in seen_targets:
                self.add_error("DUPLICATE_COVERAGE_TARGET", f"coverage target repeats canonical item {canonical_id!r}", "duplicates", pointer)
            seen_targets.add(canonical_id)
            reference = items_by_id.get(canonical_id)
            if reference is None:
                self.add_error("INVALID_CANONICAL_ITEM", f"coverage target references unknown canonical item {canonical_id!r}", "canonical references", pointer)
                continue
            if reference.get("unit_id") not in self.scope_unit_ids:
                self.add_error("OUT_OF_SCOPE_CANONICAL_ITEM", f"coverage target {canonical_id!r} is outside the requested scope", "scope", pointer)
            role = target.get("target_role", "primary")
            if not isinstance(role, str) or role not in BLUEPRINT_TARGET_ROLES:
                self.add_error("BLUEPRINT_INVALID", f"unknown coverage target role {role!r}", "blueprint", pointer)
                continue
            if role == "primary":
                level = reference.get("level")
                if level == "D":
                    self.add_error("LEVEL_PERMISSION_FAIL", f"primary coverage target {canonical_id!r} is D and may only be context", "level permissions", pointer)
                elif level == "C" and not self.reinforcement and canonical_id not in self.explicit_canonical_ids:
                    self.add_error("LEVEL_PERMISSION_FAIL", f"primary coverage target {canonical_id!r} requires reinforcement=true or an explicit named target", "level permissions", pointer)
                elif level not in PRIMARY_LEVELS and level != "C":
                    self.add_error("LEVEL_PERMISSION_FAIL", f"primary coverage target {canonical_id!r} has invalid level {level!r}", "level permissions", pointer)
            planned = target.get("planned_item_count", 1)
            if not isinstance(planned, int) or isinstance(planned, bool) or planned <= 0:
                self.add_error("BLUEPRINT_INVALID", "planned_item_count must be a positive integer", "blueprint", pointer)
                continue
            planned_total += planned
            if role == "primary" and self.primary_reference_usage.get(str(canonical_id), 0) < planned:
                self.add_error("COVERAGE_SHORTFALL", f"canonical target {canonical_id!r} is planned {planned} time(s) but cited {self.primary_reference_usage.get(str(canonical_id), 0)} time(s)", "canonical references", pointer)
        if planned_total != len(self.items):
            self.add_error("COVERAGE_PLAN_MISMATCH", f"coverage targets plan {planned_total} item(s), but assessment contains {len(self.items)} item(s)", "blueprint", "blueprint.coverage_targets")

    def validate_answer_position_bias(self) -> None:
        positions = [position for position in self.choice_positions if position >= 0]
        if len(positions) >= 4:
            counts = Counter(positions)
            most_common = counts.most_common(1)[0][1]
            if most_common / len(positions) >= 0.8:
                self.add_error("ANSWER_POSITION_SKEW", "choice answers are heavily concentrated in one option position", "answer uniqueness")

    def validate_rendered_outputs(self) -> None:
        requested_outputs = {name for name in self.request.get("outputs", []) if isinstance(name, str)}
        for name in sorted(requested_outputs - set(self.output_paths)):
            self.add_warning("OUTPUT_NOT_SUPPLIED", f"{name} was requested but no rendered output path was supplied to the validator")
        for name, path in self.output_paths.items():
            if not path:
                continue
            resolved = path
            if resolved.is_dir():
                filename = {"student": "student.md", "teacher": "teacher.md", "answer_sheet": "answer-sheet.json"}[name]
                resolved = resolved / filename
            if not resolved.exists():
                self.add_error("OUTPUT_NOT_FOUND", f"{name} output does not exist: {resolved}", "student_teacher_consistency", name)
                continue
            self.output_checked.append(name)
            try:
                if name == "student":
                    self.validate_student_output(resolved)
                elif name == "teacher":
                    self.validate_teacher_output(resolved)
                else:
                    self.validate_answer_sheet(resolved)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.add_error("OUTPUT_INVALID", f"could not read {name} output: {exc}", "student_teacher_consistency", str(resolved))

    def read_output(self, path: Path) -> tuple[Any, str]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.casefold() == ".json":
            return json.loads(text), text
        return text, text

    def output_assessment_id(self, value: Any, text: str) -> str | None:
        if isinstance(value, dict) and isinstance(value.get("assessment_id"), str):
            return value["assessment_id"]
        match = re.search(r"(?im)^\s*Assessment:\s*(\S+)\s*$", text)
        return match.group(1) if match else None

    def output_item_count(self, value: Any, text: str) -> int | None:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return len(value["items"])
        headings = re.findall(r"(?m)^\s*###\s+(\d+)\.\s+", text)
        return len(headings) if headings else None

    def validate_student_output(self, path: Path) -> None:
        value, text = self.read_output(path)
        output_id = self.output_assessment_id(value, text)
        if output_id and output_id != self.assessment.get("assessment_id"):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", "student output assessment_id does not match machine source", "student_teacher_consistency", str(path))
        count = self.output_item_count(value, text)
        if count != len(self.items):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", f"student output contains {count if count is not None else 'an unknown number of'} items; expected {len(self.items)}", "student_teacher_consistency", str(path))
        forbidden_keys = {"answer", "answers", "rationale", "canonical_item_ids", "context_item_ids", "validation"}
        if isinstance(value, dict):
            leaked = self.find_keys(value, forbidden_keys)
            if leaked:
                self.add_error("STUDENT_ANSWER_LEAK", f"student JSON exposes answer metadata: {sorted(leaked)}", "student_teacher_consistency", str(path))
        else:
            for line in text.splitlines():
                match = re.match(r"^\s*\*{0,2}(Answer|Rationale|Canonical items|Validation)\*{0,2}\s*:\s*(.*?)\s*$", line, re.IGNORECASE)
                if not match:
                    continue
                value_text = match.group(2).strip().strip("`")
                if match.group(1).casefold() == "answer" and not value_text.strip("_ .-—"):
                    continue
                self.add_error("STUDENT_ANSWER_LEAK", f"student output exposes {match.group(1).lower()} metadata", "student_teacher_consistency", str(path))
                break
            self.validate_rendered_item_fields(text, path, "student")

    def validate_teacher_output(self, path: Path) -> None:
        value, text = self.read_output(path)
        output_id = self.output_assessment_id(value, text)
        if output_id and output_id != self.assessment.get("assessment_id"):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", "teacher output assessment_id does not match machine source", "student_teacher_consistency", str(path))
        count = self.output_item_count(value, text)
        if count != len(self.items):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher output contains {count if count is not None else 'an unknown number of'} items; expected {len(self.items)}", "student_teacher_consistency", str(path))
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            self.validate_structured_teacher(value["items"], path)
            return
        blocks = re.split(r"(?m)^###\s+\d+\.\s+", text)[1:]
        if len(blocks) != len(self.items):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", "teacher output must provide one section per item", "student_teacher_consistency", str(path))
            return
        for index, item in enumerate(self.items):
            block = blocks[index]
            canonical_match = re.search(r"(?im)^\s*\*\*Canonical items:\*\*\s*(.+?)\s*$", block)
            if item.get("item_type") != "listening_blueprint":
                answer_match = re.search(r"(?im)^\s*\*\*Answer:\*\*\s*(.+?)\s*$", block)
                rationale_match = re.search(r"(?im)^\s*\*\*Rationale:\*\*\s*(.+?)\s*$", block)
                if answer_match is None or rationale_match is None:
                    self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher item {index + 1} is missing its answer or rationale", "student_teacher_consistency", str(path), str(item.get("item_id")))
                elif not self.answer_text_matches(item.get("answer"), answer_match.group(1)):
                    self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher answer row {index + 1} does not match machine source", "student_teacher_consistency", str(path), str(item.get("item_id")))
            expected_ids = item.get("canonical_item_ids", [])
            if canonical_match is None or any(str(canonical_id) not in canonical_match.group(1) for canonical_id in expected_ids):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher canonical row {index + 1} does not match machine source", "student_teacher_consistency", str(path), str(item.get("item_id")))
        self.validate_rendered_item_fields(text, path, "teacher")

    def validate_structured_teacher(self, output_items: list[Any], path: Path) -> None:
        for index, (expected, actual) in enumerate(zip(self.items, output_items)):
            if not isinstance(actual, dict):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher item {index + 1} is not an object", "student_teacher_consistency", str(path))
                continue
            if actual.get("item_id") != expected.get("item_id") or to_decimal(actual.get("score")) != to_decimal(expected.get("score")):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher item {index + 1} id or score differs from machine source", "student_teacher_consistency", str(path), str(expected.get("item_id")))
            if expected.get("item_type") != "listening_blueprint":
                if "answer" not in actual or not self.answer_values_equal(actual.get("answer"), expected.get("answer")):
                    self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher answer {index + 1} differs from machine source", "student_teacher_consistency", str(path), str(expected.get("item_id")))
                if not nonempty(actual.get("rationale")):
                    self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher item {index + 1} is missing a rationale", "student_teacher_consistency", str(path), str(expected.get("item_id")))
            if not nonempty(actual.get("canonical_item_ids")):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"teacher item {index + 1} is missing canonical references", "student_teacher_consistency", str(path), str(expected.get("item_id")))

    def validate_rendered_item_fields(self, text: str, path: Path, output_name: str) -> None:
        blocks = re.split(r"(?m)^###\s+\d+\.\s+", text)[1:]
        required_labels = {
            "listening_blueprint": ("**Script outline**", "**Speaker roles**", "**Task sequence**", "**Target skills:**"),
            "single_choice": ("**Question**",),
            "cloze": ("**Passage**", "**Blanks**", "**Options by blank**"),
            "reading_multiple_choice": ("**Passage**", "**Question**"),
            "reading_matching": ("**Passage**", "**Prompts**", "**Options**"),
            "task_based_reading": ("**Passage**", "**Tasks**"),
            "vocabulary_in_context": ("**Context**", "**Question**"),
            "grammar_fill": ("**Question**",),
            "sentence_completion": ("**Question**",),
            "word_bank_fill": ("**Question**", "**Blanks**", "**Word bank:**"),
            "practical_writing": ("**Writing task**", "**Rubric**"),
        }
        for index, item in enumerate(self.items):
            if index >= len(blocks):
                return
            labels = required_labels.get(item.get("item_type"), ())
            if output_name == "student" and item.get("item_type") in {"single_choice", "reading_multiple_choice", "vocabulary_in_context"}:
                labels = ()
            missing = [label for label in labels if label not in blocks[index]]
            if missing:
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"{output_name} output omits required rendered fields: {missing}", "student_teacher_consistency", str(path), str(item.get("item_id")))

    def validate_answer_sheet(self, path: Path) -> None:
        value, text = self.read_output(path)
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            self.add_error("OUTPUT_INVALID", "answer sheet must be a JSON object with an items array", "student_teacher_consistency", str(path))
            return
        output_id = value.get("assessment_id")
        if output_id and output_id != self.assessment.get("assessment_id"):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", "answer sheet assessment_id does not match machine source", "student_teacher_consistency", str(path))
        blueprint_id = self.blueprint.get("blueprint_id") if isinstance(self.blueprint, dict) else None
        if blueprint_id and value.get("blueprint_id") and value.get("blueprint_id") != blueprint_id:
            self.add_error("OUTPUT_CONSISTENCY_FAIL", "answer sheet blueprint_id does not match machine source", "student_teacher_consistency", str(path))
        entries = value["items"]
        if len(entries) != len(self.items):
            self.add_error("OUTPUT_CONSISTENCY_FAIL", f"answer sheet contains {len(entries)} rows; expected {len(self.items)}", "student_teacher_consistency", str(path))
        for index, expected in enumerate(self.items[: len(entries)]):
            actual = entries[index]
            if not isinstance(actual, dict):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"answer-sheet row {index + 1} is not an object", "student_teacher_consistency", str(path))
                continue
            if actual.get("item_number") not in (None, index + 1) or actual.get("item_id") != expected.get("item_id") or to_decimal(actual.get("score")) != to_decimal(expected.get("score")):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"answer-sheet row {index + 1} does not match machine source", "student_teacher_consistency", str(path), str(expected.get("item_id")))
            if "answer" in actual and not self.answer_values_equal(actual.get("answer"), expected.get("answer")):
                self.add_error("OUTPUT_CONSISTENCY_FAIL", f"answer-sheet answer {index + 1} differs from machine source", "student_teacher_consistency", str(path), str(expected.get("item_id")))

    def find_keys(self, value: Any, forbidden: set[str]) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden:
                    found.add(str(key))
                found.update(self.find_keys(child, forbidden))
        elif isinstance(value, list):
            for child in value:
                found.update(self.find_keys(child, forbidden))
        return found

    def answer_values_equal(self, left: Any, right: Any) -> bool:
        return normalized_json(left) == normalized_json(right)

    def answer_text_matches(self, expected: Any, actual_text: str) -> bool:
        candidate = actual_text.strip().strip("`")
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = candidate.strip('"\'')
        if self.answer_values_equal(parsed, expected):
            return True
        if isinstance(expected, str) and normalize_text(candidate) == normalize_text(expected):
            return True
        return False


def resolve_canonical_root(value: str | None) -> Path:
    path = Path(value).expanduser() if value else DEFAULT_REFERENCES
    if path.is_file():
        return path.parent
    return path


def collect_output_paths(assessment: dict[str, Any], args: argparse.Namespace) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for container_key in ("outputs", "rendered_outputs", "output_paths"):
        container = assessment.get(container_key)
        if not isinstance(container, dict):
            continue
        for name in OUTPUT_NAMES:
            value = container.get(name)
            if isinstance(value, dict):
                value = value.get("path")
            if isinstance(value, str):
                result.setdefault(name, Path(value).expanduser())
    for name, value in (
        ("student", getattr(args, "student", None)),
        ("teacher", getattr(args, "teacher", None)),
        ("answer_sheet", getattr(args, "answer_sheet", None)),
    ):
        if value:
            result[name] = Path(value).expanduser()
    return result


def validate_assessment(
    assessment: Any,
    canonical_root: Path | str | None = None,
    output_paths: dict[str, Path] | None = None,
    allow_candidate: bool = False,
) -> dict[str, Any]:
    document = assessment
    if not isinstance(document, dict):
        document = {}
    validator = AssessmentValidator(
        document,
        resolve_canonical_root(str(canonical_root) if canonical_root else None),
        output_paths,
        allow_candidate=allow_candidate,
    )
    return validator.run()


def emit(report: dict[str, Any], output: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _validate_report_schema(report: dict[str, Any], output: str | None) -> None:
    """Validate the produced content-validation report (PRD FR-1).

    The report is validated against the published assessment-validation schema
    after it is emitted.  A schema violation on a produced report is reported to
    stderr but does not change the validation result or exit code, because the
    report itself is a diagnostic artifact and the assessment result is already
    authoritative.  When the jsonschema runtime is missing the check is skipped
    so the legacy dependency-free CLI keeps working.
    """
    if JSONSCHEMA_AVAILABLE:
        try:
            schema = JSON_SCHEMA.load_json(JSON_SCHEMA.SCHEMA_ROOT / "assessment-validation.schema.json")
            errors = JSON_SCHEMA.normalized_errors(report, schema)
            if errors:
                path_hint = output or "<stdout>"
                print(
                    json.dumps(
                        {
                            "status": "REPORT_SCHEMA_INVALID",
                            "report_output": path_hint,
                            "errors": errors,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
        except (OSError, json.JSONDecodeError, JSON_SCHEMA.SchemaRuntimeDependencyError):
            pass


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a machine assessment and optional rendered outputs.")
    parser.add_argument("--input", "--assessment", dest="input_path", required=True, help="assessment.json path, or - for stdin")
    parser.add_argument("--canonical-root", help="references directory or Skill root; defaults to this Skill's references")
    parser.add_argument("--output", "--report", dest="output_path", help="write the JSON validation report to this path")
    parser.add_argument("--allow-candidate", action="store_true", help="allow candidate canonical books for local staging checks")
    parser.add_argument("--student", "--student-output", dest="student")
    parser.add_argument("--teacher", "--teacher-output", dest="teacher")
    parser.add_argument("--answer-sheet", "--answer-sheet-output", dest="answer_sheet")
    parser.add_argument("--include-candidates", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.input_path == "-":
            text = sys.stdin.read()
            assessment = json.loads(text)
        else:
            text = Path(args.input_path).expanduser().read_text(encoding="utf-8")
            assessment = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        report = {
            "schema_version": "1.0.0",
            "validator_version": VALIDATOR_VERSION,
            "status": "ASSESSMENT_VALIDATOR_FAIL",
            "assessment_id": None,
            "book_id": None,
            "errors": [issue("INPUT_INVALID", f"could not read assessment input: {exc}", "request", args.input_path)],
            "warnings": [],
            "summary": {
                "assessment_id": None,
                "book_id": None,
                "resolved_unit_ids": [],
                "item_count": 0,
                "canonical_reference_count": 0,
                "primary_canonical_item_count": 0,
                "expected_score": None,
                "computed_score": 0,
                "output_files_checked": [],
                "checks": {key: False if key == "request" else True for key in ("request", "scope", "blueprint", "canonical_references", "level_permissions", "answer_uniqueness", "score_arithmetic", "duplicates", "student_teacher_consistency")},
            },
        }
        emit(report, args.output_path)
        return 1
    output_paths = collect_output_paths(assessment if isinstance(assessment, dict) else {}, args)
    report = validate_assessment(
        assessment,
        args.canonical_root,
        output_paths,
        allow_candidate=args.allow_candidate or args.include_candidates,
    )
    emit(report, args.output_path)
    _validate_report_schema(report, args.output_path)
    return 0 if report["status"] == "ASSESSMENT_VALIDATOR_PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
