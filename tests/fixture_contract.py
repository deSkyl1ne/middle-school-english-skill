from __future__ import annotations

import json
from pathlib import Path
from typing import Any


POSITIVE_FIXTURES = (
    "print-basic-all-types",
    "print-matching-5x7-standard",
    "print-matching-long-options",
    "print-matching-cross-page-balanced",
    "print-task-reading-three-short-answers",
    "print-word-bank-one-line",
    "print-word-bank-two-lines",
    "print-word-bank-three-lines",
    "print-asset-valid-grayscale",
    "print-font-fallback-valid",
)

ADVERSARIAL_FIXTURES = (
    "print-schema-extra-property",
    "print-matching-column-hole",
    "print-matching-single-tail-item",
    "print-matching-10pt",
    "print-heading-orphan",
    "print-option-orphan",
    "print-response-three-detached-lines",
    "print-choice-extra-full-line",
    "print-box-off-center",
    "print-box-10pt",
    "print-asset-markdown-literal",
    "print-asset-missing",
    "print-asset-72dpi",
    "print-asset-low-contrast",
    "print-font-regular-resolves-black",
)

ALL_FIXTURES = (*POSITIVE_FIXTURES, *ADVERSARIAL_FIXTURES)


def case_path(root: Path, fixture_id: str) -> Path:
    return root / "tests" / "fixtures" / fixture_id / "case.json"


def load_case(root: Path, fixture_id: str) -> dict[str, Any]:
    path = case_path(root, fixture_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def fixture_paths(root: Path) -> list[Path]:
    return [root / "tests" / "fixtures" / name for name in ALL_FIXTURES]
