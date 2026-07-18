#!/usr/bin/env python3
"""
v9-harness-eval-runner.py — V9.4 harness mechanical regression runner.

Scope:
  - Runs deterministic positive and negative fixtures for existing harness tools.
  - Builds all fixtures in temporary directories; never mutates the production Vault.
  - Reports positive pass and negative block counts separately, so "all green"
    cannot hide a runner that never asserted known-bad behavior.

Current coverage:
  - project-ops-check.py task-card/run-log structural checks.
  - v9-starter-leak-check.py starter distribution leak checks.
  - v9-task-state-check.py submitted/accepted review-state checks.
  - v9-reflex-check.py source self-reporting for missing sources.
  - v9-reflex-check.py cooldown escalation behavior.
  - v9-scope-tamper-check.py write_scope authorization drift checks.
  - v9-handoff-check.py handoff presence/actionability checks.

Usage:
  python3 02-项目管理/脚本/v9-harness-eval-runner.py
  python3 02-项目管理/脚本/v9-harness-eval-runner.py --json
  python3 02-项目管理/脚本/v9-harness-eval-runner.py --write-latest
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


CHECK_NAME = "v9-harness-eval-runner"
TODAY = date(2026, 6, 27)
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "02-项目管理" / "脚本"
PROJECT_OPS = SCRIPT_DIR / "project-ops-check.py"
STARTER_LEAK = SCRIPT_DIR / "v9-starter-leak-check.py"
TASK_STATE = SCRIPT_DIR / "v9-task-state-check.py"
HANDOFF_CHECK = SCRIPT_DIR / "v9-handoff-check.py"
REFLEX = SCRIPT_DIR / "v9-reflex-check.py"
COST_EVENTS = REPO_ROOT / ".standards" / "agent-cost-events.py"
PHASE_G_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_g.py"
PHASE_H_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_h.py"
LATEST_REPORT = REPO_ROOT / "02-项目管理" / "巡检" / "harness-eval-latest.json"
TESTED_FILES = [
    Path(".codex/hooks.json"),
    Path(".standards/agent-cost-events.py"),
    Path(".standards/codex-cost-import.py"),
    Path(".standards/gate-enforce.py"),
    Path(".standards/harness-eval-verify.py"),
    Path(".standards/harness-tested-files.txt"),
    Path(".standards/tests/test_v9_phase_g.py"),
    Path(".standards/tests/test_v9_phase_h.py"),
    Path(".standards/hooks/codex-hook-adapter.py"),
    Path(".standards/hooks/pre-commit-harness-eval.sh"),
    Path(".standards/hooks/pre-write-hook.sh"),
    Path(".standards/v8-handshake.sh"),
    Path(".standards/v8-cost-observability.py"),
    Path(".standards/v9-accept.py"),
    Path("02-项目管理/脚本/project-ops-check.py"),
    Path("02-项目管理/脚本/v9-harness-eval-runner.py"),
    Path("02-项目管理/脚本/v9-handoff-check.py"),
    Path("02-项目管理/脚本/v9-entropy-governance.py"),
    Path("02-项目管理/脚本/v9-iteration-ops-check.py"),
    Path("02-项目管理/脚本/v9-reflex-check.py"),
    Path("02-项目管理/脚本/v9-scope-tamper-check.py"),
    Path("02-项目管理/脚本/v9-skill-shadow-check.py"),
    Path("02-项目管理/脚本/v9-starter-leak-check.py"),
    Path("02-项目管理/脚本/v9-status-summary.py"),
    Path("02-项目管理/脚本/v9-task-state-check.py"),
]


@dataclass
class EvalResult:
    case_id: str
    kind: str
    target: str
    passed: bool
    expected: str
    observed: str
    detail: dict

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "target": self.target,
            "passed": self.passed,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass
class EvalCase:
    case_id: str
    kind: str
    target: str
    description: str
    run: Callable[[], EvalResult]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_json(script: Path, args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, dict | None, str, str]:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    data = None
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
    return proc.returncode, data, proc.stdout, proc.stderr


def has_rule(report: dict, rule_id: str) -> bool:
    return any(item.get("rule_id") == rule_id for item in report.get("findings", []))


def blocking_count(report: dict) -> int:
    summary = report.get("summary", {})
    return int(summary.get("p0", 0)) + int(summary.get("p1", 0))


def gate_blocked(report: dict | None, rule_id: str) -> bool:
    """判定一条 gate/checker 报告是否以指定 rule 硬拦。兼容两种输出格式：
       - gate-enforce 原生：{p0_count, violations:[{rule_id}]}
       - checker（v9-*-check）：{summary:{p0,p1}, findings:[{rule_id}]}
    """
    if not report:
        return False
    if "violations" in report:
        return report.get("p0_count", 0) > 0 and any(v.get("rule_id") == rule_id for v in report.get("violations", []))
    return has_rule(report, rule_id) and blocking_count(report) > 0


def write_run_logs(root: Path, today: date = TODAY, lookback_days: int = 7) -> None:
    start = today - timedelta(days=lookback_days - 1)
    for offset in range(lookback_days):
        day = start + timedelta(days=offset)
        write_text(
            root / "02-项目管理" / "运行日志" / f"{day.isoformat()}.md",
            f"""---
date: {day.isoformat()}
type: 运行日志
tags: [运行日志, fixture]
---

# 运行日志 {day.isoformat()}

## 任务记录

- 09:00 | M0 | harness eval fixture | done | 临时样本日志
""",
        )


def task_card_text(task_id: str = "T-20260626-99", completed_at: str = "2026-06-26T09:00:00+08:00") -> str:
    return f"""---
task_id: {task_id}
title: "Harness Eval Fixture"
module: "Agent协作方法论"
min_level: M4
task_size: S
owner: "Codex"
status: done
priority: P2
created_at: 2026-06-26T08:00:00+08:00
updated_at: 2026-06-26T09:00:00+08:00
completed_at: {completed_at}
sla: {{}}
budget: {{}}
paths: []
deliverables: []
gates: {{}}
---

# Harness Eval Fixture

This card exists only inside a temporary eval fixture.
"""


def write_good_project_fixture(root: Path) -> None:
    write_run_logs(root)
    write_text(
        root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-99.md",
        task_card_text(),
    )


def case_project_ops_positive_clean() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-project-ok-") as tmp:
        root = Path(tmp)
        write_good_project_fixture(root)
        code, report, stdout, stderr = run_json(PROJECT_OPS, ["--today", TODAY.isoformat(), "--json"], root)
        passed = code == 0 and report is not None and blocking_count(report) == 0
        observed = "no p0/p1" if passed else "blocking findings or invalid json"
        return EvalResult(
            "project_ops_positive_clean",
            "positive",
            "project-ops-check.py",
            passed,
            "clean fixture produces zero p0/p1 findings",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_project_ops_negative_missing_frontmatter() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-project-badfm-") as tmp:
        root = Path(tmp)
        write_run_logs(root)
        write_text(
            root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-98.md",
            "# Missing frontmatter fixture\n",
        )
        code, report, stdout, stderr = run_json(PROJECT_OPS, ["--today", TODAY.isoformat(), "--json"], root)
        passed = report is not None and has_rule(report, "MISSING_FM") and blocking_count(report) > 0
        observed = "MISSING_FM blocked" if passed else "missing frontmatter was not blocked"
        return EvalResult(
            "project_ops_negative_missing_frontmatter",
            "negative",
            "project-ops-check.py",
            passed,
            "known-bad task card is rejected with MISSING_FM p1",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_project_ops_negative_done_without_completed() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-project-nodone-") as tmp:
        root = Path(tmp)
        write_run_logs(root)
        write_text(
            root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-97.md",
            task_card_text("T-20260626-97", completed_at="null"),
        )
        code, report, stdout, stderr = run_json(PROJECT_OPS, ["--today", TODAY.isoformat(), "--json"], root)
        passed = report is not None and has_rule(report, "DONE_NO_COMPLETED") and blocking_count(report) > 0
        observed = "DONE_NO_COMPLETED blocked" if passed else "done without completed_at was not blocked"
        return EvalResult(
            "project_ops_negative_done_without_completed",
            "negative",
            "project-ops-check.py",
            passed,
            "done task with completed_at=null is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_starter_leak_positive_clean() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-starter-ok-") as tmp:
        root = Path(tmp)
        write_text(root / "README.md", "# Xirang Starter\n\nClean generic starter fixture.\n")
        write_text(root / "00-MOC" / "知识管理规范.md", "公开骨架只保留通用知识管理规则。\n")
        code, report, stdout, stderr = run_json(STARTER_LEAK, ["--root", str(root), "--json"], REPO_ROOT)
        passed = code == 0 and report is not None and blocking_count(report) == 0
        observed = "no p0/p1" if passed else "clean starter reported blocking leak"
        return EvalResult(
            "starter_leak_positive_clean",
            "positive",
            "v9-starter-leak-check.py",
            passed,
            "clean starter fixture produces zero p0/p1 findings",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_starter_leak_negative_project_term() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-starter-term-") as tmp:
        root = Path(tmp)
        project_term = "D" + "MS"
        write_text(root / "00-MOC" / "知识管理规范.md", f"这里残留了 {project_term} 项目口径。\n")
        code, report, stdout, stderr = run_json(STARTER_LEAK, ["--root", str(root), "--json"], REPO_ROOT)
        passed = report is not None and has_rule(report, "PROJECT_TERM") and blocking_count(report) > 0
        observed = "PROJECT_TERM blocked" if passed else "project term leak was not blocked"
        return EvalResult(
            "starter_leak_negative_project_term",
            "negative",
            "v9-starter-leak-check.py",
            passed,
            "known-bad starter fixture is rejected with PROJECT_TERM p1",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_starter_leak_negative_secret() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-starter-secret-") as tmp:
        root = Path(tmp)
        secret_key = "app_" + "secret"
        secret_value = "abcdefghijklmnopqrstuvwxyz" + "123456"
        write_text(root / "config.json", json.dumps({secret_key: secret_value}, ensure_ascii=False) + "\n")
        code, report, stdout, stderr = run_json(STARTER_LEAK, ["--root", str(root), "--json"], REPO_ROOT)
        passed = report is not None and has_rule(report, "SECRET_JSON_APP_SECRET") and blocking_count(report) > 0
        observed = "SECRET_JSON_APP_SECRET blocked" if passed else "secret-shaped leak was not blocked"
        return EvalResult(
            "starter_leak_negative_secret",
            "negative",
            "v9-starter-leak-check.py",
            passed,
            "known-bad starter fixture is rejected with SECRET_JSON_APP_SECRET p1",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_cost_usage_positive_usage_only() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-cost-usage-ok-") as tmp:
        log = Path(tmp) / "agent-cost-events.jsonl"
        code, report, stdout, stderr = run_json(
            COST_EVENTS,
            [
                "--log", str(log),
                "append",
                "--task-id", "T-COST-USAGE",
                "--agent", "codex",
                "--model", "gpt-5",
                "--input-tokens", "120",
                "--output-tokens", "30",
                "--cost-cny", "0",
                "--phase", "execution",
                "--source", "eval",
                "--usage-source", "api_usage",
                "--billing-status", "usage_only",
                "--json",
            ],
            REPO_ROOT,
        )
        s_code, summary, s_stdout, s_stderr = run_json(
            COST_EVENTS,
            ["--log", str(log), "summary", "--task-id", "T-COST-USAGE", "--json"],
            REPO_ROOT,
        )
        passed = (
            code == 0
            and report is not None
            and s_code == 0
            and summary is not None
            and summary.get("tokens") == 150
            and summary.get("input_tokens") == 120
            and summary.get("output_tokens") == 30
            and summary.get("billing_statuses", {}).get("usage_only") == 1
        )
        observed = "usage-only token event appended and summarized" if passed else "usage-only token event failed"
        return EvalResult(
            "cost_usage_positive_usage_only",
            "positive",
            "agent-cost-events.py",
            passed,
            "input/output usage tokens can be recorded without pretending CNY billing is connected",
            observed,
            {
                "append_returncode": code,
                "summary_returncode": s_code,
                "append_stderr": stderr,
                "summary_stderr": s_stderr,
                "summary": summary,
                "stdout_head": (stdout + s_stdout)[:200],
            },
        )


def case_cost_usage_negative_connected_without_source() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-cost-connected-bad-") as tmp:
        log = Path(tmp) / "agent-cost-events.jsonl"
        code, report, stdout, stderr = run_json(
            COST_EVENTS,
            [
                "--log", str(log),
                "append",
                "--task-id", "T-COST-BAD",
                "--agent", "codex",
                "--model", "gpt-5",
                "--tokens", "10",
                "--cost-cny", "0.01",
                "--phase", "execution",
                "--source", "eval",
                "--usage-source", "api_usage",
                "--billing-status", "connected",
                "--json",
            ],
            REPO_ROOT,
        )
        passed = code == 2 and "cost_source" in stderr
        observed = "connected billing without cost_source rejected" if passed else "connected billing without cost_source was accepted"
        return EvalResult(
            "cost_usage_negative_connected_without_source",
            "negative",
            "agent-cost-events.py",
            passed,
            "billing_status=connected requires explicit platform cost source",
            observed,
            {"returncode": code, "stderr_head": stderr[:200], "stdout_head": stdout[:200], "report": report},
        )


def task_state_card(review_status: str, accepted_by: str, accepted_at: str, acceptance_result: str, owner: str = "Codex") -> str:
    return f"""---
task_id: T-20260627-96
title: "Task State Fixture"
module: "Agent协作方法论"
min_level: M4
task_size: S
owner: "{owner}"
author: "{owner}"
status: done
review_status: {review_status}
reviewer: "人工Reviewer"
submitted_at: 2026-06-27T09:00:00+08:00
accepted_by: {accepted_by}
accepted_at: {accepted_at}
acceptance_result: {acceptance_result}
acceptance_note: ""
priority: P2
created_at: 2026-06-27T08:00:00+08:00
updated_at: 2026-06-27T09:00:00+08:00
completed_at: 2026-06-27T09:00:00+08:00
sla: {{}}
budget: {{}}
paths: []
deliverables: []
gates: {{}}
---

# Task State Fixture
"""


def case_task_state_positive_submitted() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-ok-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-96.md"
        write_text(path, task_state_card("submitted", "null", "null", "null"))
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = code == 0 and report is not None and blocking_count(report) == 0
        observed = "submitted review state allowed" if passed else "submitted state produced blocking finding"
        return EvalResult(
            "task_state_positive_submitted",
            "positive",
            "v9-task-state-check.py",
            passed,
            "done + review_status=submitted + accepted_by=null produces zero p0/p1 findings",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_task_state_negative_self_accept() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-self-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-96.md"
        write_text(path, task_state_card("accepted", '"Codex"', "2026-06-26T10:00:00+08:00", "accepted"))
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = gate_blocked(report, "ACCEPTED_BY_SELF")
        observed = "ACCEPTED_BY_SELF blocked" if passed else "self-accept was not blocked"
        return EvalResult(
            "task_state_negative_self_accept",
            "negative",
            "v9-task-state-check.py",
            passed,
            "accepted_by equal to owner/author is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_task_state_negative_missing_acceptor() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-missing-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260626-96.md"
        write_text(path, task_state_card("accepted", "null", "2026-06-26T10:00:00+08:00", "accepted"))
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = report is not None and has_rule(report, "ACCEPTED_BY_MISSING") and blocking_count(report) > 0
        observed = "ACCEPTED_BY_MISSING blocked" if passed else "accepted without acceptor was not blocked"
        return EvalResult(
            "task_state_negative_missing_acceptor",
            "negative",
            "v9-task-state-check.py",
            passed,
            "review_status=accepted requires accepted_by",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_task_state_positive_reviewing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-reviewing-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-96.md"
        write_text(path, task_state_card("reviewing", "null", "null", "null"))
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = code == 0 and report is not None and blocking_count(report) == 0
        observed = "reviewing state allowed" if passed else "reviewing state produced blocking finding"
        return EvalResult(
            "task_state_positive_reviewing",
            "positive",
            "v9-task-state-check.py",
            passed,
            "reviewing state with reviewer/submitted_at and null acceptance fields stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_task_state_negative_submitted_missing_submitted_at() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-nosubmitted-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-95.md"
        text = task_state_card("submitted", "null", "null", "null").replace(
            "submitted_at: 2026-06-27T09:00:00+08:00\n", "submitted_at: null\n"
        )
        write_text(path, text)
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = report is not None and has_rule(report, "SUBMITTED_AT_MISSING") and blocking_count(report) > 0
        observed = "SUBMITTED_AT_MISSING blocked" if passed else "submitted without submitted_at was not blocked"
        return EvalResult(
            "task_state_negative_submitted_missing_submitted_at",
            "negative",
            "v9-task-state-check.py",
            passed,
            "new submitted review state requires submitted_at",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_task_state_negative_changes_requested_without_note() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-task-state-change-note-") as tmp:
        root = Path(tmp)
        path = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-94.md"
        write_text(path, task_state_card("changes_requested", "null", "null", "changes_requested"))
        code, report, stdout, stderr = run_json(TASK_STATE, ["--json"], root)
        passed = report is not None and has_rule(report, "ACCEPTANCE_NOTE_MISSING") and blocking_count(report) > 0
        observed = "ACCEPTANCE_NOTE_MISSING blocked" if passed else "changes_requested without note was not blocked"
        return EvalResult(
            "task_state_negative_changes_requested_without_note",
            "negative",
            "v9-task-state-check.py",
            passed,
            "changes_requested requires acceptance_note so the author can act",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def handoff_card(body: str) -> str:
    return f"""---
task_id: T-20260627-HO
title: "Handoff Fixture"
module: "Agent协作方法论"
min_level: M5
task_size: L
owner: "Codex"
author: "Codex"
status: done
review_status: submitted
reviewer: "人工Reviewer"
submitted_at: 2026-06-27T09:00:00+08:00
accepted_by: null
accepted_at: null
acceptance_result: null
handoff_required: true
handoff_to: "人工Reviewer"
priority: P1
created_at: 2026-06-27T08:00:00+08:00
updated_at: 2026-06-27T09:00:00+08:00
completed_at: 2026-06-27T09:00:00+08:00
sla: {{}}
budget: {{}}
paths: []
deliverables: []
gates: {{}}
---

# Handoff Fixture

{body}
"""


def case_handoff_positive_actionable() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-handoff-ok-") as tmp:
        root = Path(tmp)
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-HO.md"
        write_text(card, handoff_card("""## 5. Handoff 记录

- owner: Codex
- status: done/submitted
- 产物路径: `02-项目管理/脚本/v9-handoff-check.py`
- 验证结果: `v9-handoff-check.py --json` 通过
- next action: 人工Reviewer终审
- blocked by: 无
"""))
        code, report, stdout, stderr = run_json(HANDOFF_CHECK, ["--json"], root)
        passed = code == 0 and report is not None and blocking_count(report) == 0
        observed = "actionable handoff accepted" if passed else "actionable handoff was flagged"
        return EvalResult(
            "handoff_positive_actionable",
            "positive",
            "v9-handoff-check.py",
            passed,
            "handoff_required done task with actionable handoff produces zero p0/p1",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_handoff_negative_missing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-handoff-missing-") as tmp:
        root = Path(tmp)
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-HO.md"
        write_text(card, handoff_card("## 4. 中间产物\n\n无 Handoff。\n"))
        code, report, stdout, stderr = run_json(HANDOFF_CHECK, ["--json"], root)
        passed = report is not None and has_rule(report, "HANDOFF_MISSING") and blocking_count(report) > 0
        observed = "missing handoff blocked" if passed else "missing handoff was not blocked"
        return EvalResult(
            "handoff_negative_missing",
            "negative",
            "v9-handoff-check.py",
            passed,
            "handoff_required done task without handoff is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_handoff_negative_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-handoff-incomplete-") as tmp:
        root = Path(tmp)
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-20260627-HO.md"
        write_text(card, handoff_card("""## 5. Handoff 记录

- owner: Codex
- status: done
- blocked by: 无
"""))
        code, report, stdout, stderr = run_json(HANDOFF_CHECK, ["--json"], root)
        passed = report is not None and has_rule(report, "HANDOFF_INCOMPLETE") and blocking_count(report) > 0
        observed = "incomplete handoff blocked" if passed else "incomplete handoff was not blocked"
        return EvalResult(
            "handoff_negative_incomplete",
            "negative",
            "v9-handoff-check.py",
            passed,
            "handoff missing artifacts/verification/next_action is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_reflex_negative_missing_sources_visible() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-reflex-missing-") as tmp:
        root = Path(tmp)
        runtime_root = root / ".v9-runtime"
        env = dict(os.environ)
        env.update({
            "XIRANG_V9_RUNTIME_DIR": str(runtime_root),
            "XIRANG_GBRAIN_CLI": "/usr/bin/false",
            "XIRANG_OLLAMA_CLI": "/usr/bin/false",
            "XIRANG_CRONTAB": "/usr/bin/false",
            "XIRANG_GBRAIN_CONTRACT_VERIFY": "/usr/bin/false",
            "XIRANG_LLM_WIKI_CHECKER": "/usr/bin/false",
            "XIRANG_SKILL_SHADOW_CHECKER": "/usr/bin/false",
            "XIRANG_GBRAIN_SYNC_STATE": str(root / "missing-sync.json"),
            "XIRANG_GBRAIN_DREAM_STATE": str(root / "missing-dream.json"),
        })
        proc = subprocess.run(
            [sys.executable, str(REFLEX), "--today", TODAY.isoformat(), "--quiet"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        report_path = runtime_root / "巡检" / "health-latest.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        failed_sources = set(report.get("sources_failed", []))
        expected_missing = {
            "project-ops",
            "agent-state",
            "policy-conflict",
            "starter-leak",
            "task-state",
            "scope-tamper",
            "handoff",
            "iteration-ops",
        }
        passed = proc.returncode == 0 and expected_missing.issubset(failed_sources)
        observed = "missing sources surfaced" if passed else "missing sources were silent"
        return EvalResult(
            "reflex_negative_missing_sources_visible",
            "negative",
            "v9-reflex-check.py",
            passed,
            "empty fixture vault reports missing sources in sources_failed",
            observed,
            {
                "returncode": proc.returncode,
                "sources_run": report.get("sources_run"),
                "sources_failed": sorted(failed_sources),
                "stderr": proc.stderr,
            },
        )


def load_reflex_module():
    spec = importlib.util.spec_from_file_location("v9_reflex_check", REFLEX)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {REFLEX}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_reflex_negative_cooldown_escalation_visible() -> EvalResult:
    module = load_reflex_module()
    now = datetime(2026, 6, 26, 9, 0, 0, tzinfo=timezone.utc)
    state = {
        "RULE:fixture": {
            "first_seen": now.isoformat(timespec="seconds"),
            "last_reported": now.isoformat(timespec="seconds"),
            "count": 1,
            "severity": "advisory",
        }
    }
    finding = {
        "severity": "p1",
        "rule_id": "RULE",
        "object": "fixture",
        "message": "fixture escalated",
        "source": "eval",
    }
    module.apply_cooldown([finding], now, 24, state)
    passed = finding.get("suppressed") is False and finding.get("active_reason") == "escalated"
    observed = "escalation pierced cooldown" if passed else "escalation was suppressed"
    return EvalResult(
        "reflex_negative_cooldown_escalation_visible",
        "negative",
        "v9-reflex-check.py",
        passed,
        "severity upgrade inside cooldown remains active with active_reason=escalated",
        observed,
        {"finding": finding},
    )


# ===== V9.4.1 RED fixtures（承重墙未动 → 预期全部 RED）=====
# 钉住 v1.1 设计的"硬拦/事后检测"契约。实施前应全红；实施后该转绿。
# 本轮（T-20260626-84）只加这些 fixture，不实现门禁。
GATE_ENFORCE = REPO_ROOT / ".standards" / "gate-enforce.py"
SCOPE_TAMPER = SCRIPT_DIR / "v9-scope-tamper-check.py"


def _accept_candidate_card(owner: str, author: str, accepted_by: str) -> str:
    # 结构完整的合法任务卡：除"自验收"外不应触发任何其它门禁，
    # 确保 hook 若放行=候选 accept 校验未实现（真 RED），而非缺字段误拦（假绿）。
    return "\n".join([
        "---",
        "task_id: T-EVAL-ACCEPT",
        'title: "eval accept candidate"',
        "type: task_card",
        'module: "Agent协作方法论"',
        "min_level: M4",
        "task_size: S",
        f'owner: "{owner}"',
        f'author: "{author}"',
        "status: done",
        "review_status: accepted",
        'reviewer: "人工Reviewer"',
        f"accepted_by: {accepted_by}",
        'accepted_at: "2026-06-26T09:00:00+08:00"',
        "acceptance_result: accepted",
        "priority: P1",
        "created_at: 2026-06-26T09:00:00+08:00",
        "updated_at: 2026-06-26T09:00:00+08:00",
        'completed_at: "2026-06-26T09:00:00+08:00"',
        "sla: {target_hours: 1, hard_deadline: null}",
        "budget: {max_total_tokens: 1000, max_subagent_tokens: 0, cost_ceiling_cny: 1.0, on_exceed: alert_openclaw}",
        'paths: {allowed_write_roots: ["02-项目管理/"], temp_root: _temp/T-EVAL-ACCEPT/}',
        "deliverables: []",
        "gates: {pre_start: passed, pre_write: passed, cost_fuse: passed, handoff: passed}",
        "---",
        "# eval accept candidate",
        "",
    ])


def case_accept_gate_negative_edit_bare_accept() -> EvalResult:
    cid, kind, target = "accept_gate_negative_edit_bare_accept", "negative", "gate-enforce.py pre-accept"
    exp = "Edit 重建出的 self-accept 候选卡被 pre-accept 硬拦"
    with tempfile.TemporaryDirectory(prefix="v9-eval-accept-edit-") as tmp:
        cand = Path(tmp) / "candidate.md"
        write_text(cand, _accept_candidate_card("claudian", "claudian", "claudian"))
        code, report, _out, stderr = run_json(
            GATE_ENFORCE, ["pre-accept", "--candidate", str(cand), "--source", "edit", "--json"], cwd=REPO_ROOT
        )
        passed = gate_blocked(report, "ACCEPTED_BY_SELF")
        observed = "edit self-accept blocked" if passed else "pre-accept 未实现/未拦（预期 RED，待实施）"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": stderr[:160]})


def case_accept_gate_negative_write_full_accept() -> EvalResult:
    cid, kind, target = "accept_gate_negative_write_full_accept", "negative", "gate-enforce.py pre-accept"
    exp = "Write 全量覆盖的 self-accept 候选卡被 pre-accept 硬拦"
    with tempfile.TemporaryDirectory(prefix="v9-eval-accept-write-") as tmp:
        cand = Path(tmp) / "candidate.md"
        write_text(cand, _accept_candidate_card("claudian", "claudian", "claudian"))
        code, report, _out, stderr = run_json(
            GATE_ENFORCE, ["pre-accept", "--candidate", str(cand), "--source", "write", "--json"], cwd=REPO_ROOT
        )
        passed = gate_blocked(report, "ACCEPTED_BY_SELF")
        observed = "write self-accept blocked" if passed else "pre-accept 未实现/未拦（预期 RED，待实施）"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": stderr[:160]})


def case_scope_tamper_negative_bash_writescope() -> EvalResult:
    cid, kind, target = "scope_tamper_negative_bash_writescope", "negative", "v9-scope-tamper-check.py"
    # 授权基线：任务卡只授权 02-项目管理/脚本/，状态文件却被 Bash 改成 ./（全 vault）。
    # detector 必须比较"任务授权范围 vs 状态文件实际 scope"，而非"见到 ./ 就报错"。
    exp = "write_scope(./ 全vault) 超出任务授权(脚本/) → SCOPE_ESCALATION"
    if not SCOPE_TAMPER.exists():
        return EvalResult(cid, kind, target, False, exp,
                          "检测器 v9-scope-tamper-check.py 未建（预期 RED，待实施）", {"detector_exists": False})
    with tempfile.TemporaryDirectory(prefix="v9-eval-scope-tamper-") as tmp:
        root = Path(tmp)
        card = root / "_temp" / "T-EVAL" / "task-card.yaml"
        card.parent.mkdir(parents=True, exist_ok=True)
        write_text(card, 'task_id: T-EVAL\nauthorized_paths:\n  - "02-项目管理/脚本/"\n')
        st = root / "02-项目管理" / "智能体状态" / "Claudian.md"
        st.parent.mkdir(parents=True, exist_ok=True)
        write_text(st, '---\nagent_id: claudian\nstatus: busy\ncurrent_task_id: "T-EVAL"\n'
                       'write_scope: "./"\nscope_source: task_card\n---\n')
        code, report, _out, _err = run_json(
            SCOPE_TAMPER, ["--status", str(st), "--task-root", str(root / "_temp"), "--json"], cwd=REPO_ROOT)
        passed = report is not None and has_rule(report, "SCOPE_ESCALATION") and blocking_count(report) > 0
        observed = "越权扩张(授权 vs 实际)被检测" if passed else "未检测到越权扩张（预期 RED）"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code})


def case_eval_freshness_negative_stale_report() -> EvalResult:
    cid, kind, target = "eval_freshness_negative_stale_report", "negative", "gate-enforce.py pre-accept --require-fresh-eval"
    exp = "拿旧 eval 报告验收 harness 改动被新鲜度校验硬拦"
    with tempfile.TemporaryDirectory(prefix="v9-eval-fresh-") as tmp:
        stale = Path(tmp) / "harness-eval-latest.json"
        write_text(stale, json.dumps(
            {"check": "v9-harness-eval-runner", "generated_at": "2026-06-01T00:00:00+08:00", "summary": {"failed": 0}},
            ensure_ascii=False))
        code, report, _out, stderr = run_json(
            GATE_ENFORCE,
            ["pre-accept", "--task-id", "T-EVAL", "--require-fresh-eval", "--eval-report", str(stale), "--json"],
            cwd=REPO_ROOT,
        )
        passed = gate_blocked(report, "STALE_EVAL")
        observed = "stale eval blocked" if passed else "新鲜度校验未实现（预期 RED，待实施）"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": stderr[:160]})


# ---- 补充 RED fixture（吸收人工Reviewer检核：hook 层候选解析 / well-formed stale）----
HOOK = REPO_ROOT / ".standards" / "hooks" / "pre-write-hook.sh"


def _mini_vault(tmp: Path) -> Path:
    """最小可跑 hook 的临时 vault：symlink .standards + busy agent + scope 覆盖任务卡目录。"""
    os.symlink(REPO_ROOT / ".standards", tmp / ".standards")
    project_dir = tmp / "02-项目管理"
    project_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(REPO_ROOT / "02-项目管理" / "脚本", project_dir / "脚本")
    sd = project_dir / "智能体状态"
    sd.mkdir(parents=True, exist_ok=True)
    write_text(sd / "Claudian.md",
               '---\nagent_id: claudian\nstatus: busy\ncurrent_task_id: "T-EVAL"\n'
               'write_scope: "02-项目管理/"\nscope_source: task_card\n---\n')
    return tmp


def run_hook(payload: dict, vault_root: Path, timeout: int = 60) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["VAULT_ROOT"] = str(vault_root)
    env["V8_AGENT_ID"] = "claudian"
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def case_accept_hook_negative_edit_payload() -> EvalResult:
    cid, kind, target = "accept_hook_negative_edit_payload", "negative", "pre-write-hook.sh (Edit 候选重建)"
    exp = "Edit payload 翻 review_status→accepted(self) 经 hook 候选重建后被硬拦(exit 2)"
    with tempfile.TemporaryDirectory(prefix="v9-eval-hook-edit-") as tmp:
        root = _mini_vault(Path(tmp))
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-EVAL-ACCEPT.md"
        # 旧卡：合法且 submitted（accepted_by 已是 owner）；Edit 只翻 review_status 即构成 self-accept
        write_text(card, _accept_candidate_card("claudian", "claudian", "claudian").replace(
            "review_status: accepted", "review_status: submitted"))
        payload = {"tool_name": "Edit", "tool_input": {
            "file_path": str(card),
            "old_string": "review_status: submitted",
            "new_string": "review_status: accepted"}}
        code, _out, err = run_hook(payload, root)
        passed = code == 2 and "ACCEPTED_BY_SELF" in err
        observed = "edit 候选被 hook 硬拦" if passed else f"hook 放行(exit={code})—候选解析未实现(预期 RED)"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": err[:160]})


def case_accept_hook_negative_write_payload() -> EvalResult:
    cid, kind, target = "accept_hook_negative_write_payload", "negative", "pre-write-hook.sh (Write 全量 content)"
    exp = "Write payload 全量写 accepted(self) 经 hook 取 content 后被硬拦(exit 2)"
    with tempfile.TemporaryDirectory(prefix="v9-eval-hook-write-") as tmp:
        root = _mini_vault(Path(tmp))
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-EVAL-ACCEPT.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tool_name": "Write", "tool_input": {
            "file_path": str(card),
            "content": _accept_candidate_card("claudian", "claudian", "claudian")}}
        code, _out, err = run_hook(payload, root)
        passed = code == 2 and "ACCEPTED_BY_SELF" in err
        observed = "write content 被 hook 硬拦" if passed else f"hook 放行(exit={code})—content 解析未实现(预期 RED)"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": err[:160]})


def case_accept_command_positive_valid_reviewer() -> EvalResult:
    cid, kind, target = "accept_command_positive_valid_reviewer", "positive", "v9-accept.py"
    exp = "合法 v9_accept 经候选校验后原子写 accepted 状态"
    with tempfile.TemporaryDirectory(prefix="v9-eval-accept-command-") as tmp:
        root = _mini_vault(Path(tmp))
        card = root / "02-项目管理" / "任务卡" / "2026-06" / "T-EVAL-ACCEPT.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        old = _accept_candidate_card("claudian", "claudian", "null")
        old = old.replace("review_status: accepted", "review_status: submitted")
        old = old.replace('accepted_at: "2026-06-26T09:00:00+08:00"', "accepted_at: null")
        old = old.replace("acceptance_result: accepted", "acceptance_result: null")
        write_text(card, old)
        env = dict(os.environ)
        env["VAULT_ROOT"] = str(root)
        proc = subprocess.run(
            [
                "bash",
                "-lc",
                'source "$VAULT_ROOT/.standards/v8-handshake.sh"; v9_accept T-EVAL-ACCEPT 人工Reviewer --json',
            ],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=60,
        )
        new_text = card.read_text(encoding="utf-8")
        passed = (
            proc.returncode == 0
            and "review_status: accepted" in new_text
            and 'accepted_by: "人工Reviewer"' in new_text
            and "acceptance_result: accepted" in new_text
        )
        observed = "v9_accept 合法验收写回" if passed else "v9_accept 未能完成合法验收"
        return EvalResult(
            cid, kind, target, passed, exp, observed,
            {"returncode": proc.returncode, "stdout_head": proc.stdout[:200], "stderr_head": proc.stderr[:160]},
        )


def case_eval_freshness_negative_stale_hash() -> EvalResult:
    cid, kind, target = "eval_freshness_negative_stale_hash", "negative", "gate-enforce.py pre-accept --require-fresh-eval"
    exp = "well-formed 但 hash/mtime 过期的 eval 报告被新鲜度校验硬拦"
    with tempfile.TemporaryDirectory(prefix="v9-eval-fresh-hash-") as tmp:
        stale = Path(tmp) / "harness-eval-latest.json"
        # 结构完整、字段齐全，但 tested_hashes 是过期值（区别于"缺字段"那条）
        write_text(stale, json.dumps({
            "check": "v9-harness-eval-runner",
            "generated_at": "2026-06-26T23:15:00+08:00",
            "summary": {"failed": 0, "passed": 11},
            "tested_hashes": {
                ".standards/gate-enforce.py": "deadbeef-stale-hash",
                ".standards/hooks/pre-write-hook.sh": "deadbeef-stale-hash",
                ".standards/v9-accept.py": "deadbeef-stale-hash",
            },
            "tested_max_mtime": "2026-06-26T23:15:00+08:00",
        }, ensure_ascii=False))
        code, report, _out, err = run_json(
            GATE_ENFORCE,
            ["pre-accept", "--task-id", "T-EVAL", "--require-fresh-eval", "--eval-report", str(stale), "--json"],
            cwd=REPO_ROOT)
        passed = gate_blocked(report, "STALE_EVAL")
        observed = "stale-hash eval blocked" if passed else "hash 新鲜度校验未实现(预期 RED)"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": err[:160]})


def case_phase_g_positive_distribution_truth() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_G_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 3 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "phase_g_positive_distribution_truth", "positive",
        "skill resolution + portable Codex adapters", passed,
        "Phase G shadow rejection, explicit variants, and portable paths all pass",
        "3/3 Phase G distribution tests passed" if passed else "Phase G regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def case_phase_h_positive_long_session_stability() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_H_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 3 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "phase_h_positive_long_session_stability", "positive",
        "incremental Codex cost cursor + system-Python hook runtime", passed,
        "Phase H incremental, truncation, partial-line, and interpreter regressions all pass",
        "3/3 Phase H long-session tests passed" if passed else "Phase H regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )
def cases() -> list[EvalCase]:
    return [
        EvalCase(
            "phase_h_positive_long_session_stability", "positive",
            "incremental Codex cost cursor + system-Python hook runtime",
            "Phase H long-session runtime stability regression suite passes.",
            case_phase_h_positive_long_session_stability,
        ),
        EvalCase(
            "phase_g_positive_distribution_truth", "positive",
            "skill resolution + portable Codex adapters",
            "Phase G distribution and resolution regression suite passes.",
            case_phase_g_positive_distribution_truth,
        ),
        EvalCase(
            "project_ops_positive_clean",
            "positive",
            "project-ops-check.py",
            "Clean task-card/run-log fixture stays green.",
            case_project_ops_positive_clean,
        ),
        EvalCase(
            "project_ops_negative_missing_frontmatter",
            "negative",
            "project-ops-check.py",
            "Task card without frontmatter is blocked.",
            case_project_ops_negative_missing_frontmatter,
        ),
        EvalCase(
            "project_ops_negative_done_without_completed",
            "negative",
            "project-ops-check.py",
            "Done task with completed_at=null is blocked.",
            case_project_ops_negative_done_without_completed,
        ),
        EvalCase(
            "starter_leak_positive_clean",
            "positive",
            "v9-starter-leak-check.py",
            "Clean starter fixture stays green.",
            case_starter_leak_positive_clean,
        ),
        EvalCase(
            "starter_leak_negative_project_term",
            "negative",
            "v9-starter-leak-check.py",
            "Starter fixture with project term is blocked.",
            case_starter_leak_negative_project_term,
        ),
        EvalCase(
            "starter_leak_negative_secret",
            "negative",
            "v9-starter-leak-check.py",
            "Starter fixture with secret-shaped value is blocked.",
            case_starter_leak_negative_secret,
        ),
        EvalCase(
            "cost_usage_positive_usage_only",
            "positive",
            "agent-cost-events.py",
            "Usage tokens can be recorded while billing remains usage_only.",
            case_cost_usage_positive_usage_only,
        ),
        EvalCase(
            "cost_usage_negative_connected_without_source",
            "negative",
            "agent-cost-events.py",
            "Connected billing cannot be claimed without platform cost source.",
            case_cost_usage_negative_connected_without_source,
        ),
        EvalCase(
            "task_state_positive_submitted",
            "positive",
            "v9-task-state-check.py",
            "Submitted review state is allowed without accepted_by.",
            case_task_state_positive_submitted,
        ),
        EvalCase(
            "task_state_negative_self_accept",
            "negative",
            "v9-task-state-check.py",
            "Task card cannot be accepted by its own owner/author.",
            case_task_state_negative_self_accept,
        ),
        EvalCase(
            "task_state_negative_missing_acceptor",
            "negative",
            "v9-task-state-check.py",
            "Accepted review state requires accepted_by.",
            case_task_state_negative_missing_acceptor,
        ),
        EvalCase(
            "task_state_positive_reviewing",
            "positive",
            "v9-task-state-check.py",
            "Reviewing state is allowed only with reviewer/submitted_at and no premature acceptance fields.",
            case_task_state_positive_reviewing,
        ),
        EvalCase(
            "task_state_negative_submitted_missing_submitted_at",
            "negative",
            "v9-task-state-check.py",
            "New submitted review state requires submitted_at.",
            case_task_state_negative_submitted_missing_submitted_at,
        ),
        EvalCase(
            "task_state_negative_changes_requested_without_note",
            "negative",
            "v9-task-state-check.py",
            "Changes requested must include an actionable note.",
            case_task_state_negative_changes_requested_without_note,
        ),
        EvalCase(
            "handoff_positive_actionable",
            "positive",
            "v9-handoff-check.py",
            "Done task requiring handoff has actionable handoff fields.",
            case_handoff_positive_actionable,
        ),
        EvalCase(
            "handoff_negative_missing",
            "negative",
            "v9-handoff-check.py",
            "Done task requiring handoff cannot omit handoff.",
            case_handoff_negative_missing,
        ),
        EvalCase(
            "handoff_negative_incomplete",
            "negative",
            "v9-handoff-check.py",
            "Handoff must include artifacts, verification, and next action.",
            case_handoff_negative_incomplete,
        ),
        EvalCase(
            "reflex_negative_missing_sources_visible",
            "negative",
            "v9-reflex-check.py",
            "Reflex runner exposes missing sources instead of going silent.",
            case_reflex_negative_missing_sources_visible,
        ),
        EvalCase(
            "reflex_negative_cooldown_escalation_visible",
            "negative",
            "v9-reflex-check.py",
            "Cooldown does not hide severity escalation.",
            case_reflex_negative_cooldown_escalation_visible,
        ),
        # ---- V9.4.1 RED fixtures（实施前应全红）----
        EvalCase(
            "accept_gate_negative_edit_bare_accept",
            "negative",
            "gate-enforce.py pre-accept",
            "[V9.4.1] Edit 裸改成 self-accept 的候选卡必须被 pre-accept 硬拦。",
            case_accept_gate_negative_edit_bare_accept,
        ),
        EvalCase(
            "accept_gate_negative_write_full_accept",
            "negative",
            "gate-enforce.py pre-accept",
            "[V9.4.1] Write 全量覆盖成 self-accept 的候选卡必须被 pre-accept 硬拦。",
            case_accept_gate_negative_write_full_accept,
        ),
        EvalCase(
            "scope_tamper_negative_bash_writescope",
            "negative",
            "v9-scope-tamper-check.py",
            "[V9.4.1] Bash 直写状态文件扩权 write_scope 必须能被事后检测。",
            case_scope_tamper_negative_bash_writescope,
        ),
        EvalCase(
            "eval_freshness_negative_stale_report",
            "negative",
            "gate-enforce.py pre-accept --require-fresh-eval",
            "[V9.4.1] 拿旧 eval 报告验收 harness 改动必须被新鲜度校验硬拦。",
            case_eval_freshness_negative_stale_report,
        ),
        # ---- 吸收人工Reviewer检核补充的 fixture ----
        EvalCase(
            "accept_hook_negative_edit_payload",
            "negative",
            "pre-write-hook.sh (Edit 候选重建)",
            "[V9.4.1] Edit payload 经 hook 候选重建后 self-accept 必须被硬拦（测 hook 解析，非仅 pre-accept）。",
            case_accept_hook_negative_edit_payload,
        ),
        EvalCase(
            "accept_hook_negative_write_payload",
            "negative",
            "pre-write-hook.sh (Write 全量 content)",
            "[V9.4.1] Write payload 经 hook 取 content 后 self-accept 必须被硬拦（测 hook 解析）。",
            case_accept_hook_negative_write_payload,
        ),
        EvalCase(
            "accept_command_positive_valid_reviewer",
            "positive",
            "v9-accept.py",
            "[V9.4.1] 合法 v9_accept 必须能生成候选、通过 pre-accept、原子写回 accepted。",
            case_accept_command_positive_valid_reviewer,
        ),
        EvalCase(
            "eval_freshness_negative_stale_hash",
            "negative",
            "gate-enforce.py pre-accept --require-fresh-eval",
            "[V9.4.1] well-formed 但 hash/mtime 过期的 eval 报告必须被新鲜度校验硬拦。",
            case_eval_freshness_negative_stale_hash,
        ),
    ]


def meta_guard(results: list[EvalResult]) -> None:
    positive_count = sum(1 for r in results if r.kind == "positive")
    negative_count = sum(1 for r in results if r.kind == "negative")
    if positive_count == 0:
        results.append(
            EvalResult(
                "meta_positive_coverage_required",
                "meta",
                CHECK_NAME,
                False,
                "suite includes at least one positive fixture",
                "no positive fixture registered",
                {},
            )
        )
    if negative_count == 0:
        results.append(
            EvalResult(
                "meta_negative_coverage_required",
                "meta",
                CHECK_NAME,
                False,
                "suite includes at least one negative fixture",
                "no negative fixture registered",
                {},
            )
        )


def run_all() -> list[EvalResult]:
    results: list[EvalResult] = []
    for case in cases():
        try:
            result = case.run()
        except Exception as exc:
            result = EvalResult(
                case.case_id,
                case.kind,
                case.target,
                False,
                case.description,
                f"case crashed: {exc}",
                {"exception": repr(exc)},
            )
        results.append(result)
    meta_guard(results)
    return results


def summarize(results: list[EvalResult]) -> dict:
    positive = [r for r in results if r.kind == "positive"]
    negative = [r for r in results if r.kind == "negative"]
    meta = [r for r in results if r.kind == "meta"]
    failed = [r for r in results if not r.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "positive_total": len(positive),
        "passed_positive": sum(1 for r in positive if r.passed),
        "negative_total": len(negative),
        "blocked_negative": sum(1 for r in negative if r.passed),
        "missed_negative": sum(1 for r in negative if not r.passed),
        "meta_failed": sum(1 for r in meta if not r.passed),
        "worst": "p1" if failed else None,
    }


def file_sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def tested_fingerprints() -> tuple[dict[str, str], str | None]:
    hashes: dict[str, str] = {}
    mtimes: list[datetime] = []
    for rel in TESTED_FILES:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        hashes[rel.as_posix()] = file_sha16(path)
        mtimes.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone())
    max_mtime = max(mtimes).isoformat(timespec="seconds") if mtimes else None
    return hashes, max_mtime


def build_report(results: list[EvalResult]) -> dict:
    hashes, max_mtime = tested_fingerprints()
    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "today": TODAY.isoformat(),
        "summary": summarize(results),
        "tested_hashes": hashes,
        "tested_max_mtime": max_mtime,
        "cases": [r.as_dict() for r in results],
    }


def write_latest(report: dict) -> None:
    LATEST_REPORT.parent.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_text(report: dict) -> None:
    summary = report["summary"]
    print(f"# {CHECK_NAME}")
    print(
        "summary: "
        f"passed={summary['passed']}/{summary['total']} failed={summary['failed']} "
        f"| positive={summary['passed_positive']}/{summary['positive_total']} "
        f"| negative_blocked={summary['blocked_negative']}/{summary['negative_total']} "
        f"missed_negative={summary['missed_negative']}"
    )
    for case in report["cases"]:
        status = "pass" if case["passed"] else "fail"
        print(f"[{status}] {case['case_id']} ({case['kind']}) | {case['observed']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--write-latest", action="store_true", help=f"写入 {LATEST_REPORT}")
    args = parser.parse_args()

    report = build_report(run_all())
    if args.write_latest:
        write_latest(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)

    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
