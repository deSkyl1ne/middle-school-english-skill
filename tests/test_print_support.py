from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PRINT_POSITIVE = FIXTURES / "print-positive"
SCRIPTS = ROOT / "scripts"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_json(command: list[str], expected: int | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    if expected is not None:
        assert result.returncode == expected, result.stdout + result.stderr
    return result, payload


def copy_positive_bundle(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    bundle = root / "bundle"
    shutil.copytree(PRINT_POSITIVE, source)
    bundle.mkdir()
    return source, bundle


def prepare_positive() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="mse-prd-fixture-")
    root = Path(temp.name)
    # Build the current forward-tested paper through the same source path used
    # by the black-box fixtures.
    sys.path.insert(0, str(ROOT / "tests"))
    from test_prd_fixture_runtime import create_source

    source = create_source("print-font-fallback-valid", root)
    request_path = source / "render-request.json"
    bundle = root / "bundle"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_pdf.py"), "--request", str(request_path), "--bundle-out", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    preflight = subprocess.run(
        [sys.executable, str(SCRIPTS / "preflight_pdf.py"), "--bundle", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert preflight.returncode == 0, preflight.stdout + preflight.stderr
    return temp, bundle
