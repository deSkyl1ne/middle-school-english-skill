#!/usr/bin/env python3
"""Standard-library helpers shared by the skill-local print runtime tools."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = SKILL_ROOT / "runtime"
REQUIREMENTS_PATH = RUNTIME_SOURCE / "requirements-print.txt"
LOCK_PATH = RUNTIME_SOURCE / "requirements-print.lock"
SHA256SUMS_PATH = RUNTIME_SOURCE / "SHA256SUMS"
RUNTIME_MARKER_NAME = "runtime.json"

PRINT_PACKAGE_SPECS = (
    {"distribution": "reportlab", "module": "reportlab", "version": "5.0.0"},
    {"distribution": "PyMuPDF", "module": "fitz", "version": "1.26.5"},
    {"distribution": "Pillow", "module": "PIL", "version": "11.3.0"},
    {"distribution": "jsonschema", "module": "jsonschema", "version": "4.25.1"},
)

PRINT_LOCK_SPECS = (
    ("attrs", "26.1.0"),
    ("charset-normalizer", "3.5.1"),
    ("jsonschema", "4.25.1"),
    ("jsonschema-specifications", "2025.9.1"),
    ("Pillow", "11.3.0"),
    ("PyMuPDF", "1.26.5"),
    ("referencing", "0.37.0"),
    ("reportlab", "5.0.0"),
    ("rpds-py", "2026.6.3"),
    ("typing-extensions", "4.16.0"),
)

PYTHON_INFO_CODE = r'''
import json
import platform
import sys
import sysconfig

print(json.dumps({
    "implementation": platform.python_implementation(),
    "major": sys.version_info.major,
    "minor": sys.version_info.minor,
    "micro": sys.version_info.micro,
    "version": platform.python_version(),
    "executable": sys.executable,
    "platform": sysconfig.get_platform(),
}, sort_keys=True))
'''

PRINT_IMPORT_CODE = r'''
import importlib
import importlib.metadata
import json
import platform
import sys

specs = [
    ("reportlab", "reportlab", "5.0.0"),
    ("PyMuPDF", "fitz", "1.26.5"),
    ("Pillow", "PIL", "11.3.0"),
    ("jsonschema", "jsonschema", "4.25.1"),
]
packages = {}
for distribution, module, expected_version in specs:
    record = {
        "distribution": distribution,
        "module": module,
        "expected_version": expected_version,
        "version": None,
        "imported": False,
        "ok": False,
    }
    try:
        importlib.import_module(module)
        record["imported"] = True
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
        packages[distribution] = record
        continue
    try:
        record["version"] = importlib.metadata.version(distribution)
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
    record["ok"] = record["imported"] and record["version"] == expected_version
    packages[distribution] = record

print(json.dumps({
    "ok": all(record.get("ok") is True for record in packages.values()) and len(packages) == len(specs),
    "python": platform.python_version(),
    "implementation": platform.python_implementation(),
    "packages": packages,
}, sort_keys=True))
'''


class RuntimeErrorCode(Exception):
    """An expected print runtime failure with a stable error code."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def clip(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def error_envelope(command: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    error = {"code": code, "message": message}
    error.update({key: value for key, value in details.items() if value is not None})
    payload = {
        "schema_version": "1.0.0",
        "command": command,
        "status": "ERROR",
        "ok": False,
        "error_code": code,
        "message": message,
        "error": error,
    }
    payload.update({key: value for key, value in details.items() if value is not None})
    return payload


def success_envelope(command: str, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "command": command,
        "status": status,
        "ok": True,
        **fields,
    }


def emit_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def default_runtime_root() -> Path:
    """Return the per-user cache location for generated print runtimes."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base).expanduser() / "middle-school-english" / "print-runtime"


def requirements_digest(path: Path = REQUIREMENTS_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_digest(
    requirements: Path = REQUIREMENTS_PATH,
    lock: Path = LOCK_PATH,
    sums: Path = SHA256SUMS_PATH,
) -> str:
    """Hash every packaged runtime input with explicit file boundaries."""
    digest = hashlib.sha256()
    for label, path in (
        ("requirements-print.txt", requirements),
        ("requirements-print.lock", lock),
        ("SHA256SUMS", sums),
    ):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def normalise_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).casefold()


def load_print_requirements(path: Path = REQUIREMENTS_PATH) -> tuple[tuple[str, str], ...]:
    """Load and validate the four fixed direct print requirements."""
    requirements: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeErrorCode("REQUIREMENTS_NOT_FOUND", "print requirements file is unavailable", {"path": str(path), "reason": str(exc)}) from exc
    for line_number, line in enumerate(lines, 1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if "==" not in value:
            raise RuntimeErrorCode("REQUIREMENTS_INVALID", "print requirements must use exact == pins", {"path": str(path), "line": line_number})
        name, version = (part.strip() for part in value.split("==", 1))
        if not name or not version:
            raise RuntimeErrorCode("REQUIREMENTS_INVALID", "print requirement is incomplete", {"path": str(path), "line": line_number})
        requirements.append((name, version))
    expected = tuple((spec["distribution"], spec["version"]) for spec in PRINT_PACKAGE_SPECS)
    if tuple((normalise_distribution(name), version) for name, version in requirements) != tuple((normalise_distribution(name), version) for name, version in expected):
        raise RuntimeErrorCode(
            "REQUIREMENTS_INVALID",
            "print requirements do not match the fixed four direct dependencies",
            {"path": str(path), "requirements": requirements, "expected": expected},
        )
    return tuple(requirements)


def load_lock_requirements(path: Path = LOCK_PATH) -> tuple[tuple[str, str], ...]:
    """Load the exact ten-package bundled wheel lock."""
    requirements: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeErrorCode("LOCK_NOT_FOUND", "print runtime lock file is unavailable", {"path": str(path), "reason": str(exc)}) from exc
    for line_number, line in enumerate(lines, 1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if "==" not in value:
            raise RuntimeErrorCode("LOCK_INVALID", "print runtime lock entries must use exact == pins", {"path": str(path), "line": line_number})
        name, version = (part.strip() for part in value.split("==", 1))
        if not name or not version:
            raise RuntimeErrorCode("LOCK_INVALID", "print runtime lock entry is incomplete", {"path": str(path), "line": line_number})
        requirements.append((name, version))
    expected = tuple(PRINT_LOCK_SPECS)
    actual = tuple((normalise_distribution(name), version) for name, version in requirements)
    normalised_expected = tuple((normalise_distribution(name), version) for name, version in expected)
    if actual != normalised_expected:
        raise RuntimeErrorCode(
            "LOCK_INVALID",
            "print runtime lock does not match the bundled ten-package set",
            {"path": str(path), "requirements": requirements, "expected": expected},
        )
    return tuple(requirements)


def resolve_python(value: str | None) -> Path:
    raw = str(value).strip() if value else sys.executable
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        try:
            return candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeErrorCode("PYTHON_NOT_FOUND", "requested Python executable is unavailable", {"python": raw, "reason": str(exc)}) from exc
    found = shutil.which(raw)
    if found:
        try:
            return Path(found).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeErrorCode("PYTHON_NOT_FOUND", "requested Python executable is unavailable", {"python": raw, "reason": str(exc)}) from exc
    raise RuntimeErrorCode("PYTHON_NOT_FOUND", "requested Python executable was not found", {"python": raw})


def _parse_json_output(text: str) -> dict[str, Any] | None:
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


def probe_python(python: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", PYTHON_INFO_CODE],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise RuntimeErrorCode("PYTHON_NOT_FOUND", "Python executable could not be started", {"python": str(python)}) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeErrorCode("PYTHON_NOT_FOUND", "Python executable probe failed", {"python": str(python), "reason": str(exc)}) from exc
    if result.returncode != 0:
        raise RuntimeErrorCode(
            "PYTHON_NOT_FOUND",
            "Python executable probe returned a non-zero exit code",
            {"python": str(python), "returncode": result.returncode, "stderr": clip(result.stderr)},
        )
    info = _parse_json_output(result.stdout)
    if info is None:
        raise RuntimeErrorCode(
            "PYTHON_NOT_FOUND",
            "Python executable probe did not return JSON",
            {"python": str(python), "stdout": clip(result.stdout), "stderr": clip(result.stderr)},
        )
    return info


def python_tag(info: Mapping[str, Any]) -> str:
    implementation = str(info.get("implementation", "")).casefold()
    try:
        major = int(info["major"])
        minor = int(info["minor"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeErrorCode("PYTHON_TAG_UNSUPPORTED", "Python version information is incomplete", {"python": dict(info)}) from exc
    if implementation != "cpython" or major != 3 or minor < 9:
        raise RuntimeErrorCode(
            "PYTHON_TAG_UNSUPPORTED",
            "print runtime supports CPython 3.9 and newer",
            {"implementation": implementation, "major": major, "minor": minor},
        )
    return f"cp{major}{minor}"


def normalise_platform_tag(value: str) -> str:
    """Normalise sysconfig's platform spelling for a safe cache key."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_").casefold()
    if not normalized:
        raise RuntimeErrorCode("PLATFORM_TAG_UNSUPPORTED", "Python platform tag is empty", {"platform": value})
    return normalized


def runtime_tag(info: Mapping[str, Any]) -> str:
    """Return the Python and platform identity used by the runtime cache."""
    py_tag = python_tag(info)
    platform_value = info.get("platform")
    if not isinstance(platform_value, str) or not platform_value.strip():
        raise RuntimeErrorCode("PLATFORM_TAG_UNSUPPORTED", "Python platform tag is unavailable", {"python": dict(info)})
    return f"{py_tag}-{normalise_platform_tag(platform_value)}"


def runtime_path(runtime_root: Path, digest: str, runtime_tag_value: str) -> Path:
    return runtime_root.expanduser().resolve() / digest / runtime_tag_value


def lexical_path(path: Path) -> Path:
    """Normalize an absolute path without resolving its final symlink."""
    return Path(os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path)))))


def has_symlink_component(path: Path, root: Path) -> bool:
    """Reject symlinks between a canonical runtime root and a target directory."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def runtime_python_path(runtime: Path) -> Path:
    name = "python.exe" if os.name == "nt" else "python"
    return runtime / ("Scripts" if os.name == "nt" else "bin") / name


def runtime_marker_path(runtime: Path) -> Path:
    return runtime / RUNTIME_MARKER_NAME


def bundled_wheelhouse(runtime_tag_value: str) -> Path:
    return RUNTIME_SOURCE / "wheelhouse" / runtime_tag_value


def probe_print_runtime(runtime_python: Path) -> tuple[dict[str, Any], str, str, int]:
    try:
        result = subprocess.run(
            [str(runtime_python), "-I", "-c", PRINT_IMPORT_CODE],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        return {"ok": False, "error_code": "RUNTIME_PYTHON_NOT_FOUND"}, "", "", 127
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error_code": "RUNTIME_PYTHON_FAILED", "error": str(exc)}, "", str(exc), 1
    payload = _parse_json_output(result.stdout)
    if payload is None:
        payload = {"ok": False, "error_code": "RUNTIME_PYTHON_FAILED", "error": "runtime import probe did not return JSON"}
    if result.returncode != 0 and "error_code" not in payload:
        payload["error_code"] = "RUNTIME_PYTHON_FAILED"
    return payload, result.stdout, result.stderr, result.returncode


def print_runtime_is_healthy(payload: Mapping[str, Any]) -> bool:
    if payload.get("ok") is not True:
        return False
    packages = payload.get("packages")
    if not isinstance(packages, Mapping):
        return False
    for spec in PRINT_PACKAGE_SPECS:
        record = packages.get(spec["distribution"])
        if not isinstance(record, Mapping):
            return False
        if record.get("imported") is not True or record.get("version") != spec["version"] or record.get("ok") is not True:
            return False
    return len(packages) == len(PRINT_PACKAGE_SPECS)


def marker_matches(marker: Path, digest: str, runtime_tag_value: str) -> bool:
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(value, Mapping)
        or value.get("requirements_digest") != digest
        or value.get("runtime_digest") != digest
        or value.get("runtime_tag") != runtime_tag_value
    ):
        return False
    py_tag, separator, platform_value = runtime_tag_value.partition("-")
    if not separator:
        return False
    return value.get("python_tag") == py_tag and value.get("platform_tag") == platform_value


def is_path_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False
