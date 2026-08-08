#!/usr/bin/env python3
"""Regression test for truthful Phoenix capability state."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
METHOD = ROOT / "50-经验/Agent协作方法论/息壤方法论-V9.md"
PHOENIX_EVAL = ROOT / "50-经验/Agent进化/不死鸟Phoenix-借鉴评估报告.md"


class PhaseFTests(unittest.TestCase):
    def test_phoenix_is_explicitly_design_only(self) -> None:
        method = METHOD.read_text(encoding="utf-8")
        evaluation = PHOENIX_EVAL.read_text(encoding="utf-8")
        self.assertIn("Phoenix 当前仅为设计参考", method)
        self.assertIn("当前无执行器", method)
        self.assertNotIn("不死鸟 Phoenix 自动自愈", method)
        self.assertIn("runtime_status: design_only", evaluation)
        self.assertIn("executor: none", evaluation)
        self.assertIn("scheduler: none", evaluation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
