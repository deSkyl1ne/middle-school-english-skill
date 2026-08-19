#!/usr/bin/env python3
"""Run package and print checks used by the public CI workflow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PACKAGE_ROOT
SCRIPTS = SKILL_ROOT / "scripts"
FIXTURES = PACKAGE_ROOT / "tests" / "fixtures"
TEMP_ROOT = Path(tempfile.gettempdir())
PYTHON_CACHE = TEMP_ROOT / "middle-school-english-ci-pycache"


def command_env() -> dict[str, str]:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    PYTHON_CACHE.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(TEMP_ROOT)
    environment["PYTHONPYCACHEPREFIX"] = str(PYTHON_CACHE)
    return environment


def temporary_directory(prefix: str) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(TEMP_ROOT))


def run_json(command: list[str], expected_code: int) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(command, cwd=PACKAGE_ROOT, env=command_env(), capture_output=True, text=True, check=False)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON output from {' '.join(command)}: {exc}") from exc
    if completed.returncode != expected_code:
        raise RuntimeError(
            f"unexpected exit code from {' '.join(command)}: "
            f"expected {expected_code}, got {completed.returncode}; {payload}"
        )
    return completed.returncode, payload


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_interface() -> None:
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    interface_text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    require(skill_text.startswith("---\n"), "SKILL.md has no front matter")
    require("name: middle-school-english" in skill_text, "SKILL.md name does not match the package")
    require("description:" in skill_text, "SKILL.md description is missing")
    for field in ("display_name:", "short_description:", "default_prompt:"):
        require(field in interface_text, f"openai.yaml is missing {field}")
    require("$middle-school-english" in interface_text, "openai.yaml default prompt does not route to the Skill")
    for marker in ("Codex", "run_print"):
        require(marker in skill_text, f"SKILL.md is missing the Codex interface marker {marker!r}")


def check_unit_tests() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=PACKAGE_ROOT,
        env=command_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    require(completed.returncode == 0, f"unit tests failed: {completed.stdout}\n{completed.stderr}")


def main() -> int:
    try:
        catalog = json.loads((SKILL_ROOT / "references" / "catalog.json").read_text(encoding="utf-8"))
        candidate_mode = catalog.get("status") != "released"
        release_command = [sys.executable, str(SCRIPTS / "validate_release.py")]
        if not candidate_mode:
            release_command.append("--require-released")
        _code, release = run_json(release_command, 0)
        expected_release_status = "CANDIDATE_VALIDATION_PASS" if candidate_mode else "RELEASE_VALIDATION_PASS"
        require(release.get("status") == expected_release_status, "package release validation failed")
        candidate_flag = ["--include-candidates"] if candidate_mode else []

        _code, query = run_json([
            sys.executable,
            str(SCRIPTS / "query_knowledge.py"),
            "--book",
            "grade-07-semester-2",
            "--unit",
            "unit-01",
            "--domain",
            "grammar",
        ] + candidate_flag, 0)
        require(query.get("status") == "OK" and query.get("count", 0) > 0, "known-book query returned no knowledge")

        _code, unpublished = run_json([
            sys.executable,
            str(SCRIPTS / "query_knowledge.py"),
            "--book",
            "grade-09-semester-1",
        ], 2)
        require(unpublished.get("status") == "UNPUBLISHED_BOOK", "unpublished Grade 9 query was not blocked")

        _code, blueprint = run_json([
            sys.executable,
            str(SCRIPTS / "build_blueprint.py"),
            "--request",
            str(FIXTURES / "blueprint-request.json"),
        ] + candidate_flag, 0)
        require(blueprint.get("status") == "BLUEPRINT_OK", "blueprint smoke test failed")

        positive = FIXTURES / "assessment-positive.json"
        negative = FIXTURES / "assessment-double-answer.json"
        with temporary_directory("middle-school-english-ci-") as temp_dir:
            rendered = Path(temp_dir) / "rendered"
            _code, render_result = run_json([
                sys.executable,
                str(SCRIPTS / "render_assessment.py"),
                "--input",
                str(positive),
                "--out-dir",
                str(rendered),
            ], 0)
            require(render_result.get("status") == "RENDERED", "assessment render smoke test failed")
            _code, valid = run_json([
                sys.executable,
                str(SCRIPTS / "validate_assessment.py"),
                "--assessment",
                str(positive),
                "--student",
                str(rendered / "student.md"),
                "--teacher",
                str(rendered / "teacher.md"),
                "--answer-sheet",
                str(rendered / "answer-sheet.json"),
            ] + candidate_flag, 0)
            require(valid.get("status") == "ASSESSMENT_VALIDATOR_PASS", "positive assessment fixture failed")
            _code, invalid = run_json([
                sys.executable,
                str(SCRIPTS / "validate_assessment.py"),
                "--assessment",
                str(negative),
            ] + candidate_flag, 1)
            require(
                invalid.get("status") == "ASSESSMENT_VALIDATOR_FAIL"
                and any(item.get("code") == "ANSWER_NOT_UNIQUE" for item in invalid.get("errors", [])),
                "negative assessment fixture was not blocked by answer uniqueness",
            )

        check_interface()
        check_unit_tests()
        print_fixture = FIXTURES / "print-positive" / "render-request.json"
        print_checks = 0
        if print_fixture.exists():
            # Build the source through the scoped case-driven fixture helper and
            # Keep generated files under the platform's temporary root.
            tests_root = PACKAGE_ROOT / "tests"
            sys.path.insert(0, str(tests_root))
            from test_prd_fixture_runtime import create_source

            with temporary_directory("middle-school-english-print-ci-") as print_temp:
                source = create_source("print-font-fallback-valid", Path(print_temp) / "source-root")
                bundle = Path(print_temp) / "bundle"
                _code, printed = run_json([
                    sys.executable,
                    str(SCRIPTS / "run_print.py"),
                    "--request",
                    str(source / "render-request.json"),
                    "--bundle-out",
                    str(bundle),
                    "--runtime-root",
                    str(Path(print_temp) / "runtime"),
                    "--json",
                ], 0)
                require(printed.get("status") == "PRINT_COMPLETE", f"print wrapper failed: {printed}")
                require((bundle / "student.pdf").exists() and (bundle / "teacher.pdf").exists(), "real print PDFs missing")
                require(any(stage.get("stage") == "preflight" and stage.get("returncode") == 0 for stage in printed.get("stages", [])), "real print preflight failed")
                print_checks = 2
        print(json.dumps({"status": "CI_PACKAGE_VALIDATION_PASS", "checks": 9 + print_checks, "print_checks": print_checks}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"status": "CI_PACKAGE_VALIDATION_FAIL", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
