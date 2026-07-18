#!/usr/bin/env python3
"""
spawn-budget-check.py -- V8.5 Pre-Spawn 预算检查与模型降级建议
v1.0.0 | 2026-05-24 | 息壤 V8.5.0

职责：spawn 前评估"够不够花"，给出绿灯/黄灯/红灯 + 模型降级建议。
不阻断——只提供决策建议，由调用方（xirang-spawn.py / v8_spawn）决定是否执行。

用法:
  python3 .standards/spawn-budget-check.py check --task-id T-xxx --type prototype --model sonnet [--ceiling 5.0] [--json]
  python3 .standards/spawn-budget-check.py advise --task-id T-xxx --type prototype --remaining 0.5 [--json]

退出码:
  0 = 绿灯（可 spawn）
  1 = 黄灯（建议降级）
  2 = 红灯（建议放弃）

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/spawn-budget-check.py check --task-id T-xxx --type research --model sonnet --json"
"""
from __future__ import annotations

import sys
import os
import json
import re
import argparse
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
EVENTS_PATH = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"
COST_EVENTS_PATH = VAULT_ROOT / "02-项目管理" / "agent-cost-events.jsonl"
TASKS_DIR = VAULT_ROOT / "02-项目管理" / "任务卡"

# === 模型费率表（CNY/1K tokens） ===
# 假设 token 分布: 60% input + 40% output
MODEL_RATES_CNY_PER_1K = {
    "opus": 0.105 * 0.6 + 0.525 * 0.4,       # ≈ 0.273
    "sonnet": 0.021 * 0.6 + 0.105 * 0.4,     # ≈ 0.0546
    "haiku": 0.00175 * 0.6 + 0.00875 * 0.4,  # ≈ 0.00455
    "deepseek": 0.002 * 0.6 + 0.008 * 0.4,   # ≈ 0.0044
}

# 降级路径
DOWNGRADE_PATH = ["opus", "sonnet", "haiku"]

# === 任务类型 → 预估 tokens（复制自 xirang-spawn.py TASK_TYPES） ===
TASK_TYPICAL_TOKENS = {
    "prototype": 30000,
    "code": 50000,
    "prd": 45000,
    "research": 15000,
    "spec": 25000,
    "review": 10000,
    "batch": 80000,
    "diagram": 20000,
}

# 默认预算上限
DEFAULT_CEILING_CNY = 5.0

# 预留比例：留 20% 给 review/handoff 阶段
RESERVE_RATIO = 0.2


def find_task_card(task_id: str) -> Path | None:
    """在任务卡目录中递归查找 task card"""
    if not TASKS_DIR.exists():
        return None
    for path in TASKS_DIR.rglob(f"{task_id}.md"):
        if path.is_file():
            return path
    return None


def load_budget_from_task_card(task_id: str) -> float | None:
    """从 task card 的 frontmatter 读取 budget.cost_ceiling_cny"""
    card_path = find_task_card(task_id)
    if not card_path:
        return None
    try:
        content = card_path.read_text(encoding="utf-8")
        m = re.search(r"cost_ceiling_cny:\s*([\d.]+)", content)
        if m:
            return float(m.group(1))
    except IOError:
        pass
    return None


def aggregate_cost(task_id: str) -> dict:
    """从事件流汇总 task_id 的总成本（复制自 cost-fuse.py）"""
    total_tokens = 0
    total_cost = 0.0
    event_count = 0

    for events_path in (EVENTS_PATH, COST_EVENTS_PATH):
        if not events_path.is_file():
            continue
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("task_id") == task_id or ev.get("task") == task_id:
                        cost = ev.get("cost_cny", 0) or 0
                        tokens = ev.get("tokens", 0) or 0
                        total_cost += float(cost)
                        total_tokens += int(tokens)
                        event_type = ev.get("type") or ev.get("event")
                        if event_type in ("cost_event", "task_cost", "cost") or (
                            event_type in ("task_end", "spawn_end") and (float(cost) > 0 or int(tokens) > 0)
                        ):
                            event_count += 1
        except IOError:
            continue

    return {"tokens": total_tokens, "cost_cny": total_cost, "events": event_count}


def estimate_cost(task_type: str, model: str) -> float:
    """估算某任务类型 + 模型的成本（CNY）"""
    typical_tokens = TASK_TYPICAL_TOKENS.get(task_type, 20000)
    rate = MODEL_RATES_CNY_PER_1K.get(model, MODEL_RATES_CNY_PER_1K["sonnet"])
    return (typical_tokens / 1000) * rate


def find_affordable_model(task_type: str, effective_remaining: float) -> str | None:
    """沿降级路径找到第一个负担得起的模型"""
    for model in DOWNGRADE_PATH:
        est = estimate_cost(task_type, model)
        if est <= effective_remaining:
            return model
    return None


VALID_MODELS = set(MODEL_RATES_CNY_PER_1K.keys())


def cmd_check(args):
    """check 子命令：评估能否 spawn"""
    task_id = args.task_id
    task_type = args.type
    model = args.model or "sonnet"

    # 模型合法性校验
    if model not in VALID_MODELS:
        result = {"error": f"未知模型: {model}（有效值: {sorted(VALID_MODELS)}）"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(2)

    # 获取预算上限
    ceiling = args.ceiling
    if ceiling is None:
        ceiling = load_budget_from_task_card(task_id)
    if ceiling is None:
        ceiling = DEFAULT_CEILING_CNY

    # 汇总已花费
    agg = aggregate_cost(task_id)
    spent = agg["cost_cny"]
    remaining = ceiling - spent
    effective_remaining = remaining * (1 - RESERVE_RATIO)

    # 估算本次 spawn 成本
    estimated = estimate_cost(task_type, model)

    # 判定
    if estimated <= effective_remaining:
        can_spawn = True
        recommendation = "proceed"
        model_recommended = model
        reason = f"预算充足 (est {estimated:.4f} <= eff_remaining {effective_remaining:.4f})"
        exit_code = 0
    else:
        # 尝试降级
        affordable = find_affordable_model(task_type, effective_remaining)
        if affordable:
            can_spawn = True
            recommendation = "downgrade"
            model_recommended = affordable
            est_downgraded = estimate_cost(task_type, affordable)
            reason = f"原模型 {model} 成本 {estimated:.4f} 超出余量 {effective_remaining:.4f}，建议降级到 {affordable} (est {est_downgraded:.4f})"
            exit_code = 1
        else:
            can_spawn = False
            recommendation = "abort"
            model_recommended = None
            reason = f"所有模型均超出余量 {effective_remaining:.4f}，建议缩减范围或放弃"
            exit_code = 2

    result = {
        "can_spawn": can_spawn,
        "task_id": task_id,
        "task_type": task_type,
        "model_requested": model,
        "model_recommended": model_recommended,
        "ceiling_cny": ceiling,
        "spent_cny": round(spent, 4),
        "remaining_cny": round(remaining, 4),
        "effective_remaining_cny": round(effective_remaining, 4),
        "estimated_cost_cny": round(estimated, 4),
        "recommendation": recommendation,
        "reason": reason,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        icon = {0: "OK", 1: "WARN", 2: "STOP"}[exit_code]
        print(f"[{icon}] {reason}")
        if model_recommended and model_recommended != model:
            print(f"  建议模型: {model_recommended}")
        print(f"  预算: {spent:.4f}/{ceiling:.2f} CNY (有效余量: {effective_remaining:.4f})")

    sys.exit(exit_code)


def cmd_advise(args):
    """advise 子命令：给定余量，推荐模型"""
    task_type = args.type
    remaining = args.remaining

    if remaining is None:
        # 从 task_id 计算
        if not args.task_id:
            print(json.dumps({"error": "需要 --remaining 或 --task-id"}, ensure_ascii=False))
            sys.exit(2)
        ceiling = args.ceiling or load_budget_from_task_card(args.task_id) or DEFAULT_CEILING_CNY
        agg = aggregate_cost(args.task_id)
        remaining = (ceiling - agg["cost_cny"]) * (1 - RESERVE_RATIO)

    # 遍历所有模型，给出成本对比
    options = []
    for model in DOWNGRADE_PATH:
        est = estimate_cost(task_type, model)
        affordable = est <= remaining
        options.append({
            "model": model,
            "estimated_cost_cny": round(est, 4),
            "affordable": affordable,
        })

    # 推荐第一个负担得起的
    recommended = None
    for opt in options:
        if opt["affordable"]:
            recommended = opt["model"]
            break

    result = {
        "task_type": task_type,
        "effective_remaining_cny": round(remaining, 4),
        "model_recommended": recommended,
        "recommendation": "proceed" if recommended else "abort",
        "options": options,
        "reason": f"推荐 {recommended}" if recommended else "所有模型均超预算",
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"余量: {remaining:.4f} CNY | 任务类型: {task_type}")
        for opt in options:
            mark = "v" if opt["affordable"] else "x"
            print(f"  [{mark}] {opt['model']:<8} est={opt['estimated_cost_cny']:.4f} CNY")
        if recommended:
            print(f"  推荐: {recommended}")
        else:
            print(f"  结论: 所有模型均超预算，建议缩减范围或放弃")

    sys.exit(0 if recommended else 2)


def main():
    print(json.dumps({
        "status": "retired",
        "retired_at": "2026-07-19",
        "message": "成本预算检查已退出当前 V9 运行能力。",
    }, ensure_ascii=False))
    return 3

    parser = argparse.ArgumentParser(description="V8.5 Pre-Spawn Budget Check")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # check 子命令
    p_check = subparsers.add_parser("check", help="检查能否按指定模型 spawn")
    p_check.add_argument("--task-id", required=True, help="父任务 ID")
    p_check.add_argument("--type", required=True, choices=list(TASK_TYPICAL_TOKENS.keys()), help="任务类型")
    p_check.add_argument("--model", default="sonnet", help="请求使用的模型 (default: sonnet)")
    p_check.add_argument("--ceiling", type=float, help="自定义预算上限 CNY（不读 task card）")
    p_check.add_argument("--json", action="store_true", default=True, help="JSON 输出")

    # advise 子命令
    p_advise = subparsers.add_parser("advise", help="给定余量推荐模型")
    p_advise.add_argument("--task-id", help="父任务 ID（用于自动计算余量）")
    p_advise.add_argument("--type", required=True, choices=list(TASK_TYPICAL_TOKENS.keys()), help="任务类型")
    p_advise.add_argument("--remaining", type=float, help="有效余量 CNY（直接指定，跳过计算）")
    p_advise.add_argument("--ceiling", type=float, help="自定义预算上限 CNY")
    p_advise.add_argument("--json", action="store_true", default=True, help="JSON 输出")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)
    elif args.command == "advise":
        cmd_advise(args)


if __name__ == "__main__":
    raise SystemExit(main())
