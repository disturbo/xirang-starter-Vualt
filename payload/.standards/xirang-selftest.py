#!/usr/bin/env python3
"""Read-only XiRang governance self-test runner.

Discovers and runs the ``test_v9_*.py`` unittest suites under
``.standards/tests/``. Every suite builds its own temporary workspace and only
reads the vault's source files, so this runner never writes into the vault.

It deliberately accepts no path-redirection flags (no ``--root`` / ``--output``)
so it is safe to register in the deny-by-default shell allowlist: the only
inputs are optional test-module names and ``--list``.

Usage:
    python3 .standards/xirang-selftest.py                 # run every test_v9_* suite
    python3 .standards/xirang-selftest.py test_v9_codex_hooks test_v9_task_decision
    python3 .standards/xirang-selftest.py --list          # list discoverable suites
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent / "tests"
DEFAULT_PATTERN = "test_v9_*.py"


def available_modules() -> list[str]:
    return sorted(path.stem for path in TESTS_DIR.glob(DEFAULT_PATTERN))


def resolve_modules(names: list[str]) -> list[str]:
    available = available_modules()
    if not names:
        return available
    chosen: list[str] = []
    for name in names:
        stem = Path(name).stem
        if stem not in available:
            raise SystemExit(f"未知的测试模块：{name}（可用：{'、'.join(available) or '无'}）")
        if stem not in chosen:
            chosen.append(stem)
    return chosen


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--"]
    if "--list" in args:
        for stem in available_modules():
            print(stem)
        return 0
    names = [arg for arg in args if not arg.startswith("-")]
    modules = resolve_modules(names)
    if not modules:
        print("没有发现任何 test_v9_* 测试模块。")
        return 1
    sys.path.insert(0, str(TESTS_DIR))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for stem in modules:
        suite.addTests(loader.loadTestsFromName(stem))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
