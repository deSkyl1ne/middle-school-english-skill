"""Runtime Draft 2020-12 schema validation for the print pipeline (PRD FR-1).

Phase 0/1 coverage only: every packaged JSON schema must be a valid Draft
2020-12 schema and the shared ``validate_json_schema.py`` runtime must accept a
conforming instance and reject a violating one with the documented exit codes.
The full print fixtures arrive in later phases, so these tests use minimal
in-memory documents.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = PACKAGE_ROOT / "schema"

SPEC = importlib.util.spec_from_file_location("validate_json_schema", PACKAGE_ROOT / "scripts" / "validate_json_schema.py")
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

try:
    import jsonschema  # noqa: F401
    from jsonschema import Draft202012Validator  # noqa: F401

    JSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    JSCHEMA_AVAILABLE = False


PRINT_SCHEMAS = [
    "answer-sheet",
    "render-request",
    "render-profile",
    "render-ir",
    "render-manifest",
    "asset-manifest",
    "print-validation",
]


def schema_path(name: str) -> Path:
    return SCHEMA_ROOT / f"{name}.schema.json"


# --- Minimal conforming documents -------------------------------------------------

def render_request() -> dict:
    return {
        "schema_version": "1.0.0",
        "assessment_path": "assessment.json",
        "validation_report_path": "content-validation-report.json",
        "base_profile_path": "profiles/generic-cn-junior-english-v1.json",
        "asset_manifest_path": "asset-manifest.json",
        "outputs": ["student_pdf", "teacher_pdf", "answer_sheet"],
        "page": {"size": "A4", "orientation": "portrait"},
        "locale": "zh-CN",
        "illustration_mode": "none",
        "overrides": {"typography": {"body_min_font_size_pt": 11}},
        "section_overrides": {"section-01": {"typography": {"min_leading_multiplier": 1.25}}},
    }


def answer_sheet() -> dict:
    return {
        "schema_version": "1.0.0",
        "assessment_id": "print-basic-all-types",
        "blueprint_id": "bp-01",
        "items": [
            {"item_number": 1, "item_id": "item-01", "score": 2, "response_type": "choice", "answer": {"option_ids": ["A"]}}
        ],
    }


def render_profile() -> dict:
    return {
        "schema_version": "1.0.0",
        "profile_id": "generic-cn-junior-english-v1",
        "locale": "zh-CN",
        "page": {
            "size": "A4",
            "orientation": "portrait",
            "margins_pt": {"top": 56.7, "right": 56.7, "bottom": 56.7, "left": 56.7},
            "safe_padding_pt": 10,
        },
        "typography": {
            "body_min_font_size_pt": 10.5,
            "annotation_min_font_size_pt": 8.5,
            "reading_matching_min_font_size_pt": 10.5,
            "english_body_weight": "regular",
            "min_leading_multiplier": 1.2,
            "box_text_min_font_size_pt": 10.5,
        },
        "hard_gates": {
            "max_non_response_empty_ratio": 0.15,
            "max_hole_width_ratio": 0.25,
            "max_hole_height_ratio": 0.25,
            "min_stimulus_dpi": 300,
            "min_photo_dpi": 240,
            "box_center_tolerance_pt": 2,
            "orphan_hard_error": True,
        },
        "fonts": {
            "tokens": [
                {
                    "token": "zh_body",
                    "requested_family": "Songti SC Regular",
                    "fallback_families": ["Noto Serif CJK SC"],
                    "weights": ["regular", "bold"],
                    "embedded": True,
                }
            ]
        },
        "layout": {
            "reading_matching_candidates": ["card-grid", "stacked", "dual-independent-flow"],
            "keep_with_next": True,
            "continue_headers_on_overflow": True,
            "tie_break_order": ["card-grid", "stacked", "dual-independent-flow"],
        },
        "response_areas": {"default_line_policy": "one-line", "min_height_mm": 8, "writing_space_line_count_ceiling": 20},
        "illustrations": {"embed_required": True, "min_contrast_ratio": 1.4, "allow_placeholder_fallback": False},
    }


def render_ir() -> dict:
    return {
        "schema_version": "1.0.0",
        "assessment_id": "print-basic-all-types",
        "blueprint_id": "bp-01",
        "view": "student",
        "items": [
            {
                "item_id": "item-01",
                "item_type": "single_choice",
                "score": 2,
                "item_index": 1,
                "blocks": [
                    {
                        "block_id": "item-01.stem",
                        "role": "stem",
                        "kind": "Paragraph",
                        "source_item_id": "item-01",
                        "text": "Choose the correct answer.",
                        "font_size_pt": 10.5,
                        "alignment": "left",
                    },
                    {
                        "block_id": "item-01.response-01",
                        "role": "response_area",
                        "kind": "ResponseArea",
                        "source_item_id": "item-01",
                        "response": {
                            "kind": "ResponseArea",
                            "response_id": "item-01",
                            "source_item_id": "item-01",
                            "answer_contract": {"response_kind": "choice", "expected_max_chars": 1, "expected_words": 0, "score": 2},
                            "line_policy": "inline",
                            "line_count": 1,
                            "min_height_mm": 8,
                        },
                    },
                ],
            }
        ],
    }


def render_manifest() -> dict:
    block = {
        "block_id": "item-01.stem",
        "role": "content",
        "source_item_id": "item-01",
        "page": 1,
        "bbox_pt": [56.7, 700, 540, 720],
        "layout_region": "body",
    }
    return {
        "schema_version": "1.0.0",
        "status": "RENDERED",
        "assessment_id": "print-basic-all-types",
        "blueprint_id": "bp-01",
        "inputs": {
            "request": {"path": "render-request.json"},
            "assessment": {"path": "assessment.json"},
            "validation_report": {"path": "content-validation-report.json"},
            "base_profile": {"path": "profiles/generic-cn-junior-english-v1.json"},
            "resolved_profile": {"path": "resolved-profile.json"},
            "asset_manifest": {"path": "asset-manifest.json"},
        },
        "outputs": {
            "student_ir": {"path": "student-ir.json"},
            "teacher_ir": {"path": "teacher-ir.json"},
            "student_pdf": {"path": "student.pdf"},
            "teacher_pdf": {"path": "teacher.pdf"},
            "answer_sheet": {"path": "answer-sheet.json"},
        },
        "profiles": {
            "base": {"path": "profiles/generic-cn-junior-english-v1.json"},
            "resolved": {"path": "resolved-profile.json"},
        },
        "fonts": [
            {
                "token": "zh_body",
                "requested_family": "Songti SC Regular",
                "resolved_family": "Songti SC",
                "postscript_name": "SongtiSC-Regular",
                "resolved_file": "/System/Library/Fonts/Songti.ttc",
                "subfont_index": 0,
                "weight": "regular",
                "embedded": True,
                "fallback_used": False,
            }
        ],
        "blocks": [block],
        "assets": [],
        "response_areas": [
            {
                "response_id": "item-01",
                "source_item_id": "item-01",
                "response_contract": {"response_kind": "choice", "line_policy": "inline", "line_count": 1, "expected_max_chars": 1, "expected_words": 0, "score": 2},
                "actual_line_count": 1,
                "page": 1,
                "bbox_pt": [100, 600, 500, 620],
            }
        ],
        "matching": [],
        "pages": [{"document": "student", "page": 1, "width_pt": 595.28, "height_pt": 841.89}],
        "tool_versions": {
            "python": "3.11.0",
            "reportlab": "4.2.5",
            "pymupdf": "1.24.0",
            "pillow": "10.4.0",
            "jsonschema": "4.23.0",
        },
        "deterministic_build": True,
    }


def asset_manifest(with_entry: bool = False) -> dict:
    doc: dict = {"schema_version": "1.0.0", "assets": []}
    if with_entry:
        doc["assets"].append(
            {
                "asset_id": "img-weather-map-01",
                "file": "images/weather-map-01.png",
        "semantic_role": "stimulus",
                "required_for_answer": True,
                "caption": "Weather map",
                "rights_status": "school_license",
                "linked_item_ids": ["item-01"],
            }
        )
    return doc


def print_validation() -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "PRINT_PREFLIGHT_PASS",
        "assessment_id": "print-basic-all-types",
        "errors": [],
        "warnings": [],
        "pdfs": [
            {
                "document": "student",
                "path": "student.pdf",
                "page_count": 1,
                "pages": [{"page": 1, "width_pt": 595.28, "height_pt": 841.89, "empty_ratio": 0.02}],
            },
            {
                "document": "teacher",
                "path": "teacher.pdf",
                "page_count": 1,
                "pages": [{"page": 1, "width_pt": 595.28, "height_pt": 841.89, "empty_ratio": 0.02}],
            },
        ],
        "summary": {"errors": 0, "warnings": 0, "pdf_count": 2},
    }


SAMPLE_DOCS = {
    "answer-sheet": answer_sheet,
    "render-request": render_request,
    "render-profile": render_profile,
    "render-ir": render_ir,
    "render-manifest": render_manifest,
    "asset-manifest": asset_manifest,
    "print-validation": print_validation,
}


def write_doc(root: Path, document: dict, name: str) -> Path:
    path = root / name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class PrintSchemaMetaTest(unittest.TestCase):
    def test_all_packaged_print_schemas_are_valid_draft2020(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        from jsonschema import Draft202012Validator

        for name in PRINT_SCHEMAS:
            with self.subTest(schema=name):
                schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)

    def test_core_schemas_are_valid_draft2020(self) -> None:
        if not JSCHEMA_AVAILABLE:
            self.skipTest("jsonschema not installed")
        from jsonschema import Draft202012Validator

        for name in ("assessment", "assessment-validation", "assessment-request"):
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(json.loads(schema_path(name).read_text(encoding="utf-8")))


class PrintSchemaRuntimeTest(unittest.TestCase):
    def run_cli(self, schema: str, instance: Path) -> tuple[int, dict]:
        exit_code = VALIDATOR.main(["--schema", schema, "--instance", str(instance)])
        report = VALIDATOR.validate_document(json.loads(instance.read_text(encoding="utf-8")), json.loads(schema_path(schema).read_text(encoding="utf-8")), schema)
        return exit_code, report

    def test_each_print_schema_accepts_conforming_document(self) -> None:
        with tempfile.TemporaryDirectory(prefix="print-schema-valid-") as temp_dir:
            root = Path(temp_dir)
            for name in PRINT_SCHEMAS:
                with self.subTest(schema=name):
                    instance = write_doc(root, SAMPLE_DOCS[name](), f"{name}-valid.json")
                    exit_code, report = self.run_cli(name, instance)
                    self.assertEqual(0, exit_code, report)
                    self.assertEqual("SCHEMA_VALID", report["status"])

    def test_render_request_rejects_missing_input_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="print-schema-invalid-") as temp_dir:
            root = Path(temp_dir)
            document = render_request()
            del document["assessment_path"]
            instance = write_doc(root, document, "render-request-missing-path.json")
            exit_code, report = self.run_cli("render-request", instance)
            self.assertEqual(1, exit_code)
            self.assertEqual("SCHEMA_INVALID", report["status"])
            self.assertTrue(any("assessment_path" in error["message"] for error in report["errors"]))

    def test_render_request_rejects_extra_property(self) -> None:
        with tempfile.TemporaryDirectory(prefix="print-schema-extra-") as temp_dir:
            root = Path(temp_dir)
            document = render_request()
            document["unexpected_field"] = True
            instance = write_doc(root, document, "render-request-extra.json")
            exit_code, report = self.run_cli("render-request", instance)
            self.assertEqual(1, exit_code)
            self.assertEqual("SCHEMA_INVALID", report["status"])
            self.assertTrue(any(error["validator"] == "additionalProperties" for error in report["errors"]))

    def test_missing_schema_returns_exit_2(self) -> None:
        with tempfile.TemporaryDirectory(prefix="print-schema-missing-") as temp_dir:
            instance = Path(temp_dir) / "doc.json"
            instance.write_text(json.dumps(render_request()), encoding="utf-8")
            exit_code = VALIDATOR.main(["--schema", "does-not-exist", "--instance", str(instance)])
            self.assertEqual(2, exit_code)

    def test_asset_manifest_allows_empty_and_registered_bindings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="print-schema-assets-") as temp_dir:
            root = Path(temp_dir)
            empty = write_doc(root, asset_manifest(with_entry=False), "asset-manifest-empty.json")
            self.assertEqual(0, self.run_cli("asset-manifest", empty)[0])
            registered = write_doc(root, asset_manifest(with_entry=True), "asset-manifest-registered.json")
            exit_code, report = self.run_cli("asset-manifest", registered)
            self.assertEqual(0, exit_code, report)


if __name__ == "__main__":
    unittest.main()
