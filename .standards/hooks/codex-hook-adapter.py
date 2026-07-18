#!/usr/bin/env python3
"""Translate Codex lifecycle/tool events into the existing V9 hook contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_VAULT = Path(__file__).resolve().parents[2]
AGENT_ID = "hongmeisu"
PLATFORM = "codex"


def read_event() -> dict:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def patch_targets(event: dict) -> list[tuple[str, str]]:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    targets: list[tuple[str, str]] = []
    for match in re.finditer(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", command, re.MULTILINE):
        item = (match.group(2), match.group(1).lower())
        if item not in targets:
            targets.append(item)
    return targets


def legacy_event(event: dict, path: str, operation: str) -> dict:
    adapted = dict(event)
    adapted["tool_input"] = {
        "file_path": path,
        "codex_operation": operation,
        "codex_patch": (event.get("tool_input") or {}).get("command", ""),
    }
    return adapted


def hook_env(vault: Path) -> dict[str, str]:
    inherited_path = os.environ.get("PATH", "")
    return {
        **os.environ,
        "PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{inherited_path}",
        "XIRANG_PYTHON_BIN": "/usr/bin/python3",
        "VAULT_ROOT": str(vault),
        "V8_AGENT_ID": AGENT_ID,
        "V8_PLATFORM": PLATFORM,
    }


def deny(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason[:1800],
        }
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run_path_hook(event: dict, vault: Path, mode: str) -> int:
    targets = patch_targets(event)
    if event.get("tool_name") == "apply_patch" and not targets:
        if mode == "pre-write":
            deny("Codex apply_patch 未解析到可审计目标路径，已 fail-closed。")
        return 0
    script_name = "pre-write-hook.sh" if mode == "pre-write" else "post-write-hook.sh"
    script = vault / ".standards/hooks" / script_name
    if not script.is_file():
        if mode == "pre-write":
            deny(f"V9 门禁脚本不存在：{script}")
        return 0
    for path, operation in targets:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            input=json.dumps(legacy_event(event, path, operation), ensure_ascii=False),
            text=True,
            capture_output=True,
            env=hook_env(vault),
            timeout=45,
            check=False,
        )
        if mode == "pre-write" and proc.returncode != 0:
            reason = proc.stderr.strip() or proc.stdout.strip() or f"V9 门禁拒绝：{path}"
            deny(reason)
            return 0
    return 0


def run_session_guard(event: dict, vault: Path) -> int:
    script = vault / ".standards/hooks/session-guard.sh"
    if not script.is_file():
        return 0
    proc = subprocess.run(
        ["/bin/bash", str(script)], input=json.dumps(event, ensure_ascii=False), text=True,
        capture_output=True, env=hook_env(vault), timeout=30, check=False,
    )
    if proc.stderr:
        print(json.dumps({"systemMessage": proc.stderr.strip()[:1800]}, ensure_ascii=False))
    return 0


def append_lifecycle(event: dict, vault: Path, lifecycle: str) -> int:
    event_file = vault / "02-项目管理/智能体状态/智能体事件.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": lifecycle,
        "agent": AGENT_ID,
        "platform": PLATFORM,
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "source": event.get("source"),
    }
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    heartbeat = vault / ".standards/hooks/heartbeat-update.sh"
    if heartbeat.is_file():
        subprocess.run(
            ["/bin/bash", str(heartbeat), AGENT_ID, str(event.get("session_id") or "")],
            env=hook_env(vault), timeout=20, check=False,
        )
    return 0


def import_cost_usage(event: dict, vault: Path) -> None:
    """Best-effort incremental import from the local Codex rollout metadata."""
    session_id = str(event.get("session_id") or "").strip()
    importer = vault / ".standards/codex-cost-import.py"
    if not session_id or not importer.is_file():
        return
    try:
        subprocess.run(
            ["/usr/bin/python3", str(importer), "--session-id", session_id, "--json"],
            capture_output=True, text=True, env=hook_env(vault), timeout=45, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pre-write", "post-write", "session-guard", "session-start", "session-stop"))
    args = parser.parse_args()
    event = read_event()
    vault = Path(os.environ.get("VAULT_ROOT", str(DEFAULT_VAULT))).expanduser().resolve()
    if args.mode == "pre-write":
        return run_path_hook(event, vault, args.mode)
    if args.mode == "post-write":
        result = run_path_hook(event, vault, args.mode)
        import_cost_usage(event, vault)
        return result
    if args.mode == "session-guard":
        return run_session_guard(event, vault)
    if args.mode == "session-stop":
        import_cost_usage(event, vault)
    return append_lifecycle(event, vault, args.mode.replace("-", "_"))


if __name__ == "__main__":
    raise SystemExit(main())
