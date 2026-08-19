from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "references" / "rendering" / "profiles"
RESOLVER = ROOT / "scripts" / "resolve_render_profile.py"


class ProfileResolutionTest(unittest.TestCase):
    def test_both_generic_profiles_are_schema_valid_and_distinct(self):
        profiles = [json.loads((PROFILES / name).read_text()) for name in ("generic-cn-junior-english-v1.json", "generic-cn-compact-v1.json")]
        self.assertEqual({p["profile_id"] for p in profiles}, {"generic-cn-junior-english-v1", "generic-cn-compact-v1"})
        self.assertNotEqual(profiles[0]["page"]["margins_pt"], profiles[1]["page"]["margins_pt"])
        for profile in profiles:
            self.assertGreaterEqual(profile["typography"]["body_min_font_size_pt"], 10.5)
            self.assertEqual(len(profile["layout"]["reading_matching_candidates"]), 3)

    def test_resolution_is_deterministic(self):
        base = PROFILES / "generic-cn-junior-english-v1.json"
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "resolved.json"
            result = subprocess.run([sys.executable, str(RESOLVER), "--base", str(base), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            document = json.loads(output.read_text())
            self.assertEqual(document["profile_id"], "generic-cn-junior-english-v1")
            self.assertEqual(json.loads(result.stdout)["status"], "PROFILE_RESOLVED")


if __name__ == "__main__":
    unittest.main()
