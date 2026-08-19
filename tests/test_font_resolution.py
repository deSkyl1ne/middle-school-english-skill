from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from jsonschema import Draft202012Validator

from test_print_support import ROOT, prepare_positive

sys.path.insert(0, str(ROOT / "scripts"))
import resolve_render_profile as resolver  # noqa: E402
import preflight_pdf  # noqa: E402


def _render_positive() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    return prepare_positive()


def _face(path: Path, index: int, *, family: str, weight: str, coverage: str = "A") -> resolver.FontFace:
    return resolver.FontFace(
        source_file=str(path),
        subfont_index=index,
        family=family,
        postscript_name=f"{family.replace(' ', '')}-{weight}",
        subfamily=weight,
        weight=weight,
        coverage_codepoints=frozenset(ord(value) for value in coverage),
        coverage_ranges=(),
    )


def _token(*, requested: str = "Target Family", fallback: list[str] | None = None) -> dict:
    return {
        "token": "body",
        "requested_family": requested,
        "fallback_families": fallback or [],
        "weights": ["regular"],
        "embedded": True,
    }


class FontResolutionTest(unittest.TestCase):
    def test_windows_font_root_is_allowed_by_preflight(self):
        with tempfile.TemporaryDirectory(prefix="mse-windows-font-root-") as td:
            windows_root = Path(td) / "Windows"
            fonts_root = windows_root / "Fonts"
            fonts_root.mkdir(parents=True)
            font_path = fonts_root / "probe.ttf"
            font_path.write_bytes(b"font fixture")
            errors: list[dict] = []
            with patch.object(preflight_pdf.sys, "platform", "win32"), patch.dict(
                preflight_pdf.os.environ,
                {"WINDIR": str(windows_root), "SystemRoot": str(windows_root)},
                clear=False,
            ):
                preflight_pdf.validate_font_file_records({"fonts": [{"resolved_file": str(font_path)}]}, errors)
            self.assertEqual(errors, [])

    def test_manifest_has_actual_regular_embedded_font(self):
        temp, bundle = _render_positive()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["fonts"])
        for font in manifest["fonts"]:
            self.assertTrue(font["embedded"])
            self.assertTrue(font["resolved_family"])
            self.assertTrue(font["postscript_name"])
            self.assertEqual(font["weight"], "regular")
            self.assertEqual(font["coverage"]["covered_count"], font["coverage"]["required_count"])
            if font["fallback_used"]:
                fallback_names = {value.casefold().replace(" ", "") for value in font["fallback_families"]}
                self.assertIn(font["resolved_family"].casefold().replace(" ", ""), fallback_names)
        with fitz.open(bundle / "student.pdf") as document:
            embedded_fonts = [
                record
                for page in document
                for record in page.get_fonts(full=True)
                if len(record) > 1 and record[1] != "n/a"
            ]
        self.assertTrue(embedded_fonts, "the PDF must contain an embedded font resource")

    def test_ttc_enumerates_a_face_after_the_third_index(self):
        with tempfile.TemporaryDirectory(prefix="mse-ttc-enumeration-") as td:
            font_path = Path(td) / "faces.ttc"
            offsets = (28, 29, 30, 31)
            font_path.write_bytes(b"ttcf" + struct.pack(">II", 0x00010000, 4) + struct.pack(">IIII", *offsets) + b"\0" * 16)
            with (
                patch.object(resolver, "_table_map", return_value={}),
                patch.object(resolver, "_name_values", return_value=["TTC Family"]),
                patch.object(resolver, "_weight_from_face", return_value="regular"),
                patch.object(resolver, "_cmap_coverage", return_value=(frozenset({65}), ())),
            ):
                faces = resolver.enumerate_ttc_faces(font_path)
        self.assertEqual([face.subfont_index for face in faces], [0, 1, 2, 3])

    def test_resolution_selects_regular_face_after_first_three_faces(self):
        with tempfile.TemporaryDirectory(prefix="mse-font-face-selection-") as td:
            font_path = Path(td) / "faces.ttf"
            font_path.write_bytes(b"font fixture")
            faces = [
                _face(font_path, 0, family="Target Family", weight="black"),
                _face(font_path, 1, family="Target Family", weight="bold"),
                _face(font_path, 2, family="Target Family", weight="heavy"),
                _face(font_path, 3, family="Target Family", weight="regular"),
            ]
            with patch.object(resolver, "enumerate_font_faces", return_value=faces):
                record = resolver.resolve_font_token(_token(), font_path, required_chars=["A"], embedded=True)
        self.assertEqual(record["subfont_index"], 3)
        self.assertEqual(record["weight"], "regular")

    def test_regular_request_rejects_black_face(self):
        with tempfile.TemporaryDirectory(prefix="mse-font-black-") as td:
            font_path = Path(td) / "black.ttf"
            font_path.write_bytes(b"font fixture")
            face = _face(font_path, 0, family="Target Family", weight="black")
            with patch.object(resolver, "enumerate_font_faces", return_value=[face]):
                with self.assertRaises(resolver.FontResolutionError) as raised:
                    resolver.resolve_font_token(_token(), font_path, required_chars=["A"], embedded=True)
        self.assertEqual(raised.exception.code, "FONT_WEIGHT_INVALID")

    def test_declared_fallback_is_accepted_and_unknown_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="mse-font-fallback-") as td:
            font_path = Path(td) / "fallback.ttf"
            font_path.write_bytes(b"font fixture")
            declared = _face(font_path, 0, family="Fallback Family", weight="regular")
            with patch.object(resolver, "enumerate_font_faces", return_value=[declared]):
                record = resolver.resolve_font_token(
                    _token(requested="Missing Family", fallback=["Fallback Family"]),
                    font_path,
                    required_chars=["A"],
                    embedded=True,
                )
                self.assertTrue(record["fallback_used"])
                self.assertEqual(record["resolved_family"], "Fallback Family")
            unknown = _face(font_path, 0, family="Unlisted Family", weight="regular")
            with patch.object(resolver, "enumerate_font_faces", return_value=[unknown]):
                with self.assertRaises(resolver.FontResolutionError) as raised:
                    resolver.resolve_font_token(
                        _token(requested="Missing Family", fallback=["Declared Family"]),
                        font_path,
                        required_chars=["A"],
                        embedded=True,
                    )
        self.assertEqual(raised.exception.code, "FONT_FALLBACK_INVALID")

    def test_profiles_declare_cross_platform_fallback_order_and_windows_arial_coverage(self):
        profile_paths = [
            ROOT / "references" / "rendering" / "profiles" / "generic-cn-compact-v1.json",
            ROOT / "tests" / "fixtures" / "print-positive" / "generic-cn-compact-v1.json",
            ROOT.parent.parent / "artifacts" / "d0210b3-student-pdf-fix" / "generic-cn-compact-v1.json",
        ]
        profiles = [json.loads(path.read_text(encoding="utf-8")) for path in profile_paths]
        for path, profile in zip(profile_paths, profiles):
            fallback = profile["fonts"]["tokens"][0]["fallback_families"]
            self.assertIn("Arial", fallback, path)
            self.assertLess(fallback.index("WenQuanYi Micro Hei"), fallback.index("Noto Sans CJK SC"), path)
            self.assertTrue(profile["font_resolution"]["unknown_fallback_is_error"], path)
        token = profiles[1]["fonts"]["tokens"][0]

        sys.path.insert(0, str(ROOT / "scripts"))
        import render_pdf  # noqa: PLC0415

        assessment = json.loads(
            (ROOT / "tests" / "fixtures" / "print-positive" / "assessment.json").read_text(encoding="utf-8")
        )
        required_chars = render_pdf.collect_chars(assessment)
        self.assertTrue(required_chars)
        with tempfile.TemporaryDirectory(prefix="mse-windows-arial-fallback-") as td:
            font_path = Path(td) / "arial.ttf"
            font_path.write_bytes(b"font fixture")
            face = _face(font_path, 0, family="Arial", weight="regular", coverage="".join(required_chars))
            with patch.object(resolver, "enumerate_font_faces", return_value=[face]):
                record = resolver.resolve_font_token(token, font_path, required_chars=required_chars, embedded=True)

        self.assertTrue(record["fallback_used"])
        self.assertEqual(record["resolved_family"], "Arial")
        self.assertEqual(record["coverage"]["covered_count"], len(required_chars))

    def test_linux_cjk_prefers_embeddable_wenquanyi_before_noto(self):
        wqy_path = Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
        if not wqy_path.is_file():
            self.skipTest("WenQuanYi Micro Hei is not installed on this host")
        profile_path = ROOT.parent.parent / "artifacts" / "d0210b3-student-pdf-fix" / "generic-cn-compact-v1.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        fallback = profile["fonts"]["tokens"][0]["fallback_families"]
        self.assertLess(fallback.index("WenQuanYi Micro Hei"), fallback.index("Noto Sans CJK SC"))

        sys.path.insert(0, str(ROOT / "scripts"))
        import render_pdf  # noqa: PLC0415

        assessment = json.loads(
            (ROOT.parent.parent / "artifacts" / "d0210b3-student-pdf-fix" / "assessment.json").read_text(encoding="utf-8")
        )
        font_name, record = render_pdf.resolve_runtime_font(profile, render_pdf.collect_chars(assessment))
        self.assertEqual(font_name, "PrintBody")
        self.assertEqual(record["resolved_file"], str(wqy_path.resolve()))
        self.assertEqual(record["resolved_family"], "WenQuanYi Micro Hei")
        self.assertEqual(record["subfont_index"], 0)
        self.assertTrue(record["embedded"])

    def test_unknown_profile_override_is_blocked(self):
        profile = ROOT / "references/rendering/profiles/generic-cn-junior-english-v1.json"
        with tempfile.TemporaryDirectory(prefix="mse-invalid-profile-") as temp_dir:
            temp_root = Path(temp_dir)
            override = temp_root / "overrides.json"
            output = temp_root / "resolved-profile.json"
            override.write_text('{"hard_gates":{"max_non_response_empty_ratio":0.9}}', encoding="utf-8")
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/resolve_render_profile.py"),
                    "--base",
                    str(profile),
                    "--output",
                    str(output),
                    "--overrides",
                    str(override),
                ],
                capture_output=True,
                text=True,
            )
            stdout = process.stdout
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("PROFILE_BINDING_INVALID", stdout)

    def test_profile_schema_keeps_font_and_print_hard_gates(self):
        schema = json.loads((ROOT / "schema" / "render-profile.schema.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "references/rendering/profiles/generic-cn-junior-english-v1.json").read_text(encoding="utf-8"))
        profile["hard_gates"]["min_stimulus_dpi"] = 299
        errors = list(Draft202012Validator(schema).iter_errors(profile))
        self.assertTrue(errors)
        profile = json.loads((ROOT / "references/rendering/profiles/generic-cn-junior-english-v1.json").read_text(encoding="utf-8"))
        profile["font_resolution"] = {"enumerate_all_ttc_faces": True}
        errors = list(Draft202012Validator(schema).iter_errors(profile))
        self.assertTrue(any(error.validator == "required" for error in errors))


if __name__ == "__main__":
    unittest.main()
