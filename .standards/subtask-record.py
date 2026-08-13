#!/usr/bin/env python3
"""
subtask-record.py -- V8.5 子任务运行时记录 CRUD 工具
v1.0.0 | 2026-05-24 | 息壤 V8.5.0

每个 spawn 的子 Agent 都有一份 JSON 记录，存储于:
  _temp/{parent_task_id}/subtasks/{sub_id}.json

用法:
  python3 .standards/subtask-record.py create   --task-id T-xxx --sub-id sub-01 ...
  python3 .standards/subtask-record.py transition --task-id T-xxx --sub-id sub-01 --to RUNNING
  python3 .standards/subtask-record.py heartbeat --task-id T-xxx --sub-id sub-01 --progress "..."
  python3 .standards/subtask-record.py artifact  --task-id T-xxx --sub-id sub-01 --path "..." --type html
  python3 .standards/subtask-record.py collect   --task-id T-xxx --sub-id sub-01 [--tokens N --cost N]
  python3 .standards/subtask-record.py destroy   --task-id T-xxx --sub-id sub-01

退出码:
  0 = 成功
  1 = 记录不存在
  2 = 非法状态转换
  3 = 参数错误

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/subtask-record.py <cmd> --json ..."
"""
from __future__ import annotations

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", Path.home() / "Desktop" / "obsidianVault"))
TEMP_DIR = VAULT_ROOT / "_temp"

# V8 10 态状态机 — 合法转换表
VALID_TRANSITIONS = {
    "CREATED": {"RUNNING", "RECLAIMED", "DESTROYED"},
    "RUNNING": {"SUCCESS", "FAILED", "TIMEOUT", "RECLAIMED"},
    "SUCCESS": {"COLLECTED"},
    "FAILED": {"RETRYING", "RECLAIMED", "ESCALATED"},
    "TIMEOUT": {"RETRYING", "RECLAIMED", "ESCALATED"},
    "RETRYING": {"RUNNING", "RETRY_EXHAUSTED"},
    "RETRY_EXHAUSTED": {"RECLAIMED", "ESCALATED"},
    "RECLAIMED": {"COLLECTED"},
    "ESCALATED": {"COLLECTED"},
    "COLLECTED": {"DESTROYED"},
    "DESTROYED": set(),  # 终态
}

VALID_TASK_TYPES = {"prototype", "code", "prd", "research", "spec", "review", "batch", "diagram"}
VALID_AGENTS = {"claudian", "assistant", "xiaochong", "toubao", "workbuddy", "qingmeisu", "hongmeisu"}

# 别名映射：英文别名 -> 规范 agent_id（v8-handshake.sh 用英文别名，本工具用规范 ID）
AGENT_ALIASES = {
    "amoxicillin": "xiaochong", "amox": "xiaochong",
    "cephalosporin": "toubao", "ceph": "toubao",
    "penicillin": "qingmeisu", "peni": "qingmeisu",
    "erythromycin": "hongmeisu", "eryth": "hongmeisu",
}


def resolve_agent_id(agent: str) -> str:
    """将别名解析为规范 agent_id"""
    if agent in VALID_AGENTS:
        return agent
    return AGENT_ALIASES.get(agent, agent)


def now_iso() -> str:
    """当前时间 ISO 8601 格式"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_record_path(task_id: str, sub_id: str) -> Path:
    """获取子任务记录文件路径"""
    return TEMP_DIR / task_id / "subtasks" / f"{sub_id}.json"


def load_record(task_id: str, sub_id: str) -> dict | None:
    """加载子任务记录，不存在返回 None"""
    path = get_record_path(task_id, sub_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_record(record: dict) -> Path:
    """保存子任务记录"""
    path = get_record_path(record["task_id"], record["sub_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cmd_create(args) -> dict:
    """创建子任务记录"""
    # 参数校验：解析别名
    if args.parent:
        args.parent = resolve_agent_id(args.parent)
    if args.parent and args.parent not in VALID_AGENTS:
        return {"status": "error", "code": 3, "message": f"非法 parent agent: {args.parent}（有效值: {sorted(VALID_AGENTS)}，别名: {sorted(AGENT_ALIASES.keys())}）"}
    if args.type and args.type not in VALID_TASK_TYPES:
        return {"status": "error", "code": 3, "message": f"非法 task_type: {args.type}"}

    # 检查是否已存在
    existing = load_record(args.task_id, args.sub_id)
    if existing:
        return {"status": "error", "code": 3, "message": f"记录已存在: {args.sub_id}（state={existing['state']}）"}

    # 解析 write_scope
    write_scope = []
    if args.write_scope:
        write_scope = [s.strip() for s in args.write_scope.split(",")]

    # 解析 tool_blacklist
    tool_blacklist = ["v8_handshake", "v8_end", "xirang-spawn.py"]
    if args.tool_blacklist:
        tool_blacklist = [t.strip() for t in args.tool_blacklist.split(",")]

    ts = now_iso()
    record = {
        "task_id": args.task_id,
        "sub_id": args.sub_id,
        "parent_agent": args.parent or "unknown",
        "parent_task_id": args.task_id,
        "spawn_ts": ts,
        "state": "CREATED",
        "model": args.model or "sonnet",
        "task_type": args.type or "research",
        "task_name": args.name or args.sub_id,
        "budget": {
            "max_tokens": args.max_tokens or 50000,
            "timeout_sec": args.timeout or 300,
            "cost_ceiling_cny": args.cost_ceiling or 1.0,
        },
        "write_scope": write_scope,
        "tool_blacklist": tool_blacklist,
        "messages": [
            {"role": "spawn", "ts": ts, "content": f"Spawned by {args.parent or 'unknown'}"}
        ],
        "artifacts": [],
        "cost": {"tokens": 0, "cost_cny": 0.0, "model": args.model or "sonnet"},
        "transitions": [
            {"from": "_", "to": "CREATED", "ts": ts, "reason": "spawn"}
        ],
        "error": None,
        "collected_at": None,
        "destroyed_at": None,
    }

    path = save_record(record)
    return {"status": "ok", "sub_id": args.sub_id, "path": str(path.relative_to(VAULT_ROOT)), "state": "CREATED"}


def cmd_transition(args) -> dict:
    """推进子任务状态"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    current_state = record["state"]
    target_state = args.to

    if target_state not in VALID_TRANSITIONS.get(current_state, set()):
        allowed = sorted(VALID_TRANSITIONS.get(current_state, set()))
        return {
            "status": "error", "code": 2,
            "message": f"非法转换: {current_state} -> {target_state}",
            "allowed": allowed,
        }

    ts = now_iso()
    record["state"] = target_state
    record["transitions"].append({
        "from": current_state,
        "to": target_state,
        "ts": ts,
        "reason": args.reason or "",
    })

    save_record(record)
    return {"status": "ok", "sub_id": args.sub_id, "from": current_state, "to": target_state}


def cmd_heartbeat(args) -> dict:
    """记录心跳"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    if record["state"] not in ("RUNNING", "RETRYING"):
        return {"status": "error", "code": 2, "message": f"非运行态不可心跳: state={record['state']}"}

    ts = now_iso()
    msg = {"role": "heartbeat", "ts": ts}
    if args.progress:
        msg["progress"] = args.progress
    if args.pct is not None:
        msg["pct"] = args.pct

    record["messages"].append(msg)
    save_record(record)
    return {"status": "ok", "sub_id": args.sub_id, "heartbeat_count": sum(1 for m in record["messages"] if m["role"] == "heartbeat")}


def cmd_artifact(args) -> dict:
    """记录产物"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    artifact = {"path": args.path, "type": args.type or "unknown"}
    if args.size:
        artifact["size_bytes"] = args.size

    record["artifacts"].append(artifact)
    save_record(record)
    return {"status": "ok", "sub_id": args.sub_id, "artifact_count": len(record["artifacts"])}


def cmd_collect(args) -> dict:
    """收集子任务结果（父 Agent 调用）"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    # 只有 SUCCESS/RECLAIMED/ESCALATED 可以被 collect
    if record["state"] not in ("SUCCESS", "RECLAIMED", "ESCALATED"):
        return {
            "status": "error", "code": 2,
            "message": f"当前状态 {record['state']} 不可收集（需先到 SUCCESS/RECLAIMED/ESCALATED）",
        }

    ts = now_iso()
    record["state"] = "COLLECTED"
    record["collected_at"] = ts
    record["transitions"].append({
        "from": record["transitions"][-1]["to"] if record["transitions"] else "?",
        "to": "COLLECTED",
        "ts": ts,
        "reason": "parent collected",
    })

    # 更新成本
    if args.tokens:
        record["cost"]["tokens"] = args.tokens
    if args.cost:
        record["cost"]["cost_cny"] = args.cost

    # 记录结果消息
    result_msg = {"role": "result", "ts": ts, "content": args.summary or "collected"}
    record["messages"].append(result_msg)

    save_record(record)
    return {
        "status": "ok", "sub_id": args.sub_id, "state": "COLLECTED",
        "artifacts": len(record["artifacts"]),
        "cost_tokens": record["cost"]["tokens"],
    }


def cmd_destroy(args) -> dict:
    """销毁子任务记录"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    # COLLECTED 或 CREATED（取消）可以 destroy
    if record["state"] not in ("COLLECTED", "CREATED", "DESTROYED"):
        if not args.force:
            return {
                "status": "error", "code": 2,
                "message": f"状态 {record['state']} 不可直接销毁（使用 --force 强制）",
            }

    ts = now_iso()
    record["state"] = "DESTROYED"
    record["destroyed_at"] = ts
    record["transitions"].append({
        "from": record["transitions"][-1]["to"] if record["transitions"] else "?",
        "to": "DESTROYED",
        "ts": ts,
        "reason": "destroy" + (" (forced)" if args.force else ""),
    })

    save_record(record)

    # 可选：删除文件（默认保留供审计）
    if args.purge:
        path = get_record_path(args.task_id, args.sub_id)
        path.unlink(missing_ok=True)
        return {"status": "ok", "sub_id": args.sub_id, "action": "purged"}

    return {"status": "ok", "sub_id": args.sub_id, "state": "DESTROYED"}


def cmd_error(args) -> dict:
    """记录错误信息"""
    record = load_record(args.task_id, args.sub_id)
    if not record:
        return {"status": "error", "code": 1, "message": f"记录不存在: {args.sub_id}"}

    record["error"] = {
        "code": args.error_code or "E-UNKNOWN",
        "message": args.error_msg or "",
        "retry_count": (record["error"]["retry_count"] + 1) if record.get("error") and record["error"] else 0,
    }

    # 记录错误消息
    record["messages"].append({
        "role": "error",
        "ts": now_iso(),
        "content": f"[{args.error_code}] {args.error_msg or ''}",
    })

    save_record(record)
    return {"status": "ok", "sub_id": args.sub_id, "error_code": args.error_code, "retry_count": record["error"]["retry_count"]}


def main():
    parser = argparse.ArgumentParser(description="V8.5 子任务运行时记录管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="创建子任务记录")
    p_create.add_argument("--task-id", required=True, help="父任务 ID")
    p_create.add_argument("--sub-id", required=True, help="子任务 ID（如 sub-01）")
    p_create.add_argument("--parent", help="父 Agent ID")
    p_create.add_argument("--model", help="使用模型")
    p_create.add_argument("--type", help="任务类型")
    p_create.add_argument("--name", help="任务名称")
    p_create.add_argument("--write-scope", help="写入范围（逗号分隔）")
    p_create.add_argument("--tool-blacklist", help="工具黑名单（逗号分隔）")
    p_create.add_argument("--timeout", type=int, help="超时秒数")
    p_create.add_argument("--max-tokens", type=int, help="token 上限")
    p_create.add_argument("--cost-ceiling", type=float, help="成本上限(CNY)")
    p_create.add_argument("--json", action="store_true", default=True)

    # transition
    p_trans = subparsers.add_parser("transition", help="推进状态")
    p_trans.add_argument("--task-id", required=True)
    p_trans.add_argument("--sub-id", required=True)
    p_trans.add_argument("--to", required=True, help="目标状态")
    p_trans.add_argument("--reason", help="转换原因")
    p_trans.add_argument("--json", action="store_true", default=True)

    # heartbeat
    p_hb = subparsers.add_parser("heartbeat", help="记录心跳")
    p_hb.add_argument("--task-id", required=True)
    p_hb.add_argument("--sub-id", required=True)
    p_hb.add_argument("--progress", help="进度描述")
    p_hb.add_argument("--pct", type=float, help="完成百分比 0-1")
    p_hb.add_argument("--json", action="store_true", default=True)

    # artifact
    p_art = subparsers.add_parser("artifact", help="记录产物")
    p_art.add_argument("--task-id", required=True)
    p_art.add_argument("--sub-id", required=True)
    p_art.add_argument("--path", required=True, help="产物路径")
    p_art.add_argument("--type", help="产物类型（html/md/json/...）")
    p_art.add_argument("--size", type=int, help="文件大小(bytes)")
    p_art.add_argument("--json", action="store_true", default=True)

    # collect
    p_col = subparsers.add_parser("collect", help="收集子任务结果")
    p_col.add_argument("--task-id", required=True)
    p_col.add_argument("--sub-id", required=True)
    p_col.add_argument("--tokens", type=int, help="实际消耗 token")
    p_col.add_argument("--cost", type=float, help="实际消耗成本(CNY)")
    p_col.add_argument("--summary", help="结果摘要")
    p_col.add_argument("--json", action="store_true", default=True)

    # destroy
    p_del = subparsers.add_parser("destroy", help="销毁子任务记录")
    p_del.add_argument("--task-id", required=True)
    p_del.add_argument("--sub-id", required=True)
    p_del.add_argument("--force", action="store_true", help="强制销毁（跳过状态检查）")
    p_del.add_argument("--purge", action="store_true", help="同时删除 JSON 文件")
    p_del.add_argument("--json", action="store_true", default=True)

    # error
    p_err = subparsers.add_parser("error", help="记录错误")
    p_err.add_argument("--task-id", required=True)
    p_err.add_argument("--sub-id", required=True)
    p_err.add_argument("--error-code", help="错误代码（如 E-TIMEOUT）")
    p_err.add_argument("--error-msg", help="错误描述")
    p_err.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args()

    # 路由到对应命令
    handlers = {
        "create": cmd_create,
        "transition": cmd_transition,
        "heartbeat": cmd_heartbeat,
        "artifact": cmd_artifact,
        "collect": cmd_collect,
        "destroy": cmd_destroy,
        "error": cmd_error,
    }

    result = handlers[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码映射
    if result.get("status") == "error":
        sys.exit(result.get("code", 1))
    sys.exit(0)


if __name__ == "__main__":
    main()
