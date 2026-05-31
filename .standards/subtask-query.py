#!/usr/bin/env python3
"""
subtask-query.py -- V8.5 子任务记录查询工具（只读）
v1.0.1 | 2026-05-24 | 息壤 V8.5.0

只读查询，任何 Agent 都可安全调用。

用法:
  python3 .standards/subtask-query.py --active              # 所有活跃子任务
  python3 .standards/subtask-query.py --task T-20260524-01  # 某父任务下所有子任务
  python3 .standards/subtask-query.py --sub sub-01 --task T-20260524-01  # 单个详情
  python3 .standards/subtask-query.py --stale 300           # 超时未心跳的子任务
  python3 .standards/subtask-query.py --summary             # 统计摘要
  python3 .standards/subtask-query.py --all                 # 所有记录（含已完成）

输出: JSON（默认）
退出码: 0=成功, 1=无结果

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/subtask-query.py --active"
"""

from __future__ import annotations

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "$VAULT_ROOT"))
TEMP_DIR = VAULT_ROOT / "_temp"

# 活跃状态：仍需父 Agent 关注的所有状态（含待回收）
ACTIVE_STATES = {"CREATED", "RUNNING", "RETRYING", "FAILED", "TIMEOUT", "RETRY_EXHAUSTED", "SUCCESS", "RECLAIMED", "ESCALATED"}
# 真正的终态：已处理完毕
TERMINAL_STATES = {"COLLECTED", "DESTROYED"}


def find_all_records() -> list[dict]:
    """扫描 _temp/ 下所有子任务记录"""
    records = []
    if not TEMP_DIR.exists():
        return records

    for task_dir in TEMP_DIR.iterdir():
        if not task_dir.is_dir():
            continue
        subtasks_dir = task_dir / "subtasks"
        if not subtasks_dir.exists():
            continue
        for record_file in subtasks_dir.glob("*.json"):
            try:
                record = json.loads(record_file.read_text(encoding="utf-8"))
                records.append(record)
            except (json.JSONDecodeError, IOError):
                continue

    return records


def find_records_for_task(task_id: str) -> list[dict]:
    """查找某父任务下所有子任务记录"""
    records = []
    subtasks_dir = TEMP_DIR / task_id / "subtasks"
    if not subtasks_dir.exists():
        return records

    for record_file in subtasks_dir.glob("*.json"):
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
            records.append(record)
        except (json.JSONDecodeError, IOError):
            continue

    return records


def get_last_heartbeat_age(record: dict) -> Optional[float]:
    """计算最后一次心跳距今秒数，无心跳则用 spawn_ts"""
    heartbeats = [m for m in record.get("messages", []) if m["role"] == "heartbeat"]
    if heartbeats:
        last_ts = heartbeats[-1]["ts"]
    else:
        last_ts = record.get("spawn_ts")

    if not last_ts:
        return None

    try:
        # 尝试解析 ISO 时间
        last_dt = datetime.fromisoformat(last_ts)
        now = datetime.now(tz=last_dt.tzinfo or timezone.utc)
        return (now - last_dt).total_seconds()
    except (ValueError, TypeError):
        return None


def format_record_brief(record: dict) -> dict:
    """简化输出格式"""
    age = get_last_heartbeat_age(record)
    return {
        "sub_id": record["sub_id"],
        "task_id": record["task_id"],
        "parent_agent": record.get("parent_agent"),
        "state": record["state"],
        "task_name": record.get("task_name", ""),
        "model": record.get("model", ""),
        "spawn_ts": record.get("spawn_ts", ""),
        "artifacts": len(record.get("artifacts", [])),
        "cost_tokens": record.get("cost", {}).get("tokens", 0),
        "last_activity_sec_ago": round(age) if age else None,
    }


def main():
    parser = argparse.ArgumentParser(description="V8.5 子任务记录查询（只读）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--active", action="store_true", help="所有活跃子任务")
    group.add_argument("--task", type=str, help="查看某父任务下所有子任务")
    group.add_argument("--stale", type=int, metavar="SECONDS", help="超时未心跳的子任务")
    group.add_argument("--summary", action="store_true", help="统计摘要")
    group.add_argument("--all", action="store_true", help="所有记录")
    parser.add_argument("--sub", type=str, help="指定子任务 ID（与 --task 配合）")
    parser.add_argument("--json", action="store_true", default=True)
    parser.add_argument("--detail", action="store_true", help="输出完整记录（不简化）")
    args = parser.parse_args()

    if args.task:
        records = find_records_for_task(args.task)
        if args.sub:
            records = [r for r in records if r["sub_id"] == args.sub]
    else:
        records = find_all_records()

    # 过滤
    if args.active:
        records = [r for r in records if r["state"] in ACTIVE_STATES]
    elif args.stale is not None:
        # 活跃 + 超时
        stale_records = []
        for r in records:
            if r["state"] not in ACTIVE_STATES:
                continue
            age = get_last_heartbeat_age(r)
            if age is not None and age > args.stale:
                stale_records.append(r)
        records = stale_records

    # 输出
    if args.summary:
        all_records = find_all_records()
        state_counts = {}
        for r in all_records:
            state_counts[r["state"]] = state_counts.get(r["state"], 0) + 1

        total_tokens = sum(r.get("cost", {}).get("tokens", 0) for r in all_records)
        total_cost = sum(r.get("cost", {}).get("cost_cny", 0) for r in all_records)

        output = {
            "total_records": len(all_records),
            "state_distribution": state_counts,
            "active_count": sum(1 for r in all_records if r["state"] in ACTIVE_STATES),
            "total_tokens": total_tokens,
            "total_cost_cny": round(total_cost, 4),
            "parent_tasks": list(set(r["task_id"] for r in all_records)),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.detail or args.sub:
        # 完整记录
        output = records if len(records) != 1 else records[0]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # 简化输出
        output = [format_record_brief(r) for r in records]
        print(json.dumps({"count": len(output), "records": output}, ensure_ascii=False, indent=2))

    sys.exit(0 if records or args.summary else 1)


if __name__ == "__main__":
    main()
