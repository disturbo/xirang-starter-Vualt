#!/usr/bin/env python3
"""
error-patterns.py — V8 错误模式库读取与注入助手
v1.0 | 2026-05-17 | 息壤 V8.5.0

用途：
  - validate: 校验 .standards/error-patterns.jsonl 的 schema 与 id 唯一性
  - list: 输出当前活跃错误模式
  - match: 按任务描述匹配最相关的错误模式，用于 spawn/prompt 注入

示例：
  python3 .standards/error-patterns.py validate
  python3 .standards/error-patterns.py match --query "外部 Agent 迁移 PRD" --limit 5 --format prompt
  python3 .standards/error-patterns.py list --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


VAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIBRARY = VAULT_ROOT / ".standards" / "error-patterns.jsonl"

REQUIRED_FIELDS = {
    "id",
    "pattern",
    "severity",
    "signals",
    "guardrail",
    "prevention",
    "inject_when",
    "source",
    "status",
}


def load_patterns(path: Path = DEFAULT_LIBRARY) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"错误模式库不存在: {path}")

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} JSON 解析失败: {exc}") from exc
        item["_line"] = line_no
        patterns.append(item)
    return patterns


def validate_patterns(patterns: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    valid_severity = {"P0", "P1", "P2", "P3"}
    valid_status = {"active", "deprecated"}

    for item in patterns:
        line = item.get("_line", "?")
        missing = sorted(REQUIRED_FIELDS - set(item))
        if missing:
            errors.append(f"line {line}: 缺字段 {', '.join(missing)}")

        pattern_id = item.get("id")
        if pattern_id in seen:
            errors.append(f"line {line}: id 重复 {pattern_id}")
        if pattern_id:
            seen.add(pattern_id)

        if item.get("severity") not in valid_severity:
            errors.append(f"line {line}: severity 非法 {item.get('severity')}")
        if item.get("status") not in valid_status:
            errors.append(f"line {line}: status 非法 {item.get('status')}")

        for list_key in ("signals", "inject_when"):
            if list_key in item and not isinstance(item[list_key], list):
                errors.append(f"line {line}: {list_key} 必须是数组")

    return errors


def words(text: str) -> set[str]:
    lowered = text.lower()
    ascii_words = set(re.findall(r"[a-z0-9_\-]+", lowered))
    chinese_terms = {
        term
        for term in [
            "外部",
            "迁移",
            "路径",
            "任务卡",
            "成本",
            "熔断",
            "预算",
            "重试",
            "评审",
            "规范",
            "原型",
            "品牌",
            "看板",
            "状态",
            "巡检",
            "交接",
            "handoff",
            "frontmatter",
            "emoji",
            "spawn",
            "agent",
            "m4",
            "m5",
        ]
        if term in lowered
    }
    return ascii_words | chinese_terms


def score_pattern(item: dict[str, Any], query: str) -> int:
    query_lower = query.lower()
    query_words = words(query)
    haystacks = [
        str(item.get("id", "")),
        str(item.get("pattern", "")),
        str(item.get("guardrail", "")),
        str(item.get("prevention", "")),
        " ".join(item.get("signals", [])),
        " ".join(item.get("inject_when", [])),
    ]
    haystack = " ".join(haystacks).lower()

    score = 0
    for token in query_words:
        if token and token in haystack:
            score += 3
    for marker in item.get("inject_when", []):
        marker_text = str(marker).lower()
        if marker_text and marker_text in query_lower:
            score += 5
    if item.get("severity") == "P0":
        score += 3
    elif item.get("severity") == "P1":
        score += 2
    return score


def active_patterns(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in patterns if p.get("status") == "active"]


def strip_internal(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def command_validate(args: argparse.Namespace) -> int:
    patterns = load_patterns(args.library)
    errors = validate_patterns(patterns)
    result = {
        "status": "fail" if errors else "pass",
        "library": str(args.library),
        "patterns": len(patterns),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("[FAIL] error-patterns 校验失败")
        for err in errors:
            print(f"- {err}")
    else:
        print(f"[PASS] error-patterns 校验通过: {len(patterns)} 条")
    return 1 if errors else 0


def command_list(args: argparse.Namespace) -> int:
    patterns = active_patterns(load_patterns(args.library))
    if args.json:
        print(json.dumps([strip_internal(p) for p in patterns], ensure_ascii=False, indent=2))
    else:
        for item in patterns:
            print(f"{item['id']} | {item['severity']} | {item['pattern']} | guardrail: {item['guardrail']}")
    return 0


def command_match(args: argparse.Namespace) -> int:
    patterns = active_patterns(load_patterns(args.library))
    ranked = sorted(
        ((score_pattern(item, args.query), item) for item in patterns),
        key=lambda pair: (-pair[0], pair[1].get("id", "")),
    )
    matched = [item for score, item in ranked if score > 0][: args.limit]
    if not matched:
        matched = sorted(patterns, key=lambda item: item.get("id", ""))[: args.limit]

    if args.json:
        print(json.dumps([strip_internal(p) for p in matched], ensure_ascii=False, indent=2))
    elif args.format == "prompt":
        print("请在执行前避开以下历史高频错误：")
        for item in matched:
            print(f"- {item['id']} [{item['severity']}] {item['pattern']}：{item['prevention']}")
    else:
        for item in matched:
            print(f"{item['id']} | {item['severity']} | {item['pattern']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V8 错误模式库读取与注入助手")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY, help="错误模式 JSONL 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验错误模式库")
    validate.add_argument("--json", action="store_true", help="输出 JSON")
    validate.set_defaults(func=command_validate)

    list_cmd = sub.add_parser("list", help="列出活跃错误模式")
    list_cmd.add_argument("--json", action="store_true", help="输出 JSON")
    list_cmd.set_defaults(func=command_list)

    match = sub.add_parser("match", help="按任务描述匹配错误模式")
    match.add_argument("--query", required=True, help="任务描述或 prompt 片段")
    match.add_argument("--limit", type=int, default=5, help="返回条数")
    match.add_argument("--format", choices=["plain", "prompt"], default="plain", help="输出格式")
    match.add_argument("--json", action="store_true", help="输出 JSON")
    match.set_defaults(func=command_match)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
