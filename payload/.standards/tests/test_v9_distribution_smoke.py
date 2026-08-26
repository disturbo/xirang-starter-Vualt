from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DistributionSmokeTests(unittest.TestCase):
    def test_contract_and_methodology_closure(self) -> None:
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "9.7.0")
        policy = (ROOT / ".xirang/contract/policy.yaml").read_text(encoding="utf-8")
        self.assertIn("policy_version: 9.7.0", policy)
        self.assertTrue((ROOT / "50-经验/Agent协作方法论/息壤方法论-V9.md").is_file())
        registry = json.loads((ROOT / ".xirang/adapters/registry.json").read_text(encoding="utf-8"))
        self.assertTrue(
            {"claude", "codex", "openclaw", "hermes", "deepseek_harness", "workbuddy"}
            .issubset(set(registry["platforms"]))
        )
        self.assertTrue(all(not row["connected"] for row in registry["platforms"].values()))


if __name__ == "__main__":
    unittest.main()
