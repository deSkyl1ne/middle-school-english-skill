#!/usr/bin/env python3
"""Create or reuse the isolated, skill-local print virtual environment."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_common import (
    PRINT_PACKAGE_SPECS,
    REQUIREMENTS_PATH,
    RuntimeErrorCode,
    SHA256SUMS_PATH,
    LOCK_PATH,
    RUNTIME_SOURCE,
    clip,
    default_runtime_root,
    emit_json,
    error_envelope,
    bundled_wheelhouse,
    has_symlink_component,
    load_lock_requirements,
    load_print_requirements,
    marker_matches,
    normalise_distribution,
    normalise_platform_tag,
    print_runtime_is_healthy,
    probe_print_runtime,
    probe_python,
    python_tag,
    resolve_python,
    runtime_marker_path,
    runtime_digest,
    runtime_path,
    runtime_python_path,
    runtime_tag,
    success_envelope,
)


def _wheel_identity(path: Path) -> tuple[str, str, str, str, str] | None:
    """Read the five wheel filename fields needed for offline tag matching."""
    if path.suffix.casefold() != ".whl":
        return None
    fields = path.stem.split("-")
    if len(fields) < 5:
        return None
    distribution_version = "-".join(fields[:-3])
    python_tags, abi_tags, platform_tags = fields[-3:]
    return distribution_version, python_tags, abi_tags, platform_tags, path.name


def _wheel_matches_requirement(path: Path, distribution: str, version: str) -> bool:
    identity = _wheel_identity(path)
    if identity is None:
        return False
    expected = normalise_distribution(f"{distribution}-{version}")
    actual = normalise_distribution(identity[0])
    return actual == expected or actual.startswith(expected + "_")


def _wheel_supports_python(path: Path, tag: str) -> bool:
    identity = _wheel_identity(path)
    if identity is None:
        return False
    python_tags, abi_tags = identity[1].split("."), identity[2].split(".")
    if tag in python_tags or "py3" in python_tags:
        return True
    for candidate in python_tags:
        if candidate.startswith("py") and candidate[2:].isdigit() and candidate[2:] == tag[2:]:
            return True
        if candidate.startswith("cp") and candidate[2:].isdigit() and "abi3" in abi_tags:
            try:
                if int(candidate[2:]) <= int(tag[2:]):
                    return True
            except ValueError:
                continue
    return False


def _wheel_supports_platform(path: Path, platform_tag: str) -> bool:
    identity = _wheel_identity(path)
    if identity is None:
        return False
    for candidate in identity[3].split("."):
        try:
            normalized = normalise_platform_tag(candidate)
        except RuntimeErrorCode:
            continue
        if normalized in {"any", platform_tag}:
            return True
    return False


def find_offline_wheels(wheel_dirs: list[Path], tag: str, runtime_tag_value: str | None = None) -> dict[str, Any]:
    """Verify direct wheels before invoking pip in ``--offline`` mode."""
    wheels = sorted({path for directory in wheel_dirs if directory.is_dir() for path in directory.rglob("*.whl")})
    missing: list[dict[str, Any]] = []
    selected: dict[str, str] = {}
    platform_value = runtime_tag_value.split("-", 1)[1] if runtime_tag_value and "-" in runtime_tag_value else None
    for spec in PRINT_PACKAGE_SPECS:
        candidates = [
            path
            for path in wheels
            if _wheel_matches_requirement(path, spec["distribution"], spec["version"])
        ]
        compatible = [
            path
            for path in candidates
            if _wheel_supports_python(path, tag) and (platform_value is None or _wheel_supports_platform(path, platform_value))
        ]
        if compatible:
            selected[spec["distribution"]] = str(compatible[0])
        else:
            missing.append(
                {
                    "distribution": spec["distribution"],
                    "version": spec["version"],
                    "available": [path.name for path in candidates],
                    "reason": "no wheel supports the requested Python tag" if candidates else "no matching wheel was found",
                }
            )
    if missing:
        identity_fields: dict[str, str] = {}
        if runtime_tag_value and "-" in runtime_tag_value:
            identity_fields["platform_tag"] = runtime_tag_value.split("-", 1)[1]
            identity_fields["runtime_tag"] = runtime_tag_value
        raise RuntimeErrorCode(
            "WHEEL_NOT_FOUND",
            "offline print runtime is missing compatible wheels",
            {"python_tag": tag, **identity_fields, "wheel_dirs": [str(path) for path in wheel_dirs], "missing": missing},
        )
    return {"selected": selected, "wheel_dirs": [str(path) for path in wheel_dirs]}


def default_wheel_dirs(explicit: str | None, runtime_tag_value: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    if runtime_tag_value:
        bundled = bundled_wheelhouse(runtime_tag_value)
        if bundled.is_dir():
            return [bundled]
    candidates = [RUNTIME_SOURCE / "wheels"]
    wheelhouse_root = RUNTIME_SOURCE / "wheelhouse"
    if wheelhouse_root.is_dir() and any(path.is_file() and path.suffix.casefold() == ".whl" for path in wheelhouse_root.iterdir()):
        candidates.append(wheelhouse_root)
    return [path for path in candidates if path.is_dir()]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sha256sums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel checksum manifest is unavailable", {"path": str(path), "reason": str(exc)}) from exc
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        fields = value.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel checksum manifest has an invalid entry", {"path": str(path), "line": line_number})
        relative = fields[1].lstrip("*")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\x00" in relative:
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel checksum path escapes the runtime", {"path": str(path), "line": line_number})
        key = relative_path.as_posix()
        if key in checksums:
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel checksum manifest contains a duplicate path", {"path": str(path), "wheel": key})
        checksums[key] = fields[0].casefold()
    return checksums


def verify_bundled_wheelhouse(
    wheel_dir: Path,
    runtime_tag_value: str,
    lock_path: Path = LOCK_PATH,
    sums_path: Path = SHA256SUMS_PATH,
) -> dict[str, Any]:
    """Validate the bundled lock, wheel set, and checksums before pip runs."""
    locked = load_lock_requirements(lock_path)
    if not wheel_dir.is_dir():
        raise RuntimeErrorCode("WHEEL_NOT_FOUND", "bundled wheelhouse is unavailable", {"runtime_tag": runtime_tag_value, "wheel_dir": str(wheel_dir)})
    py_tag, separator, platform_value = runtime_tag_value.partition("-")
    if not separator:
        raise RuntimeErrorCode("PLATFORM_TAG_UNSUPPORTED", "bundled wheelhouse runtime tag is incomplete", {"runtime_tag": runtime_tag_value})
    wheels = sorted(path for path in wheel_dir.rglob("*.whl") if path.is_file())
    missing: list[dict[str, Any]] = []
    selected: dict[str, str] = {}
    for distribution, version in locked:
        candidates = [path for path in wheels if _wheel_matches_requirement(path, distribution, version)]
        compatible = [
            path
            for path in candidates
            if _wheel_supports_python(path, py_tag) and _wheel_supports_platform(path, platform_value)
        ]
        if compatible:
            selected[distribution] = str(compatible[0])
        else:
            missing.append(
                {
                    "distribution": distribution,
                    "version": version,
                    "available": [path.name for path in candidates],
                    "reason": "no compatible wheel was found" if candidates else "no matching wheel was found",
                }
            )
    if missing:
        raise RuntimeErrorCode(
            "WHEEL_NOT_FOUND",
            "bundled print wheelhouse is missing compatible locked wheels",
            {
                "runtime_tag": runtime_tag_value,
                "python_tag": py_tag,
                "platform_tag": platform_value,
                "wheel_dir": str(wheel_dir),
                "missing": missing,
            },
        )

    checksums = _load_sha256sums(sums_path)
    checksum_root = sums_path.resolve().parent
    for wheel in wheels:
        try:
            key = wheel.resolve().relative_to(checksum_root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel path is outside the checksum manifest root", {"wheel": str(wheel), "reason": str(exc)}) from exc
        expected = checksums.get(key)
        if expected is None:
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel is missing from SHA256SUMS", {"wheel": key})
        actual = _sha256_file(wheel)
        if actual != expected:
            raise RuntimeErrorCode("WHEEL_HASH_MISMATCH", "wheel SHA256 does not match SHA256SUMS", {"wheel": key, "expected": expected, "actual": actual})
    return {
        "runtime_tag": runtime_tag_value,
        "wheel_dir": str(wheel_dir),
        "lock": str(lock_path),
        "sums": str(sums_path),
        "selected": selected,
        "hash_verified": True,
    }


def runtime_lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.bootstrap.lock"


@contextmanager
def exclusive_runtime_lock(path: Path):
    """Hold an OS-level lock across one digest/tag bootstrap transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        locked = False
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            while not locked:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    time.sleep(0.05)
                    handle.seek(0)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        try:
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeErrorCode("RUNTIME_BOOTSTRAP_FAILED", "runtime subprocess could not be started", {"command": command, "reason": str(exc)}) from exc


def _marker_payload(digest: str, runtime_tag_value: str, base_python: Path, python_info: Mapping[str, Any], runtime_python: Path) -> dict[str, Any]:
    py_tag, platform_value = runtime_tag_value.split("-", 1)
    return {
        "schema_version": "1.0.0",
        "requirements_digest": digest,
        "runtime_digest": digest,
        "python_tag": py_tag,
        "platform_tag": platform_value,
        "runtime_tag": runtime_tag_value,
        "base_python": str(base_python),
        "python": dict(python_info),
        "runtime_python": str(runtime_python),
    }


def _bootstrap_locked(
    *,
    digest: str,
    base_python: Path,
    python_info: Mapping[str, Any],
    py_tag: str,
    runtime_identity: str,
    root: Path,
    target: Path,
    target_python: Path,
    marker: Path,
    offline: bool,
    wheel_dir: str | None,
    requirements: Path,
) -> dict[str, Any]:
    if wheel_dir is not None:
        requested_wheel_dir = Path(wheel_dir).expanduser().resolve()
        bundled = bundled_wheelhouse(runtime_identity).resolve()
        if requested_wheel_dir != bundled:
            raise RuntimeErrorCode(
                "WHEELHOUSE_UNTRUSTED",
                "explicit wheelhouse is not the bundled trusted wheelhouse",
                {"wheel_dir": str(requested_wheel_dir), "bundled_wheelhouse": str(bundled), "runtime_tag": runtime_identity},
            )

    bundled = bundled_wheelhouse(runtime_identity)
    bundled_selected = bundled.is_dir() and (wheel_dir is None or Path(wheel_dir).expanduser().resolve() == bundled.resolve())
    effective_offline = offline or bundled_selected
    wheel_report: dict[str, Any] | None = None
    wheel_dirs = default_wheel_dirs(wheel_dir, runtime_identity)
    if effective_offline:
        if bundled_selected:
            wheel_report = verify_bundled_wheelhouse(bundled, runtime_identity, LOCK_PATH, SHA256SUMS_PATH)
        else:
            wheel_report = find_offline_wheels(wheel_dirs, py_tag, runtime_identity)

    if target_python.is_file() and marker_matches(marker, digest, runtime_identity):
        health, _stdout, _stderr, _returncode = probe_print_runtime(target_python)
        if print_runtime_is_healthy(health):
            return success_envelope(
                "bootstrap_runtime",
                "RUNTIME_READY",
                runtime_root=str(root),
                runtime_path=str(target),
                runtime_python=str(target_python),
                requirements_digest=digest,
                runtime_digest=digest,
                python_tag=py_tag,
                platform_tag=runtime_identity.split("-", 1)[1],
                runtime_tag=runtime_identity,
                python=python_info,
                created=False,
                reused=True,
                offline=effective_offline,
                wheel_report=wheel_report,
            )

    if target.exists() and not target.is_dir():
        raise RuntimeErrorCode("RUNTIME_BOOTSTRAP_FAILED", "runtime path is not a directory", {"runtime_path": str(target)})
    target.parent.mkdir(parents=True, exist_ok=True)
    create_command = [str(base_python), "-m", "venv"]
    if target.exists():
        create_command.append("--clear")
    create_command.append(str(target))
    created = _run(create_command)
    if created.returncode != 0 or not target_python.is_file():
        raise RuntimeErrorCode(
            "RUNTIME_BOOTSTRAP_FAILED",
            "isolated venv creation failed",
            {
                "command": create_command,
                "returncode": created.returncode,
                "stdout": clip(created.stdout),
                "stderr": clip(created.stderr),
            },
        )

    install_command = [
        str(target_python),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
    ]
    if effective_offline:
        install_command.append("--no-index")
        for directory in wheel_dirs:
            install_command.extend(["--find-links", str(directory)])
    install_requirements = LOCK_PATH if effective_offline else requirements
    install_command.extend(["--requirement", str(install_requirements.resolve())])
    installed = _run(install_command, timeout=900)
    if installed.returncode != 0:
        raise RuntimeErrorCode(
            "RUNTIME_BOOTSTRAP_FAILED",
            "print dependencies could not be installed into the isolated venv",
            {
                "command": install_command,
                "returncode": installed.returncode,
                "stdout": clip(installed.stdout),
                "stderr": clip(installed.stderr),
            },
        )

    health, probe_stdout, probe_stderr, probe_returncode = probe_print_runtime(target_python)
    if not print_runtime_is_healthy(health):
        raise RuntimeErrorCode(
            "RUNTIME_BOOTSTRAP_FAILED",
            "installed print runtime failed its import and version check",
            {
                "runtime_python": str(target_python),
                "probe": health,
                "stdout": clip(probe_stdout),
                "stderr": clip(probe_stderr),
                "returncode": probe_returncode,
            },
        )
    marker.write_text(
        json.dumps(_marker_payload(digest, runtime_identity, base_python, python_info, target_python), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return success_envelope(
        "bootstrap_runtime",
        "RUNTIME_READY",
        runtime_root=str(root),
        runtime_path=str(target),
        runtime_python=str(target_python),
        requirements_digest=digest,
        runtime_digest=digest,
        python_tag=py_tag,
        platform_tag=runtime_identity.split("-", 1)[1],
        runtime_tag=runtime_identity,
        python=python_info,
        created=True,
        reused=False,
        offline=effective_offline,
        bundled_wheelhouse=str(bundled) if bundled_selected else None,
        wheel_report=wheel_report,
    )


def bootstrap(
    runtime_root: str | Path | None = None,
    python: str | None = None,
    offline: bool = False,
    wheel_dir: str | None = None,
    requirements: Path = REQUIREMENTS_PATH,
) -> dict[str, Any]:
    """Return a JSON-ready bootstrap envelope."""
    try:
        load_print_requirements(requirements)
        load_lock_requirements(LOCK_PATH)
        digest = runtime_digest(requirements, LOCK_PATH, SHA256SUMS_PATH)
        base_python = resolve_python(python)
        python_info = probe_python(base_python)
        py_tag = python_tag(python_info)
        runtime_identity = runtime_tag(python_info)
        root = (Path(runtime_root).expanduser() if runtime_root else default_runtime_root()).resolve()
        target = runtime_path(root, digest, runtime_identity)
        target_python = runtime_python_path(target)
        marker = runtime_marker_path(target)
        if has_symlink_component(target, root):
            raise RuntimeErrorCode(
                "RUNTIME_PATH_SYMLINK",
                "runtime path contains a symlinked intermediate directory",
                {"runtime_root": str(root), "runtime_path": str(target), "runtime_tag": runtime_identity},
            )
        with exclusive_runtime_lock(runtime_lock_path(target)):
            return _bootstrap_locked(
                digest=digest,
                base_python=base_python,
                python_info=python_info,
                py_tag=py_tag,
                runtime_identity=runtime_identity,
                root=root,
                target=target,
                target_python=target_python,
                marker=marker,
                offline=offline,
                wheel_dir=wheel_dir,
                requirements=requirements,
            )
    except RuntimeErrorCode as exc:
        details = dict(exc.details)
        if "command" in details:
            details["failed_command"] = details.pop("command")
        return error_envelope("bootstrap_runtime", exc.code, exc.message, **details)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("bootstrap_runtime", "RUNTIME_BOOTSTRAP_FAILED", str(exc), exception_type=type(exc).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap the skill-local print runtime")
    parser.add_argument("--runtime-root", help="per-user runtime cache root")
    parser.add_argument("--python", help="Python executable used to create the venv")
    parser.add_argument("--offline", action="store_true", help="install only from the local wheelhouse")
    parser.add_argument("--wheel-dir", help="offline wheelhouse directory")
    parser.add_argument("--requirements", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit the JSON envelope")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requirements = Path(args.requirements).expanduser().resolve() if args.requirements else REQUIREMENTS_PATH
    payload = bootstrap(args.runtime_root, args.python, args.offline, args.wheel_dir, requirements)
    emit_json(payload)
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
