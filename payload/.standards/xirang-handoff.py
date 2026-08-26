#!/usr/bin/env python3
"""Create and consume StateStore-backed cross-session handoff checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xirang_state import SCHEMA_VERSION, StateConflict, StateStore
from xirang_state_cli import probe_backend, resolve_database


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def state_store(root: Path) -> StateStore:
    root = root.expanduser().resolve()
    database = resolve_database(root)
    probe = probe_backend(root, database)
    if probe.active is not True:
        raise StateConflict(
            f"handoff 只能使用已存在且严格激活的 StateStore：{probe.reason}"
        )
    store = StateStore(database)
    try:
        with store.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise StateConflict(f"handoff StateStore 只读校验失败：{exc}") from exc
    version = int(row["version"] or 0) if row is not None else 0
    if version != SCHEMA_VERSION:
        raise StateConflict(
            f"handoff requires schema {SCHEMA_VERSION}, found {version}; 禁止自动迁移"
        )
    return store


def load_payload(path: Path | None, text: str | None) -> dict:
    if path:
        return json.loads(path.read_text(encoding="utf-8"))
    if text:
        return json.loads(text)
    raise SystemExit("checkpoint requires --payload-file or --payload-json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("checkpoint", "latest", "consume"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--task-id")
    parser.add_argument("--source-session-id")
    parser.add_argument("--consumer-session-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--handoff-id")
    parser.add_argument("--payload-file", type=Path)
    parser.add_argument("--payload-json")
    parser.add_argument("--ttl-hours", type=float, default=168)
    args = parser.parse_args()
    store = state_store(args.root)
    wid = workspace_id(args.root)
    if args.action == "checkpoint":
        if not args.task_id or not args.source_session_id:
            raise SystemExit("checkpoint requires --task-id and --source-session-id")
        result = store.create_session_handoff(
            task_id=args.task_id, session_id=args.source_session_id,
            payload=load_payload(args.payload_file, args.payload_json),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=args.ttl_hours),
        )
    elif args.action == "latest":
        if not args.task_id:
            raise SystemExit("latest requires --task-id; workspace-wide handoff discovery is forbidden")
        result = store.latest_session_handoff(
            workspace_id=wid, task_id=args.task_id, source_session_id=args.source_session_id,
        ) or {"ok": False, "reason": "no_valid_handoff"}
    else:
        if not args.handoff_id or not args.consumer_session_id or not args.agent_id:
            raise SystemExit("consume requires --handoff-id, --consumer-session-id and --agent-id")
        result = store.consume_session_handoff(
            handoff_id=args.handoff_id, consumer_session_id=args.consumer_session_id,
            consumer_agent_id=args.agent_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
