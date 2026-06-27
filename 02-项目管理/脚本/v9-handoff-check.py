#!/usr/bin/env python3
"""
v9-handoff-check.py — V9.4.2 handoff quality validator.

Scope:
  - Read-only scan for task cards that explicitly require handoff.
  - Validates that handoff content is present and minimally actionable.
  - Does not write task cards, board, run logs, or reflex state.

Migration policy:
  - `handoff_required: true` is always enforced.
  - New M5 done tasks created on/after 2026-06-27 are enforced.
  - Historical cards are not bulk-red by default; use --strict-historical to audit them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(".")
TASK_ROOT = ROOT / "02-项目管理" / "任务卡"
BOARD = ROOT / "00-MOC" / "多智能体协作看板.md"
CHECK_NAME = "v9-handoff-check"
SOURCE = "handoff"
ENFORCE_FROM = date(2026, 6, 27)
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}
NULL_VALUES = {"", "null", "none", "None", "NULL", "~", "[]"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[4:end]


def clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


def fm_value(fm: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    if not match:
        return ""
    return clean_value(match.group(1))


def is_null(value: str) -> bool:
    return clean_value(value) in NULL_VALUES


def parse_date(value: str) -> date | None:
    value = clean_value(value)
    if not value or value in NULL_VALUES:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def make_finding(severity: str, rule_id: str, obj: str, message: str, detail: dict | None = None) -> dict:
    finding = {
        "severity": severity,
        "rule_id": rule_id,
        "object": obj,
        "message": message,
        "source": SOURCE,
    }
    if detail:
        finding["detail"] = detail
    return finding


def task_cards(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("20??-??/T-*.md"))


def local_handoff_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    matches = list(re.finditer(r"^##+\s+.*handoff.*$|^##+\s+.*交接.*$", text, re.IGNORECASE | re.MULTILINE))
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        next_heading = re.search(r"\n##\s+\S", text[match.end():end])
        if next_heading:
            end = match.end() + next_heading.start()
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def board_handoff_block(task_id: str, board_text: str) -> str:
    if not board_text or task_id not in board_text:
        return ""
    idx = board_text.find(task_id)
    start = board_text.rfind("\n### ", 0, idx)
    if start == -1:
        start = max(0, idx - 800)
    end = board_text.find("\n### ", idx + len(task_id))
    if end == -1:
        end = min(len(board_text), idx + 2200)
    return board_text[start:end].strip()


def has_meaningful_value(block: str, labels: list[str]) -> bool:
    for label in labels:
        pattern = rf"^\s*[-*]?\s*{label}[^\n:：]{{0,24}}[:：]\s*(.+)$"
        for match in re.finditer(pattern, block, re.IGNORECASE | re.MULTILINE):
            value = match.group(1).strip()
            if value and value.lower() not in {"pending", "null", "none", "无", "待补", "待填写"}:
                return True
    return False


def has_context_hint(text: str, labels: list[str]) -> bool:
    for label in labels:
        if re.search(rf"^##+\s+.*{label}.*$", text, re.IGNORECASE | re.MULTILINE):
            return True
        if has_meaningful_value(text, [label]):
            return True
    return False


def handoff_quality(blocks: list[str], context_text: str) -> tuple[bool, list[str]]:
    if not blocks:
        return False, ["handoff"]
    merged = "\n\n".join(blocks)
    missing = []
    if not has_meaningful_value(merged, ["status", "状态"]):
        missing.append("status")
    if not (
        has_meaningful_value(merged, ["产物路径", "产物", "artifacts", "deliverables"])
        or has_context_hint(context_text, ["产物", "deliverables", "artifacts"])
    ):
        missing.append("artifacts")
    if not (
        has_meaningful_value(merged, ["验证结果", "验证", "验证命令", "自查", "tests", "verification"])
        or has_context_hint(context_text, ["验证", "自查", "tests", "verification"])
    ):
        missing.append("verification")
    if not has_meaningful_value(merged, ["next action", "next_action", "下一步"]):
        missing.append("next_action")
    return len(missing) == 0, missing


def requires_handoff(fm: str, strict_historical: bool) -> tuple[bool, bool, list[str]]:
    status = fm_value(fm, "status")
    min_level = fm_value(fm, "min_level")
    handoff_required = fm_value(fm, "handoff_required").lower()
    handoff_to = fm_value(fm, "handoff_to")
    created = parse_date(fm_value(fm, "created_at") or fm_value(fm, "created"))

    reasons: list[str] = []
    if status != "done":
        return False, False, []

    explicit = handoff_required == "true"
    if explicit:
        reasons.append("handoff_required")
    if status == "done" and min_level == "M5":
        if strict_historical or (created and created >= ENFORCE_FROM):
            reasons.append("m5_done")
    if status == "done" and not is_null(handoff_to):
        if strict_historical or explicit or (created and created >= ENFORCE_FROM):
            reasons.append("handoff_to")

    return bool(reasons), explicit or "m5_done" in reasons, reasons


def check_card(path: Path, board_text: str, strict_historical: bool) -> list[dict]:
    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    text = read_text(path)
    fm = frontmatter(text)
    if not fm:
        return []

    task_id = fm_value(fm, "task_id") or path.stem
    required, hard_required, reasons = requires_handoff(fm, strict_historical)
    if not required:
        return []

    blocks = local_handoff_blocks(text)
    board_block = board_handoff_block(task_id, board_text)
    if board_block:
        blocks.append(board_block)

    quality_ok, missing = handoff_quality(blocks, text)
    severity = "p1" if hard_required else "advisory"
    if not blocks:
        return [
            make_finding(
                severity,
                "HANDOFF_MISSING",
                rel,
                f"{rel}: 任务要求 Handoff，但任务卡和看板均未找到可接手交接块。",
                {"task_id": task_id, "reasons": reasons},
            )
        ]
    if not quality_ok:
        return [
            make_finding(
                severity,
                "HANDOFF_INCOMPLETE",
                rel,
                f"{rel}: Handoff 信息不完整，缺少 {', '.join(missing)}。",
                {"task_id": task_id, "reasons": reasons, "missing": missing},
            )
        ]
    return []


def summarize(findings: list[dict], cards_scanned: int) -> dict:
    def count(sev: str) -> int:
        return sum(1 for f in findings if f["severity"] == sev)

    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    return {
        "total": len(findings),
        "p0": count("p0"),
        "p1": count("p1"),
        "advisory": count("advisory"),
        "worst": worst,
        "cards_scanned": cards_scanned,
    }


def build_report(paths: list[Path], strict_historical: bool) -> dict:
    board_text = BOARD.read_text(encoding="utf-8") if BOARD.exists() else ""
    findings: list[dict] = []
    for path in paths:
        findings.extend(check_card(path, board_text, strict_historical))
    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "summary": summarize(findings, len(paths)),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", help="指定任务卡路径；可重复。默认扫描全部任务卡。")
    parser.add_argument("--strict-historical", action="store_true", help="同时审计历史 M5/handoff_to 卡。")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="发现 p0/p1 时退出码 1")
    args = parser.parse_args()

    paths = [Path(p) for p in args.task] if args.task else task_cards(TASK_ROOT)
    report = build_report(paths, args.strict_historical)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"# {CHECK_NAME}")
        print(f"summary: total={s['total']} p0={s['p0']} p1={s['p1']} advisory={s['advisory']} cards={s['cards_scanned']}")
        for finding in report["findings"]:
            print(f"[{finding['severity']}] {finding['rule_id']} | {finding['message']}")

    if args.strict and (report["summary"]["p0"] > 0 or report["summary"]["p1"] > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
