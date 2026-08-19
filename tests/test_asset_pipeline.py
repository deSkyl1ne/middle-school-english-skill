from __future__ import annotations

import json
import unittest

from test_print_support import prepare_positive


class AssetPipelineTest(unittest.TestCase):
    def test_empty_asset_manifest_is_bound_and_no_placeholder_is_emitted(self):
        temp, bundle = prepare_positive()
        self.addCleanup(temp.cleanup)
        manifest = json.loads((bundle / "render-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["assets"], [])
        self.assertEqual(manifest["inputs"]["asset_manifest"]["path"], "asset-manifest.json")

if __name__ == "__main__":
    unittest.main()
