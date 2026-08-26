#!/usr/bin/env python3
"""Archive tasks by changing SQLite state without deleting authority records."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xirang_state import StateStore, refresh_events_projection, scope_covers
from xirang_state_migrate import require_active, runtime_dir, state_database, workspace_id


TERMINAL_LIFECYCLE = {"submitted", "completed"}
TERMINAL_REVIEW = {"accepted", "not_required", "superseded", "legacy_unreviewed"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_maintenance(store: StateStore, session_id: str) -> str:
    matches = []
    for task in store.find_active_tasks(session_id):
        if task["task_kind"] != "control_plane_maintenance":
            continue
        if any(scope_covers(root, "02-项目管理/任务卡") for root in task["allowed_write_roots"]):
            matches.append(task["task_id"])
    if len(matches) != 1:
        raise ValueError("归档需要 SQLite 中当前会话唯一维护任务")
    return matches[0]


def validate_snapshot(path: Path) -> None:
    if not path.is_file() or StateStore._integrity_check_path(path) != ["ok"]:
        raise ValueError("外部 SQLite 快照不存在或完整性失败")


def archive(
    root: Path,
    store: StateStore,
    session_id: str,
    task_ids: list[str],
    summary: Path,
    backup: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    summary = summary.expanduser().resolve()
    backup = backup.expanduser().resolve()
    require_active(store)
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("任务号重复")
    maintainer = active_maintenance(store, session_id)
    if maintainer in task_ids:
        raise ValueError("不能归档当前活动维护任务")
    if not summary.is_file() or root not in summary.resolve().parents:
        raise ValueError("摘要必须是 Vault 内现有文件")
    summary_text = summary.read_text(encoding="utf-8")
    missing = [task_id for task_id in task_ids if task_id not in summary_text]
    if missing:
        raise ValueError(f"摘要未覆盖任务：{', '.join(missing)}")
    validate_snapshot(backup)
    with store.connect() as connection:
        rows = {
            row["task_id"]: row for row in connection.execute(
                f"SELECT * FROM tasks WHERE task_id IN ({','.join('?' for _ in task_ids)})", task_ids
            ).fetchall()
        }
    unknown = sorted(set(task_ids) - set(rows))
    if unknown:
        raise ValueError(f"任务不存在：{', '.join(unknown)}")
    for task_id, row in rows.items():
        if row["lifecycle_status"] not in TERMINAL_LIFECYCLE or row["review_status"] not in TERMINAL_REVIEW:
            raise ValueError(f"任务不是可归档终态：{task_id}")
    result = {
        "ok": True, "mode": "apply" if apply else "check", "count": len(task_ids),
        "active_maintenance_task": maintainer, "summary": str(summary.relative_to(root)),
        "summary_sha256": sha256(summary), "backup": str(backup), "backup_sha256": sha256(backup),
        "archived": [], "deleted": [],
    }
    if apply:
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with store.transaction(immediate=True) as connection:
            for task_id in task_ids:
                connection.execute(
                    """UPDATE tasks SET lifecycle_status='archived', runtime_status='archived',
                       updated_at=? WHERE task_id=?""",
                    (stamp, task_id),
                )
                event_id = f"E-ARCHIVE-{hashlib.sha256((task_id + stamp).encode()).hexdigest()[:20]}"
                payload = {
                    "task_id": task_id, "summary": str(summary.relative_to(root)),
                    "backup": str(backup), "authority_record_deleted": False,
                }
                connection.execute(
                    """INSERT INTO events(
                         event_id, event_type, occurred_at, workspace_id, session_id, task_id, payload_json
                       ) VALUES (?, 'task_archived', ?, ?, ?, ?, ?)""",
                    (event_id, stamp, workspace_id(root), session_id, task_id, json.dumps(payload, ensure_ascii=False)),
                )
                result["archived"].append(task_id)
        result["applied_at"] = stamp
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        store = StateStore(state_database(root, explicit=args.database))
        result = archive(
            root, store, args.session_id, args.task_id,
            (root / args.summary).resolve(), Path(args.backup).expanduser().resolve(),
            apply=args.apply,
        )
        if args.apply:
            refresh_events_projection(
                store,
                workspace_root=root,
                output=runtime_dir(root) / "events/events.jsonl",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
