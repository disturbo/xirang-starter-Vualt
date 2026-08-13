#!/usr/bin/env python3
"""Regression tests for the bounded Phoenix runtime."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
METHOD = ROOT / "50-经验/Agent协作方法论/息壤方法论-V9.md"
PHOENIX_EVAL = ROOT / "50-经验/Agent进化/不死鸟Phoenix-借鉴评估报告.md"
PHOENIX = ROOT / "02-项目管理/脚本/v9-phoenix.py"
WRAPPER = ROOT / ".standards/v9-reflex-run.sh"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class PhaseFTests(unittest.TestCase):
    def test_phoenix_is_truthfully_active(self) -> None:
        method = METHOD.read_text(encoding="utf-8")
        evaluation = PHOENIX_EVAL.read_text(encoding="utf-8")
        self.assertIn("Phoenix v1 已部署", method)
        self.assertIn("runtime_status: active_bounded", evaluation)
        self.assertIn("executor: v9-phoenix.py", evaluation)
        self.assertIn("scheduler: v9-reflex-run.sh", evaluation)
        self.assertIn('"$PHOENIX_SCRIPT" --apply-safe', WRAPPER.read_text(encoding="utf-8"))

    def test_only_allowlisted_findings_produce_repairs(self) -> None:
        module = load(PHOENIX, "phase_f_allowlist")
        findings = [
            {"rule_id": "ENTROPY_SHADOW_STALE"},
            {"rule_id": "DISTRIBUTION_DRIFT"},
            {"rule_id": "ACCEPTED_BY_SELF"},
        ]
        actions = module.repair_actions(findings)
        self.assertEqual(["refresh_entropy"], [item["action_id"] for item in actions])

    def test_allowlist_commands_ignore_environment_overrides(self) -> None:
        module = load(PHOENIX, "phase_f_fixed_commands")
        with mock.patch.dict(os.environ, {
            "XIRANG_V9_PYTHON": "/tmp/evil-python",
            "XIRANG_ENTROPY_EXECUTOR": "/tmp/evil-entropy",
            "XIRANG_GBRAIN_MAINTENANCE": "/tmp/evil-gbrain",
        }):
            catalog = module.action_catalog()
        commands = [part for item in catalog.values() for part in item["command"]]
        self.assertFalse(any(str(part).startswith("/tmp/evil-") for part in commands))

    def test_recurrence_generates_proposal_not_activation(self) -> None:
        module = load(PHOENIX, "phase_f_evolution")
        state: dict = {}
        candidates = []
        for index in range(3):
            state, candidates = module.update_observations(
                state,
                [{"rule_id": "NON_ALLOWLISTED_REPEAT", "severity": "p1", "source": "test", "object": "x"}],
                f"2026-08-13T00:00:0{index}+08:00",
            )
            if index < 2:
                state, _ = module.update_observations(
                    state, [], f"2026-08-13T00:00:1{index}+08:00",
                )
        self.assertEqual(1, len(candidates))
        self.assertTrue(candidates[0]["requires_human_review"])
        self.assertEqual("forbidden_without_external_acceptance", candidates[0]["activation"])

    def test_persistent_finding_counts_as_one_episode(self) -> None:
        module = load(PHOENIX, "phase_f_episode_dedup")
        state: dict = {}
        for index in range(3):
            state, candidates = module.update_observations(
                state,
                [{"rule_id": "PERSISTENT", "severity": "p1", "source": "test", "object": "x"}],
                f"2026-08-13T00:00:0{index}+08:00",
            )
        self.assertEqual(1, state["observations"]["PERSISTENT"]["count"])
        self.assertEqual([], candidates)
        legacy = {"observations": {"LEGACY": {"rule_id": "LEGACY", "count": 99}}}
        migrated, candidates = module.update_observations(
            legacy,
            [{"rule_id": "LEGACY", "severity": "p1", "source": "test", "object": "x"}],
            "2026-08-13T01:00:00+08:00",
        )
        self.assertEqual(1, migrated["observations"]["LEGACY"]["count"])
        self.assertEqual([], candidates)

    def test_v1_poll_counts_are_not_consumed_by_v2_runtime(self) -> None:
        module = load(PHOENIX, "phase_f_state_migration")
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            governance = runtime / "治理"
            governance.mkdir()
            (governance / "phoenix-observations.json").write_text(json.dumps({
                "schema_version": "v1",
                "observations": {"LEGACY": {"rule_id": "LEGACY", "count": 99}},
            }), encoding="utf-8")
            health = runtime / "health.json"
            health.write_text(json.dumps({
                "generated_at": "2026-08-14T00:00:00+08:00",
                "findings": [{"rule_id": "LEGACY", "severity": "p1", "source": "test", "object": "x"}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"XIRANG_V9_RUNTIME_DIR": str(runtime)}):
                report = module.build_report(health, False)
            migrated = json.loads((governance / "phoenix-observations.json").read_text(encoding="utf-8"))
            self.assertEqual("v2", migrated["schema_version"])
            self.assertEqual(1, migrated["observations"]["LEGACY"]["count"])
            self.assertEqual(0, report["upgrade_candidates"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
