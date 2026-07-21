#!/usr/bin/env python3
"""
v9-task-state-check.py — V9.4 task review-state validator.

Scope:
  - Read-only scan for task-card review fields.
  - Does not change task cards, board, run logs, or reflex state.
  - Defaults to hard-check only cards that explicitly claim review_status=accepted.

This is the validator behind the V9.4 "submitted != accepted" state-machine design.
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
CHECK_NAME = "v9-task-state-check"
SOURCE = "task-state"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}

ALLOWED_REVIEW_STATUS = {
    "draft",
    "submitted",
    "reviewing",
    "changes_requested",
    "accepted",
    "rejected",
    "not_required",
}

NULL_VALUES = {"", "null", "none", "None", "NULL", "~"}
REVIEW_ENFORCE_FROM = date(2026, 6, 27)


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


def fm_value(fm: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    if not match:
        return ""
    return clean_value(match.group(1))


def clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


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


def normalize_actor(value: str) -> str:
    value = clean_value(value)
    value = re.sub(r"\s+", "", value)
    value = value.replace("（", "(").replace("）", ")")
    return value.lower()


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


def check_card(path: Path, strict_missing_review: bool) -> list[dict]:
    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    text = read_text(path)
    fm = frontmatter(text)
    if not fm:
        return []

    status = fm_value(fm, "status")
    review_status = fm_value(fm, "review_status")
    owner = fm_value(fm, "owner")
    author = fm_value(fm, "author") or owner
    reviewer = fm_value(fm, "reviewer")
    submitted_at = fm_value(fm, "submitted_at")
    accepted_by = fm_value(fm, "accepted_by")
    accepted_at = fm_value(fm, "accepted_at")
    acceptance_result = fm_value(fm, "acceptance_result")
    acceptance_note = fm_value(fm, "acceptance_note")
    created = parse_date(fm_value(fm, "created_at") or fm_value(fm, "created"))
    hard_enforce = bool(created and created >= REVIEW_ENFORCE_FROM)

    findings: list[dict] = []

    if not review_status:
        if strict_missing_review and status == "done":
            findings.append(
                make_finding(
                    "advisory",
                    "REVIEW_STATUS_MISSING",
                    rel,
                    f"{rel}: status=done 但缺 review_status（迁移期提示）。",
                )
            )
        return findings

    if review_status not in ALLOWED_REVIEW_STATUS:
        findings.append(
            make_finding(
                "p1",
                "REVIEW_STATUS_INVALID",
                rel,
                f"{rel}: review_status={review_status} 不在 {sorted(ALLOWED_REVIEW_STATUS)}。",
                {"review_status": review_status},
            )
        )
        return findings

    if review_status != "not_required" and is_null(reviewer):
        findings.append(
            make_finding(
                "p1" if hard_enforce and review_status in {"submitted", "reviewing", "changes_requested", "rejected", "accepted"} else "advisory",
                "REVIEWER_MISSING",
                rel,
                f"{rel}: review_status={review_status} 但 reviewer 为空；默认应为人工Reviewer。",
            )
        )

    if hard_enforce and review_status in {"submitted", "reviewing", "changes_requested", "rejected", "accepted"} and is_null(submitted_at):
        findings.append(
            make_finding(
                "p1",
                "SUBMITTED_AT_MISSING",
                rel,
                f"{rel}: review_status={review_status} 但 submitted_at 为空。",
                {"review_status": review_status},
            )
        )

    if review_status != "accepted":
        if not is_null(accepted_by):
            findings.append(
                make_finding(
                    "advisory",
                    "ACCEPTED_BY_WITHOUT_ACCEPTED",
                    rel,
                    f"{rel}: review_status={review_status} 但 accepted_by={accepted_by}，请确认验收口径。",
                )
            )
        if review_status in {"submitted", "reviewing"} and (not is_null(accepted_at) or not is_null(acceptance_result)):
            findings.append(
                make_finding(
                    "p1" if hard_enforce else "advisory",
                    "ACCEPTANCE_FIELDS_PREMATURE",
                    rel,
                    f"{rel}: review_status={review_status} 仍在评审中，但 accepted_at/acceptance_result 已写值。",
                    {"accepted_at": accepted_at or None, "acceptance_result": acceptance_result or None},
                )
            )
        if review_status == "changes_requested":
            if acceptance_result != "changes_requested":
                findings.append(
                    make_finding(
                        "p1" if hard_enforce else "advisory",
                        "ACCEPTANCE_RESULT_MISMATCH",
                        rel,
                        f"{rel}: review_status=changes_requested 但 acceptance_result={acceptance_result or 'null'}。",
                        {"acceptance_result": acceptance_result or None},
                    )
                )
            if is_null(acceptance_note):
                findings.append(
                    make_finding(
                        "p1" if hard_enforce else "advisory",
                        "ACCEPTANCE_NOTE_MISSING",
                        rel,
                        f"{rel}: review_status=changes_requested 但 acceptance_note 为空，无法接手修订。",
                    )
                )
        if review_status == "rejected":
            if acceptance_result != "rejected":
                findings.append(
                    make_finding(
                        "p1" if hard_enforce else "advisory",
                        "ACCEPTANCE_RESULT_MISMATCH",
                        rel,
                        f"{rel}: review_status=rejected 但 acceptance_result={acceptance_result or 'null'}。",
                        {"acceptance_result": acceptance_result or None},
                    )
                )
            if is_null(acceptance_note):
                findings.append(
                    make_finding(
                        "p1" if hard_enforce else "advisory",
                        "ACCEPTANCE_NOTE_MISSING",
                        rel,
                        f"{rel}: review_status=rejected 但 acceptance_note 为空。",
                    )
                )
        return findings

    if is_null(accepted_by):
        findings.append(
            make_finding(
                "p1",
                "ACCEPTED_BY_MISSING",
                rel,
                f"{rel}: review_status=accepted 但 accepted_by 为空。",
            )
        )
    else:
        accepted_norm = normalize_actor(accepted_by)
        owner_norm = normalize_actor(owner)
        author_norm = normalize_actor(author)
        if accepted_norm and accepted_norm in {owner_norm, author_norm}:
            findings.append(
                make_finding(
                    "p1",
                    "ACCEPTED_BY_SELF",
                    rel,
                    f"{rel}: review_status=accepted 但 accepted_by 与 owner/author 相同。",
                    {"owner": owner, "author": author, "accepted_by": accepted_by},
                )
            )

    if is_null(accepted_at):
        findings.append(
            make_finding(
                "p1",
                "ACCEPTED_AT_MISSING",
                rel,
                f"{rel}: review_status=accepted 但 accepted_at 为空。",
            )
        )

    if acceptance_result != "accepted":
        findings.append(
            make_finding(
                "p1",
                "ACCEPTANCE_RESULT_MISMATCH",
                rel,
                f"{rel}: review_status=accepted 但 acceptance_result={acceptance_result or 'null'}。",
                {"acceptance_result": acceptance_result or None},
            )
        )

    return findings


def review_debt(paths: list[Path]) -> dict:
    counts = {key: 0 for key in sorted(ALLOWED_REVIEW_STATUS)}
    counts["missing"] = 0
    done_missing = 0
    for path in paths:
        fm = frontmatter(read_text(path))
        if not fm:
            continue
        review_status = fm_value(fm, "review_status")
        status = fm_value(fm, "status")
        if review_status in counts:
            counts[review_status] += 1
        else:
            counts["missing"] += 1
            if status == "done":
                done_missing += 1
    return {
        "review_status_counts": counts,
        "done_missing_review_status": done_missing,
        "awaiting_review": sum(counts[key] for key in ("submitted", "reviewing", "changes_requested")),
        "accepted": counts["accepted"],
    }


def summarize(findings: list[dict], cards_scanned: int, debt: dict | None = None) -> dict:
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
        **(debt or {}),
    }


def build_report(paths: list[Path], strict_missing_review: bool) -> dict:
    findings: list[dict] = []
    for path in paths:
        findings.extend(check_card(path, strict_missing_review))
    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "summary": summarize(findings, len(paths), review_debt(paths)),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", help="指定任务卡路径；可重复。默认扫描全部任务卡。")
    parser.add_argument("--strict-missing-review", action="store_true", help="done 任务缺 review_status 时给 advisory。")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="发现 p0/p1 时退出码 1")
    args = parser.parse_args()

    if args.task:
        paths = [Path(p) for p in args.task]
    else:
        paths = task_cards(TASK_ROOT)

    report = build_report(paths, args.strict_missing_review)

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
