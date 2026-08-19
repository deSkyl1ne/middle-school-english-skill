#!/usr/bin/env python3
"""Check the core interpreter or the isolated skill-local print interpreter."""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_common import (
    PRINT_PACKAGE_SPECS,
    REQUIREMENTS_PATH,
    RuntimeErrorCode,
    LOCK_PATH,
    SHA256SUMS_PATH,
    clip,
    default_runtime_root,
    emit_json,
    error_envelope,
    has_symlink_component,
    lexical_path,
    load_print_requirements,
    load_lock_requirements,
    marker_matches,
    probe_print_runtime,
    probe_python,
    print_runtime_is_healthy,
    python_tag,
    resolve_python,
    runtime_path,
    runtime_marker_path,
    runtime_python_path,
    runtime_digest,
    runtime_tag,
    success_envelope,
)


def doctor_core() -> dict[str, Any]:
    """Keep the core doctor free of all print-package imports."""
    return success_envelope(
        "runtime_doctor",
        "CORE_RUNTIME_OK",
        mode="core",
        python={
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
    )


def _package_failures(packages: Any) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    mismatched: list[str] = []
    if not isinstance(packages, Mapping):
        return [spec["distribution"] for spec in PRINT_PACKAGE_SPECS], []
    for spec in PRINT_PACKAGE_SPECS:
        record = packages.get(spec["distribution"])
        if not isinstance(record, Mapping) or record.get("imported") is not True:
            missing.append(spec["distribution"])
        elif record.get("version") != spec["version"]:
            mismatched.append(spec["distribution"])
    return missing, mismatched


def doctor_print(
    runtime_root: str | Path | None = None,
    python: str | None = None,
    runtime_python: str | Path | None = None,
    requirements: Path = REQUIREMENTS_PATH,
) -> dict[str, Any]:
    """Check exactly one isolated runtime and all four pinned print imports."""
    try:
        load_print_requirements(requirements)
        load_lock_requirements(LOCK_PATH)
        digest = runtime_digest(requirements, LOCK_PATH, SHA256SUMS_PATH)
        root = (Path(runtime_root).expanduser() if runtime_root else default_runtime_root()).resolve()
        base_info: dict[str, Any] | None = None
        if runtime_python:
            candidate_python = lexical_path(Path(runtime_python).expanduser())
            if not candidate_python.is_file():
                return error_envelope(
                    "runtime_doctor",
                    "RUNTIME_NOT_FOUND",
                    "requested runtime Python was not found",
                    mode="print",
                    runtime_root=str(root),
                    runtime_python=str(candidate_python),
                    requirements_digest=digest,
                    runtime_digest=digest,
                )
            runtime_info = probe_python(candidate_python)
            actual_python_tag = python_tag(runtime_info)
            actual_runtime_identity = runtime_tag(runtime_info)
            actual_platform_tag = actual_runtime_identity.split("-", 1)[1]
            target_runtime = runtime_path(root, digest, actual_runtime_identity)
            target_python = lexical_path(runtime_python_path(target_runtime))
            if candidate_python != target_python:
                return error_envelope(
                    "runtime_doctor",
                    "RUNTIME_PYTHON_MISMATCH",
                    "runtime Python is not the digest and platform isolated interpreter",
                    mode="print",
                    runtime_root=str(root),
                    runtime_path=str(target_runtime),
                    runtime_python=str(candidate_python),
                    expected_runtime_python=str(target_python),
                    requirements_digest=digest,
                    runtime_digest=digest,
                    python_tag=actual_python_tag,
                    platform_tag=actual_platform_tag,
                    runtime_tag=actual_runtime_identity,
                    python=runtime_info,
                )
        else:
            base_python = resolve_python(python)
            base_info = probe_python(base_python)
            expected_python_tag = python_tag(base_info)
            expected_runtime_identity = runtime_tag(base_info)
            expected_platform_tag = expected_runtime_identity.split("-", 1)[1]
            target_runtime = runtime_path(root, digest, expected_runtime_identity)
            target_python = lexical_path(runtime_python_path(target_runtime))
            if not target_python.is_file():
                return error_envelope(
                    "runtime_doctor",
                    "RUNTIME_NOT_FOUND",
                    "isolated print runtime Python was not found",
                    mode="print",
                    runtime_root=str(root),
                    runtime_path=str(target_runtime),
                    runtime_python=str(target_python),
                    requirements_digest=digest,
                    runtime_digest=digest,
                    python_tag=expected_python_tag,
                    platform_tag=expected_platform_tag,
                    runtime_tag=expected_runtime_identity,
                )
            runtime_info = probe_python(target_python)
            actual_python_tag = python_tag(runtime_info)
            actual_runtime_identity = runtime_tag(runtime_info)
            actual_platform_tag = actual_runtime_identity.split("-", 1)[1]
            if actual_runtime_identity != expected_runtime_identity:
                return error_envelope(
                    "runtime_doctor",
                    "RUNTIME_PLATFORM_MISMATCH",
                    "isolated runtime Python does not match the requested platform key",
                    mode="print",
                    runtime_root=str(root),
                    runtime_path=str(target_runtime),
                    runtime_python=str(target_python),
                    requirements_digest=digest,
                    runtime_digest=digest,
                    expected_runtime_tag=expected_runtime_identity,
                    actual_runtime_tag=actual_runtime_identity,
                    python=runtime_info,
                )

        if has_symlink_component(target_runtime, root):
            return error_envelope(
                "runtime_doctor",
                "RUNTIME_PATH_SYMLINK",
                "runtime path contains a symlinked intermediate directory",
                mode="print",
                runtime_root=str(root),
                runtime_path=str(target_runtime),
                runtime_python=str(target_python),
                requirements_digest=digest,
                runtime_digest=digest,
                runtime_tag=actual_runtime_identity,
            )

        marker = runtime_marker_path(target_runtime)
        if not marker_matches(marker, digest, actual_runtime_identity):
            return error_envelope(
                "runtime_doctor",
                "RUNTIME_MARKER_MISMATCH",
                "runtime marker does not match the requested digest and platform",
                mode="print",
                runtime_root=str(root),
                runtime_path=str(target_runtime),
                runtime_python=str(target_python),
                requirements_digest=digest,
                runtime_digest=digest,
                marker=str(marker),
                python_tag=actual_python_tag,
                platform_tag=actual_platform_tag,
                runtime_tag=actual_runtime_identity,
            )
        packages, probe_stdout, probe_stderr, probe_returncode = probe_print_runtime(target_python)
        missing, mismatched = _package_failures(packages.get("packages"))
        if not print_runtime_is_healthy(packages):
            if missing:
                code = "PRINT_IMPORT_MISSING"
                message = "one or more required print imports are missing"
            elif mismatched:
                code = "PRINT_VERSION_MISMATCH"
                message = "one or more required print package versions do not match"
            else:
                code = str(packages.get("error_code", "RUNTIME_PYTHON_FAILED"))
                message = "isolated print runtime probe failed"
            return error_envelope(
                "runtime_doctor",
                code,
                message,
                mode="print",
                runtime_root=str(root),
                runtime_path=str(target_runtime),
                runtime_python=str(target_python),
                requirements_digest=digest,
                runtime_digest=digest,
                python_tag=actual_python_tag,
                platform_tag=actual_platform_tag,
                runtime_tag=actual_runtime_identity,
                python=runtime_info,
                packages=packages.get("packages", {}),
                missing=missing,
                mismatched=mismatched,
                probe_returncode=probe_returncode,
                probe_stdout=clip(probe_stdout),
                probe_stderr=clip(probe_stderr),
            )
        return success_envelope(
            "runtime_doctor",
            "PRINT_RUNTIME_OK",
            mode="print",
            runtime_root=str(root),
            runtime_path=str(target_runtime),
            runtime_python=str(target_python),
            requirements_digest=digest,
            runtime_digest=digest,
            python_tag=actual_python_tag,
            platform_tag=actual_platform_tag,
            runtime_tag=actual_runtime_identity,
            python=runtime_info,
            packages=packages.get("packages", {}),
            base_python=base_info,
        )
    except RuntimeErrorCode as exc:
        details = dict(exc.details)
        details["mode"] = "print"
        return error_envelope("runtime_doctor", exc.code, exc.message, **details)
    except OSError as exc:
        return error_envelope("runtime_doctor", "RUNTIME_PYTHON_FAILED", str(exc), mode="print", exception_type=type(exc).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect core or isolated print runtime")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--core", action="store_true", help="check only the standard-library core runtime")
    modes.add_argument("--print", dest="print_mode", action="store_true", help="check the isolated print runtime")
    parser.add_argument("--runtime-root", help="per-user runtime cache root")
    parser.add_argument("--runtime-python", help=argparse.SUPPRESS)
    parser.add_argument("--python", help="base Python used to derive the runtime tag")
    parser.add_argument("--requirements", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit the JSON envelope")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.core:
        payload = doctor_core()
    else:
        requirements = Path(args.requirements).expanduser().resolve() if args.requirements else REQUIREMENTS_PATH
        payload = doctor_print(args.runtime_root, args.python, args.runtime_python, requirements)
    emit_json(payload)
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
