#!/usr/bin/env python3
"""
msg-queue.py -- V8.5 Agent 本地消息队列
v1.0.0 | 2026-05-24 | 息壤 V8.5.0

本地文件驱动的消息队列，Agent 间结构化通知的唯一载体。
不做分布式 broker——单机 JSONL + 文件锁即够用。

用法:
  python3 .standards/msg-queue.py enqueue --from claudian --to xiaochong --type cost_alert --priority P1 --payload '{"task_id":"T-xxx"}' [--ref-task T-xxx] [--expires 86400] [--json]
  python3 .standards/msg-queue.py peek --agent xiaochong [--type cost_alert] [--json]
  python3 .standards/msg-queue.py dequeue --agent xiaochong [--json]
  python3 .standards/msg-queue.py ack --msg-id msg-xxx [--json]
  python3 .standards/msg-queue.py list [--agent X] [--type Y] [--status pending] [--json]
  python3 .standards/msg-queue.py expire [--max-age 86400] [--json]

退出码:
  0 = 成功（有结果）
  1 = 无消息（dequeue/peek 为空）
  2 = 参数错误

存储: _temp/msg-queue.jsonl
旋转: >5MB 自动 rename 归档

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/msg-queue.py peek --agent xiaochong --json"
"""

import sys
import os
import json
import fcntl
import time
import random
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
QUEUE_FILE = VAULT_ROOT / "_temp" / "msg-queue.jsonl"
LOCK_FILE = VAULT_ROOT / "_temp" / "msg-queue.lock"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

TZ = timezone(timedelta(hours=8))

VALID_TYPES = {"cost_alert", "escalation", "handoff", "spawn_request", "result_ready", "system"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_STATUSES = {"pending", "read", "acked", "expired"}


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def gen_msg_id() -> str:
    """生成消息ID: msg-{unix_ms}-{4位hex}"""
    ts_ms = int(time.time() * 1000)
    rand_hex = format(random.randint(0, 0xFFFF), "04x")
    return f"msg-{ts_ms}-{rand_hex}"


def ensure_queue_dir():
    """确保 _temp/ 目录存在"""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)


def acquire_lock():
    """获取队列全局写锁，返回 lock fd（调用方负责释放）"""
    ensure_queue_dir()
    lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def release_lock(lock_fd: int):
    """释放队列全局写锁"""
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


def rotate_if_needed():
    """文件超 5MB 时归档（调用方应已持锁）"""
    if QUEUE_FILE.exists() and QUEUE_FILE.stat().st_size > MAX_FILE_SIZE:
        ts = int(time.time())
        archive = QUEUE_FILE.with_name(f"msg-queue.{ts}.jsonl")
        QUEUE_FILE.rename(archive)


def read_all_messages() -> list[dict]:
    """读取所有消息（不加锁，允许脏读——仅用于只读查询）"""
    if not QUEUE_FILE.exists():
        return []
    messages = []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass
    return messages


def read_all_messages_locked(lock_fd: int) -> list[dict]:
    """在已持锁状态下读取所有消息（用于 read-modify-write 操作）"""
    return read_all_messages()


def write_all_messages(messages: list[dict]):
    """原子写入所有消息（temp + os.replace）——调用方必须已持锁"""
    ensure_queue_dir()
    tmp_file = QUEUE_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    os.replace(str(tmp_file), str(QUEUE_FILE))


def append_message(msg: dict):
    """追加单条消息（自动加锁 + 释放）"""
    lock_fd = acquire_lock()
    try:
        rotate_if_needed()
        with open(QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    finally:
        release_lock(lock_fd)


def filter_for_agent(messages: list[dict], agent: str) -> list[dict]:
    """筛选某 agent 可见的消息（直接发给它的 + broadcast）"""
    return [m for m in messages if m["to"] == agent or m["to"] == "broadcast"]


# === 命令实现 ===

def cmd_enqueue(args):
    """发送消息"""
    # 校验
    if args.type not in VALID_TYPES:
        print(json.dumps({"error": f"type 必须是: {sorted(VALID_TYPES)}"}))
        sys.exit(2)
    if args.priority not in VALID_PRIORITIES:
        print(json.dumps({"error": f"priority 必须是: {sorted(VALID_PRIORITIES)}"}))
        sys.exit(2)

    # 解析 payload
    try:
        payload = json.loads(args.payload) if args.payload else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"payload JSON 解析失败: {e}"}))
        sys.exit(2)

    # 计算过期时间
    expires_at = None
    if args.expires:
        expires_dt = datetime.now(TZ) + timedelta(seconds=args.expires)
        expires_at = expires_dt.replace(microsecond=0).isoformat()

    msg = {
        "msg_id": gen_msg_id(),
        "ts": now_iso(),
        "from": getattr(args, "from"),
        "to": args.to,
        "type": args.type,
        "priority": args.priority,
        "payload": payload,
        "status": "pending",
        "expires_at": expires_at,
        "ref_task_id": args.ref_task or None,
    }

    append_message(msg)

    result = {"status": "ok", "msg_id": msg["msg_id"], "to": msg["to"], "type": msg["type"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


def cmd_peek(args):
    """查看待处理消息（不消费）"""
    messages = read_all_messages()
    visible = filter_for_agent(messages, args.agent)

    # 过滤状态和类型
    filtered = [m for m in visible if m["status"] == "pending"]
    if args.type:
        filtered = [m for m in filtered if m["type"] == args.type]

    # 按优先级排序 P0 > P1 > P2 > P3
    filtered.sort(key=lambda m: m["priority"])

    result = {"count": len(filtered), "messages": filtered}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if filtered else 1)


def cmd_dequeue(args):
    """消费消息（标记 read）——全局锁保护 read-modify-write"""
    lock_fd = acquire_lock()
    try:
        messages = read_all_messages_locked(lock_fd)
        consumed = []

        for msg in messages:
            if msg["to"] == "broadcast":
                # broadcast 不变更状态，作为可见消息返回
                if msg["status"] == "pending":
                    consumed.append(msg)
                continue
            if msg["to"] == args.agent and msg["status"] == "pending":
                msg["status"] = "read"
                consumed.append(msg)

        if consumed:
            write_all_messages(messages)
    finally:
        release_lock(lock_fd)

    # 按优先级排序
    consumed.sort(key=lambda m: m["priority"])

    result = {"count": len(consumed), "messages": consumed}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if consumed else 1)


def cmd_ack(args):
    """确认消息已处理——全局锁保护 read-modify-write"""
    lock_fd = acquire_lock()
    error = None
    exit_code = 0
    try:
        messages = read_all_messages_locked(lock_fd)
        found = False

        for msg in messages:
            if msg["msg_id"] == args.msg_id:
                if msg["status"] in ("pending", "read"):
                    msg["status"] = "acked"
                    found = True
                else:
                    error = {"error": f"消息状态为 {msg['status']}，无法 ack"}
                    exit_code = 2
                break

        if error:
            pass
        elif not found:
            error = {"error": f"消息不存在: {args.msg_id}"}
            exit_code = 1
        else:
            write_all_messages(messages)
    finally:
        release_lock(lock_fd)

    if error:
        print(json.dumps(error))
        sys.exit(exit_code)

    print(json.dumps({"status": "ok", "msg_id": args.msg_id, "new_status": "acked"}))
    sys.exit(0)


def cmd_list(args):
    """列出消息（支持过滤）"""
    messages = read_all_messages()

    filtered = messages
    if args.agent:
        filtered = filter_for_agent(filtered, args.agent)
    if args.type:
        filtered = [m for m in filtered if m["type"] == args.type]
    if args.status:
        filtered = [m for m in filtered if m["status"] == args.status]

    result = {"count": len(filtered), "messages": filtered}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


def cmd_expire(args):
    """清理过期/已确认消息——全局锁保护 read-modify-write"""
    lock_fd = acquire_lock()
    try:
        messages = read_all_messages_locked(lock_fd)
        now = datetime.now(TZ)
        max_age = args.max_age  # 秒

        kept = []
        expired_count = 0

        for msg in messages:
            should_expire = False

            # 按 expires_at 过期
            if msg.get("expires_at"):
                try:
                    exp_dt = datetime.fromisoformat(msg["expires_at"])
                    if now >= exp_dt:
                        should_expire = True
                except (ValueError, TypeError):
                    pass

            # 按 max_age 过期（基于 ts）
            if max_age is not None and not should_expire:
                try:
                    msg_dt = datetime.fromisoformat(msg["ts"])
                    age_sec = (now - msg_dt).total_seconds()
                    if age_sec > max_age and msg["status"] in ("acked", "expired"):
                        should_expire = True
                except (ValueError, TypeError):
                    pass

            if should_expire:
                expired_count += 1
            else:
                kept.append(msg)

        if expired_count > 0:
            write_all_messages(kept)
    finally:
        release_lock(lock_fd)

    result = {
        "status": "ok",
        "expired_count": expired_count,
        "remaining_count": len(kept),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="V8.5 Agent Message Queue")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # enqueue
    p_enq = subparsers.add_parser("enqueue", help="发送消息")
    p_enq.add_argument("--from", required=True, dest="from", help="发送方 agent_id")
    p_enq.add_argument("--to", required=True, help="接收方 agent_id 或 broadcast")
    p_enq.add_argument("--type", required=True, help="消息类型")
    p_enq.add_argument("--priority", required=True, help="优先级 P0-P3")
    p_enq.add_argument("--payload", default="{}", help="JSON payload")
    p_enq.add_argument("--ref-task", help="关联任务ID")
    p_enq.add_argument("--expires", type=int, help="过期秒数（从现在算）")
    p_enq.add_argument("--json", action="store_true", default=True)

    # peek
    p_peek = subparsers.add_parser("peek", help="查看待处理消息（不消费）")
    p_peek.add_argument("--agent", required=True, help="查看哪个 agent 的消息")
    p_peek.add_argument("--type", help="过滤消息类型")
    p_peek.add_argument("--json", action="store_true", default=True)

    # dequeue
    p_deq = subparsers.add_parser("dequeue", help="消费消息（标记 read）")
    p_deq.add_argument("--agent", required=True, help="消费哪个 agent 的消息")
    p_deq.add_argument("--json", action="store_true", default=True)

    # ack
    p_ack = subparsers.add_parser("ack", help="确认消息已处理")
    p_ack.add_argument("--msg-id", required=True, help="消息ID")
    p_ack.add_argument("--json", action="store_true", default=True)

    # list
    p_list = subparsers.add_parser("list", help="列出消息")
    p_list.add_argument("--agent", help="过滤 agent")
    p_list.add_argument("--type", help="过滤类型")
    p_list.add_argument("--status", help="过滤状态")
    p_list.add_argument("--json", action="store_true", default=True)

    # expire
    p_exp = subparsers.add_parser("expire", help="清理过期消息")
    p_exp.add_argument("--max-age", type=int, default=86400, help="最大保留秒数（默认24h）")
    p_exp.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args()

    commands = {
        "enqueue": cmd_enqueue,
        "peek": cmd_peek,
        "dequeue": cmd_dequeue,
        "ack": cmd_ack,
        "list": cmd_list,
        "expire": cmd_expire,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
