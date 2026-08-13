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
  - v9-iteration-ops-check.py monthly iteration contract, preview, and review loop checks.

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
ITERATION_OPS = SCRIPT_DIR / "v9-iteration-ops-check.py"
STATUS_SUMMARY = SCRIPT_DIR / "v9-status-summary.py"
HARNESS_VERIFY = REPO_ROOT / ".standards" / "harness-eval-verify.py"
REFLEX = SCRIPT_DIR / "v9-reflex-check.py"
PHASE_E_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_e.py"
PHASE_F_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_f.py"
PHASE_G_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_g.py"
PHASE_H_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_phase_h.py"
CODEX_HOOK_TEST = REPO_ROOT / ".standards" / "tests" / "test_v9_codex_hooks.py"


def runtime_inspect_dir() -> Path:
    explicit = os.environ.get("XIRANG_V9_INSPECT_DIR")
    if explicit:
        return Path(explicit).expanduser()
    runtime_root = os.environ.get("XIRANG_V9_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root).expanduser() / "巡检"
    return Path.home() / ".xirang" / "v9-runtime" / "巡检"


LATEST_REPORT = runtime_inspect_dir() / "harness-eval-latest.json"
HARNESS_MANIFEST = REPO_ROOT / ".standards/harness-tested-files.txt"


def load_tested_files() -> list[Path]:
    lines = HARNESS_MANIFEST.read_text(encoding="utf-8").splitlines()
    files = [Path(line.strip()) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not files or len(files) != len(set(files)):
        raise RuntimeError("Harness trust manifest is empty or contains duplicate paths")
    return files


TESTED_FILES = load_tested_files()


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


def run_json(
    script: Path,
    args: list[str],
    cwd: Path,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> tuple[int, dict | None, str, str]:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
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
        write_text(root / "00-MOC" / "知识管理规范.md", "这里残留了 INTERNAL_PROJECT_FIXTURE 项目口径。\n")
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
        write_text(root / "config.json", '{"app_secret": "abcdefghijklmnopqrstuvwxyz123456"}\n')
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
reviewer: "用户"
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
reviewer: "用户"
submitted_at: 2026-06-27T09:00:00+08:00
accepted_by: null
accepted_at: null
acceptance_result: null
handoff_required: true
handoff_to: "用户"
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
- next action: 用户终审
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


ITERATION_MANAGEMENT_DOCS = [
    "README.md",
    "260725-需求范围划定草案.md",
    "260725-模块变更台账.md",
    "260725-材料迁移manifest.md",
    "260725-封版归集清单.md",
    "260725-智能体写入边界.md",
    "260725-遗留项台账.md",
]


def iteration_doc_text(
    title: str,
    doc_type: str,
    scope_status: str | None = None,
    agent_assignments: bool = False,
    prototype_root: str | None = None,
    preview_status: str | None = None,
    prototype_coverage: tuple[int, int, int, int] | None = None,
    double_time: bool = True,
    omit_agent_role: str | None = None,
    unavailable_agent_role: str | None = None,
    fallback_agent_role: str | None = None,
    valid_for: str = "260725",
    extra_frontmatter: str = "",
) -> str:
    scope_line = f"scope_status: {scope_status}\n" if scope_status else ""
    memory_anchor_lines = f'valid_for: "{valid_for}"\niteration_root: 10-项目/迭代/260725迭代\nbaseline_root: 10-项目/基线\n'
    double_time_lines = 'observed_at: "2026-06-27T09:00:00+08:00"\nrecorded_at: "2026-06-27T09:10:00+08:00"\n' if double_time else ""
    coverage = prototype_coverage or (42, 42, 0, 0)
    coverage_total, coverage_covered, coverage_partial, coverage_missing = coverage
    coverage_status = "complete" if coverage_partial == 0 and coverage_missing == 0 else "partial"
    prototype = f"""prototype_root: {prototype_root}
prototype_coverage_status: {coverage_status}
prototype_requirement_total: {coverage_total}
prototype_covered_count: {coverage_covered}
prototype_partial_count: {coverage_partial}
prototype_missing_count: {coverage_missing}
prototype_coverage_checked_at: "2026-06-27T09:00:00+08:00"
prototype_coverage_ref: "fixture-coverage-audit"
visual_artifact:
  type: html_prototype
  root: {prototype_root}
  current_main: v3.2
  entries:
    - index.html
    - v3.2/pc/index.html
    - v3.2/h5/index.html
  preview_tool: flyfish viewer
  preview_status: {preview_status or "pending"}
""" if prototype_root else ""
    assignment_roles = [
        ("scope_definition", "爪爪", "需求范围划定"),
        ("prd_and_prototype", "小虫", "PRD 工作稿、原型说明、视觉交付入口"),
        ("code_implementation", "咬咬", "代码实现、验证记录、技术风险回填"),
        ("research_and_data", "喷水娃", "外部资料采集、证据索引、材料 manifest"),
        ("iteration_coord", "WorkBuddy", "迭代协调"),
        ("v9_harness_review", "Codex", "V9 只读检查与 eval"),
    ]
    assignments = ""
    if agent_assignments:
        lines = ["agent_assignments:"]
        for role, owner, responsibility in assignment_roles:
            if role == omit_agent_role:
                continue
            lines.extend(
                [
                    f"  {role}:",
                    f"    owner: {owner}",
                    f"    responsibility: {responsibility}",
                    f"    status: {'unavailable' if role == unavailable_agent_role else 'active'}",
                ]
            )
            if role == fallback_agent_role:
                lines.append("    fallback: Codex")
        assignments = "\n".join(lines) + "\n"
    return f"""---
title: {title}
type: {doc_type}
project: 示例项目EXAMPLE
iteration: "260725"
status: 当前有效
{scope_line}{memory_anchor_lines}{double_time_lines}{extra_frontmatter}{prototype}{assignments}updated: 2026-06-27
owner: 用户
tags: [示例项目EXAMPLE, 260725迭代, eval]
---

# {title}

This fixture exists only inside a temporary eval vault.
"""


def write_iteration_fixture(
    root: Path,
    omit: set[str] | None = None,
    prototype_root: str | None = None,
    preview_status: str | None = None,
    prototype_coverage: tuple[int, int, int, int] | None = None,
    workbench_scope_status: str = "范围待确认",
    workbench_double_time: bool = True,
    omit_agent_role: str | None = None,
    unavailable_agent_role: str | None = None,
    fallback_agent_role: str | None = None,
    workbench_valid_for: str = "260725",
    write_boundary_complete: bool = True,
    release_collection_complete: bool = True,
    material_manifest_complete: bool = True,
    carryover_ledger_complete: bool = True,
    workbench_extra_frontmatter: str = "",
    release_collection_extra_frontmatter: str = "",
    scope_body: str | None = None,
) -> None:
    omit = omit or set()
    write_text(
        root / "10-项目" / "README.md",
        """---
title: 10-项目
type: 项目根索引
status: 当前有效
updated: 2026-06-27
owner: 用户
tags: [项目, fixture]
---

# 10-项目

| 项目 | 当前基线 | 当前迭代 | 基线入口 | 迭代入口 |
|------|----------|----------|----------|----------|
| 示例项目 EXAMPLE | 625 | 260725 | [[基线/README|基线]] | [[迭代/260725迭代/README|260725迭代]] |
""",
    )
    management = root / "10-项目" / "迭代" / "260725迭代" / "迭代管理"
    write_boundary_body = """
| 场景 | 默认读区 | 默认写区 | 禁止动作 |
|---|---|---|---|
| 采集资料 | `10-项目/基线/`、外部来源 | `10-项目/迭代/260725迭代/` | 未登记 manifest 就跨目录搬迁 |
| 编写 PRD 工作稿 | `10-项目/基线/{模块}/` | `10-项目/迭代/260725迭代/260725-{模块}-PRD工作稿.md` | 未封版直接覆盖基线 PRD |
| 封版归集 | `10-项目/迭代/260725迭代/` | `10-项目/基线/` | 未生成 tag/压缩包前覆盖基线 |

## 口径

1. 默认写入迭代区，只有封版归集任务可以写基线。
2. 跨目录材料移动必须先登记 manifest。
""" if write_boundary_complete else """
## 口径

	本文只说明当前迭代可以协作，缺少具体写入边界。
	"""
    release_collection_body = """
> 归集目标是让 [[10-项目/基线/README|基线]] 吸收本迭代的最新稳定内容，而不是在 Vault 内长期保存一份完整迭代快照。

## 归集原则

1. 文件级全量替换或新增，不做章节级合并。
2. 未进入本轮范围的模块不改基线。
3. 归集前先生成 Git tag 或封版压缩包。
4. 归集完成后运行治理检查和 registry 校验。

## 归集台账

| 源文件 | 目标基线文件 | 归集方式 | 评审状态 | 引用更新 | 备注 |
|---|---|---|---|---|---|
| 待登记 | 待登记 | 待确认 | 待确认 | 待确认 | 范围确认后补充 |

## 完成条件

- [[260725-模块变更台账]] 中的入选模块均已评审通过或明确不归集。
- [[260725-材料迁移manifest]] 中的引用更新状态均已关闭。
- `module-registry.json` 已由基线 README frontmatter 重新生成。
- 基线入口、项目基线聚合、模块基线索引均已同步。
""" if release_collection_complete else """
	## 归集台账

	待后续填写。
	"""
    material_manifest_body = """
> 所有跨目录移动先登记，再执行。没有 manifest 的材料不做批量搬迁。

| 源路径 | 目标路径 | 迭代归属 | 是否跨迭代共用 | 引用更新状态 | 备注 |
|---|---|---|---|---|---|
| 待登记 | 待登记 | 260725 | 待判断 | 待更新 | 范围确认后补充 |

## 执行规则

1. 逐行执行，不批量盲搬。
2. 搬迁后更新 Obsidian wikilink 和明文路径。
3. 搬迁后运行链接与治理检查。
""" if material_manifest_complete else """
## 迁移记录

待后续填写。
"""
    carryover_ledger_body = """
> 本台账记录跨迭代遗留项的关闭、拆分或升级决定。

## 台账目标

跨迭代遗留项必须能在月末 review 中被关闭、拆分或升级。

| 遗留项 | 来源文件 | carryover_to | carryover_count | owner | close_decision | next_review_at | 状态 |
|---|---|---|---:|---|---|---|---|
| 待登记 | 待登记 | 待确认 | 0 | 待定 | 待评审 | 待确认 | open |

## 登记规则

- `carryover_count` 超过 2 个迭代时，触发 CARRYOVER_TOO_LONG advisory。
- `close_decision` 必须在月末 review 中选择关闭、拆分、升级、继续跟踪或转风险。
- 月末 review 需要判断是否进入规则/eval/skill 晋升闭环。
""" if carryover_ledger_complete else """
## 遗留项

待后续填写。
"""
    docs = {
        "README.md": iteration_doc_text(
            "260725迭代工作台",
            "迭代工作台",
            workbench_scope_status,
            agent_assignments=True,
            prototype_root=prototype_root,
            preview_status=preview_status,
            prototype_coverage=prototype_coverage,
            double_time=workbench_double_time,
            omit_agent_role=omit_agent_role,
            unavailable_agent_role=unavailable_agent_role,
            fallback_agent_role=fallback_agent_role,
            valid_for=workbench_valid_for,
            extra_frontmatter=workbench_extra_frontmatter,
        ),
        "260725-需求范围划定草案.md": (
            scope_body
            if scope_body is not None
            else iteration_doc_text("260725需求范围划定草案", "需求范围划定", "待评审")
        ),
        "260725-模块变更台账.md": iteration_doc_text("260725模块变更台账", "迭代变更台账", "框架占位"),
        "260725-材料迁移manifest.md": iteration_doc_text("260725材料迁移manifest", "材料迁移清单") + material_manifest_body,
        "260725-封版归集清单.md": (
            iteration_doc_text("260725封版归集清单", "封版归集清单", extra_frontmatter=release_collection_extra_frontmatter)
            + release_collection_body
        ),
        "260725-智能体写入边界.md": iteration_doc_text("260725智能体写入边界", "智能体写入边界") + write_boundary_body,
        "260725-遗留项台账.md": iteration_doc_text("260725遗留项台账", "遗留项台账") + carryover_ledger_body,
    }
    for name, text in docs.items():
        if name not in omit:
            write_text(management / name, text)


def write_declared_prototype_fixture(root: Path, blank_entry: str | None = None, script_shell: bool = False) -> Path:
    prototype = root / "prototype"
    contents = {
        "index.html": "<!doctype html><html><body>Root</body></html>\n",
        "v3.2/pc/index.html": "<!doctype html><html><body>PC</body></html>\n",
        "v3.2/h5/index.html": "<!doctype html><html><body>H5</body></html>\n",
    }
    if script_shell:
        contents["v3.2/pc/index.html"] = (
            '<!doctype html><html><body><noscript>请启用 JavaScript 后查看。</noscript>'
            '<script src="../components/pc-shell-host.js"></script></body></html>\n'
        )
        contents["v3.2/h5/index.html"] = (
            '<!doctype html><html><body><noscript>请启用 JavaScript 后查看。</noscript>'
            '<script src="../components/h5-shell-host.js"></script></body></html>\n'
        )
    if blank_entry:
        contents[blank_entry] = "<!doctype html><html><body>   </body></html>\n"
    for entry, content in contents.items():
        write_text(prototype / entry, content)
    return prototype


FACT_CHAIN_COMPLETE_IDS = [1, 3, 5, 6, 7, 8, 9, 11, 14, 16, 17, 18, 19, 23, 24, 25, 28, 29, 30, 31, 32, 34]
FACT_CHAIN_PARTIAL_IDS = [2, 4, 13, 15, 20, 26, 27, 33, 43, 44, 48, 50]
FACT_CHAIN_MISSING_IDS = [10, 12, 21, 22, 35, 37, 38, 39]


def fact_chain_frontmatter() -> str:
    return """scope_confirmed_at: "2026-06-29T12:20:00+08:00"
scope_decision_ref: "fixture-scope-decision"
scope_requirement_total: 42
vault_mapping_existing_count: 26
vault_mapping_strengthen_count: 7
vault_mapping_new_module_count: 9
"""


def write_fact_chain_fixture(root: Path, omit_module_requirement: int | None = None) -> None:
    management = root / "10-项目" / "迭代" / "260725迭代" / "迭代管理"
    write_text(
        management / "260725-需求范围划定草案.md",
        iteration_doc_text(
            "260725需求范围划定草案",
            "需求范围划定",
            "scoped",
            extra_frontmatter=fact_chain_frontmatter(),
        ),
    )
    audit_rows = []
    for requirement_id in FACT_CHAIN_COMPLETE_IDS:
        audit_rows.append(f"| {requirement_id} | fixture | fixture.html | 完整 | — |")
    for requirement_id in FACT_CHAIN_PARTIAL_IDS:
        audit_rows.append(f"| {requirement_id} | fixture | fixture.html | 部分 | gap |")
    for requirement_id in FACT_CHAIN_MISSING_IDS:
        audit_rows.append(f"| {requirement_id} | fixture | — | 缺失 | gap |")
    write_text(
        management / "260725-需求范围核对.md",
        """---
title: 260725需求范围核对
type: 需求范围核对
iteration: "260725"
scope_status: scoped
scope_requirement_total: 42
vault_mapping_existing_count: 26
vault_mapping_strengthen_count: 7
vault_mapping_new_module_count: 9
prototype_coverage_status: partial
prototype_requirement_total: 42
prototype_covered_count: 22
prototype_partial_count: 12
prototype_missing_count: 8
---

# fixture

## 7. 原型覆盖审计（2026-06-27）

| 序号 | 需求 | 证据 | 覆盖 | 差距 |
|---:|---|---|---|---|
""" + "\n".join(audit_rows) + "\n",
    )
    ledger_rows = []
    module_rows = []
    for requirement_id in [*FACT_CHAIN_PARTIAL_IDS, *FACT_CHAIN_MISSING_IDS]:
        status = "partial" if requirement_id in FACT_CHAIN_PARTIAL_IDS else "missing"
        ledger_rows.append(f"| {requirement_id} | fixture-module | {status} | gap | 小虫 | fix | active |")
        if requirement_id != omit_module_requirement:
            module_rows.append(f"| {requirement_id} | fixture | {status} | gap | fix |")
    write_text(
        management / "260725-模块变更台账.md",
        iteration_doc_text(
            "260725模块变更台账",
            "迭代变更台账",
            "scoped",
            extra_frontmatter='scope_confirmed_at: "2026-06-29T12:20:00+08:00"\nscope_decision_ref: "fixture-scope-decision"\n',
        )
        + """
## 原型覆盖缺口执行台账（2026-06-27）

| 需求序号 | 模块 | 原型状态 | 缺口 | owner | 下一动作 | 状态 |
|---|---|---|---|---|---|---|
""" + "\n".join(ledger_rows) + "\n",
    )
    write_text(
        root / "10-项目" / "迭代" / "260725迭代" / "01-fixture" / "README.md",
        """---
module: fixture
iteration: "260725"
scope_status: scoped
---

# fixture

## 725 原型覆盖回填（2026-06-27）

| 需求序号 | 范围 | 原型状态 | 缺口 | 下一动作 |
|---|---|---|---|---|
""" + "\n".join(module_rows) + "\n",
    )


def write_iteration_visual_artifact(root: Path, with_preview_record: bool) -> None:
    artifact = root / "10-项目" / "迭代" / "260725迭代" / "09-车间质检与超时预警" / "assistant-preview" / "index.html"
    write_text(artifact, "<!doctype html><html><body><h1>Preview Fixture</h1></body></html>\n")
    if with_preview_record:
        write_text(
            root / "10-项目" / "迭代" / "260725迭代" / "09-车间质检与超时预警" / "README.md",
            """---
title: 09-车间质检与超时预警
project: 示例项目EXAMPLE
iteration: "260725"
status: 当前有效
visual_artifact:
  type: html
  path: "09-车间质检与超时预警/assistant-preview/index.html"
  preview_tool: "flyfish viewer"
  preview_status: checked
  checked_at: "2026-06-27T10:30:00+08:00"
---

# 09-车间质检与超时预警
""",
        )


def write_iteration_review_fixture(
    root: Path,
    complete: bool,
    carryover_complete: bool = True,
    scope_advance_complete: bool = True,
    scope_advance_decision: str = "延后",
    v9_body_decision: str = "待定",
    v9_body_eval_status: str = "not_applicable",
    rule_candidate_name: str = "无",
    rule_candidate_target: str = "checker",
    rule_candidate_status: str = "draft",
    checker_eval_decision: str = "待定",
    checker_eval_status: str = "not_required",
    skill_runbook_decision: str = "待定",
    skill_name: str = "无",
    skill_needs_new: str = "否",
    skill_status: str = "done",
) -> None:
    management = root / "10-项目" / "迭代" / "260725迭代" / "迭代管理"
    carryover_section = """## 遗留项回填

| 遗留项 | 台账入口 | close_decision | 处理动作 | next_review_at | 备注 |
|---|---|---|---|---|---|
| 无 | [[260725-遗留项台账]] | 关闭 / 拆分 / 升级 / 继续跟踪 / 转风险 | done | 2026-06-27 | eval fixture |

""" if carryover_complete else """## 遗留项回填

本节只有标题，缺少台账链接和 close_decision 动作。

"""
    scope_advance_section = f"""## 状态晋升建议回填

| 规则 | current_status | suggested_status | 证据 | 决定 | 拍板人 | 备注 |
|---|---|---|---|---|---|---|
| SCOPE_STATUS_ADVANCE_AVAILABLE | planning | scoped | fixture | {scope_advance_decision} | 用户 | 接受 / 拒绝 / 延后 |

""" if scope_advance_complete else """## 状态晋升建议回填

本节只有标题，缺少规则 ID、current_status、suggested_status 和决定。

"""
    sections = f"""## 规则晋升候选

| 候选规则 | 来源问题 | 目标形态 | 状态 |
|---|---|---|---|
| {rule_candidate_name} | 本次 eval fixture | {rule_candidate_target} | {rule_candidate_status} |

## Eval 回填

| eval case | 正/反样本 | 覆盖规则 | 状态 |
|---|---|---|---|
| iteration_ops_positive_review_loop_complete | positive | review loop | done |

## Skill 回填

| skill / runbook | 适用场景 | 是否需要新增 | 状态 |
|---|---|---|---|
| {skill_name} | 本次 eval fixture | {skill_needs_new} | {skill_status} |

## 晋升决定

| 项 | 决定 | eval_status | 拍板人 | 日期 |
|---|---|---|---|---|
| 是否进入 V9 正文 | {v9_body_decision} | {v9_body_eval_status} | 用户 | 2026-06-27 |
| 是否进入 checker/eval | {checker_eval_decision} | {checker_eval_status} | 用户 | 2026-06-27 |
| 是否进入 skill/runbook | {skill_runbook_decision} | not_applicable | 用户 | 2026-06-27 |
"""
    if complete:
        sections = carryover_section + scope_advance_section + sections
    else:
        sections = "## 问题分层\n\n缺晋升闭环章节。\n"
    write_text(
        management / "260725-review.md",
        f"""---
title: 260725迭代review
type: iteration_review
project: 示例项目EXAMPLE
iteration: "260725"
status: draft
review_status: draft
valid_for: "260725"
iteration_root: 10-项目/迭代/260725迭代
baseline_root: 10-项目/基线
observed_at: "2026-06-27T09:00:00+08:00"
recorded_at: "2026-06-27T09:10:00+08:00"
updated: 2026-06-27
owner: 用户
tags: [示例项目EXAMPLE, 260725迭代, eval]
---

# 260725 迭代 Review

{sections}
""",
    )


def iteration_scope_body_with_included_item() -> str:
    return """---
title: 260725需求范围划定草案
type: 需求范围划定
project: 示例项目EXAMPLE
iteration: "260725"
status: draft
scope_status: 待评审
valid_for: "260725"
iteration_root: 10-项目/迭代/260725迭代
baseline_root: 10-项目/基线
observed_at: "2026-06-27T09:00:00+08:00"
recorded_at: "2026-06-27T09:10:00+08:00"
updated: 2026-06-27
owner: 用户
tags: [示例项目EXAMPLE, 260725迭代, eval]
---

# 260725 需求范围划定草案

| 需求簇 | 编码 | 数量 | 建议结论 | 划定理由 |
|---|---|---:|---|---|
| fixture 新增项 | `YJ725-CI-999` | 1 | 纳入 725 候选 | eval fixture |
"""


def write_status_runtime_fixture(
    root: Path,
    *,
    red_health: bool = False,
    failed_eval: bool = False,
    generated_at: str | None = None,
) -> Path:
    runtime_root = root / ".v9-runtime"
    inspect_dir = runtime_root / "巡检"
    generated_at = generated_at or now_iso()
    health_summary = {
        "total": 1 if red_health else 0,
        "p0": 0,
        "p1": 1 if red_health else 0,
        "advisory": 0,
        "active": 1 if red_health else 0,
        "active_p0": 0,
        "active_p1": 1 if red_health else 0,
        "active_advisory": 0,
        "suppressed": 0,
        "worst": "p1" if red_health else None,
        "worst_active": "p1" if red_health else None,
    }
    write_text(
        inspect_dir / "health-latest.json",
        json.dumps(
            {
                "check": "v9-reflex-check",
                "generated_at": generated_at,
                "today": "2026-06-27",
                "sources_run": [{"source": "iteration-ops", "status": "failed" if red_health else "ok", "findings": 1 if red_health else 0}],
                "sources_ok": 0 if red_health else 1,
                "sources_failed": ["iteration-ops"] if red_health else [],
                "summary": health_summary,
                "findings": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    eval_summary = {
        "total": 71,
        "passed": 70 if failed_eval else 71,
        "failed": 1 if failed_eval else 0,
        "positive_total": 21,
        "passed_positive": 21,
        "negative_total": 50,
        "blocked_negative": 49 if failed_eval else 50,
        "missed_negative": 1 if failed_eval else 0,
        "meta_failed": 0,
        "worst": "p1" if failed_eval else None,
    }
    trust_subject = root / "status-summary-trust-fixture.txt"
    write_text(trust_subject, "status summary harness trust fixture\n")
    write_text(root / ".standards/harness-tested-files.txt", "status-summary-trust-fixture.txt\n")
    write_text(
        inspect_dir / "harness-eval-latest.json",
        json.dumps(
            {
                "check": "v9-harness-eval-runner",
                "generated_at": generated_at,
                "today": "2026-06-27",
                "summary": eval_summary,
                "tested_hashes": {"status-summary-trust-fixture.txt": file_sha16(trust_subject)},
                "cases": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return runtime_root


def init_git_fixture_with_clean_baseline(root: Path) -> None:
    write_text(root / "10-项目" / "基线" / "README.md", "# 625 基线\n\nClean baseline fixture.\n")
    commands = [
        ["git", "init"],
        ["git", "config", "user.email", "codex-eval@example.invalid"],
        ["git", "config", "user.name", "Codex Eval"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=30, check=True)


def write_iteration_carryover_doc(root: Path, carryover_count: int, status: str = "active") -> None:
    write_text(
        root / "10-项目" / "迭代" / "260725迭代" / "迭代管理" / "260725-carryover-fixture.md",
        f"""---
title: 260725遗留项fixture
type: carryover_fixture
project: 示例项目EXAMPLE
iteration: "260725"
status: {status}
valid_for: "260725"
iteration_root: 10-项目/迭代/260725迭代
baseline_root: 10-项目/基线
carryover_to: "260825"
carryover_count: {carryover_count}
observed_at: "2026-06-27T09:00:00+08:00"
recorded_at: "2026-06-27T09:10:00+08:00"
updated: 2026-06-27
owner: 用户
tags: [示例项目EXAMPLE, 260725迭代, eval]
---

# 260725 遗留项 fixture

This fixture exists only inside a temporary eval vault.
""",
    )


def scope_lifecycle_frontmatter(*, release: bool = False, review: bool = False) -> str:
    lines = [
        'scope_included_count: 0',
        'scope_frozen_at: "2026-06-27T09:30:00+08:00"',
        'scope_freeze_ref: "260725-需求范围划定草案.md#freeze-20260627"',
    ]
    if release or review:
        lines.extend([
            'released_at: "2026-06-27T11:00:00+08:00"',
            'release_ref: "260725-封版归集清单.md#归集台账"',
        ])
    if review:
        lines.extend([
            'reviewed_at: "2026-06-27T12:00:00+08:00"',
            'review_ref: "260725-review.md#晋升决定"',
        ])
    return "\n".join(lines) + "\n"


def case_iteration_ops_positive_clean() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and report.get("current_iteration") == "260725"
            and blocking_count(report) == 0
        )
        observed = "clean 260725 iteration contract accepted" if passed else "clean iteration fixture produced blocking finding"
        return EvalResult(
            "iteration_ops_positive_clean",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "current iteration with required management docs produces zero p0/p1 findings",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_absolute_project_root() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-abs-root-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        project_root = root / "10-项目"
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", str(project_root), "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "ITERATION_ROOT_MISMATCH")
            and not has_rule(report, "BASELINE_ROOT_MISMATCH")
        )
        observed = "absolute project root accepts vault-relative anchors" if passed else "absolute project root produced anchor mismatch"
        return EvalResult(
            "iteration_ops_positive_absolute_project_root",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "absolute --project-root accepts vault-relative iteration_root/baseline_root anchors",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_status_summary_positive_green() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-status-green-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        runtime_root = write_status_runtime_fixture(root)
        code, report, stdout, stderr = run_json(
            STATUS_SUMMARY,
            [
                "--repo-root",
                str(root),
                "--project-root",
                str(root / "10-项目"),
                "--iteration-check-script",
                str(ITERATION_OPS),
                "--harness-verify-script",
                str(HARNESS_VERIFY),
                "--write-latest",
                "--json",
            ],
            root,
            env={"XIRANG_V9_RUNTIME_DIR": str(runtime_root)},
        )
        latest = runtime_root / "巡检" / "status-latest.json"
        passed = (
            code == 0
            and report is not None
            and report.get("status") == "green"
            and report.get("current_iteration") == "260725"
            and latest.exists()
            and json.loads(latest.read_text(encoding="utf-8")).get("status") == "green"
        )
        observed = "status summary produced green UI contract" if passed else "status summary green contract failed"
        return EvalResult(
            "status_summary_positive_green",
            "positive",
            "v9-status-summary.py",
            passed,
            "Clean health/eval/iteration inputs produce green UI-facing status-latest.json",
            observed,
            {"returncode": code, "status": (report or {}).get("status"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_status_summary_positive_schema_contract() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-status-schema-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        runtime_root = write_status_runtime_fixture(root)
        code, report, stdout, stderr = run_json(
            STATUS_SUMMARY,
            [
                "--repo-root",
                str(root),
                "--project-root",
                str(root / "10-项目"),
                "--iteration-check-script",
                str(ITERATION_OPS),
                "--harness-verify-script",
                str(HARNESS_VERIFY),
                "--write-latest",
                "--json",
            ],
            root,
            env={"XIRANG_V9_RUNTIME_DIR": str(runtime_root)},
        )
        paths = (report or {}).get("paths", {})
        ui = (report or {}).get("ui", {})
        badges = ui.get("badges", [])
        actions = ui.get("actions", [])
        latest = runtime_root / "巡检" / "status-latest.json"
        latest_report = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
        required_paths = {
            "runtime_dir",
            "status_latest",
            "health_latest",
            "harness_eval_latest",
            "repo_root",
            "project_root",
            "iteration_root",
            "management_root",
            "iteration_workbench",
        }
        badge_ids = {badge.get("id") for badge in badges}
        action_ids = {action.get("id") for action in actions}
        passed = (
            code == 0
            and report is not None
            and report.get("schema_version") == "v1"
            and latest_report.get("schema_version") == "v1"
            and required_paths.issubset(paths.keys())
            and {"health", "harness_eval", "iteration_ops"}.issubset(badge_ids)
            and all(badge.get("target") in paths for badge in badges)
            and all(badge.get("detail_path", "").startswith("parts.") for badge in badges)
            and {
                "open_iteration_workbench",
                "open_health_latest",
                "open_harness_eval_latest",
                "open_status_latest",
            }.issubset(action_ids)
            and all(action.get("target") in paths for action in actions)
        )
        observed = "status summary v1 UI contract is stable" if passed else "status summary v1 UI contract missing fields"
        return EvalResult(
            "status_summary_positive_schema_contract",
            "positive",
            "v9-status-summary.py",
            passed,
            "status-latest.json exposes schema_version, paths, badges, and UI actions for plugin/desktop readers",
            observed,
            {
                "returncode": code,
                "schema_version": (report or {}).get("schema_version"),
                "paths": sorted(paths.keys()),
                "badge_ids": sorted(badge_ids),
                "action_ids": sorted(action_ids),
                "stderr": stderr,
                "stdout_head": stdout[:200],
            },
        )


def case_status_summary_negative_red_health() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-status-red-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        runtime_root = write_status_runtime_fixture(root, red_health=True)
        code, report, stdout, stderr = run_json(
            STATUS_SUMMARY,
            [
                "--repo-root",
                str(root),
                "--project-root",
                str(root / "10-项目"),
                "--iteration-check-script",
                str(ITERATION_OPS),
                "--harness-verify-script",
                str(HARNESS_VERIFY),
                "--json",
            ],
            root,
            env={"XIRANG_V9_RUNTIME_DIR": str(runtime_root)},
        )
        passed = (
            code == 1
            and report is not None
            and report.get("status") == "red"
            and report.get("parts", {}).get("health", {}).get("status") == "red"
        )
        observed = "status summary surfaced red health source" if passed else "status summary failed to surface red health source"
        return EvalResult(
            "status_summary_negative_red_health",
            "negative",
            "v9-status-summary.py",
            passed,
            "Red reflex health input produces red overall status and non-zero exit",
            observed,
            {"returncode": code, "status": (report or {}).get("status"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_status_summary_negative_stale_latest() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-status-stale-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        runtime_root = write_status_runtime_fixture(root, generated_at="2026-06-26T00:00:00+08:00")
        code, report, stdout, stderr = run_json(
            STATUS_SUMMARY,
            [
                "--repo-root",
                str(root),
                "--project-root",
                str(root / "10-项目"),
                "--iteration-check-script",
                str(ITERATION_OPS),
                "--harness-verify-script",
                str(HARNESS_VERIFY),
                "--max-age-hours",
                "1",
                "--json",
            ],
            root,
            env={"XIRANG_V9_RUNTIME_DIR": str(runtime_root)},
        )
        parts = (report or {}).get("parts", {})
        passed = (
            code == 1
            and report is not None
            and report.get("status") == "red"
            and parts.get("health", {}).get("freshness", {}).get("state") == "stale"
            and parts.get("harness_eval", {}).get("freshness", {}).get("state") == "stale"
            and not parts.get("harness_eval", {}).get("verification", {}).get("valid", True)
        )
        observed = "status summary rejected stale Harness truth as red" if passed else "status summary failed to reject stale latest"
        return EvalResult(
            "status_summary_negative_stale_latest",
            "negative",
            "v9-status-summary.py",
            passed,
            "Stale Harness truth produces red status with freshness and verification details",
            observed,
            {"returncode": code, "status": (report or {}).get("status"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_scope_candidates_before_freeze() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-planning-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, scope_body=iteration_scope_body_with_included_item())
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "SCOPE_ADDED_AFTER_FREEZE")
            and summary.get("included_scope_items") == 1
        )
        observed = "planning scope candidates accepted" if passed else "planning scope candidates were incorrectly blocked"
        return EvalResult(
            "iteration_ops_positive_scope_candidates_before_freeze",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Planning iteration may contain included candidate rows without triggering freeze rule",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_added_after_freeze() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-freeze-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="frozen",
            workbench_extra_frontmatter=(
                'scope_included_count: 0\n'
                'scope_frozen_at: "2026-06-27T09:30:00+08:00"\n'
                'scope_freeze_ref: "260725-需求范围划定草案.md#freeze-20260627"\n'
            ),
            scope_body=iteration_scope_body_with_included_item(),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_ADDED_AFTER_FREEZE")
            and blocking_count(report) > 0
            and summary.get("included_scope_items") == 1
        )
        observed = "post-freeze scope addition blocked" if passed else "post-freeze scope addition was not blocked"
        return EvalResult(
            "iteration_ops_negative_scope_added_after_freeze",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Frozen iteration with more included scope rows than baseline produces SCOPE_ADDED_AFTER_FREEZE p1",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_scope_status_scoped_complete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-scoped-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="scoped",
            workbench_extra_frontmatter=(
                'scope_confirmed_at: "2026-06-27T10:00:00+08:00"\n'
                'scope_decision_ref: "260725-需求范围划定草案.md#范围会议结论"\n'
            ),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and not has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and blocking_count(report) == 0
        )
        observed = "scoped transition evidence accepted" if passed else "scoped transition evidence was flagged"
        return EvalResult(
            "iteration_ops_positive_scope_status_scoped_complete",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Scoped iteration with scope_confirmed_at and scope_decision_ref stays green",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_transition_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-transition-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="scoped")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "missing scoped transition evidence surfaced as advisory" if passed else "missing scoped transition evidence was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_transition_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Scoped iteration missing scope_confirmed_at/scope_decision_ref produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_advance_available_planning() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-advance-planning-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="范围待确认",
            workbench_extra_frontmatter=(
                'scope_confirmed_at: "2026-06-27T10:00:00+08:00"\n'
                'scope_decision_ref: "260725-需求范围划定草案.md#范围会议结论"\n'
            ),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_ADVANCE_AVAILABLE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "planning advance suggestion surfaced as advisory" if passed else "planning advance suggestion was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_advance_available_planning",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Planning iteration with scoped evidence produces SCOPE_STATUS_ADVANCE_AVAILABLE advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_advance_available_frozen() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-advance-frozen-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="frozen",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(release=True),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_ADVANCE_AVAILABLE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "frozen advance suggestion surfaced as advisory" if passed else "frozen advance suggestion was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_advance_available_frozen",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Frozen iteration with release evidence produces SCOPE_STATUS_ADVANCE_AVAILABLE advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_scope_status_released_complete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-released-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="released",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(release=True),
        )
        write_iteration_review_fixture(root, complete=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and not has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and not has_rule(report, "SCOPE_FREEZE_BASELINE_MISSING")
            and blocking_count(report) == 0
        )
        observed = "released transition evidence accepted" if passed else "released transition evidence was flagged"
        return EvalResult(
            "iteration_ops_positive_scope_status_released_complete",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Released iteration with freeze and release references stays green",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_released_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-released-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="released",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(),
        )
        write_iteration_review_fixture(root, complete=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "missing released transition evidence surfaced as advisory" if passed else "missing released transition evidence was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_released_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Released iteration missing released_at/release_ref produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_scope_status_reviewed_complete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-reviewed-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="reviewed",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True),
        )
        write_iteration_review_fixture(root, complete=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and not has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and not has_rule(report, "SCOPE_FREEZE_BASELINE_MISSING")
            and blocking_count(report) == 0
        )
        observed = "reviewed transition evidence accepted" if passed else "reviewed transition evidence was flagged"
        return EvalResult(
            "iteration_ops_positive_scope_status_reviewed_complete",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Reviewed iteration with freeze, release, and review references stays green",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_reviewed_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-reviewed-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="reviewed",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(release=True),
        )
        write_iteration_review_fixture(root, complete=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_TRANSITION_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "missing reviewed transition evidence surfaced as advisory" if passed else "missing reviewed transition evidence was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_reviewed_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Reviewed iteration missing reviewed_at/review_ref produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_authorized_baseline_write() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-baseline-authorized-") as tmp:
        root = Path(tmp)
        baseline_rel = "10-项目/基线/README.md"
        paths_digest = hashlib.sha256(f"{baseline_rel}\n".encode("utf-8")).hexdigest()
        write_iteration_fixture(
            root,
            release_collection_extra_frontmatter=(
                "baseline_write_authorized: true\n"
                "baseline_write_mode: maintenance\n"
                "baseline_write_task: T-20260719-50\n"
                "baseline_write_authority: explicit_user_request\n"
                "baseline_write_path_count: 1\n"
                f"baseline_write_paths_sha256: {paths_digest}\n"
            ),
        )
        init_git_fixture_with_clean_baseline(root)
        write_text(root / "10-项目" / "基线" / "README.md", "# 625 基线\n\nAuthorized release collection update.\n")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and not has_rule(report, "BASELINE_WRITE_WITHOUT_RELEASE")
            and blocking_count(report) == 0
            and summary.get("baseline_changed_paths") == 1
        )
        observed = "task-scoped maintenance manifest accepted" if passed else "task-scoped maintenance was blocked"
        return EvalResult(
            "iteration_ops_positive_authorized_baseline_write",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Scoped iteration may carry only an explicitly authorized, hash-pinned maintenance path set",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_baseline_write_without_release() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-baseline-unauthorized-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        init_git_fixture_with_clean_baseline(root)
        write_text(root / "10-项目" / "基线" / "README.md", "# 625 基线\n\nUnauthorized planning update.\n")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "BASELINE_WRITE_WITHOUT_RELEASE")
            and blocking_count(report) > 0
            and summary.get("baseline_changed_paths") == 1
        )
        observed = "unauthorized baseline write blocked" if passed else "unauthorized baseline write was not blocked"
        return EvalResult(
            "iteration_ops_negative_baseline_write_without_release",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Baseline change during non-release iteration produces BASELINE_WRITE_WITHOUT_RELEASE p1",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_carryover_within_limit() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-carryover-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        write_iteration_carryover_doc(root, carryover_count=2)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "CARRYOVER_TOO_LONG")
            and summary.get("carryover_docs_checked") == 1
        )
        observed = "carryover within limit accepted" if passed else "carryover within limit was flagged"
        return EvalResult(
            "iteration_ops_positive_carryover_within_limit",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Carryover count at the two-iteration limit stays green",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_carryover_too_long() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-carryover-long-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        write_iteration_carryover_doc(root, carryover_count=3)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "CARRYOVER_TOO_LONG")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("carryover_docs_checked") == 1
        )
        observed = "long carryover surfaced as advisory" if passed else "long carryover was not surfaced"
        return EvalResult(
            "iteration_ops_negative_carryover_too_long",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Carryover count greater than two iterations produces CARRYOVER_TOO_LONG advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_workbench_double_time_missing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-doubletime-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_double_time=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "DOUBLE_TIME_MISSING")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("double_time_docs_checked", 0) >= 1
        )
        observed = "missing workbench double-time surfaced as advisory" if passed else "missing workbench double-time was not surfaced"
        return EvalResult(
            "iteration_ops_negative_workbench_double_time_missing",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Current iteration workbench missing observed_at/recorded_at produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_metadata_time_order_invalid() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-meta-time-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_extra_frontmatter=(
                'valid_from: "2026-07-01"\n'
                'valid_until: "2026-06-01"\n'
            ),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "METADATA_TIME_ORDER_INVALID")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "metadata time order issue surfaced as advisory" if passed else "metadata time order issue was not surfaced"
        return EvalResult(
            "iteration_ops_negative_metadata_time_order_invalid",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "valid_from later than valid_until produces metadata advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_supersedes_target_missing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-supersedes-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_extra_frontmatter='supersedes: "missing-old-workbench.md"\n',
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SUPERSEDES_TARGET_MISSING")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "missing supersedes target surfaced as advisory" if passed else "missing supersedes target was not surfaced"
        return EvalResult(
            "iteration_ops_negative_supersedes_target_missing",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "supersedes pointing at a missing file produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_agent_assignments_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-agent-assignments-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, omit_agent_role="code_implementation")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "AGENT_ASSIGNMENTS_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("agent_assignments_checked", 0) >= 1
        )
        observed = "missing agent assignment role surfaced as advisory" if passed else "missing agent assignment role was not surfaced"
        return EvalResult(
            "iteration_ops_negative_agent_assignments_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Current iteration workbench missing key agent assignment role produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_agent_assignment_fallback_available() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-agent-fallback-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            unavailable_agent_role="code_implementation",
            fallback_agent_role="code_implementation",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            code == 0
            and report is not None
            and not has_rule(report, "AGENT_ASSIGNMENT_FALLBACK_MISSING")
            and blocking_count(report) == 0
            and summary.get("agent_assignments_checked", 0) >= 1
        )
        observed = "unavailable role with fallback passed" if passed else "fallback fixture produced a finding"
        return EvalResult(
            "iteration_ops_positive_agent_assignment_fallback_available",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Unavailable agent assignment role with fallback produces no finding",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_agent_assignment_fallback_missing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-agent-fallback-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, unavailable_agent_role="code_implementation")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "AGENT_ASSIGNMENT_FALLBACK_MISSING")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("agent_assignments_checked", 0) >= 1
        )
        observed = "unavailable role without fallback surfaced as advisory" if passed else "missing fallback was not surfaced"
        return EvalResult(
            "iteration_ops_negative_agent_assignment_fallback_missing",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Unavailable agent assignment role without fallback produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_valid_for_mismatch() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-validfor-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_valid_for="250625")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "VALID_FOR_MISMATCH")
            and blocking_count(report) > 0
            and summary.get("memory_anchor_docs_checked", 0) >= 1
        )
        observed = "valid_for mismatch blocked" if passed else "valid_for mismatch was not blocked"
        return EvalResult(
            "iteration_ops_negative_valid_for_mismatch",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Current iteration workbench valid_for mismatch produces p1",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_write_boundary_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-boundary-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, write_boundary_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "WRITE_BOUNDARY_CONTRACT_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("write_boundary_contracts_checked", 0) >= 1
        )
        observed = "incomplete write boundary surfaced as advisory" if passed else "incomplete write boundary was not surfaced"
        return EvalResult(
            "iteration_ops_negative_write_boundary_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Write boundary doc missing default-write/baseline-release/manifest/tag contract produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_release_collection_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-release-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, release_collection_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "RELEASE_COLLECTION_CONTRACT_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("release_collection_contracts_checked", 0) >= 1
        )
        observed = "incomplete release collection surfaced as advisory" if passed else "incomplete release collection was not surfaced"
        return EvalResult(
            "iteration_ops_negative_release_collection_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Release collection doc missing goal/principles/ledger/completion contract produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_material_manifest_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-manifest-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, material_manifest_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "MATERIAL_MANIFEST_CONTRACT_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("material_manifest_contracts_checked", 0) >= 1
        )
        observed = "incomplete material manifest surfaced as advisory" if passed else "incomplete material manifest was not surfaced"
        return EvalResult(
            "iteration_ops_negative_material_manifest_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Material manifest missing registration/ledger/reference-update/check contract produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_missing_carryover_ledger() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-nocarryover-ledger-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, omit={"260725-遗留项台账.md"})
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        findings = (report or {}).get("findings", [])
        passed = (
            report is not None
            and blocking_count(report) > 0
            and any(
                item.get("rule_id") == "ITERATION_MANAGEMENT_DOC_MISSING"
                and "遗留项台账" in item.get("message", "")
                for item in findings
            )
        )
        observed = "missing carryover ledger blocked" if passed else "missing carryover ledger was not blocked"
        return EvalResult(
            "iteration_ops_negative_missing_carryover_ledger",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Current iteration without 遗留项台账 is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_carryover_ledger_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-carryover-ledger-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, carryover_ledger_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "CARRYOVER_LEDGER_CONTRACT_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("carryover_ledger_contracts_checked", 0) >= 1
        )
        observed = "incomplete carryover ledger surfaced as advisory" if passed else "incomplete carryover ledger was not surfaced"
        return EvalResult(
            "iteration_ops_negative_carryover_ledger_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Carryover ledger missing columns/rules/review-loop contract produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_review_loop_complete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="released",
            workbench_extra_frontmatter=scope_lifecycle_frontmatter(release=True),
        )
        write_iteration_review_fixture(root, complete=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "REVIEW_MISSING_AFTER_RELEASE")
            and not has_rule(report, "ITERATION_REVIEW_PROMOTION_LOOP_INCOMPLETE")
            and (report.get("summary") or {}).get("review_contracts_checked") == 1
        )
        observed = "released iteration review loop accepted" if passed else "complete review loop was flagged"
        return EvalResult(
            "iteration_ops_positive_review_loop_complete",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Released iteration with complete review rule/eval/skill sections stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_missing_after_release() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="released")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "REVIEW_MISSING_AFTER_RELEASE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "released iteration missing review surfaced as advisory" if passed else "missing post-release review was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_missing_after_release",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Released iteration without review.md or {iteration}-review.md produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_loop_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed")
        write_iteration_review_fixture(root, complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "ITERATION_REVIEW_PROMOTION_LOOP_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("review_contracts_checked") == 1
        )
        observed = "incomplete review promotion loop surfaced as advisory" if passed else "incomplete review loop was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Iteration review missing rule/eval/skill sections produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_carryover_loop_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-carryover-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed")
        write_iteration_review_fixture(root, complete=True, carryover_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "ITERATION_REVIEW_CARRYOVER_LOOP_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("review_contracts_checked") == 1
        )
        observed = "incomplete review carryover loop surfaced as advisory" if passed else "incomplete review carryover loop was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_carryover_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Iteration review carryover section missing ledger link/close_decision actions produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_scope_advance_loop_incomplete() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-scope-advance-incomplete-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(root, complete=True, scope_advance_complete=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "ITERATION_REVIEW_SCOPE_ADVANCE_LOOP_INCOMPLETE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("review_contracts_checked") == 1
        )
        observed = "incomplete review scope-advance loop surfaced as advisory" if passed else "incomplete review scope-advance loop was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_scope_advance_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Iteration review scope-advance section missing rule/current/suggested/decision markers produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_scope_status_accepted_but_not_applied() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-scope-accepted-not-applied-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(
            root,
            workbench_scope_status="范围待确认",
            workbench_extra_frontmatter=(
                'scope_confirmed_at: "2026-06-27T10:00:00+08:00"\n'
                'scope_decision_ref: "260725-需求范围划定草案.md#范围会议结论"\n'
            ),
        )
        write_iteration_review_fixture(root, complete=True, scope_advance_decision="接受")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "SCOPE_STATUS_ACCEPTED_BUT_NOT_APPLIED")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "accepted scope advance without writeback surfaced as advisory" if passed else "accepted scope advance without writeback was not surfaced"
        return EvalResult(
            "iteration_ops_negative_scope_status_accepted_but_not_applied",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Review accepting scope_status advance while workbench remains unchanged produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_accepted_rule_without_eval() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-rule-no-eval-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            checker_eval_decision="进入",
            checker_eval_status="pending",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "REVIEW_ACCEPTED_RULE_WITHOUT_EVAL")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "accepted checker/eval decision without done eval surfaced as advisory" if passed else "accepted checker/eval decision without done eval was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_accepted_rule_without_eval",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Review deciding to enter checker/eval without done eval produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_review_accepted_v9_body_done() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-v9-done-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            v9_body_decision="进入",
            v9_body_eval_status="done",
            rule_candidate_name="V9_REVIEW_PROMOTION_EVIDENCE",
            rule_candidate_target="V9 正文",
            rule_candidate_status="done",
            checker_eval_decision="进入",
            checker_eval_status="done",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "REVIEW_ACCEPTED_V9_BODY_WITHOUT_EVIDENCE")
            and not has_rule(report, "REVIEW_DECISION_MATRIX_INCONSISTENT")
        )
        observed = "accepted V9 body decision with done evidence accepted" if passed else "accepted V9 body decision with done evidence produced finding"
        return EvalResult(
            "iteration_ops_positive_review_accepted_v9_body_done",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Review accepting V9 body promotion with done eval and rule evidence stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_decision_matrix_inconsistent() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-matrix-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            v9_body_decision="进入",
            v9_body_eval_status="done",
            rule_candidate_name="V9_REVIEW_PROMOTION_EVIDENCE",
            rule_candidate_target="V9 正文",
            rule_candidate_status="done",
            checker_eval_decision="待定",
            checker_eval_status="pending",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "REVIEW_DECISION_MATRIX_INCONSISTENT")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "V9 body accepted while checker/eval not accepted done surfaced as advisory" if passed else "decision matrix inconsistency was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_decision_matrix_inconsistent",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Review accepting V9 body while checker/eval is not accepted done produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_accepted_v9_body_without_evidence() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-v9-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            v9_body_decision="进入",
            v9_body_eval_status="pending",
            rule_candidate_name="无",
            rule_candidate_target="checker",
            rule_candidate_status="draft",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "REVIEW_ACCEPTED_V9_BODY_WITHOUT_EVIDENCE")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "accepted V9 body decision without evidence surfaced as advisory" if passed else "accepted V9 body decision without evidence was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_accepted_v9_body_without_evidence",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Review deciding to enter V9 body without done eval and rule evidence produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_review_accepted_skill_done() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-skill-done-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            skill_runbook_decision="进入",
            skill_name="v9-iteration-review-runbook",
            skill_needs_new="是",
            skill_status="done",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "REVIEW_ACCEPTED_SKILL_WITHOUT_WRITEBACK")
        )
        observed = "accepted skill/runbook decision with done writeback accepted" if passed else "accepted skill/runbook decision with done writeback produced finding"
        return EvalResult(
            "iteration_ops_positive_review_accepted_skill_done",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Review accepting skill/runbook promotion with done skill writeback stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_review_accepted_skill_without_writeback() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-review-skill-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, workbench_scope_status="reviewed", workbench_extra_frontmatter=scope_lifecycle_frontmatter(review=True))
        write_iteration_review_fixture(
            root,
            complete=True,
            skill_runbook_decision="进入",
            skill_name="无",
            skill_needs_new="否",
            skill_status="done",
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "REVIEW_ACCEPTED_SKILL_WITHOUT_WRITEBACK")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "accepted skill/runbook decision without done writeback surfaced as advisory" if passed else "accepted skill/runbook decision without done writeback was not surfaced"
        return EvalResult(
            "iteration_ops_negative_review_accepted_skill_without_writeback",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Review deciding to enter skill/runbook without done skill writeback produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_missing_workbench() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-noreadme-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, omit={"README.md"})
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = report is not None and has_rule(report, "ITERATION_MANAGEMENT_DOC_MISSING") and blocking_count(report) > 0
        observed = "missing iteration workbench blocked" if passed else "missing iteration workbench was not blocked"
        return EvalResult(
            "iteration_ops_negative_missing_workbench",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "current iteration without 迭代管理/README.md is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_missing_write_boundary() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-noboundary-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root, omit={"260725-智能体写入边界.md"})
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        findings = (report or {}).get("findings", [])
        passed = (
            report is not None
            and blocking_count(report) > 0
            and any(
                item.get("rule_id") == "ITERATION_MANAGEMENT_DOC_MISSING"
                and "智能体写入边界" in item.get("message", "")
                for item in findings
            )
        )
        observed = "missing write boundary blocked" if passed else "missing write boundary was not blocked"
        return EvalResult(
            "iteration_ops_negative_missing_write_boundary",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "current iteration without 智能体写入边界 is rejected",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_visual_preview_checked() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-preview-ok-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        write_iteration_visual_artifact(root, with_preview_record=True)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "VISUAL_PREVIEW_MISSING")
            and (report.get("summary") or {}).get("visual_artifacts_checked") == 1
        )
        observed = "visual artifact preview record accepted" if passed else "checked visual artifact was still flagged"
        return EvalResult(
            "iteration_ops_positive_visual_preview_checked",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "HTML visual artifact with preview_status=checked is not flagged",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_visual_preview_missing() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-preview-missing-") as tmp:
        root = Path(tmp)
        write_iteration_fixture(root)
        write_iteration_visual_artifact(root, with_preview_record=False)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "VISUAL_PREVIEW_MISSING")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
        )
        observed = "missing visual preview surfaced as advisory" if passed else "missing visual preview was not surfaced"
        return EvalResult(
            "iteration_ops_negative_visual_preview_missing",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "HTML visual artifact without preview record produces VISUAL_PREVIEW_MISSING advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_declared_prototype_checked() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-prototype-ok-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root)
        write_iteration_fixture(root, prototype_root=prototype.as_posix(), preview_status="checked")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "VISUAL_PREVIEW_PENDING")
            and not has_rule(report, "PROTOTYPE_REQUIREMENT_GAPS")
            and not has_rule(report, "PROTOTYPE_REQUIREMENT_PARTIAL")
            and (report.get("summary") or {}).get("visual_artifacts_checked") == 3
            and (report.get("summary") or {}).get("prototype_requirements_covered") == 42
        )
        observed = "declared external prototype entries accepted" if passed else "declared checked prototype was flagged"
        return EvalResult(
            "iteration_ops_positive_declared_prototype_checked",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Declared external prototype root with checked preview and complete 42/42 coverage stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_declared_prototype_requirement_gaps() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-prototype-gaps-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root)
        write_iteration_fixture(
            root,
            prototype_root=prototype.as_posix(),
            preview_status="checked",
            prototype_coverage=(42, 22, 12, 8),
        )
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "PROTOTYPE_REQUIREMENT_GAPS")
            and has_rule(report, "PROTOTYPE_REQUIREMENT_PARTIAL")
            and summary.get("p1", 0) >= 1
            and summary.get("prototype_requirements_total") == 42
            and summary.get("prototype_requirements_covered") == 22
            and summary.get("prototype_requirements_partial") == 12
            and summary.get("prototype_requirements_missing") == 8
        )
        observed = "partial and missing prototype requirements remained visible" if passed else "prototype coverage gaps were not enforced"
        return EvalResult(
            "iteration_ops_negative_declared_prototype_requirement_gaps",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "A reachable prototype with 22 covered, 12 partial and 8 missing requirements produces P1 plus advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_fact_chain_consistent() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-fact-chain-ok-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root)
        write_iteration_fixture(
            root,
            prototype_root=prototype.as_posix(),
            preview_status="checked",
            prototype_coverage=(42, 22, 12, 8),
            workbench_scope_status="scoped",
            workbench_extra_frontmatter=fact_chain_frontmatter(),
        )
        write_fact_chain_fixture(root)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and not has_rule(report, "ITERATION_FACT_CHAIN_INCONSISTENT")
            and has_rule(report, "PROTOTYPE_REQUIREMENT_GAPS")
            and summary.get("fact_chain_gap_requirements") == 20
            and summary.get("fact_chain_docs_checked") == 4
        )
        observed = "consistent scope/module/prototype fact chain accepted" if passed else "consistent fact chain was rejected"
        return EvalResult(
            "iteration_ops_positive_fact_chain_consistent",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "A consistent 42/22/12/8 fact chain keeps real prototype gaps without producing consistency drift",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_fact_chain_inconsistent() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-fact-chain-bad-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root)
        write_iteration_fixture(
            root,
            prototype_root=prototype.as_posix(),
            preview_status="checked",
            prototype_coverage=(42, 22, 12, 8),
            workbench_scope_status="scoped",
            workbench_extra_frontmatter=fact_chain_frontmatter(),
        )
        write_fact_chain_fixture(root, omit_module_requirement=50)
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        fact_finding = next(
            (item for item in (report or {}).get("findings", []) if item.get("rule_id") == "ITERATION_FACT_CHAIN_INCONSISTENT"),
            None,
        )
        mismatches = (fact_finding or {}).get("detail", {}).get("mismatches", [])
        passed = (
            fact_finding is not None
            and any(item.get("kind") == "module_coverage_writeback" and item.get("requirement_id") == 50 for item in mismatches)
            and summary.get("p1", 0) >= 2
        )
        observed = "missing module writeback surfaced as fact-chain P1" if passed else "fact-chain drift was not detected"
        return EvalResult(
            "iteration_ops_negative_fact_chain_inconsistent",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "A scope-audit gap omitted from module README writeback produces ITERATION_FACT_CHAIN_INCONSISTENT P1",
            observed,
            {"returncode": code, "summary": summary, "mismatches": mismatches, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_positive_declared_prototype_script_shell() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-prototype-shell-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root, script_shell=True)
        write_iteration_fixture(root, prototype_root=prototype.as_posix(), preview_status="checked")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        passed = (
            code == 0
            and report is not None
            and blocking_count(report) == 0
            and not has_rule(report, "VISUAL_ARTIFACT_BLANK_OR_SHELL_EMPTY")
            and (report.get("summary") or {}).get("visual_artifacts_checked") == 3
        )
        observed = "declared script-shell prototype accepted" if passed else "script-shell prototype was flagged as blank"
        return EvalResult(
            "iteration_ops_positive_declared_prototype_script_shell",
            "positive",
            "v9-iteration-ops-check.py",
            passed,
            "Declared external prototype JS shell with script src stays green",
            observed,
            {"returncode": code, "summary": (report or {}).get("summary"), "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_declared_prototype_pending() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-prototype-pending-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root)
        write_iteration_fixture(root, prototype_root=prototype.as_posix(), preview_status="pending")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "VISUAL_PREVIEW_PENDING")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("visual_artifacts_checked") == 3
        )
        observed = "declared prototype pending preview surfaced as advisory" if passed else "declared pending prototype was not surfaced"
        return EvalResult(
            "iteration_ops_negative_declared_prototype_pending",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Declared external prototype root with preview_status=pending produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_iteration_ops_negative_declared_prototype_blank_entry() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-iteration-prototype-blank-") as tmp:
        root = Path(tmp)
        prototype = write_declared_prototype_fixture(root, blank_entry="v3.2/pc/index.html")
        write_iteration_fixture(root, prototype_root=prototype.as_posix(), preview_status="checked")
        code, report, stdout, stderr = run_json(ITERATION_OPS, ["--project-root", "10-项目", "--json"], root)
        summary = (report or {}).get("summary", {})
        passed = (
            report is not None
            and has_rule(report, "VISUAL_ARTIFACT_BLANK_OR_SHELL_EMPTY")
            and summary.get("p1") == 0
            and summary.get("advisory", 0) >= 1
            and summary.get("visual_artifacts_checked") == 3
        )
        observed = "declared blank prototype entry surfaced as advisory" if passed else "blank prototype entry was not surfaced"
        return EvalResult(
            "iteration_ops_negative_declared_prototype_blank_entry",
            "negative",
            "v9-iteration-ops-check.py",
            passed,
            "Declared external prototype entry with no render signal produces advisory",
            observed,
            {"returncode": code, "summary": summary, "stderr": stderr, "stdout_head": stdout[:200]},
        )


def case_reflex_negative_missing_sources_visible() -> EvalResult:
    with tempfile.TemporaryDirectory(prefix="v9-eval-reflex-missing-") as tmp:
        root = Path(tmp)
        runtime_root = root / ".v9-runtime"
        env = dict(os.environ)
        env["XIRANG_V9_RUNTIME_DIR"] = str(runtime_root)
        # 本用例只验证“缺失治理源必须显式暴露”。隔离宿主机 GBrain/Ollama/cron，
        # 避免把外部语义查询和 180 秒契约验证带入负样本，造成环境相关超时。
        env["XIRANG_GBRAIN_CLI"] = "/usr/bin/false"
        env["XIRANG_OLLAMA_CLI"] = "/usr/bin/false"
        env["XIRANG_CRONTAB"] = "/usr/bin/false"
        env["XIRANG_GBRAIN_CONTRACT_VERIFY"] = "/usr/bin/false"
        env["XIRANG_LLM_WIKI_CHECKER"] = "/usr/bin/false"
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


def gate_test_env() -> dict[str, str]:
    return {"V9_GATE_EVENT_FILE": os.devnull}


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
        'reviewer: "用户"',
        f"accepted_by: {accepted_by}",
        'accepted_at: "2026-06-26T09:00:00+08:00"',
        "acceptance_result: accepted",
        "priority: P1",
        "created_at: 2026-06-26T09:00:00+08:00",
        "updated_at: 2026-06-26T09:00:00+08:00",
        'completed_at: "2026-06-26T09:00:00+08:00"',
        "sla: {target_hours: 1, hard_deadline: null}",
        'paths: {allowed_write_roots: ["02-项目管理/"], temp_root: _temp/T-EVAL-ACCEPT/}',
        "deliverables: []",
        "gates: {pre_start: passed, pre_write: passed, handoff: passed}",
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
            GATE_ENFORCE, ["pre-accept", "--candidate", str(cand), "--source", "edit", "--json"],
            cwd=REPO_ROOT, env=gate_test_env(),
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
            GATE_ENFORCE, ["pre-accept", "--candidate", str(cand), "--source", "write", "--json"],
            cwd=REPO_ROOT, env=gate_test_env(),
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
            cwd=REPO_ROOT, env=gate_test_env(),
        )
        passed = gate_blocked(report, "STALE_EVAL")
        observed = "stale eval blocked" if passed else "新鲜度校验未实现（预期 RED，待实施）"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": stderr[:160]})


# ---- 补充 RED fixture（吸收用户检核：hook 层候选解析 / well-formed stale）----
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
                'source "$VAULT_ROOT/.standards/v8-handshake.sh"; v9_accept T-EVAL-ACCEPT 用户 --json',
            ],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=60,
        )
        new_text = card.read_text(encoding="utf-8")
        passed = (
            proc.returncode == 0
            and "review_status: accepted" in new_text
            and 'accepted_by: "用户"' in new_text
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
            cwd=REPO_ROOT, env=gate_test_env())
        passed = gate_blocked(report, "STALE_EVAL")
        observed = "stale-hash eval blocked" if passed else "hash 新鲜度校验未实现(预期 RED)"
        return EvalResult(cid, kind, target, passed, exp, observed, {"returncode": code, "stderr_head": err[:160]})


def case_phase_e_positive_truth_chains() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_E_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "OK" in proc.stderr
    return EvalResult(
        "phase_e_positive_truth_chains",
        "positive",
        "GBrain + LLM Wiki 725 + entropy governance",
        passed,
        "Phase E current-revision, exact-root, human-gated queue regressions all pass",
        "Phase E truth-chain regression suite passed" if passed else "Phase E regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def case_phase_f_positive_capability_truth() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_F_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 3 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "phase_f_positive_capability_truth",
        "positive",
        "Phoenix capability state",
        passed,
        "Phase F bounded executor, allowlist, and proposal-only evolution regressions pass",
        "3/3 Phase F Phoenix runtime tests passed" if passed else "Phase F regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def case_phase_g_positive_distribution_truth() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_G_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 6 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "phase_g_positive_distribution_truth",
        "positive",
        "skill resolution + portable Codex adapters",
        passed,
        "Phase G shadow rejection, explicit variants, and portable paths all pass",
        "6/6 Phase G distribution tests passed" if passed else "Phase G regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def case_phase_h_positive_long_session_stability() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(PHASE_H_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 13 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "phase_h_positive_long_session_stability",
        "positive",
        "system-Python Codex hook runtime",
        passed,
        "Phase H adapter, handshake interpreter, and RFC 3339 regressions all pass",
        "13/13 Phase H long-session tests passed" if passed else "Phase H regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def case_codex_hook_adapter_positive_tool_contract() -> EvalResult:
    proc = subprocess.run(
        [sys.executable, str(CODEX_HOOK_TEST)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    passed = proc.returncode == 0 and "Ran 8 tests" in proc.stderr and "OK" in proc.stderr
    return EvalResult(
        "codex_hook_adapter_positive_tool_contract",
        "positive",
        "Codex apply_patch + exec_command adapter",
        passed,
        "Codex write, deny, identity, shell-audit, and direct-write refusal contracts all pass",
        "8/8 Codex hook adapter tests passed" if passed else "Codex hook adapter regression suite failed",
        {"returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-1000:]},
    )


def cases() -> list[EvalCase]:
    all_cases = [
        EvalCase(
            "codex_hook_adapter_positive_tool_contract",
            "positive",
            "Codex apply_patch + exec_command adapter",
            "Codex Desktop tool adapter regression suite passes.",
            case_codex_hook_adapter_positive_tool_contract,
        ),
        EvalCase(
            "phase_h_positive_long_session_stability",
            "positive",
            "system-Python Codex hook runtime",
            "Phase H long-session runtime stability regression suite passes.",
            case_phase_h_positive_long_session_stability,
        ),
        EvalCase(
            "phase_g_positive_distribution_truth",
            "positive",
            "skill resolution + portable Codex adapters",
            "Phase G distribution and resolution regression suite passes.",
            case_phase_g_positive_distribution_truth,
        ),
        EvalCase(
            "phase_f_positive_capability_truth",
            "positive",
            "Phoenix capability state",
            "Phase F truthful capability-state regression suite passes.",
            case_phase_f_positive_capability_truth,
        ),
        EvalCase(
            "phase_e_positive_truth_chains",
            "positive",
            "GBrain + LLM Wiki 725 + entropy governance",
            "Phase E truth-chain regression suite passes.",
            case_phase_e_positive_truth_chains,
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
            "iteration_ops_positive_clean",
            "positive",
            "v9-iteration-ops-check.py",
            "Clean current-iteration management fixture stays green.",
            case_iteration_ops_positive_clean,
        ),
        EvalCase(
            "iteration_ops_positive_absolute_project_root",
            "positive",
            "v9-iteration-ops-check.py",
            "Absolute project root accepts vault-relative memory anchors.",
            case_iteration_ops_positive_absolute_project_root,
        ),
        EvalCase(
            "status_summary_positive_green",
            "positive",
            "v9-status-summary.py",
            "Clean health/eval/iteration inputs produce a green UI status contract.",
            case_status_summary_positive_green,
        ),
        EvalCase(
            "status_summary_positive_schema_contract",
            "positive",
            "v9-status-summary.py",
            "status-latest.json exposes stable v1 fields for plugin/desktop readers.",
            case_status_summary_positive_schema_contract,
        ),
        EvalCase(
            "status_summary_negative_red_health",
            "negative",
            "v9-status-summary.py",
            "Red reflex health input must surface as red overall UI status.",
            case_status_summary_negative_red_health,
        ),
        EvalCase(
            "status_summary_negative_stale_latest",
            "negative",
            "v9-status-summary.py",
            "Stale runtime latest files must not surface as green UI status.",
            case_status_summary_negative_stale_latest,
        ),
        EvalCase(
            "iteration_ops_negative_workbench_double_time_missing",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration workbench without observed_at/recorded_at surfaces advisory.",
            case_iteration_ops_negative_workbench_double_time_missing,
        ),
        EvalCase(
            "iteration_ops_negative_metadata_time_order_invalid",
            "negative",
            "v9-iteration-ops-check.py",
            "Metadata valid_from later than valid_until surfaces advisory.",
            case_iteration_ops_negative_metadata_time_order_invalid,
        ),
        EvalCase(
            "iteration_ops_negative_supersedes_target_missing",
            "negative",
            "v9-iteration-ops-check.py",
            "Metadata supersedes target must resolve to an existing file.",
            case_iteration_ops_negative_supersedes_target_missing,
        ),
        EvalCase(
            "iteration_ops_negative_agent_assignments_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration workbench missing a key agent assignment role surfaces advisory.",
            case_iteration_ops_negative_agent_assignments_incomplete,
        ),
        EvalCase(
            "iteration_ops_positive_agent_assignment_fallback_available",
            "positive",
            "v9-iteration-ops-check.py",
            "Unavailable agent assignment role with fallback remains clean.",
            case_iteration_ops_positive_agent_assignment_fallback_available,
        ),
        EvalCase(
            "iteration_ops_negative_agent_assignment_fallback_missing",
            "negative",
            "v9-iteration-ops-check.py",
            "Unavailable agent assignment role without fallback surfaces advisory.",
            case_iteration_ops_negative_agent_assignment_fallback_missing,
        ),
        EvalCase(
            "iteration_ops_negative_valid_for_mismatch",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration workbench valid_for mismatch is a blocking memory-contract error.",
            case_iteration_ops_negative_valid_for_mismatch,
        ),
        EvalCase(
            "iteration_ops_positive_scope_candidates_before_freeze",
            "positive",
            "v9-iteration-ops-check.py",
            "Planning iteration may contain included candidate rows without triggering freeze rule.",
            case_iteration_ops_positive_scope_candidates_before_freeze,
        ),
        EvalCase(
            "iteration_ops_negative_scope_added_after_freeze",
            "negative",
            "v9-iteration-ops-check.py",
            "Frozen iteration cannot silently add included scope rows beyond its baseline.",
            case_iteration_ops_negative_scope_added_after_freeze,
        ),
        EvalCase(
            "iteration_ops_positive_scope_status_scoped_complete",
            "positive",
            "v9-iteration-ops-check.py",
            "Scoped iteration with transition evidence is accepted.",
            case_iteration_ops_positive_scope_status_scoped_complete,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_transition_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Scoped iteration missing transition evidence surfaces advisory.",
            case_iteration_ops_negative_scope_status_transition_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_advance_available_planning",
            "negative",
            "v9-iteration-ops-check.py",
            "Planning iteration with scoped evidence surfaces advance suggestion.",
            case_iteration_ops_negative_scope_status_advance_available_planning,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_advance_available_frozen",
            "negative",
            "v9-iteration-ops-check.py",
            "Frozen iteration with release evidence surfaces advance suggestion.",
            case_iteration_ops_negative_scope_status_advance_available_frozen,
        ),
        EvalCase(
            "iteration_ops_positive_scope_status_released_complete",
            "positive",
            "v9-iteration-ops-check.py",
            "Released iteration with transition evidence is accepted.",
            case_iteration_ops_positive_scope_status_released_complete,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_released_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Released iteration missing transition evidence surfaces advisory.",
            case_iteration_ops_negative_scope_status_released_incomplete,
        ),
        EvalCase(
            "iteration_ops_positive_scope_status_reviewed_complete",
            "positive",
            "v9-iteration-ops-check.py",
            "Reviewed iteration with transition evidence is accepted.",
            case_iteration_ops_positive_scope_status_reviewed_complete,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_reviewed_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Reviewed iteration missing transition evidence surfaces advisory.",
            case_iteration_ops_negative_scope_status_reviewed_incomplete,
        ),
        EvalCase(
            "iteration_ops_positive_authorized_baseline_write",
            "positive",
            "v9-iteration-ops-check.py",
            "Exact task-scoped maintenance manifest may update baseline without claiming iteration release.",
            case_iteration_ops_positive_authorized_baseline_write,
        ),
        EvalCase(
            "iteration_ops_negative_baseline_write_without_release",
            "negative",
            "v9-iteration-ops-check.py",
            "Baseline changes without release authorization are blocked.",
            case_iteration_ops_negative_baseline_write_without_release,
        ),
        EvalCase(
            "iteration_ops_positive_carryover_within_limit",
            "positive",
            "v9-iteration-ops-check.py",
            "Carryover up to two iterations remains advisory-free.",
            case_iteration_ops_positive_carryover_within_limit,
        ),
        EvalCase(
            "iteration_ops_negative_carryover_too_long",
            "negative",
            "v9-iteration-ops-check.py",
            "Carryover beyond two iterations surfaces review risk.",
            case_iteration_ops_negative_carryover_too_long,
        ),
        EvalCase(
            "iteration_ops_positive_review_loop_complete",
            "positive",
            "v9-iteration-ops-check.py",
            "Released iteration with complete review rule/eval/skill loop stays green.",
            case_iteration_ops_positive_review_loop_complete,
        ),
        EvalCase(
            "iteration_ops_negative_missing_workbench",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration cannot omit 迭代管理/README.md.",
            case_iteration_ops_negative_missing_workbench,
        ),
        EvalCase(
            "iteration_ops_negative_missing_write_boundary",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration cannot omit 智能体写入边界.",
            case_iteration_ops_negative_missing_write_boundary,
        ),
        EvalCase(
            "iteration_ops_negative_write_boundary_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Write boundary doc missing core contract parts surfaces advisory.",
            case_iteration_ops_negative_write_boundary_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_release_collection_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Release collection doc missing core contract parts surfaces advisory.",
            case_iteration_ops_negative_release_collection_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_material_manifest_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Material manifest missing core contract parts surfaces advisory.",
            case_iteration_ops_negative_material_manifest_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_missing_carryover_ledger",
            "negative",
            "v9-iteration-ops-check.py",
            "Current iteration cannot omit 遗留项台账.",
            case_iteration_ops_negative_missing_carryover_ledger,
        ),
        EvalCase(
            "iteration_ops_negative_carryover_ledger_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Carryover ledger missing core contract parts surfaces advisory.",
            case_iteration_ops_negative_carryover_ledger_incomplete,
        ),
        EvalCase(
            "iteration_ops_positive_visual_preview_checked",
            "positive",
            "v9-iteration-ops-check.py",
            "HTML artifact in iteration dir with checked preview record is allowed.",
            case_iteration_ops_positive_visual_preview_checked,
        ),
        EvalCase(
            "iteration_ops_negative_visual_preview_missing",
            "negative",
            "v9-iteration-ops-check.py",
            "HTML artifact in iteration dir without preview record surfaces advisory.",
            case_iteration_ops_negative_visual_preview_missing,
        ),
        EvalCase(
            "iteration_ops_positive_declared_prototype_checked",
            "positive",
            "v9-iteration-ops-check.py",
            "Declared external prototype root with checked preview and complete requirement coverage is allowed.",
            case_iteration_ops_positive_declared_prototype_checked,
        ),
        EvalCase(
            "iteration_ops_negative_declared_prototype_requirement_gaps",
            "negative",
            "v9-iteration-ops-check.py",
            "Reachable prototype entries cannot hide partial or missing formal requirements.",
            case_iteration_ops_negative_declared_prototype_requirement_gaps,
        ),
        EvalCase(
            "iteration_ops_positive_fact_chain_consistent",
            "positive",
            "v9-iteration-ops-check.py",
            "Scope, Vault mapping, prototype detail, module writeback and execution ledger remain consistent.",
            case_iteration_ops_positive_fact_chain_consistent,
        ),
        EvalCase(
            "iteration_ops_negative_fact_chain_inconsistent",
            "negative",
            "v9-iteration-ops-check.py",
            "A prototype gap omitted from module writeback is rejected as fact-chain drift.",
            case_iteration_ops_negative_fact_chain_inconsistent,
        ),
        EvalCase(
            "iteration_ops_positive_declared_prototype_script_shell",
            "positive",
            "v9-iteration-ops-check.py",
            "Declared external prototype JS shell with script src is allowed.",
            case_iteration_ops_positive_declared_prototype_script_shell,
        ),
        EvalCase(
            "iteration_ops_negative_declared_prototype_pending",
            "negative",
            "v9-iteration-ops-check.py",
            "Declared external prototype root with pending preview surfaces advisory.",
            case_iteration_ops_negative_declared_prototype_pending,
        ),
        EvalCase(
            "iteration_ops_negative_declared_prototype_blank_entry",
            "negative",
            "v9-iteration-ops-check.py",
            "Declared external prototype entry without render signal surfaces advisory.",
            case_iteration_ops_negative_declared_prototype_blank_entry,
        ),
        EvalCase(
            "iteration_ops_negative_review_missing_after_release",
            "negative",
            "v9-iteration-ops-check.py",
            "Released iteration without review.md surfaces advisory.",
            case_iteration_ops_negative_review_missing_after_release,
        ),
        EvalCase(
            "iteration_ops_negative_review_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Iteration review missing rule/eval/skill loop surfaces advisory.",
            case_iteration_ops_negative_review_loop_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_review_carryover_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Iteration review carryover loop missing ledger link or decisions surfaces advisory.",
            case_iteration_ops_negative_review_carryover_loop_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_review_scope_advance_loop_incomplete",
            "negative",
            "v9-iteration-ops-check.py",
            "Iteration review scope-advance loop missing rule/status/decision markers surfaces advisory.",
            case_iteration_ops_negative_review_scope_advance_loop_incomplete,
        ),
        EvalCase(
            "iteration_ops_negative_scope_status_accepted_but_not_applied",
            "negative",
            "v9-iteration-ops-check.py",
            "Accepted scope-status advance must be reflected in the workbench.",
            case_iteration_ops_negative_scope_status_accepted_but_not_applied,
        ),
        EvalCase(
            "iteration_ops_negative_review_accepted_rule_without_eval",
            "negative",
            "v9-iteration-ops-check.py",
            "Review cannot accept checker/eval promotion without done eval evidence.",
            case_iteration_ops_negative_review_accepted_rule_without_eval,
        ),
        EvalCase(
            "iteration_ops_positive_review_accepted_v9_body_done",
            "positive",
            "v9-iteration-ops-check.py",
            "Accepted V9 body promotion with done eval and rule evidence stays green.",
            case_iteration_ops_positive_review_accepted_v9_body_done,
        ),
        EvalCase(
            "iteration_ops_negative_review_decision_matrix_inconsistent",
            "negative",
            "v9-iteration-ops-check.py",
            "V9 body promotion requires checker/eval accepted done in the same review.",
            case_iteration_ops_negative_review_decision_matrix_inconsistent,
        ),
        EvalCase(
            "iteration_ops_negative_review_accepted_v9_body_without_evidence",
            "negative",
            "v9-iteration-ops-check.py",
            "Review cannot accept V9 body promotion without done eval and rule evidence.",
            case_iteration_ops_negative_review_accepted_v9_body_without_evidence,
        ),
        EvalCase(
            "iteration_ops_positive_review_accepted_skill_done",
            "positive",
            "v9-iteration-ops-check.py",
            "Accepted skill/runbook promotion with done writeback stays green.",
            case_iteration_ops_positive_review_accepted_skill_done,
        ),
        EvalCase(
            "iteration_ops_negative_review_accepted_skill_without_writeback",
            "negative",
            "v9-iteration-ops-check.py",
            "Review cannot accept skill/runbook promotion without done writeback.",
            case_iteration_ops_negative_review_accepted_skill_without_writeback,
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
        # ---- 吸收用户检核补充的 fixture ----
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
    optional_phase_files = {
        "phase_e_positive_truth_chains": PHASE_E_TEST,
        "phase_f_positive_capability_truth": PHASE_F_TEST,
        "phase_g_positive_distribution_truth": PHASE_G_TEST,
        "phase_h_positive_long_session_stability": PHASE_H_TEST,
        "codex_hook_adapter_positive_tool_contract": CODEX_HOOK_TEST,
    }
    return [
        case for case in all_cases
        if case.case_id not in optional_phase_files
        or optional_phase_files[case.case_id].is_file()
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
            raise FileNotFoundError(f"Harness trust-set file missing: {rel.as_posix()}")
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
