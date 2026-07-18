#!/usr/bin/env python3
"""
agent-cost-events.py — V8 agent 成本事件流工具
v1.1 | 2026-06-04 | 息壤 V9

事件流位置：
  02-项目管理/agent-cost-events.jsonl

最小 schema：
  type, ts, task_id, agent, model, tokens, cost_cny, phase, source

phase 枚举：
  routing      路由/判定/开工仪式
  context      读取上下文/检索/整理输入
  execution    主要产出
  review       评审/校验/修复
  handoff      收工/交接/复盘
  retry        失败重试

用法：
  python3 .standards/agent-cost-events.py append --task-id T-20260519-13 --agent hongmeisu --model codex --tokens 1000 --cost-cny 0.02 --phase execution --source manual --note "示例"
  python3 .standards/agent-cost-events.py validate --json
  python3 .standards/agent-cost-events.py summary --task-id T-20260519-13 --json
  python3 .standards/agent-cost-events.py aggregate [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TZ = timezone(timedelta(hours=8))
VAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = VAULT_ROOT / "02-项目管理" / "agent-cost-events.jsonl"
PHASES = {"routing", "context", "execution", "review", "handoff", "retry"}
REQUIRED = {"type", "ts", "task_id", "agent", "model", "tokens", "cost_cny", "phase", "source"}
BILLING_STATUSES = {"not_connected", "usage_only", "estimated", "connected"}
CONNECTED_COST_SOURCES = {"platform_billing", "provider_invoice", "platform_usage"}


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            row = {"_invalid": True, "_line": line_no, "_error": str(exc)}
        else:
            row["_line"] = line_no
        rows.append(row)
    return rows


def validate_row(row: dict[str, Any]) -> list[str]:
    line = row.get("_line", "?")
    if row.get("_invalid"):
        return [f"line {line}: JSON 解析失败: {row.get('_error')}"]

    errors: list[str] = []
    missing = sorted(REQUIRED - set(row))
    if missing:
        errors.append(f"line {line}: 缺字段 {', '.join(missing)}")

    if row.get("phase") not in PHASES:
        errors.append(f"line {line}: phase 非法 {row.get('phase')}")

    for key in ("tokens", "cost_cny"):
        try:
            value = float(row.get(key, 0))
        except (TypeError, ValueError):
            errors.append(f"line {line}: {key} 必须是数字")
            continue
        if value < 0:
            errors.append(f"line {line}: {key} 不能为负数")

    usage_parts: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
        if key not in row:
            continue
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            errors.append(f"line {line}: {key} 必须是整数")
            continue
        if value < 0:
            errors.append(f"line {line}: {key} 不能为负数")
        usage_parts[key] = value
    if usage_parts and "tokens" in row:
        total = int(row.get("tokens") or 0)
        expected = usage_parts.get("input_tokens", 0) + usage_parts.get("output_tokens", 0)
        if total != expected:
            errors.append(f"line {line}: tokens={total} 与 input_tokens+output_tokens={expected} 不一致")

    billing_status = str(row.get("billing_status") or "").strip()
    if billing_status and billing_status not in BILLING_STATUSES:
        errors.append(f"line {line}: billing_status 非法 {billing_status}")
    if billing_status == "connected":
        cost_source = str(row.get("cost_source") or "").strip()
        usage_source = str(row.get("usage_source") or "").strip()
        if cost_source not in CONNECTED_COST_SOURCES:
            errors.append(f"line {line}: billing_status=connected 必须提供平台成本来源 cost_source")
        if usage_source in {"", "manual", "unknown"}:
            errors.append(f"line {line}: billing_status=connected 不能使用 usage_source={usage_source or 'null'}")

    for key in ("task_id", "agent", "model", "source"):
        if key in row and not str(row.get(key)).strip():
            errors.append(f"line {line}: {key} 不能为空")

    return errors


def command_append(args: argparse.Namespace) -> int:
    tokens = args.tokens
    if tokens is None:
        input_tokens = args.input_tokens or 0
        output_tokens = args.output_tokens or 0
        if input_tokens or output_tokens:
            tokens = input_tokens + output_tokens
        else:
            print("tokens 或 input/output token 至少提供一种", file=sys.stderr)
            return 2
    event = {
        "type": "cost_event",
        "ts": args.ts or now_iso(),
        "task_id": args.task_id,
        "agent": args.agent,
        "model": args.model,
        "tokens": tokens,
        "cost_cny": args.cost_cny,
        "phase": args.phase,
        "source": args.source,
    }
    if args.input_tokens is not None:
        event["input_tokens"] = args.input_tokens
    if args.output_tokens is not None:
        event["output_tokens"] = args.output_tokens
    if args.cached_input_tokens is not None:
        event["cached_input_tokens"] = args.cached_input_tokens
    if args.reasoning_output_tokens is not None:
        event["reasoning_output_tokens"] = args.reasoning_output_tokens
    if args.usage_source:
        event["usage_source"] = args.usage_source
    if args.billing_status:
        event["billing_status"] = args.billing_status
    if args.cost_source:
        event["cost_source"] = args.cost_source
    if args.note:
        event["note"] = args.note

    errors = validate_row(event)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2

    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    if args.json:
        print(json.dumps({"status": "ok", "event": event}, ensure_ascii=False, indent=2))
    else:
        print(f"[OK] appended cost event: {event['task_id']} {event['phase']} {event['tokens']} tokens {event['cost_cny']} CNY")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.log)
    errors: list[str] = []
    for row in rows:
        errors.extend(validate_row(row))
    result = {"status": "fail" if errors else "pass", "file": str(args.log), "events": len(rows), "errors": errors}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("[FAIL] agent-cost-events 校验失败")
        for error in errors:
            print(f"- {error}")
    else:
        print(f"[PASS] agent-cost-events 校验通过: {len(rows)} 条")
    return 1 if errors else 0


def command_summary(args: argparse.Namespace) -> int:
    rows = [row for row in read_jsonl(args.log) if not row.get("_invalid")]
    if args.task_id:
        rows = [row for row in rows if row.get("task_id") == args.task_id]

    total_tokens = sum(int(row.get("tokens") or 0) for row in rows)
    input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows)
    output_tokens = sum(int(row.get("output_tokens") or 0) for row in rows)
    total_cost = sum(float(row.get("cost_cny") or 0) for row in rows)
    by_phase: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})
    by_agent: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})
    models: Counter = Counter()
    billing_statuses: Counter = Counter()
    usage_sources: Counter = Counter()

    for row in rows:
        tokens = int(row.get("tokens") or 0)
        cost = float(row.get("cost_cny") or 0)
        phase = str(row.get("phase") or "unknown")
        agent = str(row.get("agent") or "unknown")
        model = str(row.get("model") or "unknown")
        billing_statuses[str(row.get("billing_status") or "legacy_unmarked")] += 1
        usage_sources[str(row.get("usage_source") or "unknown")] += 1
        by_phase[phase]["tokens"] += tokens
        by_phase[phase]["cost_cny"] += cost
        by_phase[phase]["events"] += 1
        by_agent[agent]["tokens"] += tokens
        by_agent[agent]["cost_cny"] += cost
        by_agent[agent]["events"] += 1
        models[model] += 1

    result = {
        "status": "ok",
        "file": str(args.log),
        "task_id": args.task_id,
        "events": len(rows),
        "tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_cny": round(total_cost, 4),
        "by_phase": {k: {"tokens": int(v["tokens"]), "cost_cny": round(v["cost_cny"], 4), "events": int(v["events"])} for k, v in sorted(by_phase.items())},
        "by_agent": {k: {"tokens": int(v["tokens"]), "cost_cny": round(v["cost_cny"], 4), "events": int(v["events"])} for k, v in sorted(by_agent.items())},
        "models": dict(models.most_common()),
        "billing_statuses": dict(billing_statuses.most_common()),
        "usage_sources": dict(usage_sources.most_common()),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"events={result['events']} tokens={result['tokens']} cost_cny={result['cost_cny']}")
        for phase, values in result["by_phase"].items():
            print(f"- {phase}: {values['tokens']} tokens / {values['cost_cny']} CNY / {values['events']} events")
    return 0


STATUS_DIR = VAULT_ROOT / "02-项目管理" / "智能体状态"


def get_week_start(dt: datetime) -> datetime:
    """Get Monday 00:00:00 of the week containing dt (Asia/Shanghai)."""
    d = dt.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    d -= timedelta(days=d.weekday())  # Monday
    return d


def parse_event_ts(ts_str: str) -> datetime | None:
    """Parse ISO timestamp from event, return tz-aware datetime."""
    try:
        # Handle both Z suffix and +HH:MM offset
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def find_status_file_by_agent_id(agent_id: str) -> Path | None:
    """Find the status .md file for a given agent_id by reading frontmatter."""
    if not STATUS_DIR.exists():
        return None
    for f in STATUS_DIR.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # Quick check: look for agent_id in frontmatter
        fm_match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        # Match agent_id: value or agent: value
        for line in fm.splitlines():
            if re.match(rf"^agent_id:\s*{re.escape(agent_id)}\s*$", line):
                return f
            # 头孢 style: agent: 头孢 (agent field used as display name, not id)
            # For 头孢, the JSONL uses "toubao" but the file has "agent: 头孢"
    return None


# Build a lookup table: agent_id -> status file path
def build_agent_file_map() -> dict[str, Path]:
    """Scan status dir, return {agent_id: Path} mapping."""
    mapping: dict[str, Path] = {}
    if not STATUS_DIR.exists():
        return mapping
    for f in STATUS_DIR.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm_match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        for line in fm.splitlines():
            m = re.match(r"^agent_id:\s*(.+?)\s*$", line)
            if m:
                mapping[m.group(1)] = f
                break
    return mapping


def update_frontmatter_cost(file_path: Path, weekly_tokens: int, weekly_cost: float, model_used: str | None) -> bool:
    """Update cost_tracking fields in a status file's YAML frontmatter."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False

    fm_match = re.match(r"^(---\n)(.+?)(\n---)", text, re.DOTALL)
    if not fm_match:
        return False

    fm_content = fm_match.group(2)
    body_after = text[fm_match.end():]

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")

    # Check if cost_tracking block exists
    if "cost_tracking:" in fm_content:
        # Replace fields within cost_tracking block
        fm_content = re.sub(
            r"(cost_tracking:\n(?:  \w[^\n]*\n)*)",
            lambda _: (
                f"cost_tracking:\n"
                f"  session_tokens: 0\n"
                f"  session_cost_cny: 0.0\n"
                f"  weekly_tokens: {weekly_tokens}\n"
                f"  weekly_cost_cny: {round(weekly_cost, 4)}\n"
                f"  model_used: {json.dumps(model_used, ensure_ascii=False) if model_used else 'null'}\n"
                f"  last_reset: \"{today_str}\"\n"
            ),
            fm_content,
        )
    else:
        # Add cost_tracking block at end of frontmatter
        fm_content += (
            f"\ncost_tracking:\n"
            f"  session_tokens: 0\n"
            f"  session_cost_cny: 0.0\n"
            f"  weekly_tokens: {weekly_tokens}\n"
            f"  weekly_cost_cny: {round(weekly_cost, 4)}\n"
            f"  model_used: {json.dumps(model_used, ensure_ascii=False) if model_used else 'null'}\n"
            f"  last_reset: \"{today_str}\"\n"
        )

    new_text = fm_match.group(1) + fm_content + fm_match.group(3) + body_after
    file_path.write_text(new_text, encoding="utf-8")
    return True


def command_aggregate(args: argparse.Namespace) -> int:
    """Aggregate weekly cost events and write back to agent status files."""
    rows = [row for row in read_jsonl(args.log) if not row.get("_invalid")]

    now = datetime.now(TZ)
    week_start = get_week_start(now)

    # Filter to current week
    weekly_rows: list[dict[str, Any]] = []
    for row in rows:
        ts = parse_event_ts(str(row.get("ts", "")))
        if ts and ts >= week_start:
            weekly_rows.append(row)

    # Group by agent
    by_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tokens": 0, "cost_cny": 0.0, "models": Counter(), "events": 0}
    )
    for row in weekly_rows:
        agent = str(row.get("agent", "unknown"))
        tokens = int(row.get("tokens") or 0)
        cost = float(row.get("cost_cny") or 0)
        model = str(row.get("model") or "unknown")
        by_agent[agent]["tokens"] += tokens
        by_agent[agent]["cost_cny"] += cost
        by_agent[agent]["models"][model] += 1
        by_agent[agent]["events"] += 1

    # Build agent_id -> file mapping
    agent_map = build_agent_file_map()

    results: list[dict[str, Any]] = []
    for agent_id, stats in sorted(by_agent.items()):
        top_model = stats["models"].most_common(1)[0][0] if stats["models"] else None
        file_path = agent_map.get(agent_id)
        updated = False
        if file_path:
            updated = update_frontmatter_cost(
                file_path,
                weekly_tokens=stats["tokens"],
                weekly_cost=stats["cost_cny"],
                model_used=top_model,
            )
        results.append({
            "agent": agent_id,
            "file": str(file_path.relative_to(VAULT_ROOT)) if file_path else None,
            "weekly_tokens": stats["tokens"],
            "weekly_cost_cny": round(stats["cost_cny"], 4),
            "model_used": top_model,
            "events": stats["events"],
            "updated": updated,
        })

    output = {
        "status": "ok",
        "week_start": week_start.isoformat(),
        "week_events": len(weekly_rows),
        "agents": results,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"[aggregate] week_start={week_start.date()} events={len(weekly_rows)} agents={len(results)}")
        for r in results:
            status = "OK" if r["updated"] else "SKIP(no file)"
            print(f"  {r['agent']}: {r['weekly_tokens']} tokens / {r['weekly_cost_cny']} CNY / model={r['model_used']} [{status}]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V8 agent 成本事件流工具")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="agent-cost-events.jsonl 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="追加一条成本事件")
    append.add_argument("--task-id", required=True)
    append.add_argument("--agent", required=True)
    append.add_argument("--model", required=True)
    append.add_argument("--tokens", type=int)
    append.add_argument("--input-tokens", type=int)
    append.add_argument("--output-tokens", type=int)
    append.add_argument("--cached-input-tokens", type=int)
    append.add_argument("--reasoning-output-tokens", type=int)
    append.add_argument("--cost-cny", type=float, required=True)
    append.add_argument("--phase", choices=sorted(PHASES), required=True)
    append.add_argument("--source", default="manual")
    append.add_argument("--usage-source", default="manual")
    append.add_argument("--billing-status", choices=sorted(BILLING_STATUSES), default="not_connected")
    append.add_argument("--cost-source")
    append.add_argument("--note")
    append.add_argument("--ts")
    append.add_argument("--json", action="store_true")
    append.set_defaults(func=command_append)

    validate = sub.add_parser("validate", help="校验事件流")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=command_validate)

    summary = sub.add_parser("summary", help="汇总事件流")
    summary.add_argument("--task-id")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=command_summary)

    aggregate = sub.add_parser("aggregate", help="聚合本周成本到 Agent 状态文件")
    aggregate.add_argument("--json", action="store_true")
    aggregate.set_defaults(func=command_aggregate)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
