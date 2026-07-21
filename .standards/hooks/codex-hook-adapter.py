#!/usr/bin/env python3
"""Translate Codex lifecycle/tool events into the existing V9 hook contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_VAULT = Path(__file__).resolve().parents[2]
AGENT_ID = "hongmeisu"
PLATFORM = "codex"

DIRECT_FILE_MUTATORS = {
    "apply_patch", "chmod", "chown", "cp", "dd", "install", "ln", "mkdir",
    "mv", "patch", "rm", "rmdir", "tee", "touch", "truncate", "xattr",
}
INLINE_WRITE_MARKERS = (
    ".write_text(", ".write_bytes(", ".unlink(", ".rename(",
    "shutil.copy", "shutil.move", "shutil.rmtree", "os.remove(", "os.rename(",
    "os.replace(", "os.unlink(",
)
CODE_MODE_TOOL_NAMES = {"functions.exec", "exec"}
JS_LITERAL_RE = r'("(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)'


def read_event() -> dict:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def code_mode_source(event: dict) -> str:
    if event.get("tool_name") not in CODE_MODE_TOOL_NAMES:
        return ""
    tool_input = event.get("tool_input")
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("source", "code", "input", "command"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return ""


def decode_js_literal(value: str) -> str | None:
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, str) else None
    if not value.startswith("`") or not value.endswith("`") or "${" in value:
        return None
    body = value[1:-1]
    return (body.replace(r"\`", "`").replace(r"\n", "\n").replace(r"\r", "\r")
            .replace(r"\t", "\t").replace(r"\\", "\\"))


def code_mode_literals(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(rf"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*({JS_LITERAL_RE})", re.DOTALL)
    for match in pattern.finditer(source):
        decoded = decode_js_literal(match.group(2))
        if decoded is not None:
            values[match.group(1)] = decoded
    return values


def patch_commands(event: dict) -> list[str]:
    tool_input = event.get("tool_input")
    if event.get("tool_name") == "apply_patch" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        return [command] if isinstance(command, str) else []
    source = code_mode_source(event)
    if not source or "tools.apply_patch" not in source:
        return []
    literals = code_mode_literals(source)
    commands: list[str] = []
    for match in re.finditer(rf"tools\.apply_patch\s*\(\s*([A-Za-z_$][\w$]*|{JS_LITERAL_RE})", source, re.DOTALL):
        value = match.group(1)
        decoded = literals.get(value) if not value.startswith(('"', '`')) else decode_js_literal(value)
        if decoded is not None:
            commands.append(decoded)
    return commands


def patch_targets(event: dict) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for command in patch_commands(event):
        for match in re.finditer(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", command, re.MULTILINE):
            item = (match.group(2), match.group(1).lower())
            if item not in targets:
                targets.append(item)
    return targets


def shell_commands(event: dict) -> list[str]:
    tool_input = event.get("tool_input")
    if event.get("tool_name") in {"exec_command", "Bash"} and isinstance(tool_input, dict):
        for key in ("cmd", "command"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return [value]
        return []
    source = code_mode_source(event)
    if not source or "tools.exec_command" not in source:
        return []
    commands: list[str] = []
    for marker in re.finditer(r"tools\.exec_command\s*\(", source):
        tail = source[marker.end():]
        match = re.search(rf"\bcmd\s*:\s*({JS_LITERAL_RE})", tail, re.DOTALL)
        if not match:
            continue
        decoded = decode_js_literal(match.group(1))
        if decoded is not None:
            commands.append(decoded)
    return commands


def shell_command(event: dict) -> str:
    return "\n\n".join(shell_commands(event))


def shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def classify_shell_command(command: str, *, nested: bool = False) -> dict:
    """Fail closed for direct filesystem mutation while allowing audited workflows.

    Commands that call a repository workflow may have intentional side effects; those
    remain visible through the shell audit event. Direct shell file mutation is denied
    because it bypasses path-level pre/post-write hooks and must use apply_patch.
    """
    tokens = shell_tokens(command)
    reasons: list[str] = []
    for index, token in enumerate(tokens):
        executable = Path(token).name
        if executable in DIRECT_FILE_MUTATORS:
            reasons.append(f"direct_mutator:{executable}")
        if executable in {"sed", "perl"} and any(
            value == "-i" or value.startswith("-i") for value in tokens[index + 1:index + 5]
        ):
            reasons.append(f"in_place_editor:{executable}")
        if token in {">", ">>"}:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if target not in {"/dev/null", "/dev/stdout", "/dev/stderr"} and not target.startswith("/dev/fd/"):
                reasons.append("output_redirection")
        if executable in {"python", "python3", "bash", "sh", "zsh"} and "-c" in tokens[index + 1:index + 4]:
            try:
                inline = tokens[tokens.index("-c", index + 1) + 1]
            except (ValueError, IndexError):
                inline = ""
            if inline and any(marker in inline for marker in INLINE_WRITE_MARKERS):
                reasons.append(f"inline_writer:{executable}")
            if inline and not nested:
                nested_result = classify_shell_command(inline, nested=True)
                reasons.extend(f"nested:{reason}" for reason in nested_result["reasons"])
    unique = sorted(set(reasons))
    first = next((Path(token).name for token in tokens if token not in {";", "&&", "||", "|"}), "")
    return {
        "blocked": bool(unique),
        "classification": "direct_file_mutation" if unique else "read_or_workflow",
        "executable": first,
        "reasons": unique,
    }


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
    patch_requested = event.get("tool_name") == "apply_patch" or "tools.apply_patch" in code_mode_source(event)
    if patch_requested and not targets:
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


def active_task_id(vault: Path) -> str:
    status_file = vault / "02-项目管理/智能体状态/红霉素.md"
    try:
        text = status_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^current_task_id:\s*["\']?([^"\'\n]+)', text, re.MULTILINE)
    return "" if not match or match.group(1).strip() in {"null", "None"} else match.group(1).strip()


def append_shell_audit(event: dict, vault: Path, result: dict, lifecycle: str) -> None:
    command = shell_command(event)
    event_file = vault / "02-项目管理/智能体状态/智能体事件.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": lifecycle,
        "agent": AGENT_ID,
        "platform": PLATFORM,
        "task_id": active_task_id(vault),
        "tool": event.get("tool_name"),
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "command_bytes": len(command.encode()),
        "classification": result["classification"],
        "executable": result["executable"],
        "reasons": result["reasons"],
    }
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_shell_hook(event: dict, vault: Path, mode: str) -> int:
    command = shell_command(event)
    shell_requested = event.get("tool_name") in {"exec_command", "Bash"} or "tools.exec_command" in code_mode_source(event)
    if not shell_requested:
        return 0
    result = classify_shell_command(command)
    if mode == "pre-exec":
        if not command:
            append_shell_audit(event, vault, {
                "classification": "unparseable", "executable": "", "reasons": ["command_missing"],
            }, "shell_command_denied")
            deny("Codex exec_command 未提供可审计 cmd/command，已 fail-closed。")
        elif result["blocked"]:
            append_shell_audit(event, vault, result, "shell_command_denied")
            deny("exec_command 检测到直接文件变更，必须改用 apply_patch：" + ", ".join(result["reasons"]))
        return 0
    append_shell_audit(event, vault, result, "shell_command")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=(
        "pre-write", "post-write", "pre-exec", "post-exec",
        "session-guard", "session-start", "session-stop",
    ))
    args = parser.parse_args()
    event = read_event()
    vault = Path(os.environ.get("VAULT_ROOT", str(DEFAULT_VAULT))).expanduser().resolve()
    if args.mode == "pre-write":
        return run_path_hook(event, vault, args.mode)
    if args.mode == "post-write":
        return run_path_hook(event, vault, args.mode)
    if args.mode in {"pre-exec", "post-exec"}:
        return run_shell_hook(event, vault, args.mode)
    if args.mode == "session-guard":
        return run_session_guard(event, vault)
    return append_lifecycle(event, vault, args.mode.replace("-", "_"))


if __name__ == "__main__":
    raise SystemExit(main())
