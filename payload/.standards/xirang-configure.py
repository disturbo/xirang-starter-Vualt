#!/usr/bin/env python3
"""Generate portable platform adapters and install the macOS status timer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ADAPTER_START = "<!-- XIRANG-V97-ADAPTER-START -->"
ADAPTER_END = "<!-- XIRANG-V97-ADAPTER-END -->"
SEMANTIC_ROLES = {
    "agent_root", "project_contract", "platform_skill_surface",
    "runtime_context", "workspace_rules",
}
ACTIVITY_WINDOW_DAYS = 30


def shared_adapter_block(root: Path, platform: str, mode: str) -> str:
    protocol = root / ".xirang/adapters/PROTOCOL.md"
    return "\n".join([
        ADAPTER_START,
        "## 息壤 V9.7 当前运行入口",
        "",
        f"- 平台：`{platform}`；允许模式：`{mode}`（对用户显示为“可用（人工校验）”或“待验证”）。",
        f"- 每次处理 Vault 任务前必须读取 `{protocol}` 和 `{root / 'AGENTS.md'}`。",
        "- 旧版亮灯、看板、运行日志和 M3/M4/M5 流程已经退役，不得继续作为写入授权。",
        "- Agent 自动判断应调用的 Skill 和息壤流程，普通用户只需用自然语言说明目标、授权和决定。",
        "- 写入前一次性展示边界；用户授权后，在原范围内连续实施、验证、修复和提交，不重复确认。",
        "- 普通内容先建立执行提案；控制面修改建立维护提案。授权绑定执行包络，不绑定 Agent、模型或平台。",
        "- 接手 Agent 使用同一主任务的有效租约；不得因更换 Agent 要求用户重复授权原范围。",
        "- 接手或压缩上下文时读取 StateStore 的有效 handoff；handoff 只传状态，不授予权限。",
        "- 平台没有自动 Hook 时，必须显式调用共享适配器完成写前校验和写后取证；不得把提示词遵守冒充硬门禁。",
        ADAPTER_END,
    ])


def merge_adapter(text: str, block: str) -> str:
    pattern = re.compile(re.escape(ADAPTER_START) + r".*?" + re.escape(ADAPTER_END), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    cleaned = text.replace(ADAPTER_END, "") if ADAPTER_START not in text else text
    return cleaned.rstrip() + "\n\n" + block + "\n"


def runtime_context_adapter_block(root: Path, platform: str, mode: str) -> str:
    """Build a cache-stable governance block for a host runtime context file."""
    base = shared_adapter_block(root, platform, mode)
    cache_rule = (
        "- 本段只补充 Vault 治理，不替代宿主项目开发指南；长会话不在中途重建提示词，"
        "文件更新从下一次宿主加载或新会话生效。"
    )
    return base.replace(ADAPTER_END, cache_rule + "\n" + ADAPTER_END)


def merge_adapter_near_top(text: str, block: str) -> str:
    """Keep a managed block before host context truncation while preserving the guide."""
    pattern = re.compile(re.escape(ADAPTER_START) + r".*?" + re.escape(ADAPTER_END), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    cleaned = text.replace(ADAPTER_END, "") if ADAPTER_START not in text else text
    first_line, separator, remainder = cleaned.partition("\n")
    if separator and first_line.startswith("# "):
        return first_line.rstrip() + "\n\n" + block + "\n\n" + remainder.lstrip("\n")
    return block + "\n\n" + cleaned.lstrip("\n")


def external_adapter_targets() -> dict[str, tuple[Path, str]]:
    home = Path.home()
    return {
        "openclaw": (home / ".openclaw/workspace/AGENTS.md", "manual_guard"),
        "hermes": (home / ".hermes/SOUL.md", "manual_guard"),
        "hermes_one": (home / "Library/Application Support/hermes-desktop/skills/xirang-v9/SKILL.md", "manual_guard"),
        "reasonix": (home / "Library/Application Support/Reasonix/global-workspace/AGENTS.md", "manual_guard"),
        "deepseek_harness": (home / ".dsh/AGENTS.md", "manual_guard"),
        "workbuddy": (home / ".codebuddy/CODEBUDDY.md", "manual_guard"),
    }


def managed_runtime_context_targets() -> dict[str, tuple[Path, str]]:
    """Runtime context surfaces that must be managed in addition to identity roots."""
    return {
        "hermes": (Path.home() / ".hermes/hermes-agent/AGENTS.md", "manual_guard"),
    }


def deepseek_harness_status(root: Path) -> dict:
    """Report configured capability separately from current-session verification."""
    target = Path.home() / ".dsh" / "AGENTS.md"
    try:
        row = json.loads((root / ".xirang/adapters/registry.json").read_text(encoding="utf-8"))["platforms"]["deepseek_harness"]
        mode = str(row.get("allowed_mode") or row.get("mode") or "unsupported")
        state = str(row.get("application_state") or row.get("state") or "unverified")
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        mode, state = "unsupported", "unsupported"
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    configured = (
        mode == "manual_guard" and state == "applied"
        and "息壤" in text and "handoff" in text
    )
    return {
        "path": str(target), "mode": mode,
        "state": state if configured else "unverified", "installed": configured,
        "verification_status": "not_run_for_current_session",
        "url": "http://127.0.0.1:3080/",
    }


def _entry_path(root: Path, raw: str) -> Path:
    expanded = Path(raw).expanduser()
    return expanded.resolve() if expanded.is_absolute() else (root / expanded).resolve()


def _process_commands() -> str:
    try:
        return subprocess.run(
            ["ps", "-axo", "command="], check=True, capture_output=True,
            text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _surface_status(
    root: Path, platform: str, entry: dict, process_commands: str, *, now: float,
) -> dict:
    raw = str(entry.get("path") or "")
    path = _entry_path(root, raw)
    exists = path.is_file()
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
    role = str(entry.get("role") or "unknown")
    declared = str(entry.get("activity_state") or "")
    if declared == "active_unmanaged":
        activity = "active_unmanaged"
    elif role in {"inactive_candidate", "retired_preview", "build_preview"}:
        activity = "inactive"
    elif exists and str(path.parent) in process_commands:
        activity = "active_process_observed"
    elif exists and now - path.stat().st_mtime <= ACTIVITY_WINDOW_DAYS * 86400:
        activity = "active_recent_file"
    else:
        activity = "declared_active_unverified" if entry.get("active_authority") else "inactive"
    text = path.read_text(encoding="utf-8", errors="replace") if exists else ""
    semantic_required = role in SEMANTIC_ROLES and entry.get("management_state") != "unmanaged"
    semantically_bound = not semantic_required or (
        "息壤" in text and any(
            marker in text for marker in ("handoff", "StateStore", ".xirang/adapters/PROTOCOL.md")
        )
    )
    applied = (
        exists and semantically_bound
        and entry.get("management_state") != "unmanaged"
        and activity != "inactive"
    )
    return {
        "path": str(path), "declared_path": raw, "role": role,
        "exists": exists, "sha256": digest, "size": path.stat().st_size if exists else None,
        "activity_state": activity,
        "management_state": entry.get("management_state", "managed"),
        "surface_state": "applied_file_runtime_unverified" if applied else (
            "registered_active_unmanaged" if activity == "active_unmanaged" else "unverified"
        ),
        "semantically_bound": semantically_bound,
    }


def _unregistered_active_roots(root: Path, registered: set[Path], process_commands: str) -> list[dict]:
    """Detect explicit runtime agent roots without inventing file-exists activity."""
    findings: list[dict] = []
    for match in re.finditer(r"--agent-root(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))", process_commands):
        raw = next(value for value in match.groups() if value)
        candidate_root = Path(raw).expanduser().resolve()
        candidate = candidate_root / "AGENTS.md"
        if candidate.is_file() and candidate not in registered:
            findings.append({
                "path": str(candidate), "activity_state": "active_process_observed",
                "reason": "process --agent-root is not present in registry",
            })
    return findings


def _hermes_host_load_canary(surface: Path) -> dict:
    """Exercise Hermes' real context loader without invoking a model or mutating state."""
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    executable = surface.parent / "venv/bin/python"
    if not surface.is_file() or not executable.is_file():
        return {
            "state": "unverified", "kind": "host_loader",
            "checked_at": checked_at, "reason": "loader_or_surface_missing",
        }
    probe = (
        "from agent.prompt_builder import build_context_files_prompt\n"
        f"p=build_context_files_prompt(cwd={str(surface.parent)!r},skip_soul=True,"
        "allow_install_tree_fallback=True)\n"
        f"print('XIRANG_HOST_LOAD_CANARY=1' if {ADAPTER_START!r} in p else "
        "'XIRANG_HOST_LOAD_CANARY=0')\n"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", probe], cwd=surface.parent,
            capture_output=True, text=True, check=False, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "state": "unverified", "kind": "host_loader", "checked_at": checked_at,
            "reason": type(exc).__name__,
        }
    verified = completed.returncode == 0 and "XIRANG_HOST_LOAD_CANARY=1" in completed.stdout
    return {
        "state": "verified_current" if verified else "unverified",
        "kind": "host_loader", "checked_at": checked_at,
        "target": str(surface),
        "target_sha256": hashlib.sha256(surface.read_bytes()).hexdigest(),
        "loader": "agent.prompt_builder.build_context_files_prompt",
        "model_invoked": False,
        "reason": "managed_marker_loaded" if verified else "managed_marker_not_loaded",
    }


def _platform_host_load_canary(platform: str, surfaces: list[dict]) -> dict:
    if platform == "hermes":
        runtime = next((item for item in surfaces if item["role"] == "runtime_context"), None)
        if runtime:
            return _hermes_host_load_canary(Path(runtime["path"]))
    return {
        "state": "unverified", "kind": "no_safe_host_loader_probe",
        "reason": "process_or_file_presence_is_not_load_proof",
    }


def mark_agent_adapters_applied(root: Path, receipt: dict) -> None:
    registry_path = root / ".xirang/adapters/registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    platforms = registry.get("platforms") or {}
    installed_at = str(receipt.get("installed_at") or "")
    receipt_path = str(runtime_dir(root) / "adapters/install-receipt.json")
    for platform, detail in (receipt.get("installed") or {}).items():
        row = platforms.get(platform)
        if not isinstance(row, dict):
            continue
        row["allowed_mode"] = "manual_guard"
        has_active_unmanaged = any(
            isinstance(entry, dict)
            and entry.get("activity_state") == "active_unmanaged"
            for entry in (row.get("instruction_entries") or [])
        )
        row["application_state"] = "partial_unmanaged" if has_active_unmanaged else "applied"
        row["canary_state"] = "unverified"
        row["connected"] = False
        row.pop("mode", None)
        row.pop("state", None)
        row.pop("adapter_compatibility", None)
        row["application_evidence"] = {
            "kind": "managed_external_entry",
            "path": next(
                (
                    target.get("target")
                    for target in (row.get("generation_target") or [])
                    if isinstance(target, dict) and target.get("apply") == "external_manual"
                ),
                str(detail.get("runtime_context") or detail.get("path") or ""),
            ),
            "receipt": receipt_path,
            "installed_at": installed_at,
        }
    atomic_json(registry_path, registry, 0o644)


def install_agent_adapters(root: Path) -> dict:
    runtime = runtime_dir(root)
    backup_root = runtime / "adapter-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    installed: dict[str, dict] = {}
    for platform, (target, mode) in external_adapter_targets().items():
        old = target.read_text(encoding="utf-8") if target.is_file() else ""
        if old:
            backup = backup_root / platform / target.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(old, encoding="utf-8")
        block = shared_adapter_block(root, platform, mode)
        if platform == "hermes_one":
            block = "---\nname: xirang-v9\ndescription: 息壤 V9.7 工作区写入协议入口\n---\n\n" + block
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, {}) if target.suffix == ".json" else target.write_text(merge_adapter(old, block), encoding="utf-8")
        installed[platform] = {"path": str(target), "mode": mode, "installed": True}
    for platform, (target, mode) in managed_runtime_context_targets().items():
        old = target.read_text(encoding="utf-8") if target.is_file() else ""
        if not old:
            continue
        backup = backup_root / f"{platform}-runtime-context" / target.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(old, encoding="utf-8")
        block = runtime_context_adapter_block(root, platform, mode)
        target.write_text(merge_adapter_near_top(old, block), encoding="utf-8")
        installed.setdefault(platform, {"mode": mode, "installed": True})
        installed[platform]["runtime_context"] = str(target)
    receipt = {
        "schema_version": 1, "installed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "workspace_root": str(root), "installed": installed,
        "deepseek_harness": {**deepseek_harness_status(root), "installed_by_configure": False},
    }
    atomic_json(runtime / "adapters/install-receipt.json", receipt)
    mark_agent_adapters_applied(root, receipt)
    receipt["deepseek_harness"] = {
        **deepseek_harness_status(root),
        "installed_by_configure": True,
    }
    atomic_json(runtime / "adapters/install-receipt.json", receipt)
    return receipt


def check_agent_adapters(root: Path) -> dict:
    try:
        registry = json.loads(
            (root / ".xirang/adapters/registry.json").read_text(encoding="utf-8")
        ).get("platforms", {})
    except (OSError, json.JSONDecodeError, TypeError):
        registry = {}
    commands = _process_commands()
    now = time.time()
    result: dict[str, dict] = {}
    registered_paths: set[Path] = set()
    registered_platforms = set(registry) if isinstance(registry, dict) else set()
    for platform in sorted(registered_platforms):
        registration = registry.get(platform) if isinstance(registry, dict) else {}
        registration = registration if isinstance(registration, dict) else {}
        surfaces = []
        for entry in registration.get("instruction_entries") or []:
            if not isinstance(entry, dict):
                continue
            surface = _surface_status(root, platform, entry, commands, now=now)
            registered_paths.add(Path(surface["path"]))
            surfaces.append(surface)
        canary = str(registration.get("canary_state") or "unverified")
        connected = registration.get("connected") is True and canary == "verified"
        required_surfaces = [
            item for item in surfaces
            if item["role"] in SEMANTIC_ROLES
            and item["activity_state"] != "inactive"
            and item["management_state"] != "unmanaged"
        ]
        installed = bool(required_surfaces and all(
            item["surface_state"] == "applied_file_runtime_unverified"
            for item in required_surfaces
        ))
        active_unmanaged = [
            item["declared_path"] for item in surfaces
            if item["activity_state"] == "active_unmanaged"
            or (
                item["activity_state"].startswith("active_")
                and item["management_state"] == "unmanaged"
            )
        ]
        application_state = (
            "partial_unmanaged" if active_unmanaged
            else registration.get("application_state", "unverified")
        )
        result[platform] = {
            "mode": registration.get("allowed_mode", "unsupported"),
            "installed": installed,
            "application_state": application_state,
            "management_complete": not active_unmanaged and application_state == "applied",
            "active_unmanaged_surfaces": active_unmanaged,
            "canary_state": canary,
            "connected": connected,
            "host_load_canary": _platform_host_load_canary(platform, surfaces),
            "surfaces": surfaces,
        }
    unregistered = _unregistered_active_roots(root, registered_paths, commands)
    registry_complete = {"claude", "codex"}.issubset(registered_platforms)
    truthful_connections = all(
        not row["connected"] or row["canary_state"] == "verified" for row in result.values()
    )
    application_complete = all(row["management_complete"] for row in result.values())
    host_load_complete = all(
        row["host_load_canary"]["state"] == "verified_current" for row in result.values()
    )
    connection_complete = all(row["connected"] for row in result.values())
    p0_failures = [
        {
            "kind": "active_unmanaged_surface", "platform": platform,
            "surfaces": row["active_unmanaged_surfaces"],
        }
        for platform, row in result.items() if row["active_unmanaged_surfaces"]
    ]
    return {
        "ok": registry_complete and not unregistered and truthful_connections
        and application_complete and all(row["installed"] for row in result.values()),
        "registry_complete": registry_complete,
        "application_complete": application_complete,
        "host_load_complete": host_load_complete,
        "connection_complete": connection_complete,
        "p0_failures": p0_failures,
        "unregistered_active_entries": unregistered,
        "activity_criteria": {
            "active": ["process --agent-root", f"file modified within {ACTIVITY_WINDOW_DAYS} days"],
            "inactive": ["inactive_candidate", "retired_preview", "build_preview"],
            "active_unmanaged": "registered and visible, never counted as applied",
        },
        "platforms": result,
    }


def root_default() -> Path:
    explicit = os.environ.get("VAULT_ROOT") or os.environ.get("XIRANG_WORKSPACE_ROOT")
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def runtime_dir(root: Path) -> Path:
    if explicit := os.environ.get("XIRANG_RUNTIME_DIR"):
        return Path(explicit).expanduser()
    return Path.home() / ".xirang/workspaces" / workspace_id(root)


def quoted_command(python: Path, adapter: Path, mode: str) -> str:
    return f'"{python}" "{adapter}" {mode}'


def hook(mode: str, python: Path, adapter: Path, *, timeout: int = 30) -> dict:
    return {"type": "command", "command": quoted_command(python, adapter, mode), "timeout": timeout}


def codex_config(root: Path, python: Path) -> dict:
    adapter = root / ".standards/hooks/codex-hook-adapter.py"
    return {
        "description": "Generated by XiRang. Re-run setup.sh after moving this folder.",
        "hooks": {
            "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [hook("session-start", python, adapter)]}],
            "UserPromptSubmit": [{"hooks": [hook("user-prompt", python, adapter, timeout=45)]}],
            "PreToolUse": [
                {"matcher": "Bash|exec_command|functions\\.exec", "hooks": [hook("pre-exec", python, adapter)]},
                {"matcher": "apply_patch|Write|Edit|NotebookEdit|functions\\.exec", "hooks": [hook("pre-write", python, adapter, timeout=60)]},
            ],
            "PostToolUse": [
                {"matcher": "Bash|exec_command|functions\\.exec", "hooks": [hook("post-exec", python, adapter)]},
                {"matcher": "apply_patch|Write|Edit|NotebookEdit|functions\\.exec", "hooks": [hook("post-write", python, adapter, timeout=60)]},
            ],
            "SessionEnd": [{"hooks": [hook("session-end", python, adapter, timeout=5)]}],
        },
    }


def claude_config(root: Path, python: Path) -> dict:
    adapter = root / ".standards/hooks/codex-hook-adapter.py"
    return {
        "permissions": {
            "allow": ["Read(*)", "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(rg *)", "Bash(ls *)"],
            "deny": ["Bash(git push *)", "Bash(git reset *)", "Bash(git clean *)", "Bash(rm *)"],
        },
        "env": {"VAULT_ROOT": str(root), "XIRANG_PLATFORM": "claude", "XIRANG_AGENT_ID": "claude"},
        "hooks": {
            "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [hook("session-start", python, adapter)]}],
            "UserPromptSubmit": [{"hooks": [hook("user-prompt", python, adapter, timeout=45)]}],
            "PreToolUse": [
                {"matcher": "Bash|PowerShell", "hooks": [hook("pre-exec", python, adapter)]},
                {"matcher": "Write|Edit|NotebookEdit", "hooks": [hook("pre-write", python, adapter, timeout=60)]},
            ],
            "PostToolUse": [
                {"matcher": "Bash|PowerShell", "hooks": [hook("post-exec", python, adapter)]},
                {"matcher": "Write|Edit|NotebookEdit", "hooks": [hook("post-write", python, adapter, timeout=60)]},
            ],
            "SessionEnd": [{"hooks": [hook("session-end", python, adapter, timeout=5)]}],
        },
    }


def atomic_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def atomic_text(path: Path, payload: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def ensure_secret(runtime: Path) -> Path:
    path = runtime / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, secrets.token_hex(32).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    os.chmod(path, 0o600)
    return path


def write_adapters(root: Path, python: Path) -> list[str]:
    runtime = runtime_dir(root)
    ensure_secret(runtime)
    codex_path = root / ".codex/hooks.json"
    claude_path = root / ".claude/settings.json"
    claude_contract_path = root / ".claude/CLAUDE.md"
    atomic_json(codex_path, codex_config(root, python), 0o644)
    atomic_json(claude_path, claude_config(root, python), 0o644)
    old_contract = claude_contract_path.read_text(encoding="utf-8") if claude_contract_path.is_file() else ""
    old_v943 = "ed70417cfae3d1ac9c74795d61e9e6bfc2212b57e800aa272c4998aea0970da2"
    if old_contract and hashlib.sha256(old_contract.encode()).hexdigest() == old_v943:
        old_contract = "@../AGENTS.md\n"
    managed_contract = merge_adapter(old_contract, shared_adapter_block(root, "claude", "manual_guard"))
    if not managed_contract.lstrip().startswith("@../AGENTS.md"):
        managed_contract = "@../AGENTS.md\n\n" + managed_contract.lstrip()
    atomic_text(claude_contract_path, managed_contract)
    local = {
        "schema_version": 2, "workspace_root": str(root), "workspace_id": workspace_id(root),
        "runtime_dir": str(runtime), "python": str(python),
        "profile": "maintainer" if (root / "息壤-维护.md").is_file() else "ordinary",
        "codex_hook_state": "awaiting_user_trust_and_canary", "claude_hook_state": "awaiting_canary",
    }
    atomic_json(root / ".xirang/local-config.json", local)
    return [str(codex_path), str(claude_path), str(claude_contract_path), str(root / ".xirang/local-config.json")]


def launchd_path(root: Path) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"com.xirang.{workspace_id(root)}.status-refresh.plist"


def launchd_payload(root: Path, python: Path) -> dict:
    runtime = runtime_dir(root)
    return {
        "Label": f"com.xirang.{workspace_id(root)}.status-refresh",
        "ProgramArguments": [str(python), str(root / ".standards/xirang-user-status.py"), "--root", str(root), "--write", "--trigger", "scheduler"],
        "RunAtLoad": True, "StartCalendarInterval": [{"Minute": 0}, {"Minute": 30}], "ProcessType": "Background",
        "StandardOutPath": str(runtime / "logs/status-refresh.log"),
        "StandardErrorPath": str(runtime / "logs/status-refresh-error.log"),
    }


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def scheduler_receipt(root: Path) -> tuple[bool, str]:
    path = runtime_dir(root) / "status/scheduler-receipt.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = parse_time(data.get("completed_at"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False, "scheduler_receipt_missing"
    if not at or (datetime.now(timezone.utc).astimezone() - at.astimezone()).total_seconds() > 3700:
        return False, "scheduler_receipt_stale"
    return bool(data.get("ok")), "ok" if data.get("ok") else "scheduler_last_run_failed"


def scheduler_status(root: Path, python: Path | None = None) -> dict:
    if sys.platform != "darwin":
        return {"platform": sys.platform, "supported": False, "state": "not_supported"}
    target = launchd_path(root)
    label = f"com.xirang.{workspace_id(root)}.status-refresh"
    expected = launchd_payload(root, python or Path(sys.executable).resolve())["ProgramArguments"]
    try:
        actual = plistlib.loads(target.read_bytes()).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException):
        actual = None
    proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, check=False)
    loaded = proc.returncode == 0
    last_exit = None
    if match := re.search(r"last exit code = (-?\d+)", proc.stdout):
        last_exit = int(match.group(1))
    receipt_ok, receipt_reason = scheduler_receipt(root)
    connected = target.is_file() and actual == expected and loaded and last_exit in {0, None} and receipt_ok
    return {
        "platform": "macos", "supported": True, "installed": target.is_file(), "loaded": loaded,
        "definition_matches": actual == expected, "last_exit_code": last_exit,
        "receipt": receipt_reason, "connected": connected, "path": str(target),
    }


def scheduler_install(root: Path, python: Path) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("V9.7 最小包的自动刷新当前只支持 macOS")
    target = launchd_path(root)
    payload = launchd_payload(root, python)
    atomic_json(runtime_dir(root) / "status/scheduler-receipt.json", {
        "schema_version": 1,
        "ok": False,
        "state": "starting",
        "completed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    })
    Path(payload["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, target)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True, check=False)
    loaded = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True, check=False)
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr.strip() or "launchctl bootstrap failed")
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{payload['Label']}"], capture_output=True, check=False)
    receipt = runtime_dir(root) / "status/scheduler-receipt.json"
    for _ in range(40):
        try:
            current_receipt = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_receipt = {}
        if current_receipt.get("ok") is True and current_receipt.get("trigger") == "scheduler":
            break
        time.sleep(0.25)
    return scheduler_status(root, python)


def scheduler_uninstall(root: Path) -> dict:
    target = launchd_path(root)
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], capture_output=True, check=False)
        target.unlink(missing_ok=True)
    return scheduler_status(root)


def verify(root: Path, python: Path, *, require_scheduler: bool = True) -> dict:
    findings: list[str] = []
    for rel in ("AGENTS.md", "息壤.md", ".standards/hooks/codex-hook-adapter.py"):
        if not (root / rel).is_file():
            findings.append(f"missing:{rel}")
    for rel in (".codex/hooks.json", ".claude/settings.json"):
        try:
            text = (root / rel).read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError):
            findings.append(f"invalid:{rel}")
            continue
        if not payload.get("hooks"):
            findings.append(f"hooks_unconfigured:{rel}")
        if str(root) not in text:
            findings.append(f"root_mismatch:{rel}")
    try:
        claude_entry = (root / ".claude/CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        claude_entry = ""
    if "@../AGENTS.md" not in claude_entry or ADAPTER_START not in claude_entry:
        findings.append("invalid:.claude/CLAUDE.md")
    scheduler = scheduler_status(root, python)
    if require_scheduler and not scheduler.get("connected"):
        findings.append(f"scheduler:{scheduler.get('state') or scheduler.get('receipt') or 'not_connected'}")
    return {"ok": not findings, "root": str(root), "findings": findings, "scheduler": scheduler, "platform_canary": "required"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "install", "check", "agent-install", "agent-check", "scheduler-status", "scheduler-install", "scheduler-uninstall"))
    parser.add_argument("--root", type=Path, default=root_default())
    parser.add_argument("--python", type=Path, default=Path(sys.executable).resolve())
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()
    root, python = args.root.expanduser().resolve(), args.python.expanduser().resolve()
    try:
        if args.action == "plan":
            result = {"ok": sys.platform == "darwin", "root": str(root), "platform": sys.platform, "scheduler": "30_minutes_macos"}
        elif args.action == "install":
            result = {"configured": write_adapters(root, python)}
            if not args.no_scheduler:
                result["scheduler"] = scheduler_install(root, python)
            refresh = subprocess.run([str(python), str(root / ".standards/xirang-user-status.py"), "--root", str(root), "--write", "--trigger", "install"], capture_output=True, text=True, check=False)
            result["status_refresh"] = refresh.returncode == 0
            result["verification"] = verify(root, python, require_scheduler=not args.no_scheduler)
            result["ok"] = result["status_refresh"] and result["verification"]["ok"]
            if args.no_scheduler:
                result["degraded"] = "scheduler_not_installed"
        elif args.action == "check":
            result = verify(root, python, require_scheduler=not args.no_scheduler)
        elif args.action == "agent-install":
            result = install_agent_adapters(root)
            result["ok"] = True
        elif args.action == "agent-check":
            result = check_agent_adapters(root)
        elif args.action == "scheduler-install":
            result = scheduler_install(root, python)
            result["ok"] = bool(result.get("connected"))
        elif args.action == "scheduler-uninstall":
            result = scheduler_uninstall(root)
            result["ok"] = not result.get("installed", False)
        else:
            result = scheduler_status(root, python)
            result["ok"] = bool(result.get("connected"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is True else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
