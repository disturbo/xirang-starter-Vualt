#!/usr/bin/env python3
"""Regression tests for V9 Phase H long-session runtime stability."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / ".standards/hooks/codex-hook-adapter.py"
HANDSHAKE = ROOT / ".standards/v8-handshake.sh"
REFLEX = ROOT / "02-项目管理/脚本/v9-reflex-check.py"
FREEZE = ROOT / "02-项目管理/脚本/v9-freeze-observation.py"
REFLEX_WRAPPER = ROOT / ".standards/v9-reflex-run.sh"
FRONTMATTER_TEST = ROOT / ".standards/tests/test_frontmatter_lint.py"
CLOSEOUT = ROOT / ".standards/v8-closeout-check.py"
PRE_START = ROOT / ".standards/pre-start-check.py"
PROJECT_OPS = ROOT / "02-项目管理/脚本/project-ops-check.py"
ITERATION_OPS = ROOT / "02-项目管理/脚本/v9-iteration-ops-check.py"
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


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PhaseHTests(unittest.TestCase):
    def test_iteration_state_separates_delivery_and_preparation(self) -> None:
        module = load(ITERATION_OPS, "phase_h_iteration_state")
        text = (
            "<!-- xirang-iteration-state: current=260725 preparation=260828 -->\n"
            "[[迭代/260828迭代/README|准备区]]\n"
            "[[迭代/260725迭代/README|交付区]]\n"
        )
        self.assertEqual(
            ("260725", "迭代/260725迭代", "260828"),
            module.iteration_state_from_readme(text),
        )

    def test_preparation_iteration_requires_minimal_intake_contract(self) -> None:
        module = load(ITERATION_OPS, "phase_h_preparation_contract")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prep = root / "迭代/260828迭代"
            (prep / "迭代管理").mkdir(parents=True)
            (prep / "README.md").write_text(
                "---\niteration: \"260828\"\nphase: intake\n---\n", encoding="utf-8"
            )
            (prep / "迭代管理/260828-正式需求清单.md").write_text("# 清单\n", encoding="utf-8")
            findings, context = module.check_preparation_iteration(root, "260725", "260828")
            self.assertEqual([], findings)
            self.assertEqual(2, context["preparation_docs_found"])

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

    def test_freeze_accepts_portable_artifact_tree_release_identity(self) -> None:
        module = load(FREEZE, "phase_h_freeze_tree_identity")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "runtime.txt"
            artifact.write_text("bounded runtime\n", encoding="utf-8")
            roots = {"starter": str(root)}
            artifacts = [{"root": "starter", "path": "runtime.txt", "sha256": module.sha256(artifact)}]
            tree = module.artifact_tree_sha256(roots, artifacts, "starter")
            report = module.check_distribution({
                "roots": roots,
                "releases": [{"name": "starter", "root": "starter", "tree_sha256": tree}],
                "artifacts": artifacts,
            })
            self.assertEqual("pass", report["status"], report)

    def test_dream_partial_is_visible_as_degraded_advisory(self) -> None:
        module = load(REFLEX, "phase_h_dream_degraded")
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "maintenance-dream.json"
            now = module.now_local()
            state_path.write_text(json.dumps({
                "action": "dream",
                "status": "degraded",
                "reason": "dream_partial",
                "updated_at": now.isoformat(),
                "last_completed_at": now.isoformat(),
                "quality": {
                    "cycle_status": "partial",
                    "warning_phases": ["lint", "orphans"],
                    "summaries": {
                        "lint": "0 fix(es) applied, 609 remaining",
                        "orphans": "613 orphan page(s) out of 873 total",
                    },
                },
            }), encoding="utf-8")
            findings, check = module._maintenance_freshness(
                "dream", state_path, now, 8,
            )
            self.assertEqual(["GBRAIN_DREAM_DEGRADED"], [item["rule_id"] for item in findings])
            self.assertEqual("advisory", findings[0]["severity"])
            self.assertEqual("degraded", check["status"])

    def test_frontmatter_clean_file_count_regression_suite(self) -> None:
        proc = subprocess.run(
            ["/usr/bin/python3", str(FRONTMATTER_TEST)],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Ran 16 tests", proc.stderr)

    def test_closeout_accepts_formal_task_card_scope_and_handoff(self) -> None:
        sys.path.insert(0, str(CLOSEOUT.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLOSEOUT.parent)))
        module = load(CLOSEOUT, "phase_h_formal_closeout")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            card = root / "02-项目管理/任务卡/2026-08/T-FORMAL.md"
            card.parent.mkdir(parents=True)
            card.write_text(
                "---\ntask_id: T-FORMAL\nstatus: done\npaths:\n"
                "  allowed_write_roots:\n    - \".standards/\"\n---\n\n"
                "## Handoff\n\n- status: done/submitted\n"
                "- artifacts: `.standards/result.txt`\n"
                "- verification: pass\n- next action: review\n",
                encoding="utf-8",
            )
            artifact = root / ".standards/result.txt"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("verified\n", encoding="utf-8")
            board = root / "00-MOC/多智能体协作看板.md"
            board.parent.mkdir(parents=True)
            board.write_text("# Board\n\nT-FORMAL\n\n## Handoff\n\n- none\n", encoding="utf-8")
            module.VAULT_ROOT = root
            module.EVENT_FILE = root / "02-项目管理/智能体状态/智能体事件.jsonl"
            module.KANBAN_FILE = board
            module.AGENT_STATUS_DIR = root / "02-项目管理/智能体状态"
            issues = module.run_closeout_check("T-FORMAL", "hongmeisu", "M5")
            self.assertEqual([], issues)

    def test_reflex_wrapper_refreshes_untrusted_harness_before_health(self) -> None:
        script = REFLEX_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("harness-eval-verify.py", script)
        self.assertIn("v9-harness-eval-runner.py", script)
        self.assertIn("--write-latest", script)
        self.assertIn("runtime-contract-current.md", script)
        self.assertIn("refresh_gbrain_contract_if_needed\ngbrain_rc=$?", script)
        self.assertIn("refresh_gbrain_dream_if_stale\ndream_rc=$?", script)
        self.assertLess(script.index("refresh_gbrain_contract_if_needed\ngbrain_rc=$?"), script.index('"$SCRIPT" --quiet'))
        self.assertLess(script.index("refresh_gbrain_dream_if_stale\ndream_rc=$?"), script.index('"$SCRIPT" --quiet'))
        self.assertLess(script.index("refresh_harness_if_needed"), script.index('"$SCRIPT" --quiet'))
        for index, body in enumerate(re.findall(r"<<'PY'\n(.*?)\nPY", script, re.DOTALL), 1):
            compile(body, f"v9-reflex-run.sh:heredoc-{index}", "exec")
        proc = subprocess.run(["/bin/bash", "-n", str(REFLEX_WRAPPER)], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            vault = base / "vault"
            runtime = base / "runtime"
            vault.mkdir()
            wrapper = base / "v9-reflex-run.sh"
            wrapper.write_text(REFLEX_WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
            wrapper.chmod(0o755)
            verifier = base / "verify.py"
            runner = base / "runner.py"
            reflex = base / "reflex.py"
            summary = base / "summary.py"
            phoenix = base / "phoenix.py"
            verifier.write_text(
                "import pathlib, sys\n"
                "report = pathlib.Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "raise SystemExit(0 if report.is_file() else 1)\n",
                encoding="utf-8",
            )
            runner.write_text(
                "import os, pathlib\n"
                "report = pathlib.Path(os.environ['XIRANG_V9_HARNESS_REPORT'])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text('{\"check\": \"fixture\"}\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            reflex.write_text(
                "import json, os, pathlib\n"
                "from datetime import datetime\n"
                "path = pathlib.Path(os.environ['XIRANG_V9_RUNTIME_DIR']) / '巡检/health-latest.json'\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                "path.write_text(json.dumps({'generated_at': datetime.now().astimezone().isoformat(timespec='seconds')}) + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            summary.write_text(
                "import json, os, pathlib\n"
                "from datetime import datetime\n"
                "runtime = pathlib.Path(os.environ['XIRANG_V9_RUNTIME_DIR'])\n"
                "inspect = runtime / '巡检'\n"
                "health = json.loads((inspect / 'health-latest.json').read_text(encoding='utf-8'))\n"
                "status_path = inspect / 'status-latest.json'\n"
                "payload = {'schema_version': 'v1', 'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'status': 'green', 'paths': {'runtime_dir': str(inspect.resolve()), 'status_latest': str(status_path.resolve())}, 'parts': {'health': {'status': 'green', 'generated_at': health['generated_at']}, 'harness_eval': {'status': 'green'}}}\n"
                "status_path.write_text(json.dumps(payload) + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            phoenix.write_text(
                "import json, os, pathlib\n"
                "from datetime import datetime\n"
                "path = pathlib.Path(os.environ['XIRANG_V9_RUNTIME_DIR']) / '巡检/phoenix-latest.json'\n"
                "path.write_text(json.dumps({'schema_version': 'v1', 'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'status': 'success', 'mode': 'apply_safe', 'repairs_applied': 0, 'upgrade_candidates': 0, 'safety': {'source_note_edits': False, 'gate_changes': False, 'self_acceptance': False, 'manifest_changes': False}}) + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            contract_source = base / "runtime-contract-source.md"
            contract_mirror = base / "runtime-contract-current.md"
            maintenance = base / "maintenance.sh"
            maintenance_marker = base / "maintenance-ran"
            dream_state = base / "maintenance-dream.json"
            contract_source.write_text("current contract\n", encoding="utf-8")
            contract_mirror.write_text("stale contract\n", encoding="utf-8")
            maintenance.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$1\" >> \"$GBRAIN_MAINTENANCE_MARKER\"\n",
                encoding="utf-8",
            )
            maintenance.chmod(0o755)
            harness_report = runtime / "巡检/harness-eval-latest.json"
            env = {
                **os.environ,
                "XIRANG_V9_TEST_MODE": "1",
                "XIRANG_V9_VAULT_DIR": str(vault),
                "XIRANG_V9_RUNTIME_DIR": str(runtime),
                "XIRANG_V9_PYTHON": "/usr/bin/python3",
                "XIRANG_V9_REFLEX_SCRIPT": str(reflex),
                "XIRANG_V9_STATUS_SCRIPT": str(summary),
                "XIRANG_V9_PHOENIX_SCRIPT": str(phoenix),
                "XIRANG_V9_HARNESS_SCRIPT": str(runner),
                "XIRANG_V9_HARNESS_VERIFY_SCRIPT": str(verifier),
                "XIRANG_V9_HARNESS_REPORT": str(harness_report),
                "XIRANG_GBRAIN_CONTRACT_SOURCE": str(contract_source),
                "XIRANG_GBRAIN_CONTRACT_MIRROR": str(contract_mirror),
                "XIRANG_GBRAIN_MAINTENANCE": str(maintenance),
                "XIRANG_GBRAIN_DREAM_STATE": str(dream_state),
                "GBRAIN_MAINTENANCE_MARKER": str(maintenance_marker),
            }
            proc = subprocess.run(
                ["/bin/bash", str(wrapper)], capture_output=True, text=True,
                env=env, timeout=60,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertTrue(harness_report.is_file())
            self.assertEqual(contract_source.read_bytes(), contract_mirror.read_bytes())
            self.assertEqual(0o600, contract_mirror.stat().st_mode & 0o777)
            self.assertEqual(
                ["sync", "dream"],
                maintenance_marker.read_text(encoding="utf-8").splitlines(),
            )
            state = json.loads((runtime / "巡检/reflex-scheduler-health.json").read_text(encoding="utf-8"))
            self.assertEqual("success", state["status"])
            self.assertEqual("completed_status_green", state["reason"])

            phoenix.write_text("raise SystemExit(9)\n", encoding="utf-8")
            failed = subprocess.run(
                ["/bin/bash", str(wrapper)], capture_output=True, text=True,
                env=env, timeout=60,
            )
            self.assertEqual(9, failed.returncode)
            failed_state = json.loads(
                (runtime / "巡检/reflex-scheduler-health.json").read_text(encoding="utf-8")
            )
            self.assertEqual("failed", failed_state["status"])
            self.assertEqual("phoenix_failed", failed_state["reason"])

            verifier.write_text("raise SystemExit(7)\n", encoding="utf-8")
            combined = subprocess.run(
                ["/bin/bash", str(wrapper)], capture_output=True, text=True,
                env=env, timeout=60,
            )
            self.assertEqual(9, combined.returncode)
            combined_state = json.loads(
                (runtime / "巡检/reflex-scheduler-health.json").read_text(encoding="utf-8")
            )
            self.assertEqual("failed", combined_state["status"])
            self.assertEqual("phoenix_failed", combined_state["reason"])

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
