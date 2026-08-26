#!/usr/bin/env python3
"""Render the task MOC only from the active StateStore."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from xirang_state import StateStore, scope_covers
from xirang_state_migrate import record_projection, require_active, state_database


ACTIVE = {"in_progress", "blocked"}
AWAITING = {"submitted", "reviewing"}


def tasks(store: StateStore) -> list[dict[str, Any]]:
    require_active(store)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT t.*, l.title, l.source_path FROM tasks t
               LEFT JOIN legacy_task_cards l ON l.task_id=t.task_id
               WHERE t.lifecycle_status != 'archived'
               ORDER BY t.updated_at DESC, t.task_id DESC"""
        ).fetchall()
    return [{
        "task_id": row["task_id"], "title": row["title"] or row["task_id"],
        "status": row["lifecycle_status"], "review_status": row["review_status"],
        "updated_at": row["updated_at"], "source_path": row["source_path"],
    } for row in rows]


def require_write_authority(store: StateStore, session_id: str) -> None:
    matches = []
    for task in store.find_active_tasks(session_id):
        roots = task["allowed_write_roots"]
        if task["task_kind"] == "control_plane_maintenance" and any(
            scope_covers(root, "02-项目管理/任务卡/_MOC.md") for root in roots
        ):
            matches.append(task["task_id"])
    if len(matches) != 1:
        raise PermissionError("写入任务 MOC 需要 SQLite 中当前会话唯一维护任务授权")


def task_label(row: dict[str, Any]) -> str:
    source = row.get("source_path")
    return f"[[{Path(source).with_suffix('').as_posix()}|{row['task_id']}]]" if source else row["task_id"]


def task_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["当前无。"]
    output = ["| 任务 | 标题 | 状态 | 评审 |", "|---|---|---|---|"]
    for row in rows:
        output.append(
            f"| {task_label(row)} | {row['title']} | {row['status']} | {row['review_status']} |"
        )
    return output


def render(store: StateStore) -> str:
    all_tasks = tasks(store)
    active = [row for row in all_tasks if row["status"] in ACTIVE]
    awaiting = [row for row in all_tasks if row["review_status"] in AWAITING]
    counts = Counter(row["status"] for row in all_tasks)
    months: dict[str, Counter[str]] = defaultdict(Counter)
    for row in all_tasks:
        month = str(row.get("updated_at") or "")[:7] or "其他"
        months[month][row["status"]] += 1
    lines = [
        "---", "title: 任务卡", "type: moc", "status: active",
        f"updated: {date.today().isoformat()}",
        "generated_by: .standards/xirang-task-index.py", "source: sqlite",
        "tags: [项目管理, 任务卡]", "---", "", "# 任务卡", "",
        "> 本页由 SQLite 单向生成；任务卡不得反向修改运行状态。", "",
        "## 当前执行", "", *task_table(active), "",
        "## 等待验收", "", *task_table(awaiting), "",
        "## 总量", "", f"任务总数：**{len(all_tasks)}**。", "",
        "| 状态 | 数量 |", "|---|---:|",
    ]
    lines.extend(f"| {status} | {count} |" for status, count in sorted(counts.items()))
    lines.extend(["", "## 月份分布", "", "| 月份 | 数量 |", "|---|---:|"])
    for month, values in sorted(months.items(), reverse=True):
        lines.append(f"| {month} | {sum(values.values())} |")
    lines.extend(["", "## 规则", "", "- 当前工作只看当前执行和等待验收。", "- 归档只改变数据库状态，不删除权威记录。", ""])
    return "\n".join(lines)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    target = root / "02-项目管理/任务卡/_MOC.md"
    try:
        store = StateStore(state_database(root, explicit=args.database))
        output = render(store)
        if args.write:
            require_write_authority(store, args.session_id)
            atomic_write(target, output)
            record_projection(store, target, "task_index")
        print(json.dumps({
            "ok": True, "mode": "write" if args.write else "check",
            "target": str(target), "bytes": len(output.encode()), "task_count": len(tasks(store)),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
