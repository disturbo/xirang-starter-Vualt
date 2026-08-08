#!/usr/bin/env python3
"""
cost-fuse.py — 息壤 V9.2 成本熔断检查
v1.1 · 2026-05-31 | 息壤 V9.2（v1.0: 2026-05-18 V8.5.0）

用途：按 task_id 从 智能体事件.jsonl 汇总累计成本，与 task card 的 budget 对比。
  - 60% → warning（stdout 告警，退出码 0）+ fallback 建议
  - 100% → fuse（stdout 熔断，退出码 1）+ fallback 建议
  - 未超阈值 → pass（退出码 0）

V9.2 新增：
  - 读取 agent-contract.yaml 中的 fallback_model 配置
  - WARNING 时输出降级建议（不强制）
  - FUSE 时输出降级建议，仅 auto_fallback:true 时标记为强制

用法：
  python3 .standards/cost-fuse.py <task_id> [--json]

  # 或指定自定义阈值（不读 task card）
  python3 .standards/cost-fuse.py <task_id> --ceiling 5.0 [--json]
"""
from __future__ import annotations

import sys
import json
import os
import re
import subprocess
from pathlib import Path

from jsonl_reader import read_jsonl

EVENTS_PATH = "02-项目管理/智能体状态/智能体事件.jsonl"
AGENT_COST_EVENTS_PATH = "02-项目管理/agent-cost-events.jsonl"
TASKS_DIR = "02-项目管理/任务卡"
LOG_PATH = "02-项目管理/pre-write-check.log.jsonl"  # 复用日志目录


def find_task_card(task_id: str) -> str | None:
    """在任务卡目录中递归查找 task card，兼容 YYYY-MM 月桶和历史目录。"""
    root = Path(TASKS_DIR)
    if not root.exists():
        return None
    for path in root.rglob(f"{task_id}.md"):
        if path.is_file():
            return str(path)
    return None


def load_budget_from_task_card(task_id: str) -> float | None:
    """从 task card 的 frontmatter 读取 budget.cost_ceiling_cny"""
    card_path = find_task_card(task_id)
    if not card_path:
        return None
    with open(card_path, "r", encoding="utf-8") as f:
        content = f.read()
    # 简单解析 frontmatter 中的 cost_ceiling_cny
    m = re.search(r"cost_ceiling_cny:\s*([\d.]+)", content)
    if m:
        return float(m.group(1))
    return None


def aggregate_cost(task_id: str) -> dict:
    """从事件流汇总 task_id 的总成本"""
    total_tokens = 0
    total_cost = 0.0
    event_count = 0

    for events_path in (EVENTS_PATH, AGENT_COST_EVENTS_PATH):
        if not os.path.isfile(events_path):
            continue
        rows, _ = read_jsonl(Path(events_path), warn=True)
        for ev in rows:
            if ev.get("task_id") == task_id or ev.get("task") == task_id:
                event_type = ev.get("type") or ev.get("event")

                # V9.2 口径规则：cost_start 只标记起点，其余成本事件增量计入。
                if event_type == "cost_start":
                    event_count += 1
                    continue

                cost = ev.get("cost_cny", 0) or 0
                tokens = ev.get("tokens", 0) or 0
                total_cost += float(cost)
                total_tokens += int(tokens)
                if event_type in ("cost_event", "task_cost", "cost",
                                  "cost_checkpoint", "cost_finalize") or (
                    event_type in ("task_end", "spawn_end") and (float(cost) > 0 or int(tokens) > 0)
                ):
                    event_count += 1

    return {"tokens": total_tokens, "cost_cny": total_cost, "events": event_count}


def load_fallback_config(agent_id: str) -> dict | None:
    """从 agent-contract.yaml 读取 agent 的 fallback_model 配置"""
    contract_path = Path(__file__).parent / "agent-contract.yaml"
    if not contract_path.exists():
        return None
    try:
        # 用简单解析避免依赖 PyYAML
        content = contract_path.read_text(encoding="utf-8")
        # 找到对应 agent 块
        import re
        # 匹配 agent_id 后的 fallback_model 块
        pattern = rf"agent_id:\s*{re.escape(agent_id)}.*?fallback_model:\s*\n\s+primary:\s*(\S+)\s*\n\s+chain:\s*\[([^\]]*)\]\s*\n\s+auto_fallback:\s*(\S+)"
        m = re.search(pattern, content, re.DOTALL)
        if not m:
            return None
        primary = m.group(1).strip()
        chain_raw = m.group(2).strip()
        chain = [x.strip().strip("'\"") for x in chain_raw.split(",") if x.strip()] if chain_raw else []
        auto = m.group(3).strip().lower() == "true"
        return {"primary": primary, "chain": chain, "auto_fallback": auto}
    except Exception:
        return None


def find_agent_for_task(task_id: str) -> str | None:
    """从事件流中找到某 task_id 对应的 agent_id"""
    for events_path in (EVENTS_PATH, AGENT_COST_EVENTS_PATH):
        if not os.path.isfile(events_path):
            continue
        rows, _ = read_jsonl(Path(events_path), warn=True)
        for ev in rows:
            if (ev.get("task_id") == task_id or ev.get("task") == task_id) and ev.get("agent"):
                return ev["agent"]
    return None


def notify_msg_queue(task_id: str, agent_id: str, pct: float, spent: float, ceiling: float, level: str):
    """V8.5: 通过 msg-queue.py 发送 cost_alert 通知（advisory，失败不影响主逻辑）"""
    try:
        payload = json.dumps({
            "task_id": task_id,
            "pct": round(pct, 1),
            "spent_cny": round(spent, 4),
            "ceiling_cny": ceiling,
            "level": level,
        })
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "msg-queue.py"),
             "enqueue", "--from", "system", "--to", agent_id or "broadcast",
             "--type", "cost_alert", "--priority", "P1" if level == "warning" else "P0",
             "--payload", payload, "--ref-task", task_id, "--json"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass  # msg-queue 通知是 advisory，绝不影响 fuse 主逻辑


def main():
    print(json.dumps({
        "status": "retired",
        "retired_at": "2026-07-19",
        "message": "成本治理已退出当前 V9 运行能力；未执行熔断。",
    }, ensure_ascii=False))
    return 3

    if len(sys.argv) < 2:
        print("用法: cost-fuse.py <task_id> [--ceiling <CNY>] [--json]", file=sys.stderr)
        sys.exit(2)

    task_id = sys.argv[1]
    output_json = "--json" in sys.argv
    custom_ceiling = None

    if "--ceiling" in sys.argv:
        idx = sys.argv.index("--ceiling")
        if idx + 1 < len(sys.argv):
            custom_ceiling = float(sys.argv[idx + 1])

    # 获取预算上限
    ceiling = custom_ceiling
    if ceiling is None:
        ceiling = load_budget_from_task_card(task_id)
    if ceiling is None:
        ceiling = 5.0  # 默认 5 元

    # 汇总成本
    agg = aggregate_cost(task_id)
    pct = (agg["cost_cny"] / ceiling * 100) if ceiling > 0 else 0

    # 判定
    fallback_info = None
    if pct >= 100:
        status = "fuse"
        msg = f"[FUSE] {task_id}: {agg['cost_cny']:.2f}/{ceiling:.2f} CNY ({pct:.0f}%) — 成本已达上限，熔断！"
        exit_code = 1
        # V8.5: 自动通知
        agent_id = find_agent_for_task(task_id)
        notify_msg_queue(task_id, agent_id, pct, agg["cost_cny"], ceiling, "fuse")
        # V9.2: fallback 建议
        if agent_id:
            fb = load_fallback_config(agent_id)
            if fb and fb["chain"]:
                if fb["auto_fallback"]:
                    msg += f"\n  [AUTO-FALLBACK] 自动降级到: {fb['chain'][0]}"
                else:
                    msg += f"\n  [FALLBACK-SUGGEST] 建议降级到: {fb['chain'][0]}（需手动确认）"
                fallback_info = fb
    elif pct >= 60:
        status = "warning"
        msg = f"[WARN] {task_id}: {agg['cost_cny']:.2f}/{ceiling:.2f} CNY ({pct:.0f}%) — 已达 60% 预算"
        exit_code = 0
        # V8.5: 自动通知
        agent_id = find_agent_for_task(task_id)
        notify_msg_queue(task_id, agent_id, pct, agg["cost_cny"], ceiling, "warning")
        # V9.2: fallback 建议（仅建议，不强制）
        if agent_id:
            fb = load_fallback_config(agent_id)
            if fb and fb["chain"]:
                msg += f"\n  [FALLBACK-INFO] 可用降级链: {fb['primary']} → {' → '.join(fb['chain'])}"
                fallback_info = fb
    else:
        status = "pass"
        msg = f"[PASS] {task_id}: {agg['cost_cny']:.2f}/{ceiling:.2f} CNY ({pct:.0f}%)"
        exit_code = 0

    if output_json:
        result = {
            "task_id": task_id,
            "status": status,
            "cost_cny": agg["cost_cny"],
            "ceiling_cny": ceiling,
            "pct": round(pct, 1),
            "tokens": agg["tokens"],
            "events": agg["events"],
        }
        if fallback_info:
            result["fallback"] = {
                "primary": fallback_info["primary"],
                "chain": fallback_info["chain"],
                "auto_fallback": fallback_info["auto_fallback"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(msg)

    sys.exit(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
