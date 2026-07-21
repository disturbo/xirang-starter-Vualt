#!/usr/bin/env python3
"""Regression tests for V9 Phase H long-session runtime stability."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / ".standards/hooks/codex-hook-adapter.py"
HANDSHAKE = ROOT / ".standards/v8-handshake.sh"
REFLEX = ROOT / "02-项目管理/脚本/v9-reflex-check.py"
FREEZE = ROOT / "02-项目管理/脚本/v9-freeze-observation.py"
ENTROPY = ROOT / "02-项目管理/脚本/v9-entropy-governance.py"
PRE_START = ROOT / ".standards/pre-start-check.py"
PROJECT_OPS = ROOT / "02-项目管理/脚本/project-ops-check.py"
COST_HOOK = ROOT / ".standards/hooks/cost-event.sh"
COST_FUSE = ROOT / ".standards/cost-fuse.py"
SPAWN_BUDGET = ROOT / ".standards/spawn-budget-check.py"
COMPLIANCE_SOURCE = ROOT / ".prompt-src/v9-compliance-block.md"
PREFLIGHT_SOURCE = ROOT / ".prompt-src/preflight-auto-template.md"
AGENT_CONTRACT = ROOT / ".standards/agent-contract.yaml"
AGENT_STATE_LINT = ROOT / ".standards/agent-state-lint.py"
AGENT_STATE_SCHEMA = ROOT / ".standards/schemas/agent-state.schema.json"
EVENT_SPEC = ROOT / "30-规范/事件规范.md"
METHOD_MAIN = ROOT / "50-经验/Agent协作方法论/息壤方法论-V9.md"
CURRENT_GUIDANCE = (
    METHOD_MAIN,
    ROOT / "50-经验/Agent协作方法论/息壤V9-写入声明与Pre-flight.md",
    ROOT / "50-经验/Agent协作方法论/息壤V9-Gate与Hook机制.md",
    ROOT / "50-经验/Agent协作方法论/息壤V9-子任务与通信.md",
)
CONSTRAINT_DIR = ROOT / "30-规范/智能体约束"
STATUS_TEMPLATE_DIR = ROOT / "02-项目管理/智能体状态"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class PhaseHTests(unittest.TestCase):
    def test_codex_adapter_pins_system_python(self) -> None:
        adapter = load(ADAPTER, "phase_h_adapter")
        env = adapter.hook_env(ROOT)
        self.assertTrue(env["PATH"].startswith("/usr/bin:/bin:/usr/sbin:/sbin:"))
        self.assertEqual("/usr/bin/python3", env["XIRANG_PYTHON_BIN"])
        self.assertNotIn("codex-cost-import.py", ADAPTER.read_text(encoding="utf-8"))

    def test_handshake_uses_pinned_python(self) -> None:
        lines = HANDSHAKE.read_text(encoding="utf-8").splitlines()
        self.assertIn('V8_PYTHON="${XIRANG_PYTHON_BIN:-/usr/bin/python3}"', lines)
        executable_bare = [
            line for line in lines
            if "python3" in line and not line.lstrip().startswith("#") and not line.startswith("V8_PYTHON=")
        ]
        self.assertEqual([], executable_bare)
        self.assertNotIn("cost-event.sh", "\n".join(lines))
        handshake = "\n".join(lines)
        self.assertIn("CODEX_THREAD_ID", handshake)
        self.assertIn('_v8_default_agent_id', handshake)
        self.assertNotIn("cost_ceiling_cny", handshake)
        self.assertNotIn("cost_fuse: pending", handshake)

    def test_pre_start_no_longer_requires_retired_cost_budget(self) -> None:
        module = load(PRE_START, "phase_h_pre_start")
        with tempfile.TemporaryDirectory() as raw:
            tasks = Path(raw)
            module.TASKS_DIR = str(tasks)
            (tasks / "T-NO-COST.md").write_text(
                "---\nowner: hongmeisu\nstatus: ready\ndeliverables:\n  - path: out.md\n---\n",
                encoding="utf-8",
            )
            result = module.check_task_card("T-NO-COST")
            self.assertEqual("pass", result["status"], result)
            self.assertNotIn("budget", result["checks"])

    def test_project_ops_no_longer_requires_retired_cost_budget(self) -> None:
        module = load(PROJECT_OPS, "phase_h_project_ops")
        self.assertNotIn("budget", module.REQUIRED_KEYS)

    def test_active_prompt_sources_do_not_require_retired_cost_budget(self) -> None:
        for source in (
            COMPLIANCE_SOURCE,
            PREFLIGHT_SOURCE,
            AGENT_CONTRACT,
            AGENT_STATE_LINT,
            AGENT_STATE_SCHEMA,
            EVENT_SPEC,
        ):
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("budget", text.lower(), source)
            self.assertNotIn("预算：", text, source)
            self.assertNotIn("agent-cost-events", text, source)
            self.assertNotIn("cost_cny", text, source)
            self.assertNotIn("cost_policy", text, source)
            self.assertNotIn("cost_tracking", text, source)

        forbidden_guidance = (
            "- 预算：",
            "agent-cost-events.py append",
            "cost-event.sh checkpoint",
            "spawn-budget-check.py",
            "| cost-fuse |",
            "`budget` / `paths.allowed_write_roots`",
        )
        for source in CURRENT_GUIDANCE:
            text = source.read_text(encoding="utf-8")
            if source == METHOD_MAIN:
                text = text.split("## 13 ·", 1)[0]
            for marker in forbidden_guidance:
                self.assertNotIn(marker, text, source)
        for source in sorted(CONSTRAINT_DIR.glob("*.md")):
            text = source.read_text(encoding="utf-8")
            for marker in forbidden_guidance:
                self.assertNotIn(marker, text, source)
        for source in sorted(STATUS_TEMPLATE_DIR.glob("*.md")):
            frontmatter = source.read_text(encoding="utf-8").split("---", 2)[1]
            self.assertNotIn("cost_tracking", frontmatter, source)

    def test_freeze_requires_consecutive_calendar_days(self) -> None:
        module = load(FREEZE, "phase_h_freeze")
        now = module.parse_iso("2026-07-19T12:00:00+08:00")
        self.assertIsNotNone(now)
        history = {
            "2026-07-17": {"daily_status": "pass"},
            "2026-07-18": {"daily_status": "pass"},
            "2026-07-19": {"daily_status": "pass"},
        }
        self.assertEqual(3, module.consecutive_pass_days(history, now))
        history["2026-07-18"]["daily_status"] = "fail"
        self.assertEqual(1, module.consecutive_pass_days(history, now))

    def test_freeze_hook_evidence_cannot_precede_freeze_start(self) -> None:
        module = load(FREEZE, "phase_h_freeze_hook_window")
        hooks = {"matchers": ["apply_patch", "exec_command", "pre-exec", "post-exec"]}
        manifest = {
            "freeze_started_at": "2026-07-19T16:29:00+08:00",
            "hook_evidence": {
                "file_write_after": "2026-07-18T21:55:00+08:00",
                "shell_denied_after": "2026-07-19T16:32:00+08:00",
            },
        }
        events = [
            {
                "ts": "2026-07-18T22:00:00+08:00", "event": "file_write",
                "platform": "codex", "agent": "hongmeisu",
            },
            {
                "ts": "2026-07-19T16:33:00+08:00", "event": "shell_command_denied",
                "platform": "codex", "agent": "hongmeisu",
            },
        ]
        result = module.check_hook_evidence(events, hooks, manifest)
        self.assertEqual("fail", result["status"])
        self.assertEqual(manifest["freeze_started_at"], result["detail"]["file_write_after"])
        events.append({
            "ts": "2026-07-19T16:34:00+08:00", "event": "file_write",
            "platform": "codex", "agent": "hongmeisu",
        })
        self.assertEqual("pass", module.check_hook_evidence(events, hooks, manifest)["status"])

    def test_freeze_consumption_respects_source_cadence(self) -> None:
        module = load(FREEZE, "phase_h_freeze_consumption_cadence")
        now = module.parse_iso("2026-07-21T12:00:00+08:00")
        self.assertIsNotNone(now)
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            inspect = runtime / "巡检"
            governance = runtime / "治理"
            inspect.mkdir()
            governance.mkdir()

            def write(path: Path, payload: dict) -> None:
                path.write_text(json.dumps(payload), encoding="utf-8")

            write(governance / "entropy-governance-queue.json", {"updated_at": "2026-07-20T09:00:00+08:00"})
            write(inspect / "health-latest.json", {"generated_at": "2026-07-21T11:50:00+08:00"})
            write(inspect / "harness-eval-latest.json", {"generated_at": "2026-07-21T11:40:00+08:00"})
            write(inspect / "status-latest.json", {
                "generated_at": "2026-07-21T11:55:00+08:00",
                "parts": {"harness_eval": {"verification": {"valid": True}}},
            })
            result = module.check_consumption(runtime, now)
            self.assertEqual("pass", result["status"], result)
            self.assertTrue(result["detail"]["freshness"]["entropy_queue"]["fresh"])
            write(governance / "entropy-governance-queue.json", {"updated_at": "2026-07-12T09:00:00+08:00"})
            self.assertEqual("fail", module.check_consumption(runtime, now)["status"])

    def test_entropy_default_disposition_converges_without_note_edits(self) -> None:
        module = load(ENTROPY, "phase_h_entropy")
        detector = {
            "detector_version": "2.0.0", "mode": "shadow",
            "findings": [{
                "category": "broken_link", "confidence": "confirmed", "source": "A.md",
                "target": "Missing/X", "reason": "missing",
            }],
        }
        queue = module.ingest({}, detector, "2026-07-01T09:00:00+08:00")
        queue = module.ingest(queue, detector, "2026-07-08T09:00:00+08:00")
        self.assertEqual("deferred", queue["items"][0]["status"])
        queue = module.ingest(queue, detector, "2026-07-15T09:00:00+08:00")
        queue = module.ingest(queue, detector, "2026-07-22T09:00:00+08:00")
        self.assertEqual("archived", queue["items"][0]["status"])
        self.assertEqual(0, queue["metrics"]["current_open"])

    def test_cost_pipeline_is_explicitly_retired(self) -> None:
        self.assertNotIn("codex_cost_telemetry", REFLEX.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as raw:
            env = {**os.environ, "VAULT_ROOT": raw}
            proc = subprocess.run(
                ["/bin/bash", str(COST_HOOK), "start", "T-RETIRED", "hongmeisu"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(3, proc.returncode)
            self.assertIn("V9-RETIRED", proc.stderr)
            self.assertFalse((Path(raw) / "02-项目管理/智能体状态/智能体事件.jsonl").exists())
        for tool in (COST_FUSE, SPAWN_BUDGET):
            proc = subprocess.run(["/usr/bin/python3", str(tool)], capture_output=True, text=True)
            self.assertEqual(3, proc.returncode)
            self.assertIn('"status": "retired"', proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
