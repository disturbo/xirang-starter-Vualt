#!/usr/bin/env python3
"""Regression tests for V9 Phase G distribution and skill resolution truth."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "02-项目管理/脚本/v9-skill-shadow-check.py"
CODEX_ADAPTER = ROOT / ".standards/hooks/codex-hook-adapter.py"
CODEX_HOOKS = ROOT / ".codex/hooks.json"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def skill(root: Path, name: str, body: str, metadata: str = "") -> None:
    target = root / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {name}\n{metadata}---\n{body}\n", encoding="utf-8",
    )


class PhaseGTests(unittest.TestCase):
    def test_unowned_divergent_copies_are_p1(self) -> None:
        module = load(CHECKER, "skill_shadow_unowned")
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            skill(base / "a", "demo", "alpha")
            skill(base / "b", "demo", "beta")
            report = module.scan([base / "a", base / "b"])
            self.assertEqual(1, report["summary"]["p1"])
            self.assertEqual("SKILL_VERSION_SHADOW", report["findings"][0]["rule_id"])

    def test_same_version_explicit_platform_variants_are_unambiguous(self) -> None:
        module = load(CHECKER, "skill_shadow_variants")
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            common = "version: 3.5.0\nx-v9-shadow-group: demo-3.5.0\n"
            skill(base / "agents", "demo", "Codex entry", common + "x-v9-variant: codex\n")
            skill(base / "claude", "demo", "Claude entry", common + "x-v9-variant: claude\n")
            report = module.scan([base / "agents", base / "claude"])
            self.assertEqual(0, report["summary"]["p1"])
            self.assertEqual(1, report["summary"]["explicit_variant_groups"])

    def test_distributed_codex_tools_derive_vault_root(self) -> None:
        adapter = CODEX_ADAPTER.read_text(encoding="utf-8")
        hooks = CODEX_HOOKS.read_text(encoding="utf-8")
        personal_path = "/Users/" + bytes.fromhex("7975646f6e67626f").decode() + "/Desktop/obsidianVault"
        self.assertNotIn(personal_path, adapter)
        self.assertIn("Path(__file__).resolve().parents[2]", adapter)
        self.assertIn('"matcher": "apply_patch|Write|Edit"', hooks)
        self.assertIn("codex-hook-adapter.py pre-write", hooks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
