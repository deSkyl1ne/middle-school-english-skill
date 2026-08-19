from __future__ import annotations

import unittest
from pathlib import Path

from fixture_contract import (
    ADVERSARIAL_FIXTURES,
    ALL_FIXTURES,
    POSITIVE_FIXTURES,
    fixture_paths,
    load_case,
)


ROOT = Path(__file__).resolve().parents[1]


class PRDFixtureInventoryTest(unittest.TestCase):
    def test_every_prd_fixture_has_a_runtime_case_file(self):
        missing = []
        for path in fixture_paths(ROOT):
            if not path.is_dir() or not (path / "case.json").is_file():
                missing.append(str(path))
        self.assertFalse(missing, "missing runtime fixture case.json: " + ", ".join(missing))

    def test_fixture_counts_match_prd(self):
        self.assertEqual(len(POSITIVE_FIXTURES), 10)
        self.assertEqual(len(ADVERSARIAL_FIXTURES), 15)

    def test_case_metadata_is_complete_and_matches_the_prd_partition(self):
        seen_ids = set()
        for fixture_id in ALL_FIXTURES:
            case = load_case(ROOT, fixture_id)
            self.assertEqual(case.get("schema_version"), "1.0.0", fixture_id)
            self.assertEqual(case.get("id"), fixture_id, fixture_id)
            self.assertNotIn(fixture_id, seen_ids)
            seen_ids.add(fixture_id)
            if fixture_id in POSITIVE_FIXTURES:
                self.assertEqual(case.get("kind"), "positive", fixture_id)
                self.assertEqual(case.get("runner"), "render_pdf_and_preflight_pdf", fixture_id)
                self.assertEqual(case.get("expected_status"), "PRINT_PREFLIGHT_PASS", fixture_id)
                self.assertIsInstance(case.get("variant"), str, fixture_id)
            else:
                self.assertEqual(case.get("kind"), "adversarial", fixture_id)
                self.assertIsInstance(case.get("operation"), str, fixture_id)
                self.assertIsInstance(case.get("expected_error_codes"), list, fixture_id)
                self.assertTrue(case["expected_error_codes"], fixture_id)
                self.assertNotEqual(case.get("expected_status"), "PRINT_PREFLIGHT_PASS", fixture_id)
        self.assertEqual(seen_ids, set(POSITIVE_FIXTURES) | set(ADVERSARIAL_FIXTURES))


if __name__ == "__main__":
    unittest.main()
