#!/usr/bin/env python3
"""
xirang-a2a-client.py — 息壤 V9 跨机器协议 Mock Client
v0.1 Spike · 2026-05-17

用于测试 xirang-a2a-server.py 的客户端工具。

用法：
  # 注册 Agent
  python3 .standards/xirang-a2a-client.py --register --agent-id assistant --role "技术执行"

  # 查看 Registry
  python3 .standards/xirang-a2a-client.py --registry

  # 创建任务
  python3 .standards/xirang-a2a-client.py --create-task --target assistant --desc "批量重绘流程图"

  # 推进任务状态
  python3 .standards/xirang-a2a-client.py --advance T-20260517-001 RUNNING

  # 发送心跳
  python3 .standards/xirang-a2a-client.py --heartbeat assistant --task T-20260517-001 --progress "完成3/8"

  # 健康检查
  python3 .standards/xirang-a2a-client.py --health

  # 运行完整 Demo（注册 + 任务创建 + 状态流转 + 心跳 + 完成）
  python3 .standards/xirang-a2a-client.py --demo
"""

import sys
import json
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8900"


def api_call(method, path, data=None, token=None):
    """发起 HTTP API 调用"""
    url = f"{BASE_URL}{path}"
    body = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        return e.code, body
    except urllib.error.URLError as e:
        return 0, {"error": f"连接失败: {e.reason}。请确认 server 已启动 (python3 .standards/xirang-a2a-server.py)"}


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_register(agent_id, agent_name=None, role="", platform="codebuddy", endpoint=""):
    """注册 Agent"""
    data = {
        "agent_id": agent_id,
        "agent_name": agent_name or agent_id,
        "platform": platform,
        "role": role,
        "endpoint": endpoint,
        "capabilities": [],
        "constraints": {"max_concurrent_subs": 6},
    }
    code, resp = api_call("POST", "/registry", data)
    if code == 201:
        print(f"[OK] Agent '{agent_id}' 注册成功")
        print(f"     Token: {resp.get('token', '???')}")
        print(f"     保存此 token 用于后续认证")
    else:
        print(f"[ERROR] {code}: {resp}")
    return resp


def cmd_registry():
    """查看注册表"""
    code, resp = api_call("GET", "/registry")
    if code == 200:
        print(f"已注册 Agent: {resp['count']} 个")
        print("-" * 60)
        for agent in resp["agents"]:
            status = agent.get("status", {}).get("current", "unknown")
            print(f"  {agent['agent_id']:<12} {agent.get('role', ''):<25} [{status}]")
    else:
        print(f"[ERROR] {code}: {resp}")


def cmd_create_task(target, desc, task_type="batch", priority="P1", timeout=300):
    """创建任务"""
    data = {
        "parent_agent": "xiaochong",
        "target_agent": target,
        "type": task_type,
        "priority": priority,
        "timeout_sec": timeout,
        "budget_level": "M",
        "payload": {"description": desc},
    }
    code, resp = api_call("POST", "/tasks", data)
    if code == 201:
        task_id = resp["task_id"]
        print(f"[OK] 任务创建成功: {task_id}")
        print(f"     目标: {target}")
        print(f"     描述: {desc}")
        return task_id
    else:
        print(f"[ERROR] {code}: {resp}")
        return None


def cmd_advance(task_id, state, result=None, error=None):
    """推进任务状态"""
    data = {"state": state}
    if result:
        data["result"] = result
    if error:
        data["error"] = error
    code, resp = api_call("POST", f"/tasks/{task_id}/transition", data)
    if code == 200:
        print(f"[OK] {task_id}: -> {state}")
    else:
        print(f"[REJECT] {code}: {resp.get('error', resp)}")


def cmd_heartbeat(agent_id, task_id=None, progress="", pct=None):
    """发送心跳"""
    data = {"agent_id": agent_id, "progress": progress}
    if task_id:
        data["task_id"] = task_id
    if pct is not None:
        data["pct"] = pct
    code, resp = api_call("POST", "/heartbeat", data)
    if code == 200:
        print(f"[OK] 心跳已发送: {agent_id}")
    else:
        print(f"[ERROR] {code}: {resp}")


def cmd_tasks():
    """列出活跃任务"""
    code, resp = api_call("GET", "/tasks")
    if code == 200:
        print(f"活跃任务: {resp['count']} 个")
        print("-" * 60)
        for task in resp["tasks"]:
            print(f"  {task['task_id']:<24} [{task['state']:<10}] -> {task.get('target_agent', '?')}")
            if task.get("payload", {}).get("description"):
                print(f"    {task['payload']['description']}")
    else:
        print(f"[ERROR] {code}: {resp}")


def cmd_health():
    """健康检查"""
    code, resp = api_call("GET", "/health")
    if code == 200:
        print(f"[OK] 服务健康")
        print(f"     角色: {resp.get('role')}")
        print(f"     Agent数: {resp.get('agents')}")
        print(f"     活跃任务: {resp.get('active_tasks')}")
        print(f"     运行时间: {resp.get('uptime_sec')}s")
    else:
        print(f"[ERROR] 服务不可达: {resp}")


def cmd_events():
    """查看事件流"""
    code, resp = api_call("GET", "/events")
    if code == 200:
        print(f"事件总数: {resp['total']}（显示最近 50 条）")
        print("-" * 60)
        for ev in resp["events"]:
            print(f"  [{ev.get('ts', '?')}] {ev.get('type')}: {ev.get('agent', '')} {ev.get('task_id', '')}")
    else:
        print(f"[ERROR] {code}: {resp}")


def cmd_demo():
    """运行完整 Demo"""
    print("=" * 60)
    print("  息壤 V9 跨机器协议 Demo")
    print("=" * 60)
    print()

    # 1. 注册 Agent
    print("--- [1] 注册 Agent ---")
    r1 = cmd_register("xiaochong", "小虫", "协调中枢", "openclaw", "http://localhost:8900")
    r2 = cmd_register("assistant", "Claudian", "技术执行", "codebuddy", "http://localhost:8910")
    r3 = cmd_register("toubao", "头孢", "资料采集", "hermes", "http://localhost:8912")
    print()

    # 2. 查看注册表
    print("--- [2] 查看 Registry ---")
    cmd_registry()
    print()

    # 3. 创建任务
    print("--- [3] 创建任务 ---")
    task_id = cmd_create_task("assistant", "批量重绘 16 个 drawio 白底背景")
    if not task_id:
        print("[ABORT] 任务创建失败")
        return
    print()

    # 4. 状态流转
    print("--- [4] 状态流转 ---")
    cmd_advance(task_id, "RUNNING")

    # 5. 心跳
    print("--- [5] 心跳上报 ---")
    cmd_heartbeat("assistant", task_id, "完成 4/16 个文件", 0.25)
    cmd_heartbeat("assistant", task_id, "完成 12/16 个文件", 0.75)
    print()

    # 6. 完成
    print("--- [6] 任务完成 ---")
    cmd_advance(task_id, "SUCCESS", result={"files_modified": 16, "path": "10-项目/基线/"})
    cmd_advance(task_id, "COLLECTED")
    cmd_advance(task_id, "DESTROYED")
    print()

    # 7. 最终状态
    print("--- [7] 最终状态 ---")
    cmd_registry()
    print()
    cmd_events()

    print()
    print("=" * 60)
    print("  Demo 完成 - V9 协议验证通过")
    print("=" * 60)


def main():
    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print("""xirang-a2a-client.py -- 息壤 V9 跨机器协议 Client

用法:
  python3 .standards/xirang-a2a-client.py [命令] [参数]

命令:
  --register --agent-id ID [--role R] [--platform P]   注册 Agent
  --registry                                           查看注册表
  --create-task --target ID --desc "描述"              创建任务
  --advance TASK_ID STATE                              推进任务状态
  --heartbeat AGENT_ID [--task TASK_ID] [--progress P] 发送心跳
  --tasks                                              列出活跃任务
  --events                                             查看事件流
  --health                                             健康检查
  --demo                                               运行完整 Demo

环境:
  需要先启动 server: python3 .standards/xirang-a2a-server.py
""")
        return

    # 全局参数
    for i, arg in enumerate(sys.argv):
        if arg == "--url" and i + 1 < len(sys.argv):
            global BASE_URL
            BASE_URL = sys.argv[i + 1]

    # 路由命令
    if "--demo" in sys.argv:
        cmd_demo()

    elif "--register" in sys.argv:
        agent_id = "test-agent"
        role = ""
        platform = "codebuddy"
        for i, arg in enumerate(sys.argv):
            if arg == "--agent-id" and i + 1 < len(sys.argv):
                agent_id = sys.argv[i + 1]
            elif arg == "--role" and i + 1 < len(sys.argv):
                role = sys.argv[i + 1]
            elif arg == "--platform" and i + 1 < len(sys.argv):
                platform = sys.argv[i + 1]
        cmd_register(agent_id, agent_id, role, platform)

    elif "--registry" in sys.argv:
        cmd_registry()

    elif "--create-task" in sys.argv:
        target = ""
        desc = ""
        for i, arg in enumerate(sys.argv):
            if arg == "--target" and i + 1 < len(sys.argv):
                target = sys.argv[i + 1]
            elif arg == "--desc" and i + 1 < len(sys.argv):
                desc = sys.argv[i + 1]
        if not target or not desc:
            print("[ERROR] 需要 --target 和 --desc", file=sys.stderr)
            sys.exit(2)
        cmd_create_task(target, desc)

    elif "--advance" in sys.argv:
        idx = sys.argv.index("--advance")
        if idx + 2 >= len(sys.argv):
            print("[ERROR] 用法: --advance TASK_ID STATE", file=sys.stderr)
            sys.exit(2)
        cmd_advance(sys.argv[idx + 1], sys.argv[idx + 2])

    elif "--heartbeat" in sys.argv:
        idx = sys.argv.index("--heartbeat")
        agent_id = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "unknown"
        task_id = None
        progress = ""
        for i, arg in enumerate(sys.argv):
            if arg == "--task" and i + 1 < len(sys.argv):
                task_id = sys.argv[i + 1]
            elif arg == "--progress" and i + 1 < len(sys.argv):
                progress = sys.argv[i + 1]
        cmd_heartbeat(agent_id, task_id, progress)

    elif "--tasks" in sys.argv:
        cmd_tasks()

    elif "--events" in sys.argv:
        cmd_events()

    elif "--health" in sys.argv:
        cmd_health()

    else:
        print(f"未知命令。用 --help 查看用法。", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
