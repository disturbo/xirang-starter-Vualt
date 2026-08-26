#!/usr/bin/env python3
"""Canary evaluation backed only by the active Xi Rang StateStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from xirang_state import StateStore, refresh_events_projection
from xirang_state_migrate import (
    metadata_get,
    metadata_set,
    record_projection,
    require_active,
    runtime_dir,
    state_database,
)


def root_default() -> Path:
    return Path(__file__).resolve().parents[1]


def sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def parse_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_rows(store: StateStore, session_id: str, platform: str) -> list[dict[str, Any]]:
    require_active(store)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT event_type, occurred_at, payload_json FROM events
               WHERE session_id=? ORDER BY sequence""",
            (session_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("platform") not in (None, "", platform):
            continue
        item = dict(payload)
        item.setdefault("event", row["event_type"])
        item.setdefault("ts", row["occurred_at"])
        result.append(item)
    return result


def write_receipt_rows(store: StateStore, session_id: str) -> list[dict[str, Any]]:
    """Project authoritative successful writes into canary-shaped evidence rows."""
    if not session_id:
        return []
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT receipt_id, task_id, path, exists_after, created_at
               FROM write_receipts
               WHERE session_id=? AND status='effective' AND exists_after=1
               ORDER BY created_at, receipt_id""",
            (session_id,),
        ).fetchall()
    return [
        {
            "event": "file_write",
            "receipt_id": row["receipt_id"],
            "task_id": row["task_id"],
            "file": row["path"],
            "exists": bool(row["exists_after"]),
            "ts": row["created_at"],
            "evidence_source": "sqlite_write_receipt",
        }
        for row in rows
    ]


VERIFIED_FIELDS = (
    "actor_verified", "disclosure_verified", "sequence_verified", "enforcement_verified",
)
TRUSTED_HOST_PROOF_FIELDS = VERIFIED_FIELDS + (
    "host_proof_nonforgeable", "single_use_event_verified", "worker_identity_verified",
)
VALID_MODES = {"trusted", "manual_guard", "contract_only"}
EVIDENCE_TTL = timedelta(hours=1)


def platform_registration(root: Path, platform: str) -> dict[str, Any]:
    registry = root / ".xirang/adapters/registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"平台 registry 不可用：{exc}") from exc
    if data.get("schema_version") != 3:
        raise RuntimeError("平台 registry 必须是 schema v3")
    row = (data.get("platforms") or {}).get(platform)
    if not isinstance(row, dict):
        raise RuntimeError(f"平台未注册：{platform}")
    mode = row.get("allowed_mode")
    if mode not in VALID_MODES:
        raise RuntimeError(f"平台 allowed_mode 非法：{platform}={mode}")
    try:
        contract = (root / ".xirang/contract/policy.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"机器契约不可用：{exc}") from exc
    if not re.search(rf"(?m)^  {re.escape(str(mode))}:\s*$", contract):
        raise RuntimeError(f"allowed_mode 未在机器契约声明：{mode}")
    if not isinstance(row.get("connected"), bool):
        raise RuntimeError(f"平台 connected 必须为布尔值：{platform}")
    if mode == "manual_guard" and row.get("connected") is not False:
        raise RuntimeError(f"manual_guard 不得声明 connected：{platform}")
    compatibility = row.get("adapter_compatibility")
    if isinstance(compatibility, dict) and compatibility.get("connected") is not row.get("connected"):
        raise RuntimeError(f"兼容投影 connected 与权威字段不一致：{platform}")
    return row


def check(
    root: Path,
    platform: str,
    session_id: str | None,
    *,
    store: StateStore | None = None,
    trusted_proof: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    store = store or StateStore(state_database(root))
    require_active(store)
    registration = platform_registration(root, platform)
    mode = str(registration["allowed_mode"])
    application_state = str(registration.get("application_state") or "unverified")
    declared_canary_state = str(registration.get("canary_state") or "unverified")
    declared_connected = registration.get("connected") is True
    sid = str(session_id or "")
    rows = event_rows(store, sid, platform) if sid else []
    current = datetime.now(timezone.utc)
    relevant_types = {
        "session_start", "user_prompt", "user_event_recorded", "shell_command",
        "file_write", "shell_command_denied",
    }
    relevant_rows = [row for row in rows if row.get("event") in relevant_types]
    evidence_times = [parse_time(row.get("ts")) for row in relevant_rows]
    evidence_time = max(evidence_times) if evidence_times else None

    def is_fresh(row: dict[str, Any]) -> bool:
        age = current - parse_time(row.get("ts"))
        return timedelta(0) <= age <= EVIDENCE_TTL

    def matching(*event_types: str) -> list[dict[str, Any]]:
        return [row for row in rows if row.get("event") in event_types]

    def latest(rows_for_check: list[dict[str, Any]]) -> datetime | None:
        times = [parse_time(row.get("ts")) for row in rows_for_check]
        return max(times) if times else None

    session_rows = matching("session_start")
    prompt_rows = matching("user_prompt", "user_event_recorded")
    readonly_rows = matching("shell_command")
    deny_rows = matching("shell_command_denied")
    canary_rows = [
        row for row in rows
        if row.get("event") == "file_write" and str(row.get("file") or row.get("path") or "").endswith(".xirang/canary.tmp")
    ]
    create_rows = [row for row in canary_rows if row.get("exists") is True]
    delete_rows = [row for row in canary_rows if row.get("exists") is False]
    fresh_create = [row for row in create_rows if is_fresh(row)]
    fresh_delete = [row for row in delete_rows if is_fresh(row)]
    legacy_write_audit = bool(
        fresh_create and fresh_delete
        and parse_time(fresh_delete[-1].get("ts")) >= parse_time(fresh_create[-1].get("ts"))
    )
    receipt_rows = write_receipt_rows(store, sid)
    ordinary_write_rows = [
        row for row in [*matching("file_write"), *receipt_rows]
        if row.get("exists") is True
        and row.get("task_id")
        and not str(row.get("file") or row.get("path") or "").endswith(".xirang/canary.tmp")
    ]
    fresh_ordinary_writes = [row for row in ordinary_write_rows if is_fresh(row)]
    # A normal, scoped, receipt-backed write is a stronger and non-destructive
    # signal.  The create/delete pair remains accepted only for old evidence.
    write_audit = bool(fresh_ordinary_writes or legacy_write_audit)
    checks = {
        "session_start": any(is_fresh(row) for row in session_rows),
        "user_prompt": any(is_fresh(row) for row in prompt_rows),
        "readonly_tool": any(is_fresh(row) for row in readonly_rows),
        "write_audit": write_audit,
        "deny_control": any(is_fresh(row) for row in deny_rows),
        "cleanup": not (root / ".xirang/canary.tmp").exists(),
    }
    checks["fresh_current_session"] = all(checks.values())
    evidence_groups = {
        "session_start": session_rows,
        "user_prompt": prompt_rows,
        "readonly_tool": readonly_rows,
        "write_receipt": ordinary_write_rows,
        "write_create": create_rows,
        "write_delete": delete_rows,
        "deny_control": deny_rows,
    }
    evidence_freshness = {
        key: {
            "latest": stamp.isoformat(timespec="seconds") if stamp else None,
            "fresh": bool(stamp and timedelta(0) <= current - stamp <= EVIDENCE_TTL),
        }
        for key, group in evidence_groups.items()
        for stamp in [latest(group)]
    }
    behavior_ready = bool(sid) and all(checks.values())
    proof = dict(trusted_proof or {})
    verified = {
        field: bool(mode == "trusted" and proof.get(field) is True)
        for field in VERIFIED_FIELDS
    }
    host_proof_complete = bool(
        mode == "trusted" and all(proof.get(field) is True for field in TRUSTED_HOST_PROOF_FIELDS)
    )
    connected = bool(
        mode == "trusted"
        and declared_connected
        and application_state == "applied"
        and behavior_ready
        and host_proof_complete
        and all(verified.values())
    )
    if connected:
        canary_state = "connected"
    elif behavior_ready and mode == "manual_guard":
        canary_state = "manual_guard_ready"
    elif behavior_ready and mode == "trusted":
        canary_state = "needs_maintenance"
    elif declared_canary_state == "manual_guard_ready":
        canary_state = "unverified"
    else:
        canary_state = declared_canary_state
    write_stamp = latest(fresh_ordinary_writes)
    if write_stamp is None and legacy_write_audit:
        write_stamp = latest(fresh_delete)
    required_stamps = [
        latest(session_rows), latest(prompt_rows), latest(readonly_rows),
        write_stamp, latest(deny_rows),
    ]
    valid_until = min(stamp + EVIDENCE_TTL for stamp in required_stamps if stamp) if all(required_stamps) else None
    entry = str(registration.get("hook_entry") or registration.get("entry") or "")
    if platform == "codex":
        config = root / ".codex/hooks.json"
    elif platform == "claude":
        config = root / ".claude/settings.json"
    else:
        candidate = Path(entry).expanduser()
        config = candidate if candidate.is_absolute() else root / candidate
    write_ready = bool(
        connected
        or (
            mode == "manual_guard"
            and application_state == "applied"
            and canary_state == "manual_guard_ready"
            and behavior_ready
        )
    )
    return {
        "schema_version": 3,
        "backend": "sqlite",
        "platform": platform,
        "allowed_mode": mode,
        "application_state": application_state,
        "registered_canary_state": declared_canary_state,
        "registered_connected": declared_connected,
        "canary_state": canary_state,
        "session_id": sid,
        "checked_at": evidence_time.isoformat(timespec="seconds") if evidence_time else None,
        "evaluated_at": current.isoformat(timespec="seconds"),
        "valid_until": valid_until.isoformat(timespec="seconds") if valid_until else None,
        "contract_sha256": sha(root / ".xirang/contract/policy.yaml"),
        "hooks_sha256": sha(config),
        "adapter_sha256": sha(root / ".standards/hooks/codex-hook-adapter.py"),
        "checks": checks,
        "evidence_freshness": evidence_freshness,
        "behavior_evidence_complete": behavior_ready,
        "write_ready": write_ready,
        "host_proof_complete": host_proof_complete,
        **verified,
        "connected": connected,
        "valid_minutes": 60,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persist_result(root: Path, store: StateStore, payload: dict[str, Any]) -> Path:
    store.append_event(
        "canary_checked",
        payload,
        workspace_id=hashlib.sha256(str(root).encode()).hexdigest()[:12],
        session_id=payload.get("session_id"),
        occurred_at=payload["evaluated_at"],
    )
    states = metadata_get(store, "platform_states", {})
    if not isinstance(states, dict):
        states = {}
    states[payload["platform"]] = {
        key: payload.get(key) for key in (
            "allowed_mode", "application_state", "canary_state", "connected",
            "write_ready",
            "actor_verified", "disclosure_verified", "sequence_verified",
            "enforcement_verified", "checked_at", "evaluated_at", "valid_until",
        )
    }
    metadata_set(store, "platform_states", states)
    target = runtime_dir(root) / "canary" / f"{payload['platform']}.json"
    atomic_json(target, payload)
    record_projection(store, target, "canary")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("instructions", "check"))
    parser.add_argument("--root", type=Path, default=root_default())
    parser.add_argument("--platform", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.action == "instructions":
        print(json.dumps({
            "platform": args.platform,
            "steps": [
                "确认当前 SQLite backend 已激活",
                "执行只读命令 git status --short",
                "在当前授权范围内完成一次正常写入，并确认写后收据已登记",
                "执行无害拒绝探针并确认被 Hook 拒绝",
                f"运行 xirang-canary.py check --platform {args.platform} --write",
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    try:
        store = StateStore(state_database(root, explicit=args.database))
        payload = check(root, args.platform, args.session_id, store=store)
        if args.write:
            persist_result(root, store, payload)
            refresh_events_projection(
                store,
                workspace_root=root,
                output=runtime_dir(root) / "events/events.jsonl",
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["write_ready"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
