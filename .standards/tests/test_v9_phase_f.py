#!/usr/bin/env python3
"""Starter regression for truthful Phoenix capability claims."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PhaseFTests(unittest.TestCase):
    def test_phoenix_is_explicitly_design_only(self) -> None:
        method = (ROOT / "50-经验/Agent协作方法论/息壤方法论-V9.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        governance = (ROOT / "GOVERNANCE.md").read_text(encoding="utf-8")
        for text in (method, readme, governance):
            self.assertIn("design/reference", text)
        self.assertIn("当前无执行器", method)
        self.assertIn("未部署 scheduler 或 executor", readme)
        self.assertIn("不包含 executor 或 scheduler", governance)

    def test_starter_reflex_accepts_portable_design_evidence(self) -> None:
        reflex = (ROOT / "02-项目管理/脚本/v9-reflex-check.py").read_text(encoding="utf-8")
        self.assertIn("production_evidence or starter_evidence", reflex)
        self.assertIn('ROOT / "README.md"', reflex)
        self.assertIn('ROOT / "GOVERNANCE.md"', reflex)


if __name__ == "__main__":
    unittest.main(verbosity=2)
