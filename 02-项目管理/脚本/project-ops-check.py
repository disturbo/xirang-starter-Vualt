#!/usr/bin/env python3
"""
project-ops-check.py - project management guardrail for task cards and run logs.

Usage:
  python3 02-项目管理/脚本/project-ops-check.py
  python3 02-项目管理/脚本/project-ops-check.py --today 2026-06-09 --strict
  python3 02-项目管理/脚本/project-ops-check.py --json          # 结构化输出（第一反射器用）

输出模式:
  默认       人类可读文本（[pass]/[warn]/[summary]），向后兼容旧 Handoff 验证命令。
  --json     结构化 JSON，统一 severity schema（p0/p1/advisory），供 V9 第一反射器消费。

severity 约定（统一 schema，与 gate-enforce / health-latest 对齐）:
  p0        阻断级，需立即处理 / @负责人
  p1        结构性问题，需修复（字段缺失、状态非法、in_progress 超期）
  advisory  提示级，不一定要动（日志缺 frontmatter、距上次开卡天数）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(".")
TASK_ROOT = ROOT / "02-项目管理" / "任务卡"
RUN_LOG_ROOT = ROOT / "02-项目管理" / "运行日志"

CHECK_NAME = "project-ops-check"

TASK_ID_RE = re.compile(r"^T-(\d{8})-\d{2}\.md$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

REQUIRED_KEYS = [
    "task_id",
    "title",
    "module",
    "min_level",
    "task_size",
    "owner",
    "status",
    "priority",
    "created_at",
    "updated_at",
    "completed_at",
    "sla",
    "budget",
    "paths",
    "deliverables",
    "gates",
]

ALLOWED_STATUS = {"ready", "in_progress", "done", "blocked", "cancelled"}
DONE_STATUSES = {"done", "cancelled"}

# 严重度顺序（用于汇总和退出码判定）
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}


class Findings:
    """收集结构化发现；按模式决定是否即时打印文本。"""

    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.items: list[dict] = []

    def add(self, severity: str, rule_id: str, obj: str, message: str) -> None:
        self.items.append(
            {"severity": severity, "rule_id": rule_id, "object": obj, "message": message}
        )
        if not self.json_mode:
            tag = "fail" if severity == "p0" else "warn"
            print(f"[{tag}] {message}")

    def ok(self, message: str) -> None:
        if not self.json_mode:
            print(f"[pass] {message}")

    def by_severity(self, severity: str) -> int:
        return sum(1 for it in self.items if it["severity"] == severity)

    def worst(self) -> str | None:
        if not self.items:
            return None
        return min((it["severity"] for it in self.items), key=lambda s: SEVERITY_ORDER.get(s, 9))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[4:end]


def fm_value(fm: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip('"').strip("'")


def has_fm_key(fm: str, key: str) -> bool:
    return re.search(rf"^{re.escape(key)}:", fm, re.MULTILINE) is not None


def parse_task_date(path: Path) -> date | None:
    match = TASK_ID_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def task_cards() -> list[Path]:
    if not TASK_ROOT.exists():
        return []
    return sorted(TASK_ROOT.glob("20??-??/T-*.md"))


def run_log_dates() -> set[date]:
    dates: set[date] = set()
    for path in RUN_LOG_ROOT.glob("20??-??-??.md"):
        try:
            dates.add(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return dates


def check_task_cards(f: Findings, today: date, max_gap_days: int, all_months: bool) -> None:
    cards = task_cards()

    if not cards:
        f.add("advisory", "NO_TASK_CARDS", str(TASK_ROOT), "没有找到任何任务卡；新建 M4/M5 任务后会自动进入巡检。")
        return

    latest_date = max(
        (parse_task_date(path) for path in cards if parse_task_date(path)), default=None
    )
    if latest_date:
        gap = (today - latest_date).days
        if gap > max_gap_days:
            f.add(
                "advisory",
                "TASK_CARD_GAP",
                latest_date.isoformat(),
                f"最近任务卡是 {latest_date}，距今天 {gap} 天；M4/M5 任务可能没有开卡。",
            )
        else:
            f.ok(f"最近任务卡日期 {latest_date}，在 {max_gap_days} 天阈值内。")

    current_month = today.strftime("%Y-%m")
    scoped = cards if all_months else [p for p in cards if p.parent.name == current_month]
    if not scoped:
        f.add("advisory", "NO_MONTH_CARD", current_month, f"当月 {current_month} 没有任务卡。")

    structural_before = len(f.items)
    for path in scoped:
        rel = str(path.relative_to(ROOT))
        task_date = parse_task_date(path)
        if task_date is None:
            f.add("p1", "BAD_FILENAME", rel, f"{rel}: 文件名不符合 T-YYYYMMDD-NN.md。")
            continue

        expected_month = task_date.strftime("%Y-%m")
        if path.parent.name != expected_month:
            f.add("p1", "WRONG_MONTH_DIR", rel, f"{rel}: 所在月份目录应为 {expected_month}。")

        text = read_text(path)
        fm = frontmatter(text)
        if not fm:
            f.add("p1", "MISSING_FM", rel, f"{rel}: 缺 frontmatter。")
            continue

        missing = [key for key in REQUIRED_KEYS if not has_fm_key(fm, key)]
        if missing:
            f.add("p1", "MISSING_FIELDS", rel, f"{rel}: 缺少字段 {', '.join(missing)}。")

        task_id = fm_value(fm, "task_id")
        if task_id and f"{task_id}.md" != path.name:
            f.add("p1", "TASKID_MISMATCH", rel, f"{rel}: task_id 与文件名不一致。")

        status = fm_value(fm, "status")
        if status and status not in ALLOWED_STATUS:
            f.add("p1", "BAD_STATUS", rel, f"{rel}: status={status} 不在 {sorted(ALLOWED_STATUS)}。")

        completed_at = fm_value(fm, "completed_at")
        if status in DONE_STATUSES and completed_at in {"", "null", "None"}:
            f.add("p1", "DONE_NO_COMPLETED", rel, f"{rel}: status={status} 但 completed_at 为空。")

        if status == "done":
            handoff = fm_value(fm, "handoff")
            if "handoff:" in fm and handoff and handoff != "passed":
                f.add("advisory", "HANDOFF_NOT_PASSED", rel, f"{rel}: done 任务的 gates.handoff 不是 passed。")

        if status == "in_progress" and (today - task_date).days > max_gap_days:
            f.add(
                "p1",
                "STALE_IN_PROGRESS",
                rel,
                f"{rel}: in_progress 已超过 {max_gap_days} 天，请关闭或改 blocked。",
            )

    if len(f.items) == structural_before:
        f.ok("任务卡结构检查通过。")


def check_run_logs(f: Findings, today: date, lookback_days: int) -> None:
    dates = run_log_dates()

    today_log = RUN_LOG_ROOT / f"{today.isoformat()}.md"
    if today not in dates:
        f.add("advisory", "TODAY_LOG_MISSING", str(today_log), f"今天缺运行日志：{today_log}")
    else:
        f.ok(f"今天运行日志存在：{today_log}")

    start = today - timedelta(days=lookback_days - 1)
    missing = []
    day = start
    while day <= today:
        if day not in dates:
            missing.append(day.isoformat())
        else:
            path = RUN_LOG_ROOT / f"{day.isoformat()}.md"
            text = read_text(path)
            if not frontmatter(text):
                f.add("advisory", "RUNLOG_NO_FM", str(path), f"{path}: 缺 frontmatter。")
            if "## 任务记录" not in text:
                f.add("advisory", "RUNLOG_NO_SECTION", str(path), f"{path}: 缺少 `## 任务记录` 小节。")
        day += timedelta(days=1)

    if missing:
        f.add(
            "advisory",
            "LOG_GAP",
            ",".join(missing),
            f"最近 {lookback_days} 天缺日志：{', '.join(missing)}。如当天无工作，不补写；有工作则补日志。",
        )
    else:
        f.ok(f"最近 {lookback_days} 天运行日志连续。")


def build_report(f: Findings, today: date) -> dict:
    return {
        "check": CHECK_NAME,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "summary": {
            "total": len(f.items),
            "p0": f.by_severity("p0"),
            "p1": f.by_severity("p1"),
            "advisory": f.by_severity("advisory"),
            "worst": f.worst(),
        },
        "findings": f.items,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--max-task-gap-days", type=int, default=2)
    parser.add_argument("--log-lookback-days", type=int, default=7)
    parser.add_argument("--all", action="store_true", help="check all task cards instead of current month")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when warnings exist")
    parser.add_argument("--json", action="store_true", help="emit structured JSON (V9 reflex consumer)")
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    f = Findings(json_mode=args.json)

    if not args.json:
        print(f"# Project ops check ({today})")
        print()

    check_task_cards(f, today, args.max_task_gap_days, args.all)
    check_run_logs(f, today, args.log_lookback_days)

    if args.json:
        json.dump(build_report(f, today), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print()
        if f.items:
            print(f"[summary] {len(f.items)} warning(s)")
        else:
            print("[summary] all checks passed")

    # 退出码：p0 始终非零；其余仅在 --strict 下非零
    if f.by_severity("p0") > 0:
        return 1
    if f.items and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
