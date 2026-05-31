#!/usr/bin/env python3
"""
xirang-a2a-server.py — 息壤 V9 跨机器协议 Mock Server
v0.1 Spike · 2026-05-17

最小可行实现：验证 Agent Card 注册/发现 + 任务流转 + 心跳 SSE + Token 认证。
不依赖第三方库（纯标准库 http.server + threading）。

用法：
  # 启动协调节点（小虫角色）
  python3 .standards/xirang-a2a-server.py --port 8900 --role coordinator

  # 启动工作节点（东风角色）
  python3 .standards/xirang-a2a-server.py --port 8910 --role worker --agent-id dongfeng

  # 用 client 测试
  python3 .standards/xirang-a2a-client.py --register
  python3 .standards/xirang-a2a-client.py --create-task "批量操作"
  python3 .standards/xirang-a2a-client.py --status
"""

import sys
import json
import time
import hashlib
import secrets
import threading
import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# === 全局存储（内存模式，spike 不持久化） ===

REGISTRY = {}       # agent_id -> Agent Card
TASKS = {}          # task_id -> Task Object
EVENTS = []         # 事件流
TOKENS = {}         # agent_id -> token_hash
SERVER_TOKEN = None  # 本服务器的认证 token


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def gen_task_id():
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"T-{ts}-{secrets.token_hex(3)}"


def verify_token(headers, required_agent=None):
    """验证 Bearer Token"""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False, "缺少 Authorization header"
    token = auth[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # 检查是否为已注册 agent 的 token
    for agent_id, stored_hash in TOKENS.items():
        if stored_hash == token_hash:
            if required_agent and agent_id != required_agent:
                return False, f"Token 属于 {agent_id}，不是 {required_agent}"
            return True, agent_id
    return False, "无效 Token"


# === V8 10态状态机（复用） ===

VALID_TRANSITIONS = {
    "CREATED":         ["RUNNING", "DESTROYED"],
    "RUNNING":         ["SUCCESS", "FAILED", "TIMEOUT", "PARTIAL"],
    "PARTIAL":         ["RUNNING"],
    "SUCCESS":         ["COLLECTED"],
    "FAILED":          ["RETRYING", "RECLAIMED"],
    "TIMEOUT":         ["RETRYING", "RECLAIMED"],
    "RETRYING":        ["SUCCESS", "RETRY_EXHAUSTED"],
    "RETRY_EXHAUSTED": ["RECLAIMED", "ESCALATED"],
    "RECLAIMED":       ["COLLECTED"],
    "ESCALATED":       ["COLLECTED"],
    "COLLECTED":       ["DESTROYED"],
    "DESTROYED":       [],
}


class CoordinatorHandler(BaseHTTPRequestHandler):
    """协调节点 HTTP Handler"""

    def log_message(self, format, *args):
        """简化日志"""
        sys.stderr.write(f"[{now_iso()}] {args[0]}\n")

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def _error(self, code, msg):
        self._json_response(code, {"error": msg, "code": code})

    # --- GET ---

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/registry":
            self._json_response(200, {
                "agents": list(REGISTRY.values()),
                "count": len(REGISTRY),
            })

        elif path == "/tasks":
            active = {k: v for k, v in TASKS.items() if v["state"] not in ("DESTROYED",)}
            self._json_response(200, {
                "tasks": list(active.values()),
                "count": len(active),
            })

        elif path.startswith("/tasks/") and not path.endswith("/stream"):
            task_id = path.split("/tasks/")[1]
            if task_id in TASKS:
                self._json_response(200, TASKS[task_id])
            else:
                self._error(404, f"任务不存在: {task_id}")

        elif path.startswith("/tasks/") and path.endswith("/stream"):
            # SSE 流式心跳（简化版）
            task_id = path.split("/tasks/")[1].replace("/stream", "")
            if task_id not in TASKS:
                self._error(404, f"任务不存在: {task_id}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # 发送当前状态
            data = json.dumps(TASKS[task_id], ensure_ascii=False)
            self.wfile.write(f"event: status\ndata: {data}\n\n".encode())
            self.wfile.flush()

        elif path == "/events":
            self._json_response(200, {"events": EVENTS[-50:], "total": len(EVENTS)})

        elif path == "/health":
            self._json_response(200, {
                "status": "running",
                "role": "coordinator",
                "agents": len(REGISTRY),
                "active_tasks": len([t for t in TASKS.values() if t["state"] not in ("DESTROYED",)]),
                "uptime_sec": int(time.time() - SERVER_START),
            })

        else:
            self._error(404, f"未知路径: {path}")

    # --- POST ---

    def do_POST(self):
        path = urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}

        if path == "/registry":
            # 注册 Agent
            agent_id = body.get("agent_id")
            if not agent_id:
                self._error(400, "缺少 agent_id")
                return

            # 生成 token
            token = secrets.token_hex(16)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            TOKENS[agent_id] = token_hash

            # 存储 Agent Card
            card = {
                "agent_id": agent_id,
                "agent_name": body.get("agent_name", agent_id),
                "platform": body.get("platform", "unknown"),
                "role": body.get("role", ""),
                "endpoint": body.get("endpoint", ""),
                "capabilities": body.get("capabilities", []),
                "constraints": body.get("constraints", {}),
                "status": {"current": "idle", "current_task": None, "last_heartbeat": now_iso()},
                "registered_at": now_iso(),
            }
            REGISTRY[agent_id] = card

            EVENTS.append({
                "type": "agent_register",
                "agent": agent_id,
                "ts": now_iso(),
            })

            self._json_response(201, {
                "message": f"Agent '{agent_id}' 注册成功",
                "token": token,
                "agent_card": card,
            })

        elif path == "/tasks":
            # 创建任务
            task_id = gen_task_id()
            task = {
                "task_id": task_id,
                "parent_agent": body.get("parent_agent", "coordinator"),
                "target_agent": body.get("target_agent"),
                "type": body.get("type", "general"),
                "priority": body.get("priority", "P2"),
                "state": "CREATED",
                "created_at": now_iso(),
                "timeout_sec": body.get("timeout_sec", 300),
                "budget_level": body.get("budget_level", "M"),
                "payload": body.get("payload", {}),
                "result": None,
                "history": [{"state": "CREATED", "at": now_iso()}],
            }
            TASKS[task_id] = task

            # 更新目标 Agent 状态
            target = body.get("target_agent")
            if target and target in REGISTRY:
                REGISTRY[target]["status"]["current"] = "busy"
                REGISTRY[target]["status"]["current_task"] = task_id

            EVENTS.append({
                "type": "task_create",
                "task_id": task_id,
                "agent": body.get("parent_agent", "coordinator"),
                "target": target,
                "ts": now_iso(),
            })

            self._json_response(201, {"task_id": task_id, "task": task})

        elif path.startswith("/tasks/") and path.endswith("/transition"):
            # 状态转移
            task_id = path.split("/tasks/")[1].replace("/transition", "")
            if task_id not in TASKS:
                self._error(404, f"任务不存在: {task_id}")
                return

            task = TASKS[task_id]
            new_state = body.get("state")
            if not new_state:
                self._error(400, "缺少 state 字段")
                return

            # 验证状态转移合法性
            current = task["state"]
            if new_state not in VALID_TRANSITIONS.get(current, []):
                self._error(400, f"非法转移: {current} -> {new_state}。允许: {VALID_TRANSITIONS[current]}")
                return

            task["state"] = new_state
            task["history"].append({"state": new_state, "at": now_iso()})

            if body.get("result"):
                task["result"] = body["result"]
            if body.get("error"):
                task["error"] = body["error"]

            # 更新 Agent 状态
            target = task.get("target_agent")
            if target and target in REGISTRY:
                if new_state in ("COLLECTED", "DESTROYED"):
                    REGISTRY[target]["status"]["current"] = "idle"
                    REGISTRY[target]["status"]["current_task"] = None

            EVENTS.append({
                "type": "task_transition",
                "task_id": task_id,
                "from": current,
                "to": new_state,
                "ts": now_iso(),
            })

            self._json_response(200, {"task_id": task_id, "state": new_state, "valid": True})

        elif path == "/heartbeat":
            # 心跳上报
            agent_id = body.get("agent_id")
            if agent_id and agent_id in REGISTRY:
                REGISTRY[agent_id]["status"]["last_heartbeat"] = now_iso()
                task_id = body.get("task_id")
                if task_id and task_id in TASKS:
                    TASKS[task_id].setdefault("heartbeats", []).append({
                        "at": now_iso(),
                        "progress": body.get("progress", ""),
                        "pct": body.get("pct"),
                    })
            self._json_response(200, {"ack": True})

        else:
            self._error(404, f"未知路径: {path}")

    # --- DELETE ---

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/registry/"):
            agent_id = path.split("/registry/")[1]
            if agent_id in REGISTRY:
                del REGISTRY[agent_id]
                TOKENS.pop(agent_id, None)
                EVENTS.append({"type": "agent_deregister", "agent": agent_id, "ts": now_iso()})
                self._json_response(200, {"message": f"Agent '{agent_id}' 已注销"})
            else:
                self._error(404, f"Agent 不存在: {agent_id}")

        elif path.startswith("/tasks/"):
            task_id = path.split("/tasks/")[1]
            if task_id in TASKS:
                TASKS[task_id]["state"] = "DESTROYED"
                self._json_response(200, {"message": f"任务 '{task_id}' 已销毁"})
            else:
                self._error(404, f"任务不存在: {task_id}")

        else:
            self._error(404, f"未知路径: {path}")


SERVER_START = time.time()


def main():
    port = 8900
    role = "coordinator"

    # 简单参数解析
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        elif arg == "--role" and i + 1 < len(sys.argv):
            role = sys.argv[i + 1]
        elif arg == "--help" or arg == "-h":
            print("""xirang-a2a-server.py -- 息壤 V9 跨机器协议 Mock Server

用法:
  python3 .standards/xirang-a2a-server.py [选项]

选项:
  --port PORT    监听端口（默认 8900）
  --role ROLE    角色：coordinator / worker（默认 coordinator）
  --help         显示帮助

API 端点:
  GET  /registry              所有已注册 Agent
  POST /registry              注册新 Agent
  GET  /tasks                 所有活跃任务
  POST /tasks                 创建任务
  POST /tasks/{id}/transition 推进状态
  GET  /tasks/{id}/stream     SSE 心跳流
  POST /heartbeat             心跳上报
  GET  /events                事件流
  GET  /health                健康检查
""")
            return

    server = HTTPServer(("0.0.0.0", port), CoordinatorHandler)
    print(f"[息壤 V9 A2A Mock Server]")
    print(f"  角色: {role}")
    print(f"  端口: {port}")
    print(f"  地址: http://localhost:{port}")
    print(f"  健康检查: http://localhost:{port}/health")
    print(f"  按 Ctrl+C 停止")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止]")
        server.shutdown()


if __name__ == "__main__":
    main()
