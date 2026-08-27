#!/usr/bin/env python3
"""Repair effective write-receipt chains in the active StateStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xirang_state import StateStore, canonical_scope, refresh_events_projection, scope_covers
from xirang_state_migrate import (
    require_active,
    runtime_dir,
    state_database,
    workspace_id,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def resolve(root: Path, file: str) -> Path:
    raw = Path(file)
    return raw.expanduser().resolve() if raw.is_absolute() else (root / raw).resolve()


def receipts(store: StateStore, task_id: str) -> list[dict[str, Any]]:
    require_active(store)
    return store.list_effective_write_receipts(task_id)


def check(root: Path, task_id: str, *, store: StateStore) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    for row in receipts(store, task_id):
        file = row["path"]
        path = resolve(root, file)
        if not bool(row.get("exists_after")):
            if path.exists():
                problems.append({
                    "file": file, "kind": "unexpected_exists", "receipt_id": row["receipt_id"],
                })
            continue
        if not path.is_file():
            problems.append({"file": file, "kind": "unresolvable", "receipt_id": row["receipt_id"]})
            continue
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != row.get("sha256"):
            problems.append({
                "file": file, "kind": "hash_mismatch", "receipt_id": row["receipt_id"],
                "recorded": row.get("sha256"), "current": current,
            })
    return {"ok": True, "task_id": task_id, "problem_count": len(problems), "problems": problems}


def latest_receipt(store: StateStore, task_id: str, path: str) -> dict[str, Any] | None:
    candidates = [row for row in receipts(store, task_id) if row["path"] == path]
    return max(candidates, key=lambda row: (row["created_at"], row["receipt_id"])) if candidates else None


def repair(
    root: Path,
    task_id: str,
    old_path: str,
    new_path: str,
    *,
    rehash: bool,
    store: StateStore,
) -> dict[str, Any]:
    old = latest_receipt(store, task_id, canonical_scope(old_path))
    if old is None:
        return {"ok": False, "error": "未找到该路径的有效 SQLite 收据"}
    normalized = canonical_scope(new_path)
    disk = resolve(root, normalized)
    if not disk.is_file():
        raise ValueError(f"目标文件不存在：{normalized}")
    disk_sha = hashlib.sha256(disk.read_bytes()).hexdigest()
    sha = disk_sha if rehash else old.get("sha256")
    if not rehash and sha != disk_sha:
        raise ValueError(f"原收据哈希与磁盘不一致，请改用 refresh：{normalized}")
    receipt_id = hashlib.sha256(
        f"{old['session_id']}:evidence-repair:{normalized}:{uuid.uuid4().hex}".encode()
    ).hexdigest()[:24]
    stamp = now_iso()
    event_id = f"E-REPAIR-{receipt_id}"
    with store.transaction(immediate=True) as connection:
        task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"任务不存在：{task_id}")
        roots = json.loads(task["allowed_write_roots_json"])
        if not any(scope_covers(scope, normalized) for scope in roots):
            raise ValueError(f"修复路径超出任务授权根：{normalized}")
        current = connection.execute(
            "SELECT * FROM write_receipts WHERE receipt_id=? AND status='effective'", (old["receipt_id"],)
        ).fetchone()
        if current is None or current["superseded_by_receipt_id"]:
            raise ValueError("前序收据已失效或已有后继")
        payload = {
            "event": "file_write", "receipt_id": receipt_id, "task_id": task_id,
            "session_id": old["session_id"], "file": normalized,
            "operation": old.get("operation") or "edit", "exists": True,
            "sha256": sha, "supersedes_receipt": old["receipt_id"],
            "reason": "hash_refresh" if rehash else "path_canonicalization",
            "actor_verified": False,
        }
        connection.execute(
            """INSERT INTO events(
                 event_id, event_type, occurred_at, workspace_id, session_id, task_id, payload_json
               ) VALUES (?, 'file_write', ?, ?, ?, ?, ?)""",
            (event_id, stamp, workspace_id(root), old["session_id"], task_id, json.dumps(payload, ensure_ascii=False)),
        )
        connection.execute(
            """INSERT INTO write_receipts(
                 receipt_id, task_id, event_id, session_id, path, operation, sha256,
                 exists_after, predecessor_receipt_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                receipt_id, task_id, event_id, old["session_id"], normalized,
                old.get("operation") or "edit", sha, old["receipt_id"], stamp,
            ),
        )
        updated = connection.execute(
            """UPDATE write_receipts SET status='superseded', superseded_by_receipt_id=?
               WHERE receipt_id=? AND status='effective' AND superseded_by_receipt_id IS NULL""",
            (receipt_id, old["receipt_id"]),
        )
        if updated.rowcount != 1:
            raise ValueError("收据替代并发冲突")
    return {
        "ok": True, "task_id": task_id, "old_file": old_path, "new_file": normalized,
        "superseded_receipt": old["receipt_id"], "new_receipt_id": receipt_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤 SQLite 证据修复")
    parser.add_argument("action", choices=("check", "fix-path", "refresh"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--file")
    parser.add_argument("--rel")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        store = StateStore(state_database(root, explicit=args.database))
        if args.action == "check":
            result = check(root, args.task_id, store=store)
        elif args.action == "fix-path":
            if not args.file or not args.rel:
                raise ValueError("fix-path 需要 --file 与 --rel")
            result = repair(root, args.task_id, args.file, args.rel, rehash=False, store=store)
        else:
            if not args.file:
                raise ValueError("refresh 需要 --file")
            result = repair(root, args.task_id, args.file, args.file, rehash=True, store=store)
        if args.action != "check" and result.get("ok"):
            refresh_events_projection(
                store,
                workspace_root=root,
                output=runtime_dir(root) / "events/events.jsonl",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
