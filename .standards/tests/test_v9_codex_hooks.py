#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


VAULT = Path(__file__).resolve().parents[2]
ADAPTER = VAULT / ".standards/hooks/codex-hook-adapter.py"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def status_text() -> str:
    return """---
agent_id: hongmeisu
status: idle
current_task: null
current_task_id: null
write_scope: null
scope_source: null
---
"""


def event(command: str) -> dict:
    return {
        "session_id": "codex-canary-session",
        "turn_id": "codex-canary-turn",
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_use_id": "codex-canary-tool",
        "tool_input": {"command": command},
    }


def exec_event(command: str) -> dict:
    return {
        "session_id": "codex-canary-session",
        "turn_id": "codex-canary-turn",
        "hook_event_name": "PreToolUse",
        "tool_name": "exec_command",
        "tool_use_id": "codex-exec-canary-tool",
        "tool_input": {"cmd": command},
    }


class CodexHookAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="v9-codex-hooks-")
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        hooks = self.root / ".standards/hooks"
        hooks.mkdir(parents=True)
        for name in ("pre-write-hook.sh", "post-write-hook.sh", "session-guard.sh", "heartbeat-update.sh"):
            (hooks / name).symlink_to(VAULT / ".standards/hooks" / name)
        write(self.root / "02-项目管理/智能体状态/红霉素.md", status_text())

    def run_adapter(self, mode: str, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.sys.executable, str(ADAPTER), mode],
            input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True,
            env={**os.environ, "VAULT_ROOT": str(self.root)}, check=False,
        )

    def test_apply_patch_allowed_path_reaches_post_write_as_codex(self) -> None:
        payload = event("*** Begin Patch\n*** Add File: .codex/canary.txt\n+ok\n*** End Patch")
        pre = self.run_adapter("pre-write", payload)
        self.assertEqual(0, pre.returncode, pre.stderr)
        self.assertEqual("", pre.stdout)
        post = self.run_adapter("post-write", payload)
        self.assertEqual(0, post.returncode, post.stderr)
        rows = (self.root / "02-项目管理/智能体状态/智能体事件.jsonl").read_text().splitlines()
        observed = json.loads(rows[-1])
        self.assertEqual("file_write", observed["event"])
        self.assertEqual("hongmeisu", observed["agent"])
        self.assertEqual("codex", observed["platform"])
        self.assertEqual(".codex/canary.txt", observed["file"])
        self.assertEqual("add", observed["operation"])

    def test_apply_patch_forbidden_path_returns_official_deny(self) -> None:
        payload = event("*** Begin Patch\n*** Add File: .standards/denied.txt\n+no\n*** End Patch")
        result = self.run_adapter("pre-write", payload)
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        decision = output["hookSpecificOutput"]
        self.assertEqual("PreToolUse", decision["hookEventName"])
        self.assertEqual("deny", decision["permissionDecision"])
        self.assertIn("禁止目录", decision["permissionDecisionReason"])

    def test_apply_patch_cannot_directly_set_task_card_accepted(self) -> None:
        card = self.root / "02-项目管理/任务卡/2026-07/T-CANARY.md"
        write(card, "---\nreview_status: submitted\n---\n")
        payload = event(
            "*** Begin Patch\n"
            "*** Update File: 02-项目管理/任务卡/2026-07/T-CANARY.md\n"
            "@@\n"
            "-review_status: submitted\n"
            "+review_status: accepted\n"
            "*** End Patch"
        )
        result = self.run_adapter("pre-write", payload)
        self.assertEqual(0, result.returncode, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual("deny", decision["permissionDecision"])
        self.assertIn("DIRECT_ACCEPTED_WRITE", decision["permissionDecisionReason"])

    def test_lifecycle_event_is_attributed_to_codex(self) -> None:
        payload = {"session_id": "s1", "turn_id": "t1", "source": "startup"}
        result = self.run_adapter("session-start", payload)
        self.assertEqual(0, result.returncode, result.stderr)
        row = json.loads((self.root / "02-项目管理/智能体状态/智能体事件.jsonl").read_text().splitlines()[-1])
        self.assertEqual("session_start", row["event"])
        self.assertEqual("hongmeisu", row["agent"])
        self.assertEqual("codex", row["platform"])

    def test_exec_command_read_is_allowed_and_audited_without_command_body(self) -> None:
        payload = exec_event("rg -n 'needle -> value' README.md")
        pre = self.run_adapter("pre-exec", payload)
        self.assertEqual(0, pre.returncode, pre.stderr)
        self.assertEqual("", pre.stdout)
        post = self.run_adapter("post-exec", payload)
        self.assertEqual(0, post.returncode, post.stderr)
        row = json.loads((self.root / "02-项目管理/智能体状态/智能体事件.jsonl").read_text().splitlines()[-1])
        self.assertEqual("shell_command", row["event"])
        self.assertEqual("codex", row["platform"])
        self.assertEqual("read_or_workflow", row["classification"])
        self.assertEqual(64, len(row["command_sha256"]))
        self.assertNotIn("command", row)

    def test_exec_command_direct_file_writes_are_denied_and_recorded(self) -> None:
        commands = (
            "printf x > denied.txt",
            "printf x | tee denied.txt",
            "sed -i '' 's/a/b/' denied.txt",
            "bash -c 'touch denied.txt'",
            "python3 -c \"from pathlib import Path; Path('x').write_text('x')\"",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_adapter("pre-exec", exec_event(command))
                self.assertEqual(0, result.returncode, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual("deny", decision["permissionDecision"])
                self.assertIn("apply_patch", decision["permissionDecisionReason"])
        rows = (self.root / "02-项目管理/智能体状态/智能体事件.jsonl").read_text().splitlines()
        observed = json.loads(rows[-1])
        self.assertEqual("shell_command_denied", observed["event"])
        self.assertEqual("direct_file_mutation", observed["classification"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
