#!/usr/bin/env python3
"""Bootstrap a skill-local print runtime and run render plus preflight in it."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bootstrap_runtime
import runtime_doctor
from runtime_common import (
    REQUIREMENTS_PATH,
    RuntimeErrorCode,
    clip,
    default_runtime_root,
    emit_json,
    error_envelope,
    is_path_inside,
    success_envelope,
)


SCRIPTS = SCRIPT_DIR
RENDER_SCRIPT = SCRIPTS / "render_pdf.py"
PREFLIGHT_SCRIPT = SCRIPTS / "preflight_pdf.py"


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in list(environment):
        if key.upper() in {"PYTHONPATH", "PYTHONHOME"}:
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _run_child(stage: str, command: list[str], environment: Mapping[str, str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "stage": stage,
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "json": None,
            "error_code": "SUBPROCESS_FAILED",
        }
    return {
        "stage": stage,
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "json": _last_json(result.stdout),
    }


def _input_paths(request_value: str, bundle_value: str, runtime_root: Path) -> tuple[Path, Path, Path]:
    try:
        request = Path(request_value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeErrorCode("INPUT_MISSING", "render request was not found", {"request": request_value, "reason": str(exc)}) from exc
    if not request.is_file():
        raise RuntimeErrorCode("INPUT_INVALID", "render request is not a regular file", {"request": str(request)})
    source_root = request.parent
    bundle = Path(bundle_value).expanduser().resolve()
    if bundle == source_root or is_path_inside(bundle, source_root):
        raise RuntimeErrorCode(
            "INPUT_OUTPUT_OVERLAP",
            "bundle output must not be inside the request source directory",
            {"request_root": str(source_root), "bundle_out": str(bundle)},
        )
    raw_bundle = Path(bundle_value).expanduser()
    if raw_bundle.exists() or raw_bundle.is_symlink():
        raise RuntimeErrorCode(
            "BUNDLE_ALREADY_EXISTS",
            "bundle output must be a new path",
            {"bundle_out": str(raw_bundle.resolve())},
        )
    if bundle == runtime_root or is_path_inside(bundle, runtime_root):
        raise RuntimeErrorCode(
            "INPUT_OUTPUT_OVERLAP",
            "bundle output must not be inside the runtime directory",
            {"runtime_root": str(runtime_root), "bundle_out": str(bundle)},
        )
    if runtime_root == source_root or is_path_inside(runtime_root, source_root) or is_path_inside(source_root, runtime_root):
        raise RuntimeErrorCode(
            "INPUT_RUNTIME_OVERLAP",
            "runtime root must not be inside the request source directory",
            {"request_root": str(source_root), "runtime_root": str(runtime_root)},
        )
    return request, bundle, source_root


def run_print(
    request: str,
    bundle_out: str,
    runtime_root: str | Path | None = None,
    python: str | None = None,
    offline: bool = False,
    wheel_dir: str | None = None,
    requirements: Path = REQUIREMENTS_PATH,
) -> tuple[dict[str, Any], int]:
    root = (Path(runtime_root).expanduser() if runtime_root else default_runtime_root()).resolve()
    try:
        request_path, bundle_path, _source_root = _input_paths(request, bundle_out, root)
        boot = bootstrap_runtime.bootstrap(root, python, offline, wheel_dir, requirements)
        if boot.get("ok") is not True:
            return (
                error_envelope(
                    "run_print",
                    str(boot.get("error_code", "RUNTIME_BOOTSTRAP_FAILED")),
                    str(boot.get("message", "print runtime bootstrap failed")),
                    request=str(request_path),
                    bundle_out=str(bundle_path),
                    runtime=boot,
                    stages=[],
                ),
                1,
            )
        runtime_python = str(boot["runtime_python"])
        doctor = runtime_doctor.doctor_print(root, python, runtime_python, requirements)
        if doctor.get("ok") is not True:
            return (
                error_envelope(
                    "run_print",
                    str(doctor.get("error_code", "RUNTIME_DOCTOR_FAILED")),
                    str(doctor.get("message", "print runtime doctor failed")),
                    request=str(request_path),
                    bundle_out=str(bundle_path),
                    runtime=boot,
                    doctor=doctor,
                    stages=[],
                ),
                1,
            )

        try:
            bundle_path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RuntimeErrorCode("BUNDLE_ALREADY_EXISTS", "bundle output was claimed by another process", {"bundle_out": str(bundle_path)}) from exc

        environment = _child_environment()
        render_command = [
            runtime_python,
            "-E",
            "-s",
            str(RENDER_SCRIPT),
            "--request",
            str(request_path),
            "--bundle-out",
            str(bundle_path),
        ]
        render = _run_child("render", render_command, environment)
        stages = [render]
        if render["returncode"] != 0:
            return (
                error_envelope(
                    "run_print",
                    "RENDER_FAILED",
                    "render_pdf.py failed in the isolated runtime",
                    request=str(request_path),
                    bundle_out=str(bundle_path),
                    runtime=boot,
                    doctor=doctor,
                    stages=stages,
                ),
                int(render["returncode"] or 1),
            )

        preflight_command = [runtime_python, "-E", "-s", str(PREFLIGHT_SCRIPT), "--bundle", str(bundle_path)]
        preflight = _run_child("preflight", preflight_command, environment)
        stages.append(preflight)
        if preflight["returncode"] != 0:
            return (
                error_envelope(
                    "run_print",
                    "PREFLIGHT_FAILED",
                    "preflight_pdf.py failed in the isolated runtime",
                    request=str(request_path),
                    bundle_out=str(bundle_path),
                    runtime=boot,
                    doctor=doctor,
                    stages=stages,
                ),
                int(preflight["returncode"] or 1),
            )
        return (
            success_envelope(
                "run_print",
                "PRINT_COMPLETE",
                request=str(request_path),
                bundle_out=str(bundle_path),
                runtime=boot,
                doctor=doctor,
                stages=stages,
            ),
            0,
        )
    except RuntimeErrorCode as exc:
        return error_envelope("run_print", exc.code, exc.message, request=request, bundle_out=bundle_out, stages=[]), 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return error_envelope("run_print", "RUNTIME_WRAPPER_FAILED", str(exc), request=request, bundle_out=bundle_out, stages=[]), 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated print pipeline")
    parser.add_argument("--request", required=True, help="existing render_pdf request JSON")
    parser.add_argument("--bundle-out", required=True, help="new output bundle directory")
    parser.add_argument("--runtime-root", help="per-user runtime cache root")
    parser.add_argument("--python", help="Python executable used for runtime creation")
    parser.add_argument("--offline", action="store_true", help="install only from the local wheelhouse")
    parser.add_argument("--wheel-dir", help="offline wheelhouse directory")
    parser.add_argument("--requirements", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit the JSON envelope")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requirements = Path(args.requirements).expanduser().resolve() if args.requirements else REQUIREMENTS_PATH
    payload, returncode = run_print(args.request, args.bundle_out, args.runtime_root, args.python, args.offline, args.wheel_dir, requirements)
    emit_json(payload)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
