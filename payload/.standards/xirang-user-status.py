#!/usr/bin/env python3
"""Render ordinary-user status only from the active StateStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xirang_state import StateStore, canonical_execution_budget
from xirang_state_migrate import (
    metadata_get,
    record_projection,
    require_active,
    runtime_dir,
    state_database,
)


AWAITING = {"submitted", "reviewing"}
RETURNED = {"changes_requested"}
ACTIVE = {"in_progress", "blocked"}
PLATFORM_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "deepseek_harness": "DeepSeek",
    "hermes": "Hermes",
    "hermes_one": "Hermes One",
    "openclaw": "OpenClaw",
    "reasonix": "Reasonix",
    "workbuddy": "WorkBuddy",
}


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def scan_tasks(store: StateStore) -> list[dict[str, Any]]:
    require_active(store)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT t.*, l.title, l.source_path
               FROM tasks t LEFT JOIN legacy_task_cards l ON l.task_id=t.task_id
               WHERE t.lifecycle_status != 'archived'
               ORDER BY t.updated_at DESC, t.task_id DESC"""
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        if not isinstance(metadata, dict):
            raise RuntimeError(f"任务元数据不是对象：{row['task_id']}")
        roots = json.loads(row["allowed_write_roots_json"] or "[]")
        operations = json.loads(row["allowed_operations_json"] or "[]")
        grants = json.loads(row["grants_json"] or "[]")
        legacy_powerless = bool(
            metadata.get("legacy_import") is True
            and row["lifecycle_status"] in ACTIVE
            and not roots and not operations and not grants
        )
        budget = metadata.get("execution_budget")
        result.append({
            "task_id": row["task_id"],
            "title": metadata.get("title") or row["title"] or row["task_id"],
            "status": row["lifecycle_status"],
            "runtime_status": row["runtime_status"],
            "review_status": row["review_status"],
            "updated_at": row["updated_at"],
            "session_id": row["session_id"],
            "card": metadata.get("card_path") or row["source_path"],
            "execution_budget": canonical_execution_budget(budget) if budget is not None else None,
            "authority_state": "legacy_powerless" if legacy_powerless else "not_evaluated",
        })
    return result


def protection_label(states: dict[str, dict[str, Any]]) -> str:
    labels = {
        "connected": "可信接通",
        "manual_guard_ready": "可用（人工校验）",
        "needs_maintenance": "需要维护",
        "unverified": "待验证",
        "not_enabled": "未启用",
        "failed": "异常",
    }
    parts: list[str] = []
    for platform, state in sorted(states.items()):
        name = PLATFORM_LABELS.get(platform, platform.replace("_", " "))
        parts.append(f"{name}：{state.get('user_state') or labels.get(str(state.get('canary_state')), '待验证')}")
    return "；".join(parts)


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def _time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def interaction_policy(store: StateStore, root: Path) -> str:
    try:
        preference = store.get_interaction_preference(workspace_id(root), "default")
    except (AttributeError, TypeError, ValueError):
        return "default"
    values: list[object] = [preference]
    observed: set[str] = set()
    while values:
        value = values.pop()
        if isinstance(value, str) and value in {"report_once_no_prompt", "never"}:
            observed.add(str(value))
        if isinstance(value, dict):
            for key in (
                "review_prompt_policy", "prompt_frequency", "acceptance_prompt",
                "preference_value", "value", "preferences",
            ):
                if key in value:
                    values.append(value[key])
    if "never" in observed:
        return "report_once_no_prompt/never"
    if "report_once_no_prompt" in observed:
        return "report_once_no_prompt"
    return "default"


def platform_truth(root: Path, store: StateStore, now: datetime) -> dict[str, dict[str, Any]]:
    try:
        registry = json.loads((root / ".xirang/adapters/registry.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    evidence = metadata_get(store, "platform_states", {})
    if not isinstance(evidence, dict):
        evidence = {}
    result: dict[str, dict[str, Any]] = {}
    for platform, row in sorted((registry.get("platforms") or {}).items()):
        if not isinstance(row, dict):
            continue
        application = str(row.get("application_state") or "unverified")
        canary = str(row.get("canary_state") or "unverified")
        current = evidence.get(platform)
        fresh = False
        if isinstance(current, dict):
            expires = _time(current.get("valid_until"))
            evaluated = _time(current.get("evaluated_at"))
            if expires is None and evaluated is not None:
                expires = evaluated + timedelta(minutes=60)
            fresh = bool(expires and now <= expires)
            if fresh:
                canary = str(current.get("canary_state") or canary)
        if canary in {"connected", "manual_guard_ready"} and not fresh:
            canary = "unverified"
        mode = str(row.get("allowed_mode") or "contract_only")
        registered_connected = row.get("connected") is True
        connected = bool(
            registered_connected and mode == "trusted"
            and application == "applied" and canary == "connected" and fresh
        )
        write_ready = bool(
            connected
            or (
                mode == "manual_guard"
                and application == "applied"
                and canary == "manual_guard_ready"
                and fresh
            )
        )
        if connected:
            user_state = "可信接通"
        elif write_ready:
            user_state = "可用（人工校验）"
        elif application == "applied":
            user_state = "入口已应用，等待当前验证"
        elif mode == "contract_only":
            user_state = "只读契约"
        else:
            user_state = "入口待应用"
        result[platform] = {
            "registration_state": "registered",
            "allowed_mode": mode,
            "application_state": application,
            "canary_state": canary,
            "registered_connected": registered_connected,
            "canary_fresh": fresh,
            "connected": connected,
            "write_ready": write_ready,
            "user_state": user_state,
        }
    return result


def build_status(root: Path, *, store: StateStore, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_local()
    tasks = scan_tasks(store)
    awaiting = [item for item in tasks if item["review_status"] in AWAITING]
    returned = [item for item in tasks if item["review_status"] in RETURNED]
    orphaned_active = [
        item for item in tasks
        if item["status"] in ACTIVE and item["authority_state"] == "legacy_powerless"
    ]
    active = [
        item for item in tasks
        if item["status"] in ACTIVE
        and item["review_status"] not in AWAITING
        and item["authority_state"] != "legacy_powerless"
    ]
    historical_unreviewed = [
        item for item in tasks if item["review_status"] == "legacy_unreviewed"
    ]
    current = (active or returned or awaiting or [None])[0]
    policy = interaction_policy(store, root)
    if active:
        state, action = "进行中", "无需操作；可以随时问进度"
    elif returned:
        state, action = "修改中", "等待 AI 修改后重新提交"
    elif awaiting and policy in {"report_once_no_prompt", "report_once_no_prompt/never"}:
        state, action = "有待处理交付", "交付已报告并保持待验收，无需操作"
    elif awaiting:
        state, action = "有待处理交付", "AI 会在对应任务中逐项呈现结果"
    else:
        state, action = "空闲", "告诉 AI 你想完成什么"
    truth = platform_truth(root, store, now)
    return {
        "schema_version": 3,
        "backend": "sqlite",
        "state_store": {"backend": "sqlite", "active": True, "authority": "sqlite"},
        "generated_at": now.isoformat(timespec="seconds"),
        "valid_until": (now + timedelta(minutes=60)).isoformat(timespec="seconds"),
        "freshness": "fresh",
        "state": state,
        "current_task": current,
        "awaiting_review_count": len(awaiting),
        "awaiting_review": awaiting[:3],
        "returned_count": len(returned),
        "active_count": len(active),
        "orphaned_active_count": len(orphaned_active),
        "archived_count": _count_archived(store),
        "legacy_review_debt_count": 0,
        "historical_unreviewed_count": len(historical_unreviewed),
        "user_action": action,
        "interaction_policy": policy,
        "platform_truth": truth,
        "platform_connections": {key: value["user_state"] for key, value in truth.items()},
        "protection": protection_label(truth) or "尚无平台运行证据",
    }


def _count_archived(store: StateStore) -> int:
    with store.connect() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE lifecycle_status='archived'"
        ).fetchone()[0])


def render_status_markdown(report: dict[str, Any]) -> str:
    current = report.get("current_task") or {}
    valid = datetime.fromisoformat(report["valid_until"]).astimezone().strftime("%Y-%m-%d %H:%M")
    expires = _time(report.get("valid_until"))
    freshness = "新鲜" if expires and now_local() <= expires else "可能过期"
    backend = report.get("state_store") or {"backend": report.get("backend", "unknown"), "active": False}
    preference_labels = {
        "default": "按默认规则",
        "report_once_no_prompt": "仅报告一次，不催验收",
        "report_once_no_prompt/never": "仅报告一次，之后不再提醒",
    }
    lines = [
        "# 息壤当前状态", "",
        f"> 状态：**{report['state']}**　·　有效至：**{valid}**（超时即视为可能过期）", "",
        f"- 最近交付：{current.get('title') or '无'}",
        f"- 待验收交付：{report['awaiting_review_count']} 项",
        f"- 当前安排：**{report['user_action']}**",
        f"- 交互偏好：**{preference_labels.get(report.get('interaction_policy', 'default'), '按默认规则')}**",
        f"- AI 保护：**{report['protection']}**",
        f"- 状态库：**SQLite／{'已启用' if backend.get('active') else '待验证'}**",
        f"- 状态证据：**{freshness}**",
        f"- 已归档记录：{report.get('archived_count', 0)} 项",
    ]
    historical_count = int(report.get("historical_unreviewed_count", 0) or 0)
    if historical_count:
        lines.append(f"- 历史未验收记录：{historical_count} 项（无需逐项处理）")
    lines.extend(["", "AI 状态来自 SQLite；任务卡和 JSONL 只作为单向投影。"])
    return "\n".join(lines) + "\n"


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_status(store: StateStore, report: dict[str, Any], output: Path) -> None:
    rendered = output.with_suffix(".md")
    atomic_text(output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_text(rendered, render_status_markdown(report))
    record_projection(store, output, "user_status_json")
    record_projection(store, rendered, "user_status_markdown")


def write_scheduler_receipt(root: Path, trigger: str, *, ok: bool = True) -> Path:
    target = runtime_dir(root) / "status/scheduler-receipt.json"
    payload = {
        "schema_version": 1,
        "trigger": trigger,
        "ok": bool(ok),
        "completed_at": now_local().isoformat(timespec="seconds"),
    }
    atomic_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新息壤普通用户状态")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--trigger",
        choices=("manual", "scheduler", "install", "task", "decision", "session-start"),
        default="manual",
    )
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output = args.output.expanduser() if args.output else runtime_dir(root) / "status/current-status.json"
    try:
        database = state_database(root, explicit=args.database)
        if not database.is_file():
            raise RuntimeError("SQLite 状态库尚未激活，未创建空数据库")
        store = StateStore(database)
        report = build_status(root, store=store)
        if args.write:
            write_status(store, report, output)
            if args.trigger == "scheduler":
                write_scheduler_receipt(root, args.trigger)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else f"息壤状态：{report['state']}；{report['user_action']}")
        return 0
    except Exception as exc:
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"状态刷新失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
