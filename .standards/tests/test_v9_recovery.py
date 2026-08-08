#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path


VAULT_ROOT = Path(__file__).resolve().parents[2]
HANDSHAKE = VAULT_ROOT / ".standards" / "v8-handshake.sh"
PRE_HOOK = VAULT_ROOT / ".standards" / "hooks" / "pre-write-hook.sh"
POST_HOOK = VAULT_ROOT / ".standards" / "hooks" / "post-write-hook.sh"
SESSION_GUARD = VAULT_ROOT / ".standards" / "hooks" / "session-guard.sh"
HEARTBEAT_UPDATE = VAULT_ROOT / ".standards" / "hooks" / "heartbeat-update.sh"
GATE = VAULT_ROOT / ".standards" / "gate-enforce.py"
CLOSEOUT = VAULT_ROOT / ".standards" / "v8-closeout-check.py"
DETECTOR = Path.home() / ".hermes/skills/yijing-dms/spec-auto-fusion/scripts/doc-gardening.py"
JSONL_READER = VAULT_ROOT / ".standards/jsonl_reader.py"
EVENT_MIGRATOR = VAULT_ROOT / ".standards/event-jsonl-migrate.py"
PROJECT_OPS = VAULT_ROOT / "02-项目管理/脚本/project-ops-check.py"
V9_REFLEX = VAULT_ROOT / "02-项目管理/脚本/v9-reflex-check.py"
V9_STATUS_SUMMARY = VAULT_ROOT / "02-项目管理/脚本/v9-status-summary.py"
GBRAIN_MAINTENANCE = Path.home() / ".gbrain/maintenance-run.sh"
GBRAIN_CLI = Path.home() / ".npm-global/bin/gbrain"
M3_MARKER = Path("/tmp/.v8-m3-context-claudian.json")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def status_text(status: str, task_id: str = "null", scope: str = "null", source: str = "null") -> str:
    return f"""---
agent_id: claudian
status: {status}
current_task: null
current_task_id: {task_id}
write_scope: {scope}
scope_source: {source}
---
# Embedded copy
agent_id: claudian
current_task_id: stale-body-value
"""


class V9RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="v9-recovery-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        M3_MARKER.unlink(missing_ok=True)
        self.addCleanup(lambda: M3_MARKER.unlink(missing_ok=True))

    def hook_input(self, relative_path: str) -> str:
        return json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.tmp / relative_path), "content": "test"},
        }, ensure_ascii=False)

    def run_hook(self, hook: Path, relative_path: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "VAULT_ROOT": str(self.tmp), "V8_AGENT_ID": "claudian"}
        return subprocess.run(
            ["bash", str(hook)], input=self.hook_input(relative_path), text=True,
            capture_output=True, env=env, check=False,
        )

    def test_atomic_closeout_only_updates_first_frontmatter(self) -> None:
        status = self.tmp / "Claudian.md"
        write(status, status_text("busy", '"T-1"', '"10-项目/"', "task_card"))
        command = (
            f"source {shlex_quote(str(HANDSHAKE))}; "
            f"_v8_close_status_atomic {shlex_quote(str(status))} '2026-07-16T12:00:00+08:00'"
        )
        subprocess.run(["bash", "-c", command], check=True, env={**os.environ, "VAULT_ROOT": str(self.tmp)})
        content = status.read_text(encoding="utf-8")
        frontmatter, body = content.split("---", 2)[1:]
        self.assertIn("status: idle", frontmatter)
        self.assertIn("current_task_id: null", frontmatter)
        self.assertIn("write_scope: null", frontmatter)
        self.assertIn("scope_source: null", frontmatter)
        self.assertIn("heartbeat_pid: null", frontmatter)
        self.assertIn("heartbeat_session: null", frontmatter)
        self.assertIn("heartbeat_source: null", frontmatter)
        self.assertIn("current_task_id: stale-body-value", body)

    def test_safe_yaml_update_only_updates_first_frontmatter(self) -> None:
        status = self.tmp / "Claudian.md"
        write(status, status_text("busy", '"T-1"', '"10-项目/"', "task_card"))
        command = (
            f"source {shlex_quote(str(HANDSHAKE))}; "
            f"_v8_safe_update_yaml {shlex_quote(str(status))} agent_id claudian-v9"
        )
        subprocess.run(["bash", "-c", command], check=True, env={**os.environ, "VAULT_ROOT": str(self.tmp)})
        content = status.read_text(encoding="utf-8")
        frontmatter, body = content.split("---", 2)[1:]
        self.assertIn("agent_id: claudian-v9", frontmatter)
        self.assertIn("agent_id: claudian", body)

    def test_handshake_frontmatter_reader_ignores_body_status(self) -> None:
        status = self.tmp / "Claudian.md"
        write(status, status_text("idle") + "status: busy\n")
        command = (
            f"source {shlex_quote(str(HANDSHAKE))}; "
            f"_v8_frontmatter_value {shlex_quote(str(status))} status"
        )
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=True,
            env={**os.environ, "VAULT_ROOT": str(self.tmp)},
        )
        self.assertEqual("idle", result.stdout.strip())

    def test_handshake_task_id_reservation_skips_history_and_existing_dirs(self) -> None:
        day = datetime.now().strftime("%Y%m%d")
        event_file = self.tmp / "02-项目管理/智能体状态/智能体事件.jsonl"
        write(
            event_file,
            "".join(
                json.dumps({"event": "task_start", "task_id": f"T-{day}-{n:02d}"}) + "\n"
                for n in range(1, 98)
            ),
        )
        (self.tmp / "_temp" / f"T-{day}-98").mkdir(parents=True)
        command = (
            f"VAULT_ROOT={shlex_quote(str(self.tmp))}; "
            f"source {shlex_quote(str(HANDSHAKE))}; "
            "first=$(_v8_reserve_task_id); first_rc=$?; "
            "second=$(_v8_reserve_task_id); second_rc=$?; "
            "printf '%s|%s\\n%s|%s\\n' \"$first\" \"$first_rc\" \"$second\" \"$second_rc\""
        )
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=True,
        )
        first, second = result.stdout.strip().splitlines()
        self.assertEqual(f"T-{day}-99|0", first)
        self.assertEqual("|1", second)
        self.assertTrue((self.tmp / "_temp" / f"T-{day}-99").is_dir())
        self.assertIn("已耗尽", result.stderr)

    def test_heartbeat_update_only_reads_and_updates_first_frontmatter(self) -> None:
        status = self.tmp / "02-项目管理/智能体状态/Claudian.md"
        write(status, status_text("busy", '"T-HB"', '".standards/"', "task_card") + "last_heartbeat: body-value\n")
        result = subprocess.run(
            ["bash", str(HEARTBEAT_UPDATE), "claudian", "test-session"],
            text=True, capture_output=True, check=False,
            env={**os.environ, "VAULT_ROOT": str(self.tmp)},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        content = status.read_text(encoding="utf-8")
        frontmatter, body = content.split("---", 2)[1:]
        self.assertIn("heartbeat_session: \"test-session\"", frontmatter)
        self.assertIn("last_heartbeat: body-value", body)

    def test_session_guard_warns_when_reflex_snapshot_is_missing(self) -> None:
        status = self.tmp / "02-项目管理/智能体状态/Claudian.md"
        write(status, status_text("idle"))
        stamp = Path("/tmp/.v8-session-guard-stamp-claudian")
        stamp.unlink(missing_ok=True)
        self.addCleanup(lambda: stamp.unlink(missing_ok=True))
        result = subprocess.run(
            ["bash", str(SESSION_GUARD)], text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "VAULT_ROOT": str(self.tmp),
                "V8_AGENT_ID": "claudian",
                "XIRANG_V9_RUNTIME_DIR": str(self.tmp / "runtime"),
            },
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("第一反射器快照不存在", result.stderr)

    def test_session_guard_warns_when_reflex_scheduler_is_not_loaded(self) -> None:
        status = self.tmp / "02-项目管理/智能体状态/Claudian.md"
        write(status, status_text("idle"))
        runtime = self.tmp / "runtime/巡检"
        write(runtime / "health-latest.json", "{}\n")
        launchctl = self.tmp / "launchctl"
        write(launchctl, "#!/bin/sh\nexit 1\n")
        launchctl.chmod(0o755)
        stamp = Path("/tmp/.v8-session-guard-stamp-claudian")
        stamp.unlink(missing_ok=True)
        self.addCleanup(lambda: stamp.unlink(missing_ok=True))
        result = subprocess.run(
            ["bash", str(SESSION_GUARD)], text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "VAULT_ROOT": str(self.tmp),
                "V8_AGENT_ID": "claudian",
                "XIRANG_V9_RUNTIME_DIR": str(self.tmp / "runtime"),
                "XIRANG_LAUNCHCTL": str(launchctl),
            },
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("第一反射器 launchd 未加载", result.stderr)

    def test_reflex_runtime_liveness_detects_missing_cli_and_stale_sync(self) -> None:
        sync_state = self.tmp / "maintenance-sync.json"
        dream_state = self.tmp / "maintenance-dream.json"
        old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=10)).isoformat()
        write(sync_state, json.dumps({"status": "success", "updated_at": old}))
        write(dream_state, json.dumps({"status": "success", "updated_at": old}))
        entropy = self.tmp / "entropy"
        write(entropy / "影子熵报告-2026-07-18.md", "# shadow\n")
        missing_cli = self.tmp / "missing-gbrain"
        fake_ollama = self.tmp / "ollama"
        write(fake_ollama, "#!/bin/sh\necho 'bge-m3:latest x x'\n")
        fake_ollama.chmod(0o755)
        fake_launchctl = self.tmp / "launchctl"
        write(fake_launchctl, "#!/bin/sh\necho 'state = running'\n")
        fake_launchctl.chmod(0o755)
        runtime_health = self.tmp / "runtime-health.json"
        write(runtime_health, json.dumps({"state": "running", "updated_at": old}))
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            "f,c=m.collect_runtime_liveness(m.now_local(),2,8,216);"
            "print(json.dumps({'rules':[x['rule_id'] for x in f],'checks':c}))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], cwd=self.tmp,
            text=True, capture_output=True, check=True,
            env={
                **os.environ,
                "XIRANG_GBRAIN_CLI": str(missing_cli),
                "XIRANG_OLLAMA_CLI": str(fake_ollama),
                "XIRANG_LAUNCHCTL": str(fake_launchctl),
                "XIRANG_GBRAIN_RUNTIME_HEALTH": str(runtime_health),
                "XIRANG_GBRAIN_SYNC_STATE": str(sync_state),
                "XIRANG_GBRAIN_DREAM_STATE": str(dream_state),
                "XIRANG_ENTROPY_SHADOW_DIR": str(entropy),
            },
        )
        rules = set(json.loads(result.stdout)["rules"])
        self.assertIn("GBRAIN_CLI_MISSING", rules)
        self.assertIn("GBRAIN_SYNC_STALE", rules)
        self.assertIn("GBRAIN_DREAM_STALE", rules)

    def test_reflex_runtime_liveness_rejects_wrong_gbrain_package(self) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        gbrain = self.tmp / "gbrain"
        write(gbrain, "#!/bin/sh\necho 'gbrain 1.3.1'\n")
        gbrain.chmod(0o755)
        ollama = self.tmp / "ollama"
        write(ollama, "#!/bin/sh\necho 'bge-m3:latest x x'\n")
        ollama.chmod(0o755)
        launchctl = self.tmp / "launchctl"
        write(launchctl, "#!/bin/sh\necho 'state = running'\n")
        launchctl.chmod(0o755)
        runtime_health = self.tmp / "runtime-health.json"
        write(runtime_health, json.dumps({"state": "running", "updated_at": now}))
        sync_state = self.tmp / "maintenance-sync.json"
        dream_state = self.tmp / "maintenance-dream.json"
        write(sync_state, json.dumps({"status": "success", "updated_at": now}))
        write(dream_state, json.dumps({"status": "success", "updated_at": now}))
        entropy = self.tmp / "entropy"
        write(entropy / "影子熵报告-2026-07-18.md", "# shadow\n")
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            "f,c=m.collect_runtime_liveness(m.now_local(),2,8,216);"
            "print(json.dumps([x['rule_id'] for x in f]))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], cwd=self.tmp,
            text=True, capture_output=True, check=True,
            env={
                **os.environ,
                "XIRANG_GBRAIN_CLI": str(gbrain),
                "XIRANG_OLLAMA_CLI": str(ollama),
                "XIRANG_LAUNCHCTL": str(launchctl),
                "XIRANG_GBRAIN_RUNTIME_HEALTH": str(runtime_health),
                "XIRANG_GBRAIN_SYNC_STATE": str(sync_state),
                "XIRANG_GBRAIN_DREAM_STATE": str(dream_state),
                "XIRANG_ENTROPY_SHADOW_DIR": str(entropy),
            },
        )
        rules = set(json.loads(result.stdout))
        self.assertIn("GBRAIN_VERSION_DRIFT", rules)

    def test_reflex_runtime_liveness_detects_scheduler_conflict_and_missing_cron(self) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        gbrain = self.tmp / "gbrain"
        write(gbrain, "#!/bin/sh\necho 'gbrain 0.33.0'\n")
        gbrain.chmod(0o755)
        ollama = self.tmp / "ollama"
        write(ollama, "#!/bin/sh\necho 'bge-m3:latest x x'\n")
        ollama.chmod(0o755)
        launchctl = self.tmp / "launchctl"
        write(launchctl, "#!/bin/sh\necho 'state = running'\n")
        launchctl.chmod(0o755)
        crontab = self.tmp / "crontab"
        write(crontab, "#!/bin/sh\nexit 0\n")
        crontab.chmod(0o755)
        sync_state = self.tmp / "maintenance-sync.json"
        dream_state = self.tmp / "maintenance-dream.json"
        write(sync_state, json.dumps({"status": "success", "last_success_at": now}))
        write(dream_state, json.dumps({"status": "success", "last_success_at": now}))
        entropy = self.tmp / "entropy"
        write(entropy / "影子熵报告-2026-07-18.md", "# shadow\n")
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            "f,c=m.collect_runtime_liveness(m.now_local(),2,8,216);"
            "print(json.dumps([x['rule_id'] for x in f]))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], cwd=self.tmp,
            text=True, capture_output=True, check=True,
            env={
                **os.environ,
                "XIRANG_GBRAIN_CLI": str(gbrain),
                "XIRANG_OLLAMA_CLI": str(ollama),
                "XIRANG_LAUNCHCTL": str(launchctl),
                "XIRANG_CRONTAB": str(crontab),
                "XIRANG_GBRAIN_SYNC_STATE": str(sync_state),
                "XIRANG_GBRAIN_DREAM_STATE": str(dream_state),
                "XIRANG_ENTROPY_SHADOW_DIR": str(entropy),
            },
        )
        rules = set(json.loads(result.stdout))
        self.assertIn("GBRAIN_SCHEDULER_CONFLICT", rules)
        self.assertIn("GBRAIN_CRON_MISSING", rules)

    def test_reflex_runtime_liveness_detects_dead_entropy_scheduler(self) -> None:
        old = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=20)).isoformat()
        heartbeat = self.tmp / "ticker_heartbeat"
        write(heartbeat, old + "\n")
        stale_epoch = time.time() - 20 * 60
        os.utime(heartbeat, (stale_epoch, stale_epoch))
        jobs = self.tmp / "jobs.json"
        write(jobs, json.dumps({"jobs": [{
            "id": "328aa7f7b498", "enabled": False, "state": "paused",
            "schedule": {"expr": "0 9 * * 0"}, "no_agent": False,
            "script": None, "last_status": "error",
        }]}))
        state = self.tmp / "entropy-state.json"
        write(state, json.dumps({
            "status": "failed", "reason": "detector_exit", "updated_at": old,
        }))
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            "f,c=m.collect_runtime_liveness(m.now_local(),2,8,216);"
            "print(json.dumps([x['rule_id'] for x in f]))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], cwd=self.tmp,
            text=True, capture_output=True, check=True,
            env={
                **os.environ,
                "XIRANG_HERMES_CRON_HEARTBEAT": str(heartbeat),
                "XIRANG_HERMES_CRON_JOBS": str(jobs),
                "XIRANG_ENTROPY_JOB_STATE": str(state),
            },
        )
        rules = set(json.loads(result.stdout))
        self.assertIn("HERMES_CRON_SCHEDULER_STALE", rules)
        self.assertIn("ENTROPY_SCHEDULER_INVALID", rules)
        self.assertIn("ENTROPY_JOB_STATE_INVALID", rules)

    def test_reflex_maintenance_skip_does_not_fake_or_erase_success(self) -> None:
        now = datetime.now(timezone.utc).astimezone()
        state = self.tmp / "maintenance-sync.json"
        write(state, json.dumps({
            "status": "skipped",
            "reason": "cycle_locked",
            "updated_at": now.isoformat(),
            "last_success_at": now.isoformat(),
        }))
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            f"f,c=m._maintenance_freshness('sync',m.Path({str(state)!r}),m.now_local(),2);"
            "print(json.dumps({'rules':[x['rule_id'] for x in f],'check':c}))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual([], data["rules"])
        self.assertEqual("ok", data["check"]["status"])

    def test_gbrain_scheduled_sync_uses_local_truth_without_git_pull(self) -> None:
        script = GBRAIN_MAINTENANCE.read_text(encoding="utf-8")
        self.assertIn('"$GBRAIN" sync --repo "$REPO" --no-pull', script)
        self.assertNotIn('"$GBRAIN" sync --repo "$REPO" 2>&1', script)

    def test_gbrain_lint_distinguishes_embedded_from_wrapping_markdown_fence(self) -> None:
        embedded = self.tmp / "embedded.md"
        write(
            embedded,
            "---\ntitle: Test\ntype: guide\ncreated: 2026-07-18\n---\n\n"
            "# Guide\n\n```markdown\n# Example\n```\n\nClosing guidance.\n",
        )
        embedded_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(embedded)],
            text=True, capture_output=True, check=False,
        )
        self.assertNotIn("code-fence-wrap", embedded_result.stdout + embedded_result.stderr)

        wrapped = self.tmp / "wrapped.md"
        write(wrapped, "```markdown\n# Wrapped page\n```\n")
        wrapped_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(wrapped)],
            text=True, capture_output=True, check=False,
        )
        self.assertIn("code-fence-wrap", wrapped_result.stdout + wrapped_result.stderr)

        date_format = self.tmp / "date-format.md"
        write(
            date_format,
            "---\ntitle: Date formats\ntype: guide\ncreated: 2026-07-18\n---\n\n"
            "| Field | Format |\n|---|---|\n| Created date | YYYY-MM-DD |\n",
        )
        date_format_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(date_format)],
            text=True, capture_output=True, check=False,
        )
        self.assertNotIn("placeholder-date", date_format_result.stdout + date_format_result.stderr)

        unfilled = self.tmp / "unfilled.md"
        write(unfilled, "---\ntitle: Unfilled\ntype: guide\ncreated: YYYY-MM-DD\n---\n")
        unfilled_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(unfilled)],
            text=True, capture_output=True, check=False,
        )
        self.assertIn("placeholder-date", unfilled_result.stdout + unfilled_result.stderr)

        valid_flow_array = self.tmp / "valid-flow-array.md"
        write(
            valid_flow_array,
            "---\ntitle: Flow array\ntype: guide\ncreated: 2026-07-18\n"
            "aliases: [\"A\", \"B\", \"C\"]\n---\n",
        )
        valid_flow_array_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(valid_flow_array)],
            text=True, capture_output=True, check=False,
        )
        self.assertNotIn(
            "frontmatter-nested-quotes",
            valid_flow_array_result.stdout + valid_flow_array_result.stderr,
        )

        malformed_nested = self.tmp / "malformed-nested.md"
        write(
            malformed_nested,
            "---\ntitle: \"Outer \"Inner\" Value\"\ntype: guide\ncreated: 2026-07-18\n---\n",
        )
        malformed_nested_result = subprocess.run(
            [str(GBRAIN_CLI), "lint", str(malformed_nested)],
            text=True, capture_output=True, check=False,
        )
        self.assertIn(
            "frontmatter-nested-quotes",
            malformed_nested_result.stdout + malformed_nested_result.stderr,
        )

    def test_reflex_gbrain_lint_contract_rejects_false_positive(self) -> None:
        correct = self.tmp / "gbrain-correct"
        write(
            correct,
            "#!/bin/sh\n"
            "if [ \"$1\" = lint ]; then\n"
            "  case \"$2\" in *wrapped.md) echo code-fence-wrap;; *unfilled.md) echo placeholder-date;; "
            "*malformed-nested.md) echo frontmatter-nested-quotes;; esac\n"
            "  exit 0\n"
            "fi\n"
            "echo 'gbrain 0.33.0'\n",
        )
        correct.chmod(0o755)
        broken = self.tmp / "gbrain-broken"
        write(
            broken,
            "#!/bin/sh\n"
            "if [ \"$1\" = lint ]; then\n"
            "  case \"$2\" in *wrapped.md) echo code-fence-wrap;; *unfilled.md) echo placeholder-date;; "
            "*valid-flow-array.md|*malformed-nested.md) echo frontmatter-nested-quotes;; esac\n"
            "  exit 0\n"
            "fi\n"
            "echo 'gbrain 0.33.0'\n",
        )
        broken.chmod(0o755)
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            f"print(json.dumps([m._gbrain_lint_contract(m.Path({str(correct)!r})),"
            f"m._gbrain_lint_contract(m.Path({str(broken)!r}))]))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], text=True, capture_output=True, check=True,
        )
        good, bad = json.loads(result.stdout)
        self.assertTrue(good[0])
        self.assertFalse(bad[0])
        self.assertEqual("flow_sequence_false_positive", bad[1])

    def test_project_ops_does_not_treat_calendar_gaps_as_missing_m3_logs(self) -> None:
        result = subprocess.run(
            [sys_executable(), str(PROJECT_OPS), "--today", "2026-07-17", "--json"],
            cwd=self.tmp, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        rules = {item["rule_id"] for item in report["findings"]}
        self.assertNotIn("TODAY_LOG_MISSING", rules)
        self.assertNotIn("LOG_GAP", rules)

    def test_health_badge_stays_red_for_suppressed_unresolved_p1(self) -> None:
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_STATUS_SUMMARY)!r};"
            "s=importlib.util.spec_from_file_location('status_summary',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['status_summary']=m;s.loader.exec_module(m);"
            "now=m.datetime.now(m.timezone.utc).astimezone();"
            "h={'generated_at':now.isoformat(),'sources_failed':[],"
            "'summary':{'p0':0,'p1':1,'advisory':0,'active_p0':0,'active_p1':0,'active_advisory':0}};"
            "print(json.dumps(m.health_status(h,None,now,24)))"
        )
        result = subprocess.run([sys_executable(), "-c", code], text=True, capture_output=True, check=True)
        self.assertEqual("red", json.loads(result.stdout)["status"])

    def test_reflex_heartbeat_ignores_status_examples_below_frontmatter(self) -> None:
        write(
            self.tmp / "02-项目管理/智能体状态/Claudian.md",
            status_text("idle") + "status: busy\nlast_heartbeat: 2020-01-01T00:00:00+08:00\n",
        )
        code = (
            "import importlib.util,json,sys;"
            f"p={str(V9_REFLEX)!r};"
            "s=importlib.util.spec_from_file_location('v9_reflex',p);"
            "m=importlib.util.module_from_spec(s);sys.modules['v9_reflex']=m;s.loader.exec_module(m);"
            "print(json.dumps(m.collect_heartbeat(m.now_local(),24)))"
        )
        result = subprocess.run(
            [sys_executable(), "-c", code], cwd=self.tmp,
            text=True, capture_output=True, check=True,
        )
        self.assertEqual([], json.loads(result.stdout))

    def test_jsonl_reader_skips_and_counts_invalid_history(self) -> None:
        sample = self.tmp / "events.jsonl"
        write(sample, '{"event":"ok"}\n     2|{"event":"legacy"}\n[]\n\n')
        result = subprocess.run(
            [sys_executable(), "-c", (
                "import importlib.util,json,sys;"
                f"p={str(JSONL_READER)!r};"
                "s=importlib.util.spec_from_file_location('jr',p);"
                "m=importlib.util.module_from_spec(s);sys.modules['jr']=m;s.loader.exec_module(m);"
                f"rows,d=m.read_jsonl(__import__('pathlib').Path({str(sample)!r}));"
                "print(json.dumps({'rows':rows,'diag':d.to_dict()}))"
            )],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual([{"event": "ok"}], data["rows"])
        self.assertEqual(2, data["diag"]["invalid_rows"])
        self.assertEqual(1, data["diag"]["valid_rows"])

    def test_event_migrator_repairs_only_known_legacy_forms(self) -> None:
        events = self.tmp / "events.jsonl"
        write(events, (
            '{"event":"task_start","agent":"claudian"}\n'
            '     2|{"event":"task_end","agent":"claudian"}\n'
            '{"event":"file_write","agent":"claudian\n'
            'claudian","task_id":"T-OLD","file":"10-项目/old.md"}\n'
        ))
        backup_dir = self.tmp / "backups"
        result = subprocess.run(
            [sys_executable(), str(EVENT_MIGRATOR), "--path", str(events),
             "--backup-dir", str(backup_dir), "--apply", "--json"],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["applied"])
        self.assertEqual(1, data["stats"]["number_pipe_repaired"])
        self.assertEqual(1, data["stats"]["split_agent_pairs_repaired"])
        rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(3, len(rows))
        self.assertEqual("claudian", rows[-1]["agent"])
        self.assertEqual(data["stats"]["source_sha256"], __import__("hashlib").sha256(
            Path(data["backup_path"]).read_bytes()
        ).hexdigest())

    def test_event_migrator_aborts_on_unknown_corruption(self) -> None:
        events = self.tmp / "events.jsonl"
        write(events, '{"event":"ok"}\nnot recoverable\n')
        result = subprocess.run(
            [sys_executable(), str(EVENT_MIGRATOR), "--path", str(events), "--json"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("migration aborted", result.stderr)

    def test_idle_core_write_is_blocked_without_m3_marker(self) -> None:
        write(self.tmp / "02-项目管理/智能体状态/Claudian.md", status_text("idle", '"T-OLD"', '"10-项目/"', "task_card"))
        result = self.run_hook(PRE_HOOK, "10-项目/new.md")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("核心目录写入需要", result.stderr)

    def test_m3_marker_allows_one_exact_core_write_and_is_consumed(self) -> None:
        write(self.tmp / "02-项目管理/智能体状态/Claudian.md", status_text("idle"))
        M3_MARKER.write_text(json.dumps({
            "agent": "claudian", "task": "single edit", "scope": "10-项目/one.md",
            "created_at_epoch": int(time.time()), "max_writes": 1,
        }), encoding="utf-8")
        self.assertEqual(0, self.run_hook(PRE_HOOK, "10-项目/one.md").returncode)
        self.assertEqual(2, self.run_hook(PRE_HOOK, "10-项目/two.md").returncode)
        self.assertEqual(0, self.run_hook(POST_HOOK, "10-项目/one.md").returncode)
        self.assertFalse(M3_MARKER.exists())
        event_file = self.tmp / "02-项目管理/智能体状态/智能体事件.jsonl"
        self.assertFalse(event_file.exists(), "M3 should not write event.jsonl")

    def test_post_hook_uses_first_frontmatter_and_ignores_idle_stale_task(self) -> None:
        write(self.tmp / "02-项目管理/智能体状态/Claudian.md", status_text("idle", '"T-OLD"', '"10-项目/"', "task_card"))
        result = self.run_hook(POST_HOOK, "10-项目/new.md")
        self.assertEqual(0, result.returncode, result.stderr)
        line = (self.tmp / "02-项目管理/智能体状态/智能体事件.jsonl").read_text(encoding="utf-8").strip()
        event = json.loads(line)
        self.assertEqual("claudian", event["agent"])
        self.assertIsNone(event["task_id"])
        self.assertNotIn("\n", line)

    def test_busy_core_write_requires_matching_active_task_context(self) -> None:
        write(
            self.tmp / "02-项目管理/智能体状态/Claudian.md",
            status_text("busy", '"T-ACTIVE"', '"10-项目/"', "task_card"),
        )
        write(self.tmp / "_temp/T-ACTIVE/task-card.yaml", """task_id: T-ACTIVE
authorized_paths:
  - 10-项目/
""")
        gate_copy = self.tmp / ".standards/gate-enforce.py"
        gate_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(GATE, gate_copy)
        result = self.run_hook(PRE_HOOK, "10-项目/allowed.md")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_m5_closeout_recognizes_chinese_handoff_without_run_log(self) -> None:
        task_id = "T-HANDOFF-CN"
        write(
            self.tmp / "02-项目管理/智能体状态/智能体事件.jsonl",
            json.dumps({"event": "file_write", "task_id": task_id, "file": ".standards/x.py"}) + "\n",
        )
        write(
            self.tmp / "00-MOC/多智能体协作看板.md",
            f"## 任务队列\n{task_id}\n\n## 交接记录\n\n### {task_id}\n- status: submitted\n",
        )
        write(self.tmp / "02-项目管理/智能体状态/Claudian.md", status_text("busy", f'"{task_id}"', '".standards/"', "task_card"))
        result = subprocess.run(
            [sys_executable(), str(CLOSEOUT), "--task-id", task_id, "--agent", "claudian", "--gear", "M5", "--json"],
            text=True, capture_output=True, check=False,
            env={**os.environ, "VAULT_ROOT": str(self.tmp)},
        )
        data = json.loads(result.stdout)
        rules = {item["rule_id"] for item in data.get("issues", []) + data.get("advisories", [])}
        self.assertNotIn("NO_HANDOFF", rules)
        self.assertNotIn("NO_RUN_LOG", rules)

    def test_detector_fixture_precision_and_seeded_detection(self) -> None:
        vault = self.tmp / "vault"
        write(vault / "Valid.md", "---\naliases: [有效别名]\n---\n# Valid\n")
        write(vault / "Source.md", """[[Valid]] [[有效别名]] [[folder/Missing]] [[ExternalDocument]]
`[[folder/ExampleOnly]]`
[[assets/present.png]]
""")
        write(vault / "assets/present.png", "not-a-real-png")
        write(vault / "Dataview.md", '```dataview\nLIST FROM "Area"\n```\n')
        write(vault / "Area/Covered.md", "# Covered\n")
        write(vault / "Loose.md", "# Needs review orphan\n")
        duplicate_body = "# Shared\n" + ("same normalized content " * 30)
        write(vault / "A/Alpha.md", duplicate_body)
        write(vault / "B/Beta.md", duplicate_body)
        write(vault / "ModuleA/README.md", "# Module A\n" + ("alpha " * 50))
        write(vault / "ModuleB/README.md", "# Module B\n" + ("beta " * 50))
        write(vault / "Active/Thing-v1.0.md", "old")
        write(vault / "Active/Thing-v2.0.md", "new")
        write(vault / "60-归档/Thing-v1.0.md", "archive old")
        write(vault / "60-归档/Thing-v2.0.md", "archive new")

        result = subprocess.run(
            [sys_executable(), str(DETECTOR), "--vault", str(vault), "--no-write", "--json"],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        confirmed = [item for item in data["findings"] if item["confidence"] == "confirmed"]
        signatures = {(item["category"], item["source"], item["target"]) for item in confirmed}
        self.assertIn(("broken_link", "Source.md", "folder/Missing"), signatures)
        self.assertTrue(any(item["category"] == "duplicate" for item in confirmed))
        self.assertTrue(any(item["category"] == "expired_version" and item["source"] == "Active/Thing-v1.0.md" for item in confirmed))
        self.assertFalse(any(item["target"] in {"Valid", "有效别名", "assets/present.png", "folder/ExampleOnly"} for item in confirmed))
        self.assertFalse(any(item["category"] == "duplicate" and "README.md" in item["source"] for item in confirmed))
        self.assertFalse(any(item["category"] == "expired_version" and item["source"].startswith("60-归档/") for item in confirmed))

    def test_detector_shadow_report_satisfies_frontmatter_contract(self) -> None:
        vault = self.tmp / "vault"
        write(vault / "Source.md", "# Source\n[[Missing/Target]]\n")
        result = subprocess.run(
            [sys_executable(), str(DETECTOR), "--vault", str(vault), "--shadow", "--json"],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        report = Path(data["report_path"])
        content = report.read_text(encoding="utf-8")
        frontmatter = content.split("---", 2)[1]
        self.assertIn("type: 熵报告", frontmatter)
        self.assertIn("created:", frontmatter)
        self.assertIn("detector_version: 2.0.0", frontmatter)
        self.assertIn("mode: shadow", frontmatter)


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def sys_executable() -> str:
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main(verbosity=2)
