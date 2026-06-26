#!/usr/bin/env python3
"""
v8-cost-observability.py — V8 成本可观测性周报
v1.0 | 2026-05-17 | 息壤 V8.5.0

目标不是强行熔断，而是回答四个问题：
  1. 这周各运行档位有多少任务？
  2. token/cost 主要花在哪些 Agent 和 task_id 上？
  3. 失败、重试、异常消耗占比是多少？
  4. 当前数据还缺什么，哪些指标不能装作精确？

用法：
  python3 .standards/v8-cost-observability.py --json
  python3 .standards/v8-cost-observability.py --output 02-项目管理/成本周报/v8-cost-report-2026-05-19.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TZ = timezone(timedelta(hours=8))
VAULT_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = VAULT_ROOT / "02-项目管理" / "任务卡"
RUN_LOG_DIR = VAULT_ROOT / "02-项目管理" / "运行日志"
EVENTS_LOG = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"
COST_EVENTS_LOG = VAULT_ROOT / "02-项目管理" / "agent-cost-events.jsonl"
DEFAULT_REPORT_DIR = VAULT_ROOT / "02-项目管理" / "成本周报"


@dataclass
class TaskCard:
    task_id: str
    title: str
    status: str
    owner: str
    min_level: str
    task_size: str
    budget_cny: float
    actual_cny: float
    actual_tokens: int
    path: Path


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().strip('"')
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.match(r".*[+-]\d{4}$", text):
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def parse_frontmatter(content: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = match.group(1)
    result: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        result[key.strip()] = value.strip('"')
    return result


def extract_nested_number(content: str, key: str) -> float:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([\d.]+)", content, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


def load_task_cards() -> dict[str, TaskCard]:
    cards: dict[str, TaskCard] = {}
    if not TASKS_DIR.exists():
        return cards
    for path in sorted(TASKS_DIR.rglob("T-*.md")):
        content = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(content)
        task_id = fm.get("task_id")
        if not task_id:
            continue
        cards[task_id] = TaskCard(
            task_id=task_id,
            title=fm.get("title", ""),
            status=fm.get("status", ""),
            owner=fm.get("owner", ""),
            min_level=fm.get("min_level", ""),
            task_size=fm.get("task_size", ""),
            budget_cny=extract_nested_number(content, "cost_ceiling_cny"),
            actual_cny=extract_nested_number(content, "cost_cny"),
            actual_tokens=int(extract_nested_number(content, "tokens")),
            path=path,
        )
    return cards


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            rows.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return rows


def within_window(event: dict[str, Any], since: datetime) -> bool:
    ts = parse_iso(str(event.get("ts") or event.get("timestamp") or ""))
    return ts is not None and ts >= since


def normalize_event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type") or event.get("event")
    if event_type:
        return str(event_type)
    if event.get("phase") and "cost_cny" in event and "tokens" in event:
        return "cost_event"
    return "unknown"


def infer_task_level(card: TaskCard | None, events: list[dict[str, Any]]) -> str:
    if card and card.min_level in {"M0", "M1", "M2", "M3", "M4", "M5"}:
        return card.min_level
    if any(normalize_event_type(e) in {"sub_route", "spawn_start", "spawn_end", "heartbeat"} for e in events):
        return "M5"
    if card:
        if card.task_size in {"L", "XL"}:
            return "M5"
        return "M4"
    return "unknown"


def parse_run_logs(since: datetime) -> Counter:
    levels: Counter = Counter()
    if not RUN_LOG_DIR.exists():
        return levels
    for path in sorted(RUN_LOG_DIR.glob("*.md")):
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        if date_match:
            day = parse_iso(date_match.group(1) + "T00:00:00+08:00")
            if day and day < since.replace(hour=0, minute=0, second=0, microsecond=0):
                continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^-\s+\d{1,2}:\d{2}\s+\|\s+(M[0-5])\s+\|", line.strip())
            if match:
                levels[match.group(1)] += 1
    return levels


def compute_metrics(days: int) -> dict[str, Any]:
    now = datetime.now(TZ)
    since = now - timedelta(days=days)
    cards = load_task_cards()
    events = [e for e in read_jsonl(EVENTS_LOG) if within_window(e, since)]
    cost_events = [e for e in read_jsonl(COST_EVENTS_LOG) if within_window(e, since)]
    all_cost_rows = events + cost_events

    events_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        task_id = str(event.get("task_id") or "")
        if task_id:
            events_by_task[task_id].append(event)

    task_costs: dict[str, dict[str, Any]] = {}
    agent_costs: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})
    failed_cost = {"tokens": 0, "cost_cny": 0.0, "events": 0}
    total_tokens = 0
    total_cost = 0.0
    event_types: Counter = Counter()
    phase_costs: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})

    for event in all_cost_rows:
        event_type = normalize_event_type(event)
        event_types[event_type] += 1
        tokens = int(event.get("tokens") or 0)
        cost = float(event.get("cost_cny") or 0)
        phase = str(event.get("phase") or "unknown")
        total_tokens += tokens
        total_cost += cost

        if event_type == "cost_event":
            phase_costs[phase]["tokens"] += tokens
            phase_costs[phase]["cost_cny"] += cost
            phase_costs[phase]["events"] += 1

        agent = str(event.get("agent") or "unknown")
        agent_costs[agent]["tokens"] += tokens
        agent_costs[agent]["cost_cny"] += cost
        agent_costs[agent]["events"] += 1

        task_id = str(event.get("task_id") or "unknown")
        task = task_costs.setdefault(task_id, {"tokens": 0, "cost_cny": 0.0, "events": 0})
        task["tokens"] += tokens
        task["cost_cny"] += cost
        task["events"] += 1

        result = str(event.get("result") or event.get("status") or "").lower()
        if event_type in {"error", "retry", "escalation"} or result in {"failed", "fail", "error", "blocked"}:
            failed_cost["tokens"] += tokens
            failed_cost["cost_cny"] += cost
            failed_cost["events"] += 1

    levels = parse_run_logs(since)
    for task_id, grouped in events_by_task.items():
        level = infer_task_level(cards.get(task_id), grouped)
        if level != "unknown":
            levels[level] += 1

    top_tasks = []
    for task_id, cost_data in sorted(task_costs.items(), key=lambda pair: -pair[1]["cost_cny"])[:10]:
        card = cards.get(task_id)
        top_tasks.append(
            {
                "task_id": task_id,
                "title": card.title if card else "",
                "owner": card.owner if card else "",
                "tokens": int(cost_data["tokens"]),
                "cost_cny": round(float(cost_data["cost_cny"]), 4),
                "events": int(cost_data["events"]),
            }
        )

    failed_ratio = (failed_cost["cost_cny"] / total_cost * 100) if total_cost else 0.0
    positive_cost_events = sum(1 for e in all_cost_rows if float(e.get("cost_cny") or 0) > 0)
    if total_cost == 0:
        billing_status = "not_connected"
        billing_note = "未接真实计费；当前 CNY 只能说明事件流里没有真实 cost_cny。"
    elif positive_cost_events < len(all_cost_rows):
        billing_status = "partial"
        billing_note = "部分事件含 cost_cny，仍不能视为完整账单。"
    else:
        billing_status = "connected"
        billing_note = "事件窗口内 cost_cny 全部来自事件流；仍需与平台账单对账。"

    coverage_notes = []
    if not COST_EVENTS_LOG.exists():
        coverage_notes.append("agent-cost-events.jsonl 不存在，当前主要依赖智能体事件流中的 tokens/cost_cny。")
    if not any(e.get("phase") for e in all_cost_rows):
        coverage_notes.append("事件未记录 phase，暂不能精确拆分仪式 token 与产出 token。")
    if not total_cost:
        coverage_notes.append("未接真实计费；成本指标只能看 token/事件结构分布，不能作为费用账单。")

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "window_days": days,
        "since": since.strftime("%Y-%m-%d %H:%M"),
        "billing_status": billing_status,
        "billing_note": billing_note,
        "positive_cost_event_count": positive_cost_events,
        "total_tokens": total_tokens,
        "total_cost_cny": round(total_cost, 4),
        "failed_retry_cost_cny": round(failed_cost["cost_cny"], 4),
        "failed_retry_tokens": failed_cost["tokens"],
        "failed_retry_ratio_pct": round(failed_ratio, 1),
        "level_counts": dict(sorted(levels.items())),
        "agent_costs": {
            agent: {
                "tokens": int(values["tokens"]),
                "cost_cny": round(float(values["cost_cny"]), 4),
                "events": int(values["events"]),
            }
            for agent, values in sorted(agent_costs.items(), key=lambda pair: -pair[1]["cost_cny"])
        },
        "event_types": dict(event_types.most_common()),
        "phase_costs": {
            phase: {
                "tokens": int(values["tokens"]),
                "cost_cny": round(float(values["cost_cny"]), 4),
                "events": int(values["events"]),
            }
            for phase, values in sorted(phase_costs.items())
        },
        "top_tasks": top_tasks,
        "task_card_count": len(cards),
        "event_count": len(events),
        "cost_event_count": len(cost_events),
        "coverage_notes": coverage_notes,
    }


def render_markdown(metrics: dict[str, Any]) -> str:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    title = f"V8 成本可观测性周报 {today}"
    lines = [
        "---",
        f'title: "{title}"',
        "version: v1.0",
        "status: generated",
        "maturity: draft",
        "type: cost_report",
        "tags: [V8, 成本治理, 可观测性]",
        f"created: {today}",
        f"generated: {metrics['generated_at']}",
        "---",
        "",
        f"# {title}",
        "",
        f"> 统计窗口：最近 {metrics['window_days']} 天，自 {metrics['since']} 起。此报告用于趋势观察，不作为硬熔断凭证。",
        f"> 计费口径：`{metrics['billing_status']}` — {metrics['billing_note']}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|------|---:|",
        f"| 总 tokens | {metrics['total_tokens']} |",
        f"| 计费状态 | {metrics['billing_status']} |",
        f"| 总成本 CNY | {metrics['total_cost_cny']} |",
        f"| 正成本事件数 | {metrics['positive_cost_event_count']} |",
        f"| 失败/重试成本 CNY | {metrics['failed_retry_cost_cny']} |",
        f"| 失败/重试成本占比 | {metrics['failed_retry_ratio_pct']}% |",
        f"| 事件数 | {metrics['event_count']} |",
        f"| 独立 cost event 数 | {metrics['cost_event_count']} |",
        f"| task card 数 | {metrics['task_card_count']} |",
        "",
        "## 档位分布",
        "",
        "| 档位 | 数量 |",
        "|:--:|---:|",
    ]
    for level in ["M0", "M1", "M2", "M3", "M4", "M5", "unknown"]:
        if level in metrics["level_counts"]:
            lines.append(f"| {level} | {metrics['level_counts'][level]} |")

    lines.extend(["", "## Agent 成本", "", "| Agent | tokens | CNY | events |", "|---|---:|---:|---:|"])
    for agent, values in metrics["agent_costs"].items():
        lines.append(f"| {agent} | {values['tokens']} | {values['cost_cny']} | {values['events']} |")

    lines.extend(["", "## Top Task", "", "| task_id | 标题 | owner | tokens | CNY | events |", "|---|---|---|---:|---:|---:|"])
    for item in metrics["top_tasks"]:
        lines.append(
            f"| {item['task_id']} | {item['title']} | {item['owner']} | {item['tokens']} | {item['cost_cny']} | {item['events']} |"
        )

    lines.extend(["", "## 事件类型", "", "| 类型 | 数量 |", "|---|---:|"])
    for event_type, count in metrics["event_types"].items():
        lines.append(f"| {event_type} | {count} |")

    if metrics.get("phase_costs"):
        lines.extend(["", "## Phase 成本分布", "", "| phase | tokens | CNY | events |", "|---|---:|---:|---:|"])
        for phase, values in metrics["phase_costs"].items():
            lines.append(f"| {phase} | {values['tokens']} | {values['cost_cny']} | {values['events']} |")

    lines.extend(["", "## 数据覆盖说明", ""])
    if metrics["coverage_notes"]:
        for note in metrics["coverage_notes"]:
            lines.append(f"- {note}")
    else:
        lines.append("- 事件流已覆盖 task、agent、tokens、cost 基础字段。")

    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "- 给真实模型调用补 `agent-cost-events.jsonl`，字段至少包含 ts/task_id/agent/model/tokens/cost_cny/phase。",
            "- 事件写入 phase 后，再计算仪式 token / 产出 token 比率。",
            "- 将本脚本纳入 Hermes 熵报告的每周采集。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V8 成本可观测性周报")
    parser.add_argument("--days", type=int, default=7, help="统计窗口天数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="输出 Markdown 报告路径")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    metrics = compute_metrics(args.days)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    report = render_markdown(metrics)
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = VAULT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"[OK] 成本周报已生成: {output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
