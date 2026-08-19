"""Unit tests for the skill-local print runtime."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap_runtime  # noqa: E402
import run_print  # noqa: E402
import runtime_common  # noqa: E402
import runtime_doctor  # noqa: E402


class RuntimePathAndTagTest(unittest.TestCase):
    def test_runtime_path_isolated_by_digest_python_and_platform_tag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-path-") as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_bytes(b"reportlab==5.0.0\nPyMuPDF==1.26.5\nPillow==11.3.0\njsonschema==4.25.1\n")
            digest = runtime_common.requirements_digest(requirements)
            self.assertEqual(digest, hashlib.sha256(requirements.read_bytes()).hexdigest())
            windows_info = {"implementation": "CPython", "major": 3, "minor": 12, "platform": "win-amd64"}
            linux_info = {"implementation": "CPython", "major": 3, "minor": 12, "platform": "linux-x86_64"}
            windows_tag = runtime_common.runtime_tag(windows_info)
            linux_tag = runtime_common.runtime_tag(linux_info)
            self.assertEqual(windows_tag, "cp312-win_amd64")
            self.assertEqual(linux_tag, "cp312-linux_x86_64")
            target = runtime_common.runtime_path(Path(temp_dir) / "cache", digest, windows_tag)
            self.assertEqual(target.parts[-2:], (digest, "cp312-win_amd64"))
            self.assertNotEqual(target, runtime_common.runtime_path(Path(temp_dir) / "cache", digest, linux_tag))
            self.assertNotEqual(target, runtime_common.runtime_path(Path(temp_dir) / "cache", digest, "cp311-win_amd64"))

    def test_runtime_digest_covers_requirements_lock_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-digest-") as temp_dir:
            root = Path(temp_dir)
            requirements = root / "requirements-print.txt"
            lock = root / "requirements-print.lock"
            sums = root / "SHA256SUMS"
            requirements.write_bytes(b"requirements-v1")
            lock.write_bytes(b"lock-v1")
            sums.write_bytes(b"sums-v1")
            first = runtime_common.runtime_digest(requirements, lock, sums)
            lock.write_bytes(b"lock-v2")
            second = runtime_common.runtime_digest(requirements, lock, sums)
            sums.write_bytes(b"sums-v2")
            third = runtime_common.runtime_digest(requirements, lock, sums)
            requirements.write_bytes(b"requirements-v2")
            fourth = runtime_common.runtime_digest(requirements, lock, sums)
        self.assertEqual(len({first, second, third, fourth}), 4)

    def test_default_root_uses_current_platform_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-localappdata-") as temp_dir:
            local_appdata = Path(temp_dir) / "local"
            xdg_cache = Path(temp_dir) / "xdg"
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local_appdata), "XDG_CACHE_HOME": str(xdg_cache)},
                clear=False,
            ):
                actual = runtime_common.default_runtime_root()
            if os.name == "nt":
                expected = local_appdata / "middle-school-english" / "print-runtime"
            elif sys.platform == "darwin":
                expected = Path.home() / "Library" / "Caches" / "middle-school-english" / "print-runtime"
            else:
                expected = xdg_cache / "middle-school-english" / "print-runtime"
            self.assertEqual(actual, expected)

    def test_unsupported_python_tag_has_stable_error(self) -> None:
        with self.assertRaises(runtime_common.RuntimeErrorCode) as raised:
            runtime_common.python_tag({"implementation": "CPython", "major": 3, "minor": 8})
        self.assertEqual(raised.exception.code, "PYTHON_TAG_UNSUPPORTED")

    def test_marker_mismatch_cannot_reuse_other_platform(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-marker-") as temp_dir:
            marker = Path(temp_dir) / "runtime.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "requirements_digest": "digest",
                        "runtime_digest": "digest",
                        "python_tag": "cp312",
                        "platform_tag": "linux_x86_64",
                        "runtime_tag": "cp312-win_amd64",
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(runtime_common.marker_matches(marker, "digest", "cp312-win_amd64"))
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "requirements_digest": "digest",
                        "runtime_digest": "digest",
                        "python_tag": "cp312",
                        "platform_tag": "win_amd64",
                        "runtime_tag": "cp312-win_amd64",
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(runtime_common.marker_matches(marker, "digest", "cp312-win_amd64"))


class RuntimeDoctorTest(unittest.TestCase):
    def test_print_doctor_error_preserves_wrapper_mode(self) -> None:
        failure = runtime_common.RuntimeErrorCode(
            "DOCTOR_TEST_FAILURE",
            "doctor failure",
            {"mode": "core", "detail": "from exception"},
        )
        with patch.object(runtime_doctor, "load_print_requirements", side_effect=failure):
            payload = runtime_doctor.doctor_print(requirements=Path("unused-requirements.txt"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "runtime_doctor")
        self.assertEqual(payload["mode"], "print")
        self.assertEqual(payload["detail"], "from exception")

    def test_core_doctor_does_not_need_print_packages(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONNOUSERSITE"] = "1"
        with tempfile.TemporaryDirectory(prefix="mse-runtime-doctor-") as temp_dir:
            environment["PYTHONPYCACHEPREFIX"] = temp_dir
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(SCRIPTS / "runtime_doctor.py"), "--core", "--json"],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "CORE_RUNTIME_OK")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "core")

    def test_print_doctor_rejects_global_runtime_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-doctor-global-") as temp_dir:
            root = Path(temp_dir) / "runtime"
            global_python = Path(temp_dir) / "global-python"
            global_python.write_bytes(b"not the isolated interpreter")
            info = {
                "implementation": "CPython",
                "major": 3,
                "minor": 12,
                "platform": "win-amd64" if os.name == "nt" else "linux-x86_64",
            }
            with patch.object(runtime_doctor, "probe_python", return_value=info):
                payload = runtime_doctor.doctor_print(root, runtime_python=global_python)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "RUNTIME_PYTHON_MISMATCH")

    def test_print_doctor_requires_matching_marker_for_explicit_runtime_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-doctor-marker-") as temp_dir:
            root = Path(temp_dir) / "runtime"
            info = {
                "implementation": "CPython",
                "major": 3,
                "minor": 12,
                "platform": "win-amd64" if os.name == "nt" else "linux-x86_64",
            }
            digest = runtime_common.runtime_digest()
            identity = runtime_common.runtime_tag(info)
            target = runtime_common.runtime_path(root, digest, identity)
            target_python = runtime_common.runtime_python_path(target)
            target_python.parent.mkdir(parents=True, exist_ok=True)
            target_python.write_bytes(b"isolated interpreter placeholder")
            with patch.object(runtime_doctor, "probe_python", return_value=info):
                payload = runtime_doctor.doctor_print(root, runtime_python=target_python)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "RUNTIME_MARKER_MISMATCH")

    def test_print_doctor_accepts_posix_final_python_symlink_lexically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-doctor-posix-") as temp_dir:
            root = Path(temp_dir) / "runtime"
            info = {
                "implementation": "CPython",
                "major": 3,
                "minor": 12,
                "platform": "win-amd64" if os.name == "nt" else "linux-x86_64",
            }
            digest = runtime_common.runtime_digest()
            identity = runtime_common.runtime_tag(info)
            target = runtime_common.runtime_path(root, digest, identity)
            posix_python = target / "bin" / "python"
            health = {
                "ok": True,
                "packages": {
                    spec["distribution"]: {"imported": True, "version": spec["version"], "ok": True}
                    for spec in runtime_common.PRINT_PACKAGE_SPECS
                },
            }
            with patch.object(runtime_doctor, "probe_python", return_value=info), patch.object(runtime_doctor, "runtime_python_path", return_value=posix_python), patch.object(runtime_doctor, "marker_matches", return_value=True), patch.object(runtime_doctor, "probe_print_runtime", return_value=(health, "", "", 0)), patch.object(Path, "is_file", return_value=True):
                payload = runtime_doctor.doctor_print(root, runtime_python=posix_python)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["runtime_python"], str(runtime_common.lexical_path(posix_python)))


class BootstrapFailureTest(unittest.TestCase):
    def test_bootstrap_error_envelope_separates_tool_and_failed_command(self) -> None:
        failed_command = [sys.executable, "-m", "venv"]
        failure = runtime_common.RuntimeErrorCode(
            "RUNTIME_BOOTSTRAP_FAILED",
            "runtime subprocess failed",
            {"command": failed_command},
        )
        with patch.object(bootstrap_runtime, "load_print_requirements", side_effect=failure):
            payload = bootstrap_runtime.bootstrap(requirements=Path("unused-requirements.txt"))
        self.assertEqual(payload["command"], "bootstrap_runtime")
        self.assertEqual(payload["failed_command"], failed_command)
        self.assertNotIn("command", payload["error"])

    def test_default_bootstrap_prefers_matching_bundled_wheelhouse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-online-") as temp_dir:
            root = Path(temp_dir) / "runtime"
            base_python = Path(temp_dir) / "base-python.exe"
            python_info = {"implementation": "CPython", "major": 3, "minor": 12, "micro": 10, "platform": "win-amd64"}
            health = {
                "ok": True,
                "packages": {
                    spec["distribution"]: {"imported": True, "version": spec["version"], "ok": True}
                    for spec in runtime_common.PRINT_PACKAGE_SPECS
                },
            }
            commands: list[list[str]] = []

            def fake_run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    target = Path(command[-1])
                    python_path = runtime_common.runtime_python_path(target)
                    python_path.parent.mkdir(parents=True, exist_ok=True)
                    python_path.write_bytes(b"fake runtime python")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(bootstrap_runtime, "resolve_python", return_value=base_python), patch.object(bootstrap_runtime, "probe_python", return_value=python_info), patch.object(bootstrap_runtime, "probe_print_runtime", return_value=(health, "", "", 0)), patch.object(bootstrap_runtime, "_run", side_effect=fake_run) as mocked_run, patch.object(bootstrap_runtime, "find_offline_wheels") as mocked_wheels:
                payload = bootstrap_runtime.bootstrap(runtime_root=root, python=str(base_python), offline=False)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["runtime_tag"], "cp312-win_amd64")
            self.assertEqual(Path(payload["runtime_path"]).name, "cp312-win_amd64")
            self.assertTrue(payload["offline"])
            self.assertTrue(payload["wheel_report"]["hash_verified"])
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][0:3], [str(base_python), "-m", "venv"])
            self.assertEqual(commands[1][0], str(Path(payload["runtime_python"])))
            self.assertEqual(commands[1][1:4], ["-m", "pip", "install"])
            self.assertIn("--no-index", commands[1])
            self.assertIn("--find-links", commands[1])
            self.assertEqual(commands[1][commands[1].index("--requirement") + 1], str(bootstrap_runtime.LOCK_PATH.resolve()))
            mocked_run.assert_called()
            mocked_wheels.assert_not_called()

    def test_bundled_wheel_hash_mismatch_is_rejected_before_install(self) -> None:
        wheel_dir = bootstrap_runtime.bundled_wheelhouse("cp312-win_amd64")
        with patch.object(bootstrap_runtime, "_sha256_file", return_value="0" * 64):
            with self.assertRaises(runtime_common.RuntimeErrorCode) as raised:
                bootstrap_runtime.verify_bundled_wheelhouse(wheel_dir, "cp312-win_amd64")
        self.assertEqual(raised.exception.code, "WHEEL_HASH_MISMATCH")

    def test_nonbundled_explicit_wheelhouse_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-untrusted-wheelhouse-") as temp_dir:
            payload = bootstrap_runtime.bootstrap(
                runtime_root=Path(temp_dir) / "runtime",
                python=sys.executable,
                offline=True,
                wheel_dir=temp_dir,
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "WHEELHOUSE_UNTRUSTED")

    def test_same_runtime_bootstrap_is_exclusive_and_installs_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-lock-") as temp_dir:
            root = Path(temp_dir) / "runtime"
            base_python = Path(temp_dir) / "base-python"
            info = {
                "implementation": "CPython",
                "major": 3,
                "minor": 12,
                "platform": "win-amd64" if os.name == "nt" else "linux-x86_64",
            }
            health = {
                "ok": True,
                "packages": {
                    spec["distribution"]: {"imported": True, "version": spec["version"], "ok": True}
                    for spec in runtime_common.PRINT_PACKAGE_SPECS
                },
            }
            install_started = threading.Event()
            second_started = threading.Event()
            release_install = threading.Event()
            count_lock = threading.Lock()
            install_count = 0
            results: list[dict[str, Any]] = []

            def fake_run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
                nonlocal install_count
                if command[1:3] == ["-m", "venv"]:
                    target = Path(command[-1])
                    target_python = runtime_common.runtime_python_path(target)
                    target_python.parent.mkdir(parents=True, exist_ok=True)
                    target_python.write_bytes(b"isolated interpreter placeholder")
                elif command[1:4] == ["-m", "pip", "install"]:
                    with count_lock:
                        install_count += 1
                    install_started.set()
                    if not release_install.wait(5):
                        raise AssertionError("test install did not release")
                return subprocess.CompletedProcess(command, 0, "", "")

            def invoke() -> None:
                results.append(bootstrap_runtime.bootstrap(runtime_root=root, python=str(base_python)))

            with patch.object(bootstrap_runtime, "resolve_python", return_value=base_python), patch.object(bootstrap_runtime, "probe_python", return_value=info), patch.object(bootstrap_runtime, "probe_print_runtime", return_value=(health, "", "", 0)), patch.object(bootstrap_runtime, "_run", side_effect=fake_run), patch.object(bootstrap_runtime, "bundled_wheelhouse", return_value=root / "no-bundled-wheelhouse"), patch.object(bootstrap_runtime, "default_wheel_dirs", return_value=[]):
                first = threading.Thread(target=invoke)
                second = threading.Thread(target=lambda: (second_started.set(), invoke()))
                first.start()
                self.assertTrue(install_started.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                try:
                    time.sleep(0.2)
                    with count_lock:
                        self.assertEqual(install_count, 1)
                finally:
                    release_install.set()
                    first.join(5)
                    second.join(5)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["ok"] for result in results))
            self.assertEqual(sum(bool(result.get("created")) for result in results), 1)

    def test_missing_python_returns_json_envelope_without_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-missing-python-") as temp_dir:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "bootstrap_runtime.py"),
                    "--runtime-root",
                    str(Path(temp_dir) / "runtime"),
                    "--python",
                    "mse-python-that-does-not-exist",
                    "--offline",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "bootstrap_runtime")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "PYTHON_NOT_FOUND")
        self.assertIn("error", payload)

    def test_offline_unsupported_wheel_returns_wheel_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-runtime-wheel-") as temp_dir:
            wheel_dir = Path(temp_dir)
            (wheel_dir / "reportlab-5.0.0-cp38-cp38-win_amd64.whl").write_bytes(b"not a real wheel")
            with self.assertRaises(runtime_common.RuntimeErrorCode) as raised:
                bootstrap_runtime.find_offline_wheels([wheel_dir], "cp312", "cp312-win_amd64")
        self.assertEqual(raised.exception.code, "WHEEL_NOT_FOUND")
        self.assertEqual(raised.exception.details["runtime_tag"], "cp312-win_amd64")
        self.assertTrue(any(item["reason"].startswith("no wheel") for item in raised.exception.details["missing"]))


class RunPrintTest(unittest.TestCase):
    def _runtime_payloads(self, runtime_python: str) -> tuple[dict[str, object], dict[str, object]]:
        bootstrap = {
            "schema_version": "1.0.0",
            "command": "bootstrap_runtime",
            "status": "RUNTIME_READY",
            "ok": True,
            "runtime_python": runtime_python,
            "runtime_path": str(Path(runtime_python).parent.parent),
            "runtime_root": "runtime-root",
            "requirements_digest": "digest",
            "python_tag": "cp312",
        }
        doctor = {
            "schema_version": "1.0.0",
            "command": "runtime_doctor",
            "status": "PRINT_RUNTIME_OK",
            "ok": True,
            "packages": {},
        }
        return bootstrap, doctor

    def test_parser_accepts_render_and_runtime_arguments(self) -> None:
        args = run_print.build_parser().parse_args(
            [
                "--request",
                "request.json",
                "--bundle-out",
                "bundle",
                "--runtime-root",
                "runtime",
                "--python",
                "python312",
                "--offline",
                "--wheel-dir",
                "wheels",
                "--json",
            ]
        )
        self.assertEqual(args.request, "request.json")
        self.assertEqual(args.bundle_out, "bundle")
        self.assertTrue(args.offline)
        self.assertEqual(args.wheel_dir, "wheels")

    def test_run_print_constructs_isolated_render_then_preflight_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-run-print-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            request = source / "render-request.json"
            request.write_text("{\"request\": true}\n", encoding="utf-8")
            output = root / "output"
            runtime_root = root / "runtime-cache"
            runtime_python = str(runtime_root / "digest" / "cp312-win_amd64" / "Scripts" / "python.exe")
            bootstrap, doctor = self._runtime_payloads(runtime_python)
            before = request.read_bytes()
            children = [
                subprocess.CompletedProcess([], 0, '{"status":"RENDERED"}\n', ""),
                subprocess.CompletedProcess([], 0, '{"status":"PRINT_PREFLIGHT_PASS"}\n', ""),
            ]
            with patch.dict(os.environ, {"PYTHONPATH": "untrusted", "PYTHONHOME": "untrusted"}, clear=False), patch.object(bootstrap_runtime, "bootstrap", return_value=bootstrap) as mocked_bootstrap, patch.object(runtime_doctor, "doctor_print", return_value=doctor) as mocked_doctor, patch.object(run_print.subprocess, "run", side_effect=children) as mocked_run:
                payload, returncode = run_print.run_print(
                    str(request),
                    str(output),
                    runtime_root=runtime_root,
                    python="python312",
                    offline=False,
                )
            self.assertEqual(returncode, 0)
            self.assertEqual(payload["status"], "PRINT_COMPLETE")
            self.assertEqual([stage["stage"] for stage in payload["stages"]], ["render", "preflight"])
            self.assertFalse(mocked_bootstrap.call_args.args[2])
            self.assertEqual(mocked_doctor.call_args.args[2], runtime_python)
            commands = [item.args[0] for item in mocked_run.call_args_list]
            self.assertEqual(
                commands,
                [
                    [runtime_python, "-E", "-s", str(SCRIPTS / "render_pdf.py"), "--request", str(request.resolve()), "--bundle-out", str(output.resolve())],
                    [runtime_python, "-E", "-s", str(SCRIPTS / "preflight_pdf.py"), "--bundle", str(output.resolve())],
                ],
            )
            for item in mocked_run.call_args_list:
                self.assertEqual(item.kwargs["env"]["PYTHONNOUSERSITE"], "1")
                self.assertNotIn("PYTHONPATH", item.kwargs["env"])
                self.assertNotIn("PYTHONHOME", item.kwargs["env"])
            self.assertEqual(request.read_bytes(), before)
            self.assertTrue(output.is_dir())

            second_payload, second_returncode = run_print.run_print(
                str(request),
                str(output),
                runtime_root=runtime_root,
                python="python312",
                offline=False,
            )
            self.assertNotEqual(second_returncode, 0)
            self.assertEqual(second_payload["error_code"], "BUNDLE_ALREADY_EXISTS")
            self.assertEqual(mocked_run.call_count, 2)

    def test_input_paths_reject_bidirectional_source_runtime_overlap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-run-print-overlap-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            request = source / "render-request.json"
            request.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(runtime_common.RuntimeErrorCode) as inside_source:
                run_print._input_paths(str(request), str(root / "output"), source / "runtime")
            with self.assertRaises(runtime_common.RuntimeErrorCode) as source_inside_runtime:
                run_print._input_paths(str(request), str(root.parent / (root.name + "-output-2")), root)
        self.assertEqual(inside_source.exception.code, "INPUT_RUNTIME_OVERLAP")
        self.assertEqual(source_inside_runtime.exception.code, "INPUT_RUNTIME_OVERLAP")

    def test_input_paths_reject_preexisting_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-run-print-existing-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            request = source / "render-request.json"
            request.write_text("{}\n", encoding="utf-8")
            bundle = root / "output"
            bundle.mkdir()
            with self.assertRaises(runtime_common.RuntimeErrorCode) as raised:
                run_print._input_paths(str(request), str(bundle), root / "runtime")
        self.assertEqual(raised.exception.code, "BUNDLE_ALREADY_EXISTS")

    def test_render_failure_is_nonzero_and_does_not_start_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mse-run-print-fail-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            request = source / "render-request.json"
            request.write_text("{}\n", encoding="utf-8")
            runtime_python = str(root / "runtime" / "digest" / "cp312-win_amd64" / "Scripts" / "python.exe")
            bootstrap, doctor = self._runtime_payloads(runtime_python)
            failed = subprocess.CompletedProcess([], 7, "render output\n", "render error\n")
            with patch.object(bootstrap_runtime, "bootstrap", return_value=bootstrap), patch.object(runtime_doctor, "doctor_print", return_value=doctor), patch.object(run_print.subprocess, "run", return_value=failed) as mocked_run:
                payload, returncode = run_print.run_print(str(request), str(root / "output"), runtime_root=root / "cache")
            self.assertEqual(returncode, 7)
            self.assertEqual(payload["error_code"], "RENDER_FAILED")
            self.assertEqual(len(payload["stages"]), 1)
            mocked_run.assert_called_once()
            self.assertEqual(payload["stages"][0]["stdout"], "render output\n")
            self.assertEqual(payload["stages"][0]["stderr"], "render error\n")


if __name__ == "__main__":
    unittest.main()
