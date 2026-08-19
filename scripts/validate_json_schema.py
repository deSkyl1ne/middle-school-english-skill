#!/usr/bin/env python3
"""Draft 2020-12 runtime JSON schema validation shared by the print pipeline.

PRD FR-1: every JSON document read by a formal CLI must be validated against
its published Draft 2020-12 schema before it is processed.  Merely being
parseable by ``json.load`` -- or by the schema file itself -- is not a pass.

This module is the single shared validator used by the formal CLIs.  The only
runtime dependency (``jsonschema``, Draft 2020-12 support) is required: a
missing dependency is reported as ``SCHEMA_RUNTIME_DEPENDENCY`` with exit code
3 and is never silently downgraded to a permissive fallback.

Exit codes (PRD section 15):

- 0: the document conforms to its schema (SCHEMA_VALID);
- 1: the document parses but violates its schema (SCHEMA_INVALID);
- 2: CLI, path, missing file, or JSON/schema input is invalid;
- 3: the required jsonschema runtime dependency is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schema"

try:
    import jsonschema  # type: ignore
    from jsonschema import Draft202012Validator  # type: ignore
except ImportError:  # pragma: no cover - dependency gate
    jsonschema = None
    Draft202012Validator = None


class SchemaRuntimeDependencyError(RuntimeError):
    """Raised when the required jsonschema runtime dependency is unavailable."""


def require_runtime() -> None:
    """Fail hard when jsonschema (Draft 2020-12) is not installed."""
    if Draft202012Validator is None:
        raise SchemaRuntimeDependencyError(
            "the jsonschema package (Draft 2020-12 support) is required and not installed; "
            "install requirements-print.txt before running the print pipeline"
        )


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_schema(schema_arg: str) -> Path:
    """Resolve a schema argument as a file path or a packaged schema name."""
    path = Path(schema_arg).expanduser()
    if path.is_file():
        return path
    name = path.name
    for candidate in (SCHEMA_ROOT / name, SCHEMA_ROOT / f"{name}.schema.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"schema not found: {schema_arg!r} (no such file and not a packaged schema name)"
    )


def check_schema_meta(schema: Any) -> list[dict[str, Any]]:
    """Meta-validate that ``schema`` is itself a valid Draft 2020-12 schema."""
    require_runtime()
    errors: list[dict[str, Any]] = []
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        errors.append(
            {
                "path": json_pointer_to_path(exc.json_path or "$"),
                "message": str(exc),
                "validator": exc.validator or "",
            }
        )
    return errors


def json_pointer_to_path(pointer: str) -> str:
    """Convert a JSON Pointer (jsonschema error.json_path) to dotted/bracket path.

    ``/blueprint/sections/0`` -> ``blueprint.sections[0]``; the root -> ``$``.
    """
    if not pointer or pointer == "$":
        return "$"
    if pointer.startswith("/"):
        pointer = pointer[1:]
    parts: list[str] = []
    for segment in pointer.split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if segment.isdigit():
            parts.append(f"[{segment}]")
        elif not parts:
            parts.append(segment)
        else:
            parts.append(f".{segment}")
    result = "".join(parts)
    return result or "$"


def normalized_errors(instance: Any, schema: Any) -> list[dict[str, Any]]:
    """Validate ``instance`` against ``schema`` and return normalized errors.

    Raises :class:`SchemaRuntimeDependencyError` when jsonschema is missing.
    """
    require_runtime()
    # Older system jsonschema releases treat this package-relative root ID as
    # a new base for nested local refs and duplicate the package prefix.  The
    # refs in bundled schemas are all local, so removing only that in-memory
    # root ID keeps resolution deterministic without changing the published
    # schema contract.
    validation_schema = schema
    if isinstance(schema, dict) and isinstance(schema.get("$id"), str) and "://" not in schema["$id"]:
        validation_schema = dict(schema)
        validation_schema.pop("$id", None)
    validator = Draft202012Validator(validation_schema)
    errors: list[dict[str, Any]] = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path)):
        errors.append(
            {
                "path": error.json_path,
                "message": error.message,
                "validator": error.validator or "",
            }
        )
    return errors


def validate_document(instance: Any, schema: Any, schema_label: str) -> dict[str, Any]:
    """Return a structured report for ``instance`` against ``schema``."""
    errors = normalized_errors(instance, schema)
    return {
        "schema": schema_label,
        "status": "SCHEMA_VALID" if not errors else "SCHEMA_INVALID",
        "errors": errors,
    }


def emit(report: dict[str, Any], output: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a JSON document against a Draft 2020-12 schema.")
    parser.add_argument("--schema", required=True, help="path to the schema file or a packaged schema name")
    parser.add_argument("--instance", required=True, help="path to the JSON instance, or - for stdin")
    parser.add_argument("--output", help="write the JSON validation report to this path")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        schema_path = resolve_schema(args.schema)
        schema = load_json(schema_path)
        require_runtime()
        meta_errors = check_schema_meta(schema)
        if meta_errors:
            report = {
                "schema": args.schema,
                "status": "SCHEMA_INPUT_INVALID",
                "errors": [
                    {"path": error["path"], "message": f"schema file is not a valid Draft 2020-12 schema: {error['message']}", "validator": error["validator"]}
                    for error in meta_errors
                ],
            }
            emit(report, args.output)
            return 2
        if args.instance == "-":
            instance = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        else:
            instance = load_json(Path(args.instance).expanduser())
    except SchemaRuntimeDependencyError as exc:
        emit(
            {"schema": args.schema, "status": "SCHEMA_RUNTIME_DEPENDENCY", "errors": [{"message": str(exc)}]},
            args.output,
        )
        return 3
    except (OSError, FileNotFoundError, json.JSONDecodeError) as exc:
        emit(
            {"schema": args.schema, "status": "SCHEMA_INPUT_INVALID", "errors": [{"message": str(exc)}]},
            args.output,
        )
        return 2

    report = validate_document(instance, schema, args.schema)
    emit(report, args.output)
    return 0 if report["status"] == "SCHEMA_VALID" else 1


if __name__ == "__main__":
    sys.exit(main())
