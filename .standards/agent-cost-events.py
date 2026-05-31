#!/usr/bin/env python3
"""
agent-cost-events.py — V8 agent 成本事件流工具
v1.0 | 2026-05-17 | 息壤 V8.5.0

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
"""

from __future__ import annotations

import argparse
import json
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

    for key in ("task_id", "agent", "model", "source"):
        if key in row and not str(row.get(key)).strip():
            errors.append(f"line {line}: {key} 不能为空")

    return errors


def command_append(args: argparse.Namespace) -> int:
    event = {
        "type": "cost_event",
        "ts": args.ts or now_iso(),
        "task_id": args.task_id,
        "agent": args.agent,
        "model": args.model,
        "tokens": args.tokens,
        "cost_cny": args.cost_cny,
        "phase": args.phase,
        "source": args.source,
    }
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
    total_cost = sum(float(row.get("cost_cny") or 0) for row in rows)
    by_phase: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})
    by_agent: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost_cny": 0.0, "events": 0})
    models: Counter = Counter()

    for row in rows:
        tokens = int(row.get("tokens") or 0)
        cost = float(row.get("cost_cny") or 0)
        phase = str(row.get("phase") or "unknown")
        agent = str(row.get("agent") or "unknown")
        model = str(row.get("model") or "unknown")
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
        "cost_cny": round(total_cost, 4),
        "by_phase": {k: {"tokens": int(v["tokens"]), "cost_cny": round(v["cost_cny"], 4), "events": int(v["events"])} for k, v in sorted(by_phase.items())},
        "by_agent": {k: {"tokens": int(v["tokens"]), "cost_cny": round(v["cost_cny"], 4), "events": int(v["events"])} for k, v in sorted(by_agent.items())},
        "models": dict(models.most_common()),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"events={result['events']} tokens={result['tokens']} cost_cny={result['cost_cny']}")
        for phase, values in result["by_phase"].items():
            print(f"- {phase}: {values['tokens']} tokens / {values['cost_cny']} CNY / {values['events']} events")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V8 agent 成本事件流工具")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="agent-cost-events.jsonl 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="追加一条成本事件")
    append.add_argument("--task-id", required=True)
    append.add_argument("--agent", required=True)
    append.add_argument("--model", required=True)
    append.add_argument("--tokens", type=int, required=True)
    append.add_argument("--cost-cny", type=float, required=True)
    append.add_argument("--phase", choices=sorted(PHASES), required=True)
    append.add_argument("--source", default="manual")
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

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
