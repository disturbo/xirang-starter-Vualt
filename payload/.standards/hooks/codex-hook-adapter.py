#!/usr/bin/env python3
"""Portable Codex/Claude hook adapter for XiRang's ordinary-user package.

File writes require one active task and must stay inside its declared roots.
XiRang's control plane cannot be edited with ordinary file tools. Shell access
is deny-by-default. Successful post-write receipts live outside the workspace
and are required by the acceptance backend.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


STANDARDS_DIR = Path(__file__).resolve().parents[1]
if str(STANDARDS_DIR) not in sys.path:
    sys.path.insert(0, str(STANDARDS_DIR))
from xirang_state import (
    StateConflict, StateStore, canonical_operation, canonical_task_kind,
    refresh_events_projection, scope_covers,
)


DEFAULT_ROOT = Path(__file__).resolve().parents[2]
PLATFORM = os.environ.get("XIRANG_PLATFORM", "codex")
AGENT_ID = os.environ.get("XIRANG_AGENT_ID", "codex" if PLATFORM == "codex" else "claude")
CODE_MODE_NAMES = {"functions.exec", "exec"}
JS_LITERAL_RE = r'("(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)'
TASK_ACTIVE = {"in_progress", "blocked"}
WRITE_STAGES = {"implementing", "repairing", "revalidating"}
READ_ONLY_PROGRAMS = {
    "cat", "cut", "diff", "du", "file", "find", "git", "grep",
    "head", "ls", "md5", "pwd", "readlink", "rg", "sed", "shasum", "sort",
    "stat", "tail", "test", "true", "uname", "wc", "which",
}
SAFE_GIT_SUBCOMMANDS = {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
SAFE_PYTHON_WORKFLOWS = {
    ".standards/xirang-task.py", ".standards/xirang_runtime.py", ".standards/xirang_delivery.py",
    ".standards/xirang_state_migrate.py", ".standards/xirang_state_backup.py",
    ".standards/xirang-configure.py", ".standards/xirang-canary.py",
    ".standards/xirang-user-status.py",
    ".standards/xirang-rescue.py",
    ".prompt-src/prompt-build.py",
}
PROTECTED_PREFIXES = (
    ".git/", ".xirang/", ".standards/",
    ".codex/", ".claude/", "02-项目管理/任务卡/",
)
PROTECTED_FILES = {".git", "AGENTS.md", "GOVERNANCE.md", "VERSION", "setup.sh", "息壤.md"}
AUTHORIZATION_DEFAULT_INTENTS = {
    "continue_execution", "adversarial_review", "no_intermediate_confirmation",
}
AUTHORIZATION_ALLOWED_INTENTS = AUTHORIZATION_DEFAULT_INTENTS | {"report_once_no_prompt"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def expired_at(value: str, *, current: datetime | None = None) -> bool:
    try:
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc) <= (current or datetime.now(timezone.utc)).astimezone(timezone.utc)


def read_event() -> dict:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def runtime_dir(root: Path) -> Path:
    try:
        configured = json.loads((root / ".xirang/local-config.json").read_text(encoding="utf-8")).get("runtime_dir")
    except (OSError, json.JSONDecodeError, TypeError):
        configured = None
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".xirang/workspaces" / workspace_id(root)


def active_state_store(root: Path) -> StateStore | None:
    """Return the active backend; an existing broken database always fails closed."""
    from xirang_state_cli import sqlite_authority_artifacts_present

    path = runtime_dir(root) / "state" / "state.sqlite3"
    if not path.exists():
        if sqlite_authority_artifacts_present(path):
            raise RuntimeError("SQLite state directory exists but authority database is missing")
        return None
    store = StateStore(path)
    try:
        active = store.is_backend_active()
    except Exception as exc:
        raise RuntimeError(f"SQLite authority database is unreadable: {type(exc).__name__}") from exc
    if not active:
        raise RuntimeError("SQLite authority database exists but is not strictly active")
    return store


def active_task_access(root: Path, session_id: str, *, require_write: bool = False,
                       capability: str | None = None) -> dict | None:
    store = active_state_store(root)
    if store is None:
        return None
    candidates = []
    for task in store.list_tasks(lifecycle_statuses=["authorized", "in_progress", "blocked"]):
        selected_capability = capability or ("file_write" if require_write else "read")
        access = store.resolve_task_access(
            session_id=session_id, task_id=task["task_id"], capability=selected_capability)
        if (access is not None and require_write and access.get("kind") == "lease"
                and access["lease"].get("read_only")):
            access = None
        if access is not None and require_write and access["task"].get("runtime_status") not in WRITE_STAGES:
            access = None
        if access is not None and selected_capability == "delivery" and access["task"].get("runtime_status") != "committing":
            access = None
        if access is not None:
            candidates.append(access)
    if len(candidates) != 1:
        return None
    candidates[0]["task"]["execution_authorized"] = store.task_execution_authorized(
        candidates[0]["task"]["task_id"]
    )
    return candidates[0]


def event_log(root: Path) -> Path:
    return runtime_dir(root) / "events" / "events.jsonl"


def atomic_append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def record(root: Path, event_name: str, source: dict, **extra: object) -> dict:
    payload = {
        "ts": now_iso(), "event": event_name, "platform": PLATFORM, "agent": AGENT_ID,
        "session_id": str(source.get("session_id") or ""), "turn_id": str(source.get("turn_id") or ""), **extra,
    }
    store = active_state_store(root)
    if store is not None:
        store.append_event(
            event_name,
            payload,
            workspace_id=workspace_id(root),
            session_id=payload["session_id"] or None,
            task_id=str(extra.get("task_id") or "") or None,
        )
    else:
        atomic_append_jsonl(event_log(root), payload)
    return payload


def clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end >= 0 else ""


def scalar_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in frontmatter(text).splitlines():
        if match := re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line):
            result[match.group(1)] = clean(match.group(2))
    return result


def list_field(text: str, name: str) -> list[str]:
    result: list[str] = []
    active = False
    for line in frontmatter(text).splitlines():
        if re.match(rf"^{re.escape(name)}:\s*$", line):
            active = True
            continue
        if active and re.match(r"^\s+-\s+", line):
            result.append(clean(re.sub(r"^\s+-\s+", "", line)))
            continue
        if active and line and not line.startswith(" "):
            break
    return result


def task_roots(root: Path) -> list[Path]:
    candidates = [root / ".xirang/tasks", root / "02-项目管理/任务卡"]
    return [path for path in candidates if path.exists()]


def task_cards(root: Path) -> list[tuple[Path, dict[str, str], list[str], list[str]]]:
    result: list[tuple[Path, dict[str, str], list[str], list[str]]] = []
    for base in task_roots(root):
        for path in sorted(base.glob("**/T-*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            result.append((path, scalar_fields(text), list_field(text, "allowed_write_roots"),
                           list_field(text, "external_write_roots")))
    return result


def active_task(root: Path, session_id: str) -> tuple[Path, dict[str, str], list[str], list[str]] | None:
    if not session_id:
        return None
    active = [row for row in task_cards(root) if row[1].get("status") in TASK_ACTIVE]
    same_session = [row for row in active if row[1].get("session_id") == session_id]
    return same_session[0] if len(same_session) == 1 else None


def decode_js_literal(value: str) -> str | None:
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, str) else None
    if not (value.startswith("`") and value.endswith("`")) or "${" in value:
        return None
    return value[1:-1].replace(r"\`", "`").replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t").replace(r"\\", "\\")


def code_source(event: dict) -> str:
    if event.get("tool_name") not in CODE_MODE_NAMES:
        return ""
    value = event.get("tool_input")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("source", "code", "input", "command"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def literals(source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(rf"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*({JS_LITERAL_RE})", re.DOTALL)
    for match in pattern.finditer(source):
        if (decoded := decode_js_literal(match.group(2))) is not None:
            result[match.group(1)] = decoded
    return result


def patch_commands(event: dict) -> list[str]:
    tool_input = event.get("tool_input")
    if event.get("tool_name") == "apply_patch" and isinstance(tool_input, dict):
        value = tool_input.get("command")
        return [value] if isinstance(value, str) else []
    source = code_source(event)
    if "tools.apply_patch" not in source:
        return []
    known = literals(source)
    result: list[str] = []
    for match in re.finditer(rf"tools\.apply_patch\s*\(\s*([A-Za-z_$][\w$]*|{JS_LITERAL_RE})", source, re.DOTALL):
        token = match.group(1)
        decoded = known.get(token) if not token.startswith(('"', '`')) else decode_js_literal(token)
        if decoded is not None:
            result.append(decoded)
    return result


def patch_targets(event: dict, root: Path | None = None) -> list[tuple[str, str]]:
    tool_input = event.get("tool_input")
    if event.get("tool_name") in {"Write", "Edit", "NotebookEdit"} and isinstance(tool_input, dict):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path.strip():
            return []
        if event["tool_name"] == "Write":
            candidate = Path(path).expanduser()
            absolute = candidate if candidate.is_absolute() or root is None else root / candidate
            operation = "update" if absolute.exists() else "add"
        else:
            operation = event["tool_name"].lower()
        return [(path, operation)]
    result: list[tuple[str, str]] = []
    for patch in patch_commands(event):
        headers = list(re.finditer(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", patch, re.MULTILINE))
        for index, match in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(patch)
            body = patch[match.end():end]
            moved = re.search(r"^\*\*\* Move to: (.+?)\s*$", body, re.MULTILINE)
            items = (
                [(match.group(2).strip(), "move"), (moved.group(1).strip(), "move")]
                if moved else [(match.group(2).strip(), match.group(1).lower())]
            )
            for item in items:
                if item not in result:
                    result.append(item)
    return result


def shell_commands(event: dict) -> list[str]:
    tool_input = event.get("tool_input")
    if event.get("tool_name") in {"exec_command", "Bash", "PowerShell"} and isinstance(tool_input, dict):
        for key in ("cmd", "command"):
            if isinstance(tool_input.get(key), str):
                return [tool_input[key]]
        return []
    source = code_source(event)
    if "tools.exec_command" not in source:
        return []
    result: list[str] = []
    for marker in re.finditer(r"tools\.exec_command\s*\(", source):
        if match := re.search(rf"\bcmd\s*:\s*({JS_LITERAL_RE})", source[marker.end():], re.DOTALL):
            if (decoded := decode_js_literal(match.group(1))) is not None:
                result.append(decoded)
    return result


def normalize_target(root: Path, raw: str) -> str | None:
    candidate = Path(raw).expanduser()
    absolute = candidate if candidate.is_absolute() else root / candidate
    try:
        return absolute.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return None


def allowed_external_target(path: Path, targets: list[Path]) -> bool:
    """Allow an exact external file, or a descendant of an authorized directory."""
    return any(path == target or (target.is_dir() and target in path.parents) for target in targets)


def under(rel: str, allowed: str) -> bool:
    prefix = allowed.strip().replace("\\", "/")
    prefix = prefix[2:] if prefix.startswith("./") else prefix
    prefix = prefix.rstrip("/")
    return bool(prefix) and (rel == prefix or rel.startswith(prefix + "/"))


def protected(rel: str) -> bool:
    return rel in PROTECTED_FILES or any(rel.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def maintainer_profile(root: Path) -> bool:
    try:
        return json.loads((root / ".xirang/local-config.json").read_text(encoding="utf-8")).get("profile") == "maintainer"
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def immutable_control(rel: str) -> bool:
    return rel.startswith((".xirang/state/", ".xirang/tasks/", "02-项目管理/任务卡/"))


def authorized_execution_envelope(task: dict | None) -> bool:
    return bool(task and task.get("execution_authorized") is True)


def authorized_maintenance_envelope(task: dict | None) -> bool:
    return bool(task and task.get("task_kind") == "control_plane_maintenance"
                and authorized_execution_envelope(task))


def grant_allows(grants: list[dict], candidate: str, operation: str) -> bool:
    return any(
        scope_covers(str(grant.get("path") or ""), candidate)
        and operation in (grant.get("operations") or [])
        for grant in grants
    )


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason[:1800],
    }}, ensure_ascii=False, separators=(",", ":")))


def run_pre_write(event: dict, root: Path) -> int:
    targets = patch_targets(event, root)
    requested = event.get("tool_name") in {"apply_patch", "Write", "Edit", "NotebookEdit"} or "tools.apply_patch" in code_source(event)
    if requested and not targets:
        deny("写入请求无法解析目标路径，息壤已按默认拒绝处理。")
        return 0
    store = active_state_store(root)
    access = active_task_access(root, str(event.get("session_id") or ""), require_write=True) if store is not None else None
    if store is not None:
        task_data = access["task"] if access else None
        task = None
        task_allowed = access["allowed_write_roots"] if access else []
        task_operations = access.get("allowed_operations", []) if access else []
        task_grants = access.get("grants", []) if access else []
        task_external = (task_data or {}).get("external_write_roots") or []
    else:
        task = active_task(root, str(event.get("session_id") or ""))
        task_data = task[1] if task else None
        task_allowed = task[2] if task else []
        task_operations = []
        task_grants = []
        task_external = task[3] if task else []
    external_roots = [Path(value).expanduser().resolve() for value in task_external]
    if store is not None and task_data is not None and not authorized_execution_envelope(task_data):
        deny("当前任务没有 user_event→disclosure→authorization→envelope 权威链。")
        return 0
    validated_targets: list[dict[str, str]] = []
    for raw, raw_operation in targets:
        if store is not None:
            try:
                operation = canonical_operation(raw_operation)
            except Exception as exc:
                deny(f"写入操作类型无法规范化：{exc}")
                return 0
            if task_operations and operation not in task_operations:
                deny(f"路径已获授权，但操作 {operation} 未获授权。")
                return 0
        rel = normalize_target(root, raw)
        if rel is None:
            # Vault 外目标：必须是已授权维护任务，且精确落在声明的文件或目录内。
            if task_data is None:
                deny("工作区外目标需要当前会话的已授权维护任务。")
                return 0
            receipt = task_data.get("proposal_id") if store is not None else task_data.get("maintenance_authorization_receipt")
            if (not maintainer_profile(root) or not authorized_maintenance_envelope(task_data)
                    or receipt in {None, "", "null", "none", "~"}):
                deny("工作区外目标仅可由已授权维护任务写入。")
                return 0
            abs_path = Path(raw).expanduser().resolve()
            if task_grants and not grant_allows(task_grants, str(abs_path), operation):
                deny(f"外部路径缺少逐路径操作授权：{abs_path} + {operation}")
                return 0
            if store is not None:
                try:
                    store.validate_external_target(task_id=task_data["task_id"], target=str(abs_path))
                except Exception as exc:
                    deny(f"目标不满足冻结的外部 file/dir 权限：{exc}")
                    return 0
            elif not allowed_external_target(abs_path, external_roots):
                deny(f"目标不在当前工作区且未列入维护外部根：{raw}")
                return 0
            validated_targets.append({"path": raw, "operation": operation})
            continue
        if rel == ".xirang/canary.tmp":
            validated_targets.append({"path": raw, "operation": operation})
            continue
        if task_grants and not grant_allows(task_grants, rel, operation):
            deny(f"路径缺少逐路径操作授权：{rel} + {operation}")
            return 0
        if task_data is not None and task_data.get("delivery_mode", "files") == "chat":
            deny(f"聊天交付任务不能写文件：{rel}。请创建文件交付任务。")
            return 0
        if protected(rel):
            maintenance_ok = (
                maintainer_profile(root) and task_data is not None
                and (authorized_maintenance_envelope(task_data) if store is not None
                     else task_data.get("maintenance") in {True, "true"})
                and ((task_data.get("proposal_id") not in {None, "", "null", "none", "~"}) if store is not None
                     else task_data.get("maintenance_authorization_receipt")
                          not in {None, "", "null", "none", "~"})
                and not immutable_control(rel) and any(under(rel, prefix) for prefix in task_allowed)
            )
            if not maintenance_ok:
                deny(f"息壤控制文件不能用普通文件工具直接修改：{rel}")
                return 0
            validated_targets.append({"path": raw, "operation": operation})
            continue
        if task_data is None:
            deny("当前会话没有唯一活动任务。请先按写入声明调用 xirang-task.py start。")
            return 0
        if not any(under(rel, prefix) for prefix in task_allowed):
            deny(f"目标超出任务 {task_data.get('task_id')} 的授权范围：{rel}")
            return 0
        validated_targets.append({"path": raw, "operation": operation})
    if store is not None and task_data is not None and event.get("tool_name") == "Write":
        record(
            root, "file_write_intent", event,
            task_id=task_data["task_id"], tool_use_id=str(event.get("tool_use_id") or ""),
            targets=validated_targets,
        )
    return 0


def refresh_status(root: Path, trigger: str) -> tuple[bool, str]:
    script = root / ".standards/xirang-user-status.py"
    if not script.is_file():
        return False, "status renderer missing"
    proc = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "--write"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-800:]


def run_post_write(event: dict, root: Path) -> int:
    targets = patch_targets(event, root)
    requested = event.get("tool_name") in {"apply_patch", "Write", "Edit", "NotebookEdit"} or "tools.apply_patch" in code_source(event)
    if not requested:
        return 0
    if not targets:
        print(json.dumps({"systemMessage": "息壤无法记录本次写入证据；本次交付不能验收。"}, ensure_ascii=False))
        return 1
    store = active_state_store(root)
    access = active_task_access(root, str(event.get("session_id") or ""), require_write=True) if store is not None else None
    task = active_task(root, str(event.get("session_id") or "")) if store is None else None
    task_data = access["task"] if access else (task[1] if task else None)
    task_allowed = access["allowed_write_roots"] if access else (task[2] if task else [])
    external_values = (task_data or {}).get("external_write_roots") or (task[3] if task else [])
    external_roots = [Path(value).expanduser().resolve() for value in external_values]
    try:
        if store is not None and event.get("tool_name") == "Write":
            tool_use_id = str(event.get("tool_use_id") or "")
            with store.connect(readonly=True) as connection:
                rows = connection.execute(
                    """SELECT payload_json FROM events
                       WHERE event_type='file_write_intent' AND session_id=?
                       ORDER BY sequence DESC LIMIT 100""",
                    (str(event.get("session_id") or ""),),
                ).fetchall()
            intent = next(
                (
                    json.loads(row["payload_json"] or "{}")
                    for row in rows
                    if json.loads(row["payload_json"] or "{}").get("tool_use_id") == tool_use_id
                ),
                None,
            )
            if intent is None:
                raise PermissionError("Write 缺少已验证的 pre-write 操作意图")
            targets = [
                (str(item["path"]), str(item["operation"]))
                for item in intent.get("targets") or []
            ]
            if not targets:
                raise PermissionError("Write pre-write 操作意图为空")
        if store is not None and task_data is not None and not authorized_execution_envelope(task_data):
            raise PermissionError("post-write 任务缺少完整授权包络")
        for raw, operation in targets:
            rel = normalize_target(root, raw)
            if rel is None:
                abs_path = Path(raw).expanduser().resolve()
                if store is not None:
                    if task_data is None:
                        raise PermissionError("post-write 外部目标没有活动任务")
                    store.validate_external_target(task_id=task_data["task_id"], target=str(abs_path))
                elif not any(abs_path == er or er in abs_path.parents for er in external_roots):
                    raise ValueError(f"outside workspace and external roots: {raw}")
                path = abs_path
                rel_record = str(abs_path)
            else:
                path = root / rel
                rel_record = rel
                if task_data is None or not any(scope_covers(prefix, rel_record) for prefix in task_allowed):
                    raise PermissionError(f"post-write 路径不再属于有效 owner/worker lease：{rel_record}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            receipt_id = hashlib.sha256(f"{event.get('session_id')}:{event.get('tool_use_id')}:{rel_record}:{now_iso()}".encode()).hexdigest()[:24]
            if store is not None:
                if task_data is None:
                    raise PermissionError("post-write 时没有唯一有效任务访问权")
                previous = [row for row in store.list_effective_write_receipts(task_data["task_id"])
                            if row["path"] == rel_record]
                store.record_write_receipt(
                    receipt_id=receipt_id,
                    task_id=task_data["task_id"],
                    session_id=str(event.get("session_id") or ""),
                    path=rel_record,
                    operation=operation,
                    sha256=digest,
                    exists_after=path.exists(),
                    predecessor_receipt_id=(previous[-1]["receipt_id"] if previous else None),
                )
            # SQLite write receipts are authoritative, while the event is the
            # observable behavior signal used by current-session canaries.  Both
            # must exist; previously the active SQLite branch skipped the event.
            record(root, "file_write", event, receipt_id=receipt_id,
                   task_id=task_data.get("task_id") if task_data else None,
                   file=rel_record, operation=operation, exists=path.exists(),
                   sha256=digest, tool_name=event.get("tool_name"))
        # Status rendering is an independent projection workflow. A successful
        # evidence write must not be downgraded by renderer API drift.
    except Exception as exc:
        print(json.dumps({"systemMessage": f"息壤写入证据失败；文件已保留，但本次交付不能验收：{exc}"}, ensure_ascii=False))
        return 1
    return 0


def split_shell(command: str) -> list[list[str]]:
    if "\n" in command or "\r" in command or re.search(r"`|\$\(|<\(|>\(", command):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&", "&&", "||", "|"}:
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def option_values(args: list[str], name: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == name:
            if index + 1 >= len(args):
                return []
            values.append(args[index + 1])
            index += 2
            continue
        if token.startswith(name + "="):
            values.append(token.split("=", 1)[1])
        index += 1
    return values


def classify_delivery_workflow(args: list[str], root: Path | None, session_id: str) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "delivery_context_missing"
    forbidden = {"amend", "reset", "clean", "push"}
    if any(token.lower().lstrip("-") in forbidden for token in args):
        return False, "delivery_git_passthrough"
    allowed_options = {
        "--db", "--repo", "--task-id", "--session-id", "--path", "--path-category", "--category", "--message",
        "--delivery-id", "--validation-summary", "--adversarial-review-summary", "--recovery-manifest",
    }
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split("=", 1)[0]
        if not token.startswith("--") or option not in allowed_options:
            return False, f"delivery_argument_not_allowed:{token}"
        if "=" not in token:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                return False, f"delivery_argument_missing_value:{option}"
            index += 2
        else:
            index += 1
    task_ids = option_values(args, "--task-id")
    sessions = option_values(args, "--session-id")
    databases = option_values(args, "--db")
    repositories = option_values(args, "--repo")
    paths = option_values(args, "--path")
    if any(len(values) != 1 for values in (task_ids, sessions, databases, repositories)):
        return False, "delivery_required_context_missing"
    store = active_state_store(root)
    access = active_task_access(root, session_id, capability="delivery") if store is not None else None
    legacy = active_task(root, session_id) if store is None else None
    task = access["task"] if access else (legacy[1] if legacy else None)
    allowed_roots = access["allowed_write_roots"] if access else (legacy[2] if legacy else [])
    external_roots = ((access["task"].get("external_write_roots") or []) if access
                      else (legacy[3] if legacy else []))
    if task is None or task_ids[0] != task.get("task_id") or sessions[0] != session_id:
        return False, "delivery_task_or_session_mismatch"
    if not paths and task.get("delivery_mode") != "chat":
        return False, "delivery_paths_missing"
    if Path(repositories[0]).expanduser().resolve() != root.resolve():
        return False, "delivery_repo_override"
    if Path(databases[0]).expanduser().resolve() != (runtime_dir(root) / "state/state.sqlite3").resolve():
        return False, "delivery_database_override"
    external = [Path(value).expanduser().resolve() for value in (external_roots or [])]
    for value in paths:
        rel = normalize_target(root, value)
        if rel is None:
            if not allowed_external_target(Path(value).expanduser().resolve(), external):
                return False, f"delivery_path_outside_scope:{value}"
        elif not any(under(rel, prefix) for prefix in allowed_roots):
            return False, f"delivery_path_outside_scope:{value}"
    for value in option_values(args, "--recovery-manifest"):
        if "=" not in value:
            return False, "delivery_recovery_manifest_shape_invalid"
        exact_path, manifest_path = value.split("=", 1)
        if exact_path not in paths:
            return False, "delivery_recovery_manifest_path_mismatch"
        resolved_manifest = Path(manifest_path).expanduser().resolve()
        if not allowed_external_target(resolved_manifest, external):
            return False, "delivery_recovery_manifest_outside_scope"
    return True, "xirang_delivery_current_task"


def _workflow_command(args: list[str]) -> str:
    options_with_values = {
        "--db", "--task-id", "--session-id", "--workspace-root", "--events-output",
        "--root", "--database", "--snapshot", "--output",
    }
    index = 0
    while index < len(args):
        token = args[index]
        if token in options_with_values:
            index += 2
        elif token.startswith("--"):
            index += 1
        else:
            return token
    return ""


def _parse_option_tokens(
    tokens: list[str], *, value_options: set[str], flag_options: set[str] | None = None
) -> tuple[bool, list[str], str]:
    flags = flag_options or set()
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            positional.append(token)
            index += 1
            continue
        option, separator, inline_value = token.partition("=")
        if option in flags:
            if separator:
                return False, positional, f"flag_has_value:{option}"
            index += 1
            continue
        if option not in value_options:
            return False, positional, f"option_not_allowed:{option}"
        if separator:
            if not inline_value:
                return False, positional, f"option_missing_value:{option}"
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            return False, positional, f"option_missing_value:{option}"
        index += 2
    return True, positional, "ok"


def classify_runtime_workflow(args: list[str], root: Path | None, session_id: str) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "runtime_context_missing"
    if option_values(args, "--session-id") != [session_id]:
        return False, "runtime_session_mismatch"
    expected_db = (runtime_dir(root) / "state/state.sqlite3").resolve()
    databases = option_values(args, "--db")
    if len(databases) != 1 or Path(databases[0]).expanduser().resolve() != expected_db:
        return False, "runtime_database_override"
    workspace_roots = option_values(args, "--workspace-root")
    if workspace_roots and (len(workspace_roots) != 1 or Path(workspace_roots[0]).expanduser().resolve() != root.resolve()):
        return False, "runtime_workspace_override"
    if option_values(args, "--events-output"):
        return False, "runtime_events_override"
    if "--health-command" in args:
        return False, "runtime_freeform_probe"
    task_ids = option_values(args, "--task-id")
    access = active_task_access(root, session_id)
    if len(task_ids) != 1 or access is None or task_ids[0] != access["task"].get("task_id"):
        return False, "runtime_task_mismatch"
    command = _workflow_command(args)
    allowed = {
        "status": {"--db", "--task-id", "--session-id"},
        "advance": {"--db", "--task-id", "--session-id", "--workspace-root", "--to", "--details-json"},
        "block": {"--db", "--task-id", "--session-id", "--workspace-root", "--details-json"},
        "resume": {"--db", "--task-id", "--session-id", "--workspace-root", "--probe", "--fuse-record-id", "--review-artifact-id", "--user-event-id"},
        "lease": {"--db", "--task-id", "--session-id", "--workspace-root", "--worker-session-id", "--role", "--duration-seconds", "--read-only", "--root"},
    }
    if command not in allowed:
        return False, f"runtime_command_not_allowed:{command or 'missing'}"
    command_index = args.index(command)
    prefix_ok, prefix_positionals, prefix_reason = _parse_option_tokens(
        args[:command_index],
        value_options={"--db", "--task-id", "--session-id", "--workspace-root", "--events-output"},
    )
    if not prefix_ok or prefix_positionals:
        return False, f"runtime_global_shape_invalid:{prefix_reason}"
    tail = args[command_index + 1:]
    mutating = command in {"advance", "block", "resume"}
    if command == "status":
        shape_ok, positional, shape_reason = _parse_option_tokens(tail, value_options=set())
    elif command == "advance":
        shape_ok, positional, shape_reason = _parse_option_tokens(
            tail, value_options={"--to", "--details-json"})
    elif command == "block":
        shape_ok, positional, shape_reason = _parse_option_tokens(
            tail, value_options={"--details-json"})
        if shape_ok and (len(positional) != 1 or positional[0] not in {
            "blocked_budget", "blocked_nonconvergent", "blocked_external_dependency",
            "awaiting_material_user_choice", "suspended_lease_expired", "canceled",
        }):
            return False, "runtime_block_stage_not_allowed"
    elif command == "resume":
        shape_ok, positional, shape_reason = _parse_option_tokens(
            tail, value_options={"--probe", "--fuse-record-id", "--review-artifact-id", "--user-event-id"})
    else:
        if not tail or tail[0] not in {"create", "list", "revoke"}:
            return False, "runtime_lease_action_not_allowed"
        lease_action, lease_tail = tail[0], tail[1:]
        if lease_action == "revoke":
            shape_ok, positional, shape_reason = _parse_option_tokens(
                lease_tail, value_options=set())
            if shape_ok and len(positional) != 1:
                return False, "runtime_lease_revoke_shape_invalid"
        elif lease_action == "create":
            shape_ok, positional, shape_reason = _parse_option_tokens(
                lease_tail,
                value_options={"--worker-session-id", "--root", "--role", "--duration-seconds"},
                flag_options={"--read-only"},
            )
        else:
            shape_ok, positional, shape_reason = _parse_option_tokens(
                lease_tail, value_options={"--worker-session-id"})
        mutating = lease_action in {"create", "revoke"}
    if not shape_ok:
        return False, f"runtime_argument_shape_invalid:{shape_reason}"
    if command not in {"block", "lease"} and positional:
        return False, "runtime_positional_argument_forbidden"
    if command == "lease" and lease_action != "revoke" and positional:
        return False, "runtime_lease_positional_argument_forbidden"
    if mutating and not workspace_roots:
        return False, "runtime_mutation_requires_bound_workspace"
    for token in args:
        if token.startswith("--") and token.split("=", 1)[0] not in allowed[command]:
            return False, f"runtime_argument_not_allowed:{token}"
    if command != "status" and access.get("kind") == "lease" and access["lease"].get("read_only"):
        return False, "runtime_read_only_lease"
    return True, f"xirang_runtime:{command}"


def classify_task_workflow(args: list[str], root: Path | None, session_id: str) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "task_context_missing"
    command = _workflow_command(args)
    if command in {
        "submit", "authorize-execution", "authorize-maintenance",
        "repair-submitted-evidence", "repair-active-maintenance-scope",
    }:
        return False, f"task_parallel_authority_forbidden:{command}"
    if command not in {"start", "propose", "propose-maintenance", "present-review"}:
        return False, f"task_command_not_allowed:{command or 'missing'}"
    sessions = option_values(args, "--session-id")
    if sessions and sessions != [session_id]:
        return False, "task_session_mismatch"
    roots = option_values(args, "--root")
    if len(roots) != 1 or Path(roots[0]).expanduser().resolve() != root.resolve():
        return False, "task_root_override"
    if sessions != [session_id]:
        return False, "task_session_missing_or_mismatch"
    allowed_options = {
        "start": {"--root", "--title", "--scope", "--exclude", "--session-id", "--platform",
                  "--executor", "--delivery-mode", "--maintenance", "--authorization-receipt", "--external-root"},
        "propose-maintenance": {"--root", "--title", "--scope", "--exclude", "--session-id",
                                "--platform", "--executor", "--external-root", "--operation"},
        "propose": {"--root", "--title", "--scope", "--exclude", "--session-id",
                    "--platform", "--executor", "--operation"},
        "present-review": {"--root", "--session-id", "--platform"},
    }[command]
    command_index = args.index(command)
    if args[:command_index]:
        return False, "task_action_must_be_first"
    flags = {"--maintenance"} if command == "start" else set()
    shape_ok, positional, shape_reason = _parse_option_tokens(
        args[command_index + 1:], value_options=allowed_options - flags, flag_options=flags)
    if not shape_ok:
        return False, f"task_argument_shape_invalid:{shape_reason}"
    if command == "present-review":
        access = active_task_access(root, session_id)
        if len(positional) != 1 or access is None or positional[0] != access["task"]["task_id"]:
            return False, "task_present_target_mismatch"
    elif positional:
        return False, "task_positional_argument_forbidden"
    return True, f"xirang_task:{command}"


def classify_rescue_workflow(
    args: list[str], root: Path | None, session_id: str,
) -> tuple[bool, str]:
    """A narrow out-of-band route; the rescue script revalidates DB authority itself."""
    if root is None or not session_id or not args:
        return False, "rescue_context_missing"
    action = args[0]
    allowed = {
        "inspect": {"--database", "--session-id"},
        "repair": {
            "--database", "--session-id", "--snapshot", "--audit", "--maintenance-task-id",
            "--keep-task", "--cancel-task",
        },
        "repair-invalid-envelope": {
            "--database", "--session-id", "--snapshot", "--audit", "--maintenance-task-id",
            "--old-task-id", "--corrected-scope",
        },
    }
    if action not in allowed:
        return False, f"rescue_action_not_allowed:{action}"
    shape_ok, positional, reason = _parse_option_tokens(
        args[1:], value_options=allowed[action],
    )
    if not shape_ok or positional:
        return False, f"rescue_shape_invalid:{reason}"
    databases = option_values(args[1:], "--database")
    sessions = option_values(args[1:], "--session-id")
    expected_database = (runtime_dir(root) / "state/state.sqlite3").resolve()
    if len(databases) != 1 or Path(databases[0]).expanduser().resolve() != expected_database:
        return False, "rescue_database_override"
    if sessions != [session_id]:
        return False, "rescue_session_mismatch"
    if action != "inspect":
        required = ("--snapshot", "--audit", "--maintenance-task-id")
        if any(len(option_values(args[1:], option)) != 1 for option in required):
            return False, "rescue_required_context_missing"
        if action == "repair-invalid-envelope":
            if len(option_values(args[1:], "--old-task-id")) != 1:
                return False, "rescue_old_task_missing"
            if not option_values(args[1:], "--corrected-scope"):
                return False, "rescue_corrected_scope_missing"
        else:
            keep = option_values(args[1:], "--keep-task")
            maintenance = option_values(args[1:], "--maintenance-task-id")
            if len(keep) != 1 or keep != maintenance:
                return False, "rescue_must_keep_maintenance_task"
    return True, f"xirang_rescue:{action}"


def classify_admin_read_workflow(script: str, args: list[str]) -> tuple[bool, str]:
    command = _workflow_command(args)
    if script.endswith("xirang_state_migrate.py"):
        return (command == "status" and not any(token.startswith("--") for token in args),
                f"migration:{command or 'missing'}")
    if script.endswith("xirang_state_backup.py"):
        if command not in {"verify", "drift"}:
            return False, f"backup_mutation_forbidden:{command or 'missing'}"
        if any(token.split("=", 1)[0] in {"--database", "--root", "--output"} for token in args if token.startswith("--")):
            return False, "backup_authority_override"
        return True, f"backup_read:{command}"
    return False, "admin_workflow_not_allowed"


def classify_configure_workflow(
    args: list[str], root: Path | None, session_id: str
) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "configure_context_missing"
    command = _workflow_command(args)
    allowed_commands = {
        "plan", "check", "agent-check", "scheduler-status",
        "install", "agent-install", "scheduler-install",
    }
    if command not in allowed_commands:
        return False, f"configure_command_not_allowed:{command or 'missing'}"
    command_index = args.index(command)
    if command_index != 0:
        return False, "configure_action_must_be_first"
    shape_ok, positional, reason = _parse_option_tokens(
        args[1:],
        value_options={"--root", "--python"},
        flag_options={"--no-scheduler"},
    )
    if not shape_ok or positional:
        return False, f"configure_shape_invalid:{reason}"
    roots = option_values(args, "--root")
    if len(roots) != 1 or Path(roots[0]).expanduser().resolve() != root.resolve():
        return False, "configure_root_mismatch"
    if command in {"install", "agent-install", "scheduler-install"}:
        access = active_task_access(root, session_id, require_write=True)
        if access is None or not authorized_maintenance_envelope(access["task"]):
            return False, "configure_requires_active_maintenance_envelope"
    return True, f"xirang_configure:{command}"


def classify_projection_workflow(
    script: str, args: list[str], root: Path | None, session_id: str
) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "projection_context_missing"
    roots = option_values(args, "--root")
    if len(roots) != 1 or Path(roots[0]).expanduser().resolve() != root.resolve():
        return False, "projection_root_mismatch"
    if script.endswith("xirang-user-status.py"):
        allowed_options = {"--root", "--database", "--output", "--write", "--json", "--trigger"}
        command = "status"
        outputs = option_values(args, "--output")
        expected_output = (runtime_dir(root) / "status/current-status.json").resolve()
        if outputs and (len(outputs) != 1 or Path(outputs[0]).expanduser().resolve() != expected_output):
            return False, "projection_output_mismatch"
    else:
        allowed_options = {"--root", "--platform", "--session-id", "--database", "--write"}
        command = _workflow_command(args)
        if command not in {"instructions", "check"}:
            return False, f"canary_command_not_allowed:{command or 'missing'}"
    for token in args:
        if token.startswith("--") and token.split("=", 1)[0] not in allowed_options:
            return False, f"projection_argument_not_allowed:{token}"
    databases = option_values(args, "--database")
    expected_database = (runtime_dir(root) / "state/state.sqlite3").resolve()
    if databases and (len(databases) != 1 or Path(databases[0]).expanduser().resolve() != expected_database):
        return False, "projection_database_mismatch"
    if "--write" in args:
        access = active_task_access(root, session_id, require_write=True)
        if access is None or not authorized_maintenance_envelope(access["task"]):
            return False, "projection_write_requires_active_maintenance_envelope"
    return True, f"xirang_projection:{command}"


def classify_prompt_build_workflow(
    args: list[str], root: Path | None, session_id: str
) -> tuple[bool, str]:
    if root is None or not session_id:
        return False, "prompt_build_context_missing"
    if any(token not in {"--verify", "--diff"} for token in args):
        return False, "prompt_build_argument_not_allowed"
    if not args:
        access = active_task_access(root, session_id, require_write=True)
        if access is None or not authorized_maintenance_envelope(access["task"]):
            return False, "prompt_build_requires_active_maintenance_envelope"
        return True, "prompt_build:build"
    return True, "prompt_build:read"


def classify_segment(tokens: list[str], *, root: Path | None = None, session_id: str = "") -> tuple[bool, str]:
    if tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        return False, "environment_assignment"
    if not tokens:
        return False, "empty_or_assignment"
    program = Path(tokens[0]).name
    args = tokens[1:]
    if any(token in args for token in (">", ">>", "<")):
        return False, "redirection"
    if program not in READ_ONLY_PROGRAMS and not program.startswith("python"):
        return False, f"program_not_allowlisted:{program}"
    if program == "git":
        sub = next((arg for arg in args if not arg.startswith("-")), "")
        dangerous = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager", "--exec", "--paginate")
        if any(arg == "-P" or arg.startswith(dangerous) for arg in args):
            return False, "git_external_or_output"
        return (sub in SAFE_GIT_SUBCOMMANDS, f"git:{sub or 'missing'}")
    if program == "find" and any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for arg in args):
        return False, "find_mutation"
    if program == "find" and any(arg.startswith(("-fprint", "-fprintf", "-fls")) for arg in args):
        return False, "find_output_file"
    if program == "sed":
        if any(arg == "-i" or arg.startswith("-i") for arg in args):
            return False, "in_place_edit"
        if any(re.search(r"(?:^|[;{}/])\s*[we](?:\s|$)", arg) for arg in args):
            return False, "sed_write_or_execute"
    if program == "sort" and any(arg == "-o" or arg.startswith(("-o", "--output", "--compress-program")) for arg in args):
        return False, "sort_output_or_program"
    if program in {"diff", "grep", "rg"} and any(arg == "--pre" or arg.startswith("--output") for arg in args):
        return False, "reader_external_or_output"
    if program.startswith("python"):
        if "-c" in args or "-" in args:
            return False, "inline_python"
        if not args or args[0].startswith("-"):
            return False, "python_interpreter_options_forbidden"
        script = args[0]
        normalized = script.replace("\\", "/")
        normalized = normalized[2:] if normalized.startswith("./") else normalized
        if normalized in SAFE_PYTHON_WORKFLOWS:
            if normalized == ".standards/xirang_delivery.py":
                return classify_delivery_workflow(args[1:], root, session_id)
            if normalized == ".standards/xirang_runtime.py":
                return classify_runtime_workflow(args[1:], root, session_id)
            if normalized == ".standards/xirang-task.py":
                return classify_task_workflow(args[1:], root, session_id)
            if normalized == ".standards/xirang-rescue.py":
                return classify_rescue_workflow(args[1:], root, session_id)
            if normalized == ".standards/xirang-configure.py":
                return classify_configure_workflow(args[1:], root, session_id)
            if normalized in {
                ".standards/xirang-canary.py", ".standards/xirang-user-status.py"
            }:
                return classify_projection_workflow(normalized, args[1:], root, session_id)
            if normalized == ".prompt-src/prompt-build.py":
                return classify_prompt_build_workflow(args[1:], root, session_id)
            if normalized in {".standards/xirang_state_migrate.py", ".standards/xirang_state_backup.py"}:
                return classify_admin_read_workflow(normalized, args[1:])
            if any(arg in {"--root", "--output"} or arg.startswith(("--root=", "--output=")) for arg in args[1:]):
                return False, "workflow_path_override"
            return True, f"xirang_read_workflow:{normalized}"
        return False, f"python_script_not_allowlisted:{normalized or 'missing'}"
    return True, f"read_only:{program}"


def classify_shell_command(command: str, *, nested: bool = False,
                           root: Path | None = None, session_id: str = "") -> dict:
    segments = split_shell(command)
    checks = [classify_segment(segment, root=root, session_id=session_id) for segment in segments]
    allowed = bool(checks) and all(value[0] for value in checks)
    return {
        "blocked": not allowed,
        "classification": "read_or_registered_workflow" if allowed else "unapproved_shell_effect",
        "executable": Path(segments[0][0]).name if segments and segments[0] else "",
        "reasons": [reason for ok, reason in checks if not ok] or [reason for _ok, reason in checks],
    }


def record_registered_workflow_writes(event: dict, root: Path, commands: list[str]) -> None:
    targets: list[Path] = []
    for command in commands:
        segments = split_shell(command)
        if len(segments) != 1:
            continue
        tokens = segments[0]
        normalized = tokens[1].replace("\\", "/") if len(tokens) > 1 else ""
        action = _workflow_command(tokens[2:]) if len(tokens) > 2 else ""
        if normalized.endswith(".standards/xirang-configure.py") and action == "agent-install":
            receipt_path = runtime_dir(root) / "adapters/install-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            targets.extend(
                Path(row["path"]).expanduser().resolve()
                for row in (receipt.get("installed") or {}).values()
                if isinstance(row, dict) and row.get("path")
            )
            targets.extend([receipt_path, root / ".xirang/adapters/registry.json"])
        elif normalized.endswith(".standards/xirang-configure.py") and action == "scheduler-install":
            targets.extend([
                Path.home() / "Library/LaunchAgents"
                / f"com.xirang.{workspace_id(root)}.status-refresh.plist",
                runtime_dir(root) / "status/scheduler-receipt.json",
            ])
        elif normalized.endswith(".prompt-src/prompt-build.py") and len(tokens) == 2:
            registry = json.loads(
                (root / ".xirang/adapters/registry.json").read_text(encoding="utf-8")
            )
            for row in (registry.get("platforms") or {}).values():
                if not isinstance(row, dict):
                    continue
                targets.extend(
                    root / target["build"]
                    for target in (row.get("generation_target") or [])
                    if isinstance(target, dict) and target.get("build")
                )
        elif normalized.endswith(".standards/xirang-canary.py") and "--write" in tokens:
            platforms = option_values(tokens[2:], "--platform")
            if len(platforms) == 1:
                targets.append(runtime_dir(root) / "canary" / f"{platforms[0]}.json")
        elif normalized.endswith(".standards/xirang-user-status.py") and "--write" in tokens:
            outputs = option_values(tokens[2:], "--output")
            output = Path(outputs[0]).expanduser() if outputs else runtime_dir(root) / "status/current-status.json"
            targets.extend([output, output.with_suffix(".md")])
            if option_values(tokens[2:], "--trigger") == ["scheduler"]:
                targets.append(runtime_dir(root) / "status/scheduler-receipt.json")
    if not targets:
        return
    store = active_state_store(root)
    if store is None:
        return
    session_id = str(event.get("session_id") or "")
    access = active_task_access(root, session_id, require_write=True)
    if access is None:
        raise PermissionError("登记工作流完成后找不到当前维护任务")
    task_id = access["task"]["task_id"]
    for path in dict.fromkeys(target.resolve() for target in targets):
        rel = normalize_target(root, str(path))
        recorded_path = rel if rel is not None else str(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        previous = [
            row for row in store.list_effective_write_receipts(task_id)
            if row["path"] == recorded_path
        ]
        store.record_write_receipt(
            receipt_id=hashlib.sha256(
                f"{session_id}:{event.get('tool_use_id')}:{recorded_path}:{now_iso()}".encode()
            ).hexdigest()[:24],
            task_id=task_id,
            session_id=session_id,
            path=recorded_path,
            operation="registered_workflow_update",
            sha256=digest,
            exists_after=path.exists(),
            predecessor_receipt_id=previous[-1]["receipt_id"] if previous else None,
        )


def run_shell(event: dict, root: Path, pre: bool) -> int:
    commands = shell_commands(event)
    requested = event.get("tool_name") in {"exec_command", "Bash", "PowerShell"} or "tools.exec_command" in code_source(event)
    if not requested:
        return 0
    results = [classify_shell_command(
        command, root=root, session_id=str(event.get("session_id") or "")
    ) for command in commands] if commands else []
    blocked = not results or any(item["blocked"] for item in results)
    reasons = sorted({reason for item in results for reason in item["reasons"]}) if results else ["command_missing"]
    classification = "unapproved_shell_effect" if blocked else "read_or_registered_workflow"
    if pre and blocked:
        try:
            record(root, "shell_command_denied", event, classification=classification, reasons=reasons)
        finally:
            deny("Shell 命令不在只读或已登记工作流允许表中：" + ", ".join(reasons))
        return 0
    if not pre:
        record(root, "shell_command", event, classification=classification, reasons=reasons)
        try:
            record_registered_workflow_writes(event, root, commands)
        except Exception as exc:
            print(json.dumps({
                "systemMessage": f"登记工作流已执行，但写入证据失败，当前交付不能提交：{exc}"
            }, ensure_ascii=False))
            return 1
    return 0


def prompt_text(event: dict) -> str:
    for key in ("prompt", "user_prompt", "message", "content"):
        if isinstance(event.get(key), str):
            return event[key]
    if isinstance(event.get("tool_input"), dict):
        for key in ("prompt", "text", "message", "content"):
            if isinstance(event["tool_input"].get(key), str):
                return event["tool_input"][key]
    return ""


def model_action_proposal(event: dict) -> dict | None:
    """Validate a model interpretation without treating it as user provenance."""
    value = event.get("xirang_action_proposal")
    if value is None:
        return None
    if event.get("_verified_model_handoff") is not True:
        raise PermissionError("模型动作提议只能通过独立 model-decision 入口提交")
    if not isinstance(value, dict):
        raise ValueError("模型动作提议必须是对象")
    allowed = {"schema_version", "source_event_id", "action", "target", "reason", "object", "target_reference",
               "polarity", "temporality", "conditional", "quoted", "self_repaired",
               "final_commitment", "additional_intents"}
    if set(value) - allowed:
        raise ValueError("模型动作提议包含身份、状态或内部标识等禁止字段")
    source_event_id = str(event.get("message_id") or event.get("turn_id") or "")
    if not source_event_id or value.get("source_event_id") != source_event_id:
        raise PermissionError("模型动作提议未绑定当前用户事件")
    allowed_tuples = {
        ("accept", "current_review", "current_delivery"),
        ("request_changes", "current_review", "current_delivery"),
        ("authorize_execution", "current_execution_proposal", "current_execution_proposal"),
        ("authorize_maintenance", "current_maintenance_proposal", "current_maintenance_proposal"),
        ("cancel", "current_task", "current_task"),
        ("resubmit", "current_task", "current_task"),
        ("set_interaction_preference", "interaction_preference", "interaction_preference"),
        ("clear_interaction_preference", "interaction_preference", "interaction_preference"),
    }
    if (value.get("action"), value.get("target"), value.get("object")) not in allowed_tuples:
        raise ValueError("模型动作提议的动作、目标和对象组合不受允许")
    if (value.get("polarity") != "positive" or value.get("temporality") != "now"
            or value.get("conditional") is not False or value.get("quoted") is not False
            or value.get("self_repaired") is not False or value.get("final_commitment") is not True):
        raise ValueError("模型动作提议不是当前、无条件、非引用且最终明确的用户决定")
    return value


def platform_mode(root: Path) -> tuple[str, str]:
    try:
        row = json.loads((root / ".xirang/adapters/registry.json").read_text(encoding="utf-8"))["platforms"][PLATFORM]
        mode = str(row.get("allowed_mode") or row.get("mode") or "unsupported")
        application = str(row.get("application_state") or "")
        if not application:
            legacy_state = str(row.get("state") or "unsupported")
            application = "applied" if mode == "manual_guard" and legacy_state == "manual_guard" else legacy_state
        return mode, application
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return "unsupported", "unsupported"


def effective_authorization_intents(proposal: dict | None) -> list[str]:
    values = (proposal or {}).get("additional_intents")
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("additional_intents 必须是字符串数组")
    for index, intent in enumerate(values):
        if not isinstance(intent, str):
            raise ValueError(f"additional_intents[{index}] 必须是字符串")
        if intent not in AUTHORIZATION_ALLOWED_INTENTS:
            raise ValueError(f"不支持的 additional_intent：{intent}")
    requested = set(values)
    return sorted(AUTHORIZATION_DEFAULT_INTENTS | requested)


def proposal_task_kind(store: StateStore, proposal_id: str) -> str | None:
    """Read the immutable disclosure kind instead of inferring it from a generic table name."""
    with store.connect(readonly=True) as connection:
        row = connection.execute(
            """SELECT d.task_kind
               FROM maintenance_proposals p
               JOIN disclosures d ON d.disclosure_id = p.disclosure_id
               WHERE p.proposal_id = ?""",
            (proposal_id,),
        ).fetchone()
    return canonical_task_kind(str(row["task_kind"])) if row is not None else None


def expected_authorization_action(task_kind: str | None) -> str | None:
    if task_kind == "ordinary":
        return "authorize_execution"
    if task_kind == "control_plane_maintenance":
        return "authorize_maintenance"
    return None


def _reference_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def resolve_delivery_target(store: StateStore, reference: str) -> tuple[str, str] | None:
    key = _reference_key(reference)
    if not key:
        return None
    matches: list[tuple[str, str]] = []
    for task in store.list_tasks(review_statuses=["submitted", "reviewing"]):
        delivery = store.get_latest_delivery(task["task_id"])
        if delivery is None:
            continue
        labels = {task.get("title"), task.get("submission_summary"), delivery.get("validation_summary")}
        if key in {_reference_key(label) for label in labels if label}:
            matches.append((task["task_id"], delivery["delivery_id"]))
    return matches[0] if len(matches) == 1 else None


def resolve_proposal_target(store: StateStore, workspace: str, reference: str) -> str | None:
    key = _reference_key(reference)
    if not key:
        return None
    matches: list[str] = []
    for proposal in store.list_pending_maintenance_proposals(None, None, workspace_id=workspace):
        payload = proposal.get("payload") or {}
        labels = {payload.get("title"), payload.get("objective"), proposal.get("proposal_id")}
        if key in {_reference_key(label) for label in labels if label}:
            matches.append(proposal["proposal_id"])
    return matches[0] if len(matches) == 1 else None


def run_model_decision(event: dict, root: Path) -> int:
    text = prompt_text(event).strip()
    source_event_id = str(event.get("source_event_id") or "")
    session_id = str(event.get("session_id") or "")
    digest = hashlib.sha256(text.encode()).hexdigest()
    mode, state = platform_mode(root)
    # This portable adapter receives JSON and environment variables from the
    # caller, so it cannot establish a trusted host actor or sequence.  A
    # connected automatic guard needs a separate host-owned entry point.
    if not (mode == "manual_guard" and state == "applied"):
        print(json.dumps({"systemMessage": f"动作提议未应用：当前平台处于 {state}，只能保留建议，不能改变任务状态。"}, ensure_ascii=False))
        return 0
    normalized_authorization_intents: list[str] | None = None
    if event.get("action") in {"authorize_execution", "authorize_maintenance"}:
        try:
            normalized_authorization_intents = effective_authorization_intents(event)
        except ValueError as exc:
            print(json.dumps({"systemMessage": f"动作提议未应用：{exc}"}, ensure_ascii=False))
            return 0
    store = active_state_store(root)
    if store is not None:
        saved = store.get_user_event(source_event_id)
        if saved is None or saved["session_id"] != session_id or saved["prompt_sha256"] != digest:
            print(json.dumps({"systemMessage": "动作提议未应用：找不到匹配的当前用户事件。"}, ensure_ascii=False))
            return 0
        if saved["consumed_at"] is not None or expired_at(saved["expires_at"]):
            print(json.dumps({"systemMessage": "动作提议未应用：对应用户事件已消费或已过期。"}, ensure_ascii=False))
            return 0
        bindings = saved.get("bindings") or {}
        reference = str(event.get("target_reference") or "").strip()
        additions: dict[str, object] = {}
        if reference and event.get("target") == "current_review":
            resolved = resolve_delivery_target(store, reference)
            if resolved is None:
                print(json.dumps({"systemMessage": "动作提议未应用：交付指向不唯一或不存在。"}, ensure_ascii=False))
                return 0
            saved = store.freeze_review_target_reference(
                event_id=source_event_id, task_id=resolved[0], delivery_id=resolved[1],
                target_reference=reference, user_prompt_text=text,
            )
            bindings = saved.get("bindings") or {}
        elif reference and event.get("target") in {
            "current_execution_proposal", "current_maintenance_proposal"
        }:
            proposal_id = resolve_proposal_target(store, saved["workspace_id"], reference)
            if proposal_id is None:
                print(json.dumps({"systemMessage": "动作提议未应用：维护范围指向不唯一或不存在。"}, ensure_ascii=False))
                return 0
            proposal = next((row for row in store.list_pending_maintenance_proposals(
                None, None, workspace_id=saved["workspace_id"]
            ) if row["proposal_id"] == proposal_id), None)
            if proposal is None:
                print(json.dumps({"systemMessage": "动作提议未应用：维护提案已经失效。"}, ensure_ascii=False))
                return 0
            additions.update({"maintenance_proposal_id": proposal_id,
                              "disclosure_id": proposal["disclosure_id"],
                              "explicit_proposal_reference": True,
                              "target_reference": reference})
        requested_intents = normalized_authorization_intents or sorted({
            str(intent) for intent in (event.get("additional_intents") or [])
            if isinstance(intent, str)
            and intent in {
                "continue_execution", "adversarial_review", "no_intermediate_confirmation",
                "record_followup", "report_once_no_prompt",
            }
        })
        if requested_intents:
            additions["additional_intents"] = requested_intents
        if additions:
            saved = store.freeze_user_event_bindings(source_event_id, additions)
            bindings = saved.get("bindings") or {}
        if event.get("action") == "set_interaction_preference":
            task_id = str(bindings.get("task_id") or "")
            if not task_id:
                print(json.dumps({"systemMessage": "动作提议未应用：交互偏好必须绑定当前执行包络。"}, ensure_ascii=False))
                return 0
            store.freeze_interaction_preference_intent(
                event_id=source_event_id, task_id=task_id,
                intent_code="never_report_once_no_prompt",
            )
        forwarded = dict(event)
        forwarded.update({
            "turn_id": source_event_id,
            "_verified_model_handoff": True,
            "_bound_task_id": bindings.get("task_id") or "",
            "_bound_delivery_id": bindings.get("delivery_id") or "",
            "_bound_review_focus_id": bindings.get("focus_id") or "",
            "_bound_maintenance_proposal_id": bindings.get("maintenance_proposal_id") or "",
        })
        forwarded["xirang_action_proposal"] = {
            "schema_version": 1,
            "source_event_id": source_event_id,
            "action": event.get("action"),
            "target": event.get("target"),
            "reason": event.get("reason") or "",
            "object": event.get("object"),
            "target_reference": reference,
            "polarity": event.get("polarity"),
            "temporality": event.get("temporality"),
            "conditional": event.get("conditional"),
            "quoted": event.get("quoted"),
            "self_repaired": event.get("self_repaired"),
            "final_commitment": event.get("final_commitment"),
            "additional_intents": (
                normalized_authorization_intents
                if normalized_authorization_intents is not None
                else event.get("additional_intents") or []
            ),
        }
        return run_user_prompt(forwarded, root)
    matched: dict | None = None
    try:
        raw_rows = (event_log(root)).read_text(encoding="utf-8").splitlines()
    except OSError:
        raw_rows = []
    rows: list[dict] = []
    for line in raw_rows:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        rows.append(row)
        if (row.get("event") == "user_prompt" and row.get("session_id") == session_id
                and row.get("turn_id") == source_event_id and row.get("prompt_sha256") == digest):
            matched = row
    consumed = any(row.get("event") == "semantic_event_consumed" and row.get("session_id") == session_id
                   and row.get("source_event_id") == source_event_id for row in rows)
    if not matched:
        print(json.dumps({"systemMessage": "动作提议未应用：找不到匹配的当前用户事件。"}, ensure_ascii=False))
        return 0
    try:
        issued = datetime.fromisoformat(str(matched.get("ts") or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        issued = datetime.fromtimestamp(0, timezone.utc).astimezone()
    if consumed or (datetime.now(timezone.utc).astimezone() - issued).total_seconds() > 600:
        print(json.dumps({"systemMessage": "动作提议未应用：对应用户事件已消费或已过期。"}, ensure_ascii=False))
        return 0
    if event.get("target") == "current_review" and not matched.get("review_task_id"):
        print(json.dumps({"systemMessage": "动作提议未应用：原用户事件未绑定唯一交付。"}, ensure_ascii=False))
        return 0
    if event.get("target") in {
        "current_execution_proposal", "current_maintenance_proposal"
    } and not matched.get("maintenance_proposal_id"):
        print(json.dumps({"systemMessage": "动作提议未应用：原用户事件未绑定唯一维护提案。"}, ensure_ascii=False))
        return 0
    forwarded = dict(event)
    forwarded["turn_id"] = source_event_id
    forwarded["_verified_model_handoff"] = True
    forwarded["_bound_task_id"] = matched.get("review_task_id")
    forwarded["_bound_submitted_at"] = matched.get("review_submitted_at") or ""
    forwarded["_bound_review_focus_id"] = matched.get("review_focus_id") or ""
    forwarded["_bound_maintenance_proposal_id"] = matched.get("maintenance_proposal_id")
    forwarded.pop("_xirang_action_applied", None)
    forwarded["xirang_action_proposal"] = {
        "schema_version": 1,
        "source_event_id": source_event_id,
        "action": event.get("action"),
        "target": event.get("target"),
        "reason": event.get("reason") or "",
            "object": event.get("object"),
            "target_reference": event.get("target_reference") or "",
        "polarity": event.get("polarity"),
        "temporality": event.get("temporality"),
        "conditional": event.get("conditional"),
        "quoted": event.get("quoted"),
        "self_repaired": event.get("self_repaired"),
        "final_commitment": event.get("final_commitment"),
        "additional_intents": (
            normalized_authorization_intents
            if normalized_authorization_intents is not None
            else event.get("additional_intents") or []
        ),
    }
    result = run_user_prompt(forwarded, root)
    if forwarded.pop("_xirang_action_applied", False) is True:
        record(root, "semantic_event_consumed", forwarded, source_event_id=source_event_id,
               action=str(event.get("action") or ""), target=str(event.get("target") or ""),
               additional_intents=[str(intent) for intent in (event.get("additional_intents") or []) if isinstance(intent, str)])
    return result


def pending_maintenance_candidates(root: Path, session_id: str) -> list[dict]:
    candidates: list[dict] = []
    current = datetime.now(timezone.utc).astimezone()
    for path in sorted((runtime_dir(root) / "maintenance-proposals").glob("M-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(str(row.get("expires_at") or "").replace("Z", "+00:00")).astimezone()
            secret = (runtime_dir(root) / "secret.key").read_text(encoding="utf-8").strip().encode()
            body = json.dumps({key: row[key] for key in sorted(row) if key != "signature"},
                              ensure_ascii=False, separators=(",", ":")).encode()
            expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if (row.get("status") == "pending" and row.get("session_id") == session_id
                and row.get("platform") == PLATFORM and current < expires
                and hmac.compare_digest(str(row.get("signature") or ""), expected)):
            candidates.append(row)
    return candidates


def presented_focus(root: Path, session_id: str) -> dict | None:
    """Return the latest valid, unconsumed delivery focus for this conversation.

    The complete presentation snapshot is retained so a later resubmission of the
    same task ID cannot inherit an earlier acceptance.  A newer presentation
    supersedes the older one; a consumed or expired focus is never reused.
    """
    if not session_id:
        return None
    try:
        raw_rows = event_log(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    latest: dict | None = None
    consumed: set[str] = set()
    for line in raw_rows:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("session_id") != session_id:
            continue
        if row.get("event") == "review_presented":
            latest = row
        elif row.get("event") == "review_focus_consumed" and row.get("focus_id"):
            consumed.add(str(row.get("focus_id")))
    if not latest:
        return None
    focus = dict(latest)
    focus_id = str(latest.get("focus_id") or "")
    if not focus_id or focus_id in consumed or not latest.get("task_id") or not latest.get("submitted_at"):
        focus["_focus_valid"] = False
        return focus
    try:
        expires = datetime.fromisoformat(str(latest.get("expires_at") or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        focus["_focus_valid"] = False
        return focus
    if datetime.now(timezone.utc).astimezone() >= expires:
        focus["_focus_valid"] = False
        return focus
    focus["_focus_valid"] = True
    return focus


def current_review_candidate(review_rows: list[tuple[Path, dict[str, str], list[str]]],
                             focus: dict | None = None) -> tuple[Path, dict[str, str], list[str]] | None:
    """Bind to an exact presentation snapshot, otherwise only to a sole candidate."""
    if focus is not None:
        if focus.get("_focus_valid") is not True:
            return None
        focused = [row for row in review_rows
                   if (row[1].get("task_id") or row[0].stem) == str(focus.get("task_id") or "")
                   and row[1].get("submitted_at", "") == str(focus.get("submitted_at") or "")]
        return focused[0] if len(focused) == 1 else None
    if len(review_rows) == 1:
        return review_rows[0]
    return None


def run_user_prompt_v3(event: dict, root: Path, store: StateStore) -> int:
    outcome = event
    event = dict(event)
    mode, state = platform_mode(root)
    if not (mode == "manual_guard" and state == "applied"):
        print(json.dumps({"systemMessage": f"用户决定未应用：当前平台处于 {state}。"}, ensure_ascii=False))
        return 0
    if not (event.get("message_id") or event.get("turn_id")):
        event["turn_id"] = f"ME-{uuid.uuid4().hex}"
    event_id = str(event.get("message_id") or event.get("turn_id"))
    session_id = str(event.get("session_id") or "")
    text = prompt_text(event).strip()
    prompt_digest = hashlib.sha256(text.encode()).hexdigest()
    saved = store.get_user_event(event_id)
    if saved is None:
        focus = store.get_active_review_focus(session_id)
        task_id = str(event.get("_bound_task_id") or "")
        delivery_id = str(event.get("_bound_delivery_id") or "")
        focus_id = str(event.get("_bound_review_focus_id") or "")
        if focus is not None and not task_id:
            task_id, delivery_id, focus_id = focus["task_id"], focus["delivery_id"], focus["focus_id"]
        embedded = re.search(r"\b(T-[A-Za-z0-9_-]+)\b", text)
        explicit_target = bool(embedded)
        if embedded:
            task_id = embedded.group(1)
            delivery = store.get_latest_delivery(task_id)
            delivery_id = delivery["delivery_id"] if delivery else ""
            focus_id = ""
        if not task_id:
            review_tasks = store.list_tasks(session_id=session_id, review_statuses=["submitted", "reviewing"])
            if len(review_tasks) == 1:
                task_id = review_tasks[0]["task_id"]
                delivery = store.get_latest_delivery(task_id)
                delivery_id = delivery["delivery_id"] if delivery else ""
        proposals = store.list_pending_maintenance_proposals(session_id, PLATFORM)
        bindings = {
            "task_id": task_id or None,
            "delivery_id": delivery_id or None,
            "focus_id": focus_id or None,
            "maintenance_proposal_id": proposals[0]["proposal_id"] if len(proposals) == 1 else None,
            "disclosure_id": proposals[0]["disclosure_id"] if len(proposals) == 1 else None,
            "explicit_target": explicit_target,
        }
    else:
        bindings = saved.get("bindings") or {}
    store.record_user_event(
        event_id=event_id,
        workspace_id=workspace_id(root),
        session_id=session_id,
        platform=PLATFORM,
        host_message_id=str(event.get("message_id") or "") or None,
        prompt_sha256=prompt_digest,
        bindings=bindings,
        observed_at=datetime.now(timezone.utc),
        ttl_seconds=600,
        actor_verified=False,
    )
    try:
        proposal = model_action_proposal(event)
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"systemMessage": f"动作提议未应用：{exc}"}, ensure_ascii=False))
        return 0
    legacy = re.fullmatch(r"授权本次息壤维护\s+(M-[A-Za-z0-9_-]+)", text)
    natural = text.rstrip("。！!") in {"授权本次维护", "同意本次维护", "批准本次维护", "授权这次维护"}
    model_auth = bool(
        proposal and proposal.get("action") in {"authorize_execution", "authorize_maintenance"}
    )
    proposal_id = legacy.group(1) if legacy else (bindings.get("maintenance_proposal_id") if natural or model_auth else None)
    if proposal_id:
        task_kind = proposal_task_kind(store, str(proposal_id))
        expected_action = expected_authorization_action(task_kind)
        proposed_action = str(proposal.get("action") or "") if proposal else "authorize_maintenance"
        if expected_action is None:
            print(json.dumps({"systemMessage": "执行授权未应用：提案任务类型不存在或不受支持。"}, ensure_ascii=False))
            return 0
        if proposed_action != expected_action:
            print(json.dumps({
                "systemMessage": (
                    "执行授权未应用：普通知识任务必须使用普通执行授权。"
                    if task_kind == "ordinary"
                    else "维护授权未应用：控制面任务必须使用维护授权。"
                )
            }, ensure_ascii=False))
            return 0
        try:
            intents = effective_authorization_intents(proposal)
        except ValueError as exc:
            print(json.dumps({"systemMessage": f"执行授权未应用：{exc}"}, ensure_ascii=False))
            return 0
        applied = store.authorize_maintenance_from_user_event(
            event_id=event_id,
            proposal_id=str(proposal_id),
            consumer_id="codex-hook-adapter",
            additional_intents=intents,
        )
        if applied:
            outcome["_xirang_action_applied"] = True
        print(json.dumps({
            "systemMessage": "本次执行范围已授权。" if applied else "本次执行授权已处理。",
            "xirangInternal": {
                "authorization_kind": task_kind,
                "continuation_required": applied and "continue_execution" in intents,
                "adversarial_review_required": applied and "adversarial_review" in intents,
                "suppress_intermediate_confirmation": (
                    applied and "no_intermediate_confirmation" in intents
                ),
                "additional_intents": intents,
            },
        }, ensure_ascii=False))
        return 0
    fixed = any(pattern.fullmatch(text) for pattern in (
        re.compile(r"接受本次交付(?:\s+T-[A-Za-z0-9_-]+)?"),
        re.compile(r"退回修改[：:].+"),
        re.compile(r"取消本次任务(?:\s+T-[A-Za-z0-9_-]+)?"),
        re.compile(r"重新提交本次交付(?:\s+T-[A-Za-z0-9_-]+)?"),
    ))
    if proposal is None and not fixed:
        return 0
    script = root / ".standards/xirang-task-decision.py"
    action = str(proposal.get("action") or "") if proposal else ""
    proc = subprocess.run(
        [sys.executable, str(script), "--from-user-prompt", "--root", str(root),
         "--session-id", session_id, "--platform", PLATFORM, "--event-id", event_id,
         "--text", text,
         "--bound-task-id", str(bindings.get("task_id") or ""),
         "--bound-delivery-id", str(bindings.get("delivery_id") or ""),
         "--bound-focus-id", str(bindings.get("focus_id") or ""),
         "--explicit-target" if bindings.get("explicit_target") else "--implicit-target",
         *(["--proposed-action", action, "--proposed-reason", str(proposal.get("reason") or "")]
           if proposal else [])],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    try:
        payload = json.loads(proc.stdout or proc.stderr)
    except json.JSONDecodeError:
        payload = {"ok": False, "message": (proc.stderr or proc.stdout).strip()[-800:]}
    if proc.returncode == 0 and payload.get("decision_applied"):
        outcome["_xirang_action_applied"] = True
    if proc.returncode != 0:
        print(json.dumps({"systemMessage": f"没有改变任务状态：{payload.get('message') or '目标或证据不满足'}"}, ensure_ascii=False))
    elif payload.get("decision_applied"):
        print(json.dumps({"systemMessage": "本次交付决定已记录。"}, ensure_ascii=False))
    return 0


def run_user_prompt(event: dict, root: Path) -> int:
    store = active_state_store(root)
    if store is not None:
        return run_user_prompt_v3(event, root, store)
    outcome = event
    event = dict(event)
    generated_handle = ""
    if not (event.get("message_id") or event.get("turn_id")):
        mode, state = platform_mode(root)
        if mode == "manual_guard" and state == "applied":
            generated_handle = f"ME-{uuid.uuid4().hex}"
            event["turn_id"] = generated_handle
    text = prompt_text(event).strip()
    session_id = str(event.get("session_id") or "")
    review_rows = [row for row in task_cards(root) if row[1].get("session_id") == session_id
                   and row[1].get("review_status") in {"submitted", "reviewing"}]
    focus = presented_focus(root, session_id)
    review_row = current_review_candidate(review_rows, focus)
    focus_id = str(focus.get("focus_id") or "") if focus and focus.get("_focus_valid") is True and review_row else ""
    maintenance_rows = pending_maintenance_candidates(root, session_id)
    record(root, "user_prompt", event, prompt_sha256=hashlib.sha256(text.encode()).hexdigest(),
           prompt_bytes=len(text.encode()), review_task_id=(review_row[1].get("task_id") if review_row else None),
           review_submitted_at=(review_row[1].get("submitted_at", "") if review_row else None),
           review_focus_id=(focus_id or None),
           maintenance_proposal_id=(maintenance_rows[0].get("proposal_id") if len(maintenance_rows) == 1 else None))
    try:
        proposal = model_action_proposal(event)
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"systemMessage": f"动作提议未应用：{exc}"}, ensure_ascii=False))
        return 0
    maintenance = re.fullmatch(r"授权本次息壤维护\s+(M-[a-f0-9]{16})", text)
    natural_maintenance = text.rstrip("。！!") in {"授权本次维护", "同意本次维护", "批准本次维护", "授权这次维护"}
    proposal_id = maintenance.group(1) if maintenance else None
    model_authorization = bool(proposal and proposal.get("action") == "authorize_maintenance"
                               and proposal.get("target") == "current_maintenance_proposal")
    if model_authorization:
        proposal_id = str(event.get("_bound_maintenance_proposal_id") or "") or None
    if (natural_maintenance or model_authorization) and proposal_id is None:
        candidates = pending_maintenance_candidates(root, str(event.get("session_id") or ""))
        if len(candidates) == 1:
            proposal_id = str(candidates[0].get("proposal_id") or "")
        elif len(candidates) != 1:
            print(json.dumps({"systemMessage": "当前维护请求无法唯一确定，请让 AI 用任务标题重新说明维护范围。"}, ensure_ascii=False))
            return 0
    if proposal_id:
        script = root / ".standards/xirang-task.py"
        try:
            intents = effective_authorization_intents(proposal)
        except ValueError as exc:
            print(json.dumps({"systemMessage": f"维护授权未应用：{exc}"}, ensure_ascii=False))
            return 0
        proc = subprocess.run(
            [sys.executable, str(script), "authorize-maintenance", proposal_id, "--from-user-prompt",
             "--session-id", str(event.get("session_id") or ""), "--platform", PLATFORM,
             "--source-event-id", str(event.get("message_id") or event.get("turn_id") or ""),
             "--prompt-sha256", hashlib.sha256(text.encode()).hexdigest(),
             *(item for intent in intents for item in ("--additional-intent", intent))],
            capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "XIRANG_USER_PROMPT_HOOK": "1"},
        )
        try:
            authorization_payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            authorization_payload = {}
        message = ("本次维护已授权。" if proc.returncode == 0
                   else f"维护授权未应用：{(proc.stdout or proc.stderr).strip()[-600:]}")
        response: dict[str, object] = {"systemMessage": message}
        if proc.returncode == 0:
            outcome["_xirang_action_applied"] = True
            persisted = [
                str(intent) for intent in (authorization_payload.get("additional_intents") or [])
                if isinstance(intent, str) and intent in AUTHORIZATION_ALLOWED_INTENTS
            ]
            if persisted:
                source_event_id = str(event.get("message_id") or event.get("turn_id") or "")
                record(root, "maintenance_continuation_requested", event,
                       source_event_id=source_event_id, maintenance_proposal_id=proposal_id,
                       additional_intents=persisted)
                response["xirangInternal"] = {
                    "continuation_required": "continue_execution" in persisted,
                    "adversarial_review_required": "adversarial_review" in persisted,
                    "suppress_intermediate_confirmation": "no_intermediate_confirmation" in persisted,
                    "maintenance_proposal_id": proposal_id,
                    "additional_intents": persisted,
                    "source_event_id": source_event_id,
                }
        print(json.dumps(response, ensure_ascii=False))
        return 0
    script = root / ".standards/xirang-task-decision.py"
    if proposal is None and not any(pattern.fullmatch(text) for pattern in (
            re.compile(r"接受本次交付(?:\s+T-[A-Za-z0-9_-]+)?"),
            re.compile(r"退回修改[：:].+"), re.compile(r"取消本次任务(?:\s+T-[A-Za-z0-9_-]+)?"),
            re.compile(r"重新提交本次交付(?:\s+T-[A-Za-z0-9_-]+)?"))):
        if generated_handle:
            print(json.dumps({"xirangInternal": {"semantic_event_handle": generated_handle}}, ensure_ascii=False))
        return 0
    proc = subprocess.run(
        [sys.executable, str(script), "--from-user-prompt", "--root", str(root), "--session-id",
         str(event.get("session_id") or ""), "--platform", PLATFORM, "--event-id",
         str(event.get("message_id") or event.get("turn_id") or ""), "--text", text,
         *(["--proposed-action", str(proposal.get("action") or ""), "--proposed-reason",
            str(proposal.get("reason") or ""), "--bound-task-id",
            str(event.get("_bound_task_id") or ""), "--bound-submitted-at",
            str(event.get("_bound_submitted_at") or "")] if proposal else [])],
        capture_output=True, text=True, check=False, timeout=45,
        env={**os.environ, "XIRANG_USER_PROMPT_HOOK": "1"},
    )
    try:
        payload = json.loads(proc.stdout or proc.stderr)
    except json.JSONDecodeError:
        payload = {"ok": False, "message": (proc.stderr or proc.stdout).strip()[-800:]}
    if proc.returncode == 0 and not payload.get("decision_applied"):
        return 0
    message = "本次交付决定已记录。" if proc.returncode == 0 else f"没有改变任务状态：{payload.get('message') or payload.get('reason') or '需要确认你指的是哪项交付'}"
    response: dict[str, object] = {"systemMessage": message}
    if proc.returncode == 0 and payload.get("decision_applied"):
        outcome["_xirang_action_applied"] = True
        bound_focus_id = str(event.get("_bound_review_focus_id") or focus_id or "")
        task_id = str(payload.get("task_id") or event.get("_bound_task_id") or "")
        source_event_id = str(event.get("message_id") or event.get("turn_id") or "")
        if bound_focus_id:
            record(root, "review_focus_consumed", event, focus_id=bound_focus_id,
                   task_id=task_id, source_event_id=source_event_id)
        requested = [str(intent) for intent in (proposal.get("additional_intents") or [])
                     if isinstance(intent, str) and intent in {"continue_execution", "record_followup"}] if proposal else []
        if "continue_execution" in requested:
            record(root, "continuation_requested", event, task_id=task_id,
                   source_event_id=source_event_id, trigger="accepted_delivery")
        if "record_followup" in requested:
            record(root, "followup_requested", event, task_id=task_id,
                   source_event_id=source_event_id, prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
        if requested:
            response["xirangInternal"] = {
                "continuation_required": "continue_execution" in requested,
                "additional_intents": requested,
                "source_event_id": source_event_id,
            }
    print(json.dumps(response, ensure_ascii=False))
    return 0


def run_session(event: dict, root: Path, start: bool) -> int:
    record(root, "session_start" if start else "session_end", event)
    if start:
        ok, detail = refresh_status(root, "session-start")
        message = "息壤状态已刷新。" if ok else f"息壤状态刷新失败：{detail}"
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pre-write", "post-write", "pre-exec", "post-exec", "user-prompt", "model-decision", "session-start", "session-end"))
    args = parser.parse_args()
    event = read_event()
    root = Path(os.environ.get("VAULT_ROOT", str(DEFAULT_ROOT))).expanduser().resolve()
    if args.mode == "pre-write":
        result = run_pre_write(event, root)
    elif args.mode == "post-write":
        result = run_post_write(event, root)
    elif args.mode == "pre-exec":
        result = run_shell(event, root, True)
    elif args.mode == "post-exec":
        result = run_shell(event, root, False)
    elif args.mode == "user-prompt":
        result = run_user_prompt(event, root)
    elif args.mode == "model-decision":
        result = run_model_decision(event, root)
    else:
        result = run_session(event, root, args.mode == "session-start")
    if args.mode != "pre-write":
        store = active_state_store(root)
        if store is not None:
            try:
                refresh_events_projection(
                    store, workspace_root=root, output=event_log(root)
                )
            except Exception as exc:
                print(json.dumps({
                    "systemMessage": f"息壤诊断投影刷新失败；本次操作不可提交：{exc}"
                }, ensure_ascii=False))
                return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
