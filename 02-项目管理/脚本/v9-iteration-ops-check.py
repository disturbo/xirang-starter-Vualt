#!/usr/bin/env python3
"""
v9-iteration-ops-check.py — V9.5 monthly iteration-ops validator.

Scope:
  - Read-only scan of the current project iteration structure.
  - Starts with deterministic checks for the existing 基线/迭代双区 contract.
  - Does not change project files, task cards, gates, or reflex state.

Usage:
  python3 02-项目管理/脚本/v9-iteration-ops-check.py
  python3 02-项目管理/脚本/v9-iteration-ops-check.py --project-root 10-项目 --json
  python3 02-项目管理/脚本/v9-iteration-ops-check.py --strict
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(".")
CHECK_NAME = "v9-iteration-ops-check"
SOURCE = "iteration-ops"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}

REQUIRED_MANAGEMENT_DOCS = [
    ("README.md", "迭代工作台"),
    ("{iteration}-需求范围划定草案.md", "需求范围草案"),
    ("{iteration}-模块变更台账.md", "模块变更台账"),
    ("{iteration}-材料迁移manifest.md", "材料迁移 manifest"),
    ("{iteration}-封版归集清单.md", "封版归集清单"),
    ("{iteration}-智能体写入边界.md", "智能体写入边界"),
    ("{iteration}-遗留项台账.md", "遗留项台账"),
]

REVIEW_DOC_TEMPLATES = [
    "{iteration}-review.md",
    "review.md",
]

FRONTMATTER_REQUIRED_DOCS = {
    "README.md",
    "{iteration}-需求范围划定草案.md",
    "{iteration}-模块变更台账.md",
    "{iteration}-材料迁移manifest.md",
    "{iteration}-封版归集清单.md",
    "{iteration}-智能体写入边界.md",
    "{iteration}-遗留项台账.md",
}

VISUAL_ARTIFACT_EXTS = {".html", ".htm"}
PREVIEW_MARKERS = [
    "preview_status: checked",
    "preview_status: not_applicable",
    "visual_preview: not_applicable",
    "preview_tool:",
    "flyfish viewer",
]

REVIEW_REQUIRED_SECTIONS = [
    "## 遗留项回填",
    "## 状态晋升建议回填",
    "## 规则晋升候选",
    "## Eval 回填",
    "## Skill 回填",
]

REVIEW_CARRYOVER_REQUIRED_MARKERS = [
    "[[{iteration}-遗留项台账",
    "close_decision",
    "关闭",
    "拆分",
    "升级",
]

REVIEW_SCOPE_ADVANCE_REQUIRED_MARKERS = [
    "SCOPE_STATUS_ADVANCE_AVAILABLE",
    "current_status",
    "suggested_status",
    "接受",
    "拒绝",
    "延后",
]

AFFIRMATIVE_REVIEW_DECISIONS = {"是", "进入", "接受", "yes", "true", "approved"}

REVIEW_TRIGGER_STATUSES = {
    "released",
    "reviewed",
    "已封版",
    "封版完成",
    "已发布",
    "已复盘",
}

SCOPE_STATUS_ALIASES = {
    "planning": {"planning", "draft", "范围待确认", "待评审", "框架占位"},
    "scoped": {"scoped", "范围已确认", "已确认"},
    "frozen": {"frozen", "已冻结", "范围冻结", "范围已冻结"},
    "released": {"released", "已封版", "封版完成", "已发布"},
    "reviewed": {"reviewed", "已复盘"},
    "aborted": {"aborted", "已中止", "中止"},
}

SCOPE_LOCKED_STATES = {"frozen", "released", "reviewed"}
SCOPE_INCLUDE_MARKERS = ["纳入725", "纳入 725", "纳入725候选", "纳入 725 候选", "纳入 725 风险池"]
SCOPE_STATUS_REQUIRED_FIELDS = {
    "scoped": ["scope_confirmed_at", "scope_decision_ref"],
    "released": ["released_at", "release_ref"],
    "reviewed": ["released_at", "release_ref", "reviewed_at", "review_ref"],
}

SCOPE_STATUS_ADVANCE_RULES = {
    "planning": ("scoped", ["scope_confirmed_at", "scope_decision_ref"]),
    "scoped": ("frozen", ["scope_included_count", "scope_frozen_at", "scope_freeze_ref"]),
    "frozen": ("released", ["released_at", "release_ref"]),
    "released": ("reviewed", ["reviewed_at", "review_ref"]),
}

DOUBLE_TIME_REQUIRED_TEMPLATES = {
    "README.md",
}

DOUBLE_TIME_FIELDS = ["observed_at", "recorded_at"]
METADATA_TIME_FIELDS = ["observed_at", "recorded_at", "valid_from", "valid_until"]

MEMORY_ANCHOR_REQUIRED_TEMPLATES = {
    "README.md",
}

MEMORY_ANCHOR_FIELDS = ["valid_for", "iteration_root", "baseline_root"]

REQUIRED_AGENT_ASSIGNMENT_ROLES = {
    "scope_definition": "范围定义",
    "prd_and_prototype": "PRD 与原型",
    "code_implementation": "代码实现",
    "research_and_data": "研究与资料",
    "iteration_coord": "迭代协调",
    "v9_harness_review": "V9 Harness 评审",
}

REQUIRED_AGENT_ASSIGNMENT_FIELDS = ["owner", "responsibility"]
AGENT_ASSIGNMENT_UNAVAILABLE_STATUSES = {
    "unavailable",
    "paused",
    "blocked",
    "inactive",
    "缺席",
    "暂停",
    "阻塞",
    "不可用",
    "不参与",
    "暂不参与",
}

WRITE_BOUNDARY_TEMPLATE = "{iteration}-智能体写入边界.md"

WRITE_BOUNDARY_REQUIRED_MARKERS = {
    "iteration_write_root": ["默认写入迭代区", "10-项目/迭代/{iteration}迭代/"],
    "baseline_release_only": ["只有封版归集任务可以写基线", "10-项目/基线"],
    "manifest_before_move": ["未登记 manifest", "先登记"],
    "release_before_baseline": ["未生成 tag", "压缩包前覆盖基线"],
}

RELEASE_COLLECTION_TEMPLATE = "{iteration}-封版归集清单.md"

RELEASE_COLLECTION_REQUIRED_MARKERS = {
    "release_goal": ["基线", "吸收本迭代的最新稳定内容"],
    "release_principles": ["文件级全量替换或新增", "未进入本轮范围的模块不改基线", "归集前先生成 Git tag 或封版压缩包"],
    "ledger_columns": ["| 源文件 | 目标基线文件 | 归集方式 | 评审状态 | 引用更新 | 备注 |"],
    "completion_conditions": ["260725-模块变更台账", "260725-材料迁移manifest", "module-registry.json", "基线入口"],
}

MATERIAL_MANIFEST_TEMPLATE = "{iteration}-材料迁移manifest.md"

MATERIAL_MANIFEST_REQUIRED_MARKERS = {
    "register_before_move": ["跨目录移动先登记", "再执行"],
    "ledger_columns": ["| 源路径 | 目标路径 | 迭代归属 | 是否跨迭代共用 | 引用更新状态 | 备注 |"],
    "row_by_row": ["逐行执行", "不批量盲搬"],
    "reference_update": ["搬迁后更新 Obsidian wikilink", "明文路径"],
    "post_move_checks": ["搬迁后运行链接与治理检查"],
}

CARRYOVER_LEDGER_TEMPLATE = "{iteration}-遗留项台账.md"

CARRYOVER_LEDGER_REQUIRED_MARKERS = {
    "ledger_goal": ["跨迭代遗留项", "关闭、拆分或升级"],
    "ledger_columns": ["| 遗留项 | 来源文件 | carryover_to | carryover_count | owner | close_decision | next_review_at | 状态 |"],
    "count_rule": ["carryover_count", "超过 2 个迭代"],
    "close_decision": ["close_decision", "关闭", "拆分", "升级"],
    "review_loop": ["月末 review", "规则/eval/skill"],
}

BASELINE_WRITE_AUTH_TRUE = {"true", "yes", "authorized", "approved", "已授权", "允许"}
CARRYOVER_CLOSED_STATUSES = {"done", "closed", "resolved", "released", "accepted", "已关闭", "已完成", "完成", "已发布", "已验收"}
CARRYOVER_LIMIT = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


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


def normalize_status(value: str) -> str:
    return clean_value(value).lower()


def canonical_scope_status(value: str) -> str:
    normalized = normalize_status(value)
    if not normalized:
        return ""
    for canonical, aliases in SCOPE_STATUS_ALIASES.items():
        if normalized in {normalize_status(item) for item in aliases}:
            return canonical
    return ""


def make_finding(
    severity: str,
    rule_id: str,
    obj: str,
    message: str,
    detail: dict | None = None,
) -> dict:
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


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def path_anchor_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    try:
        return Path(actual).resolve() == Path(expected).resolve()
    except OSError:
        return False


def parse_frontmatter_time(value: str) -> datetime | None:
    cleaned = clean_value(value)
    if not cleaned or normalize_status(cleaned) == "null":
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return datetime.fromisoformat(f"{cleaned}T00:00:00").astimezone()
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def resolve_supersedes_target(path: Path, target: str) -> Path:
    cleaned = clean_value(target)
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].split("|", 1)[0]
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate
    if not candidate.suffix:
        candidate = candidate.with_suffix(".md")
    return (path.parent / candidate).resolve()


def current_iteration_from_readme(text: str) -> tuple[str, str]:
    """Return (iteration, iteration_path_hint) from the project root README.

    Primary source is the wiki link used by 10-项目/README.md:
    [[迭代/260725迭代/README|260725迭代]]
    Fallback is the first table cell that looks like a 6-digit iteration id.
    """
    link = re.search(r"\[\[(迭代/(\d{6})迭代)(?:/README)?(?:\|[^\]]+)?\]\]", text)
    if link:
        return link.group(2), link.group(1)

    table_match = re.search(r"\|\s*[^|\n]+\s*\|\s*[^|\n]+\s*\|\s*(\d{6})\s*\|", text)
    if table_match:
        iteration = table_match.group(1)
        return iteration, f"迭代/{iteration}迭代"

    loose = re.search(r"(\d{6})迭代", text)
    if loose:
        iteration = loose.group(1)
        return iteration, f"迭代/{iteration}迭代"

    return "", ""


def management_doc_paths(management_root: Path, iteration: str) -> list[tuple[Path, str, str]]:
    docs: list[tuple[Path, str, str]] = []
    for template, label in REQUIRED_MANAGEMENT_DOCS:
        name = template.format(iteration=iteration)
        docs.append((management_root / name, template, label))
    return docs


def parse_agent_assignments(fm: str) -> dict[str, dict[str, str]]:
    assignments: dict[str, dict[str, str]] = {}
    current_role = ""
    in_section = False
    for line in fm.splitlines():
        if re.match(r"^agent_assignments:\s*$", line):
            in_section = True
            current_role = ""
            continue
        if not in_section:
            continue
        if line and not line.startswith(" "):
            break
        role = re.match(r"^\s{2}([A-Za-z0-9_]+):\s*$", line)
        if role:
            current_role = role.group(1)
            assignments.setdefault(current_role, {})
            continue
        field = re.match(r"^\s{4}([A-Za-z0-9_]+):\s*(.+?)\s*$", line)
        if field and current_role:
            assignments[current_role][field.group(1)] = clean_value(field.group(2))
    return assignments


def check_agent_assignments_contract(path: Path, fm: str) -> list[dict]:
    assignments = parse_agent_assignments(fm)
    if not assignments:
        return [
            make_finding(
                "advisory",
                "AGENT_ASSIGNMENTS_MISSING",
                rel(path),
                f"{rel(path)}: 当前迭代尚未声明 agent_assignments。",
            )
        ]

    missing_roles = [role for role in REQUIRED_AGENT_ASSIGNMENT_ROLES if role not in assignments]
    missing_fields = []
    missing_fallbacks = []
    for role, fields in assignments.items():
        if role not in REQUIRED_AGENT_ASSIGNMENT_ROLES:
            continue
        for field in REQUIRED_AGENT_ASSIGNMENT_FIELDS:
            if not fields.get(field):
                missing_fields.append({"role": role, "field": field})
        status = normalize_status(fields.get("status", ""))
        if status in AGENT_ASSIGNMENT_UNAVAILABLE_STATUSES and not fields.get("fallback"):
            missing_fallbacks.append({"role": role, "status": fields.get("status", "")})

    findings: list[dict] = []
    if missing_roles:
        findings.append(
            make_finding(
                "advisory",
                "AGENT_ASSIGNMENTS_INCOMPLETE",
                rel(path),
                f"{rel(path)}: agent_assignments 缺关键职责。",
                {
                    "missing_roles": missing_roles,
                    "labels": {role: REQUIRED_AGENT_ASSIGNMENT_ROLES[role] for role in missing_roles},
                },
            )
        )
    if missing_fields:
        findings.append(
            make_finding(
                "advisory",
                "AGENT_ASSIGNMENTS_FIELD_MISSING",
                rel(path),
                f"{rel(path)}: agent_assignments 中部分职责缺 owner 或 responsibility。",
                {"missing_fields": missing_fields},
            )
        )
    if missing_fallbacks:
        findings.append(
            make_finding(
                "advisory",
                "AGENT_ASSIGNMENT_FALLBACK_MISSING",
                rel(path),
                f"{rel(path)}: agent_assignments 中存在不可用角色但未声明 fallback。",
                {"missing_fallbacks": missing_fallbacks},
            )
        )
    return findings


def check_memory_anchor_contract(
    path: Path,
    fm: str,
    iteration: str,
    expected_iteration_root: str,
    expected_baseline_root: str | None = None,
    required_fields: list[str] | None = None,
) -> list[dict]:
    required = required_fields or ["valid_for"]
    missing_fields = [field for field in required if not fm_value(fm, field) or fm_value(fm, field) == "null"]
    findings: list[dict] = []
    if missing_fields:
        findings.append(
            make_finding(
                "advisory",
                "MEMORY_ANCHOR_MISSING",
                rel(path),
                f"{rel(path)}: 缺 Memory Contract 锚点字段，无法稳定判断当前事实适用范围。",
                {"missing_fields": missing_fields},
            )
        )

    valid_for = fm_value(fm, "valid_for")
    if valid_for and valid_for != iteration:
        findings.append(
            make_finding(
                "p1",
                "VALID_FOR_MISMATCH",
                rel(path),
                f"{rel(path)}: valid_for={valid_for}，但当前迭代为 {iteration}。",
                {"expected": iteration, "actual": valid_for},
            )
        )

    iteration_root = fm_value(fm, "iteration_root")
    if iteration_root and not path_anchor_matches(iteration_root, expected_iteration_root):
        findings.append(
            make_finding(
                "p1",
                "ITERATION_ROOT_MISMATCH",
                rel(path),
                f"{rel(path)}: iteration_root={iteration_root}，但当前迭代根为 {expected_iteration_root}。",
                {"expected": expected_iteration_root, "actual": iteration_root},
            )
        )

    baseline_root = fm_value(fm, "baseline_root")
    if expected_baseline_root and baseline_root and not path_anchor_matches(baseline_root, expected_baseline_root):
        findings.append(
            make_finding(
                "p1",
                "BASELINE_ROOT_MISMATCH",
                rel(path),
                f"{rel(path)}: baseline_root={baseline_root}，但当前基线根为 {expected_baseline_root}。",
                {"expected": expected_baseline_root, "actual": baseline_root},
            )
        )

    return findings


def check_metadata_integrity(path: Path, fm: str) -> list[dict]:
    findings: list[dict] = []
    parsed_times: dict[str, datetime] = {}
    invalid_time_fields: list[dict] = []

    for field in METADATA_TIME_FIELDS:
        value = fm_value(fm, field)
        if not value or normalize_status(value) == "null":
            continue
        parsed = parse_frontmatter_time(value)
        if parsed is None:
            invalid_time_fields.append({"field": field, "value": value})
        else:
            parsed_times[field] = parsed

    if invalid_time_fields:
        findings.append(
            make_finding(
                "advisory",
                "METADATA_TIME_INVALID",
                rel(path),
                f"{rel(path)}: 时间元数据格式无法解析。",
                {"invalid_fields": invalid_time_fields},
            )
        )

    order_errors: list[dict] = []
    if "observed_at" in parsed_times and "recorded_at" in parsed_times and parsed_times["observed_at"] > parsed_times["recorded_at"]:
        order_errors.append({"earlier": "observed_at", "later": "recorded_at"})
    if "valid_from" in parsed_times and "valid_until" in parsed_times and parsed_times["valid_from"] > parsed_times["valid_until"]:
        order_errors.append({"earlier": "valid_from", "later": "valid_until"})
    if order_errors:
        findings.append(
            make_finding(
                "advisory",
                "METADATA_TIME_ORDER_INVALID",
                rel(path),
                f"{rel(path)}: 时间元数据顺序异常。",
                {"order_errors": order_errors},
            )
        )

    supersedes = fm_value(fm, "supersedes")
    if supersedes and normalize_status(supersedes) != "null":
        target = resolve_supersedes_target(path, supersedes)
        if not target.exists():
            findings.append(
                make_finding(
                    "advisory",
                    "SUPERSEDES_TARGET_MISSING",
                    rel(path),
                    f"{rel(path)}: supersedes 指向的文件不存在。",
                    {"supersedes": supersedes, "resolved": rel(target)},
                )
            )

    return findings


def count_included_scope_items(scope_doc: Path) -> int:
    if not scope_doc.exists():
        return 0
    count = 0
    for line in read_text(scope_doc).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        if "YJ725-" not in stripped:
            continue
        normalized = stripped.replace(" ", "")
        if any(marker.replace(" ", "") in normalized for marker in SCOPE_INCLUDE_MARKERS):
            count += 1
    return count


def check_scope_state_contract(workbench: Path, scope_doc: Path, iteration: str) -> tuple[list[dict], int]:
    text = read_text(workbench)
    fm = frontmatter(text)
    scope_status = fm_value(fm, "scope_status")
    canonical = canonical_scope_status(scope_status)
    findings: list[dict] = []

    if scope_status and not canonical:
        findings.append(
            make_finding(
                "advisory",
                "SCOPE_STATUS_UNKNOWN",
                rel(workbench),
                f"{rel(workbench)}: scope_status={scope_status} 不在 V9.5 迭代状态机已知状态内。",
                {"actual": scope_status, "known_states": sorted(SCOPE_STATUS_ALIASES)},
            )
        )

    included_count = count_included_scope_items(scope_doc)

    required_transition_fields = SCOPE_STATUS_REQUIRED_FIELDS.get(canonical, [])
    missing_transition_fields = [
        field for field in required_transition_fields if not fm_value(fm, field) or fm_value(fm, field) == "null"
    ]
    if missing_transition_fields:
        findings.append(
            make_finding(
                "advisory",
                "SCOPE_STATUS_TRANSITION_INCOMPLETE",
                rel(workbench),
                f"{rel(workbench)}: scope_status={scope_status} 已进入 {canonical}，但缺状态转换证据字段。",
                {
                    "scope_status": scope_status,
                    "canonical_status": canonical,
                    "missing_fields": missing_transition_fields,
                },
            )
        )

    advance_rule = SCOPE_STATUS_ADVANCE_RULES.get(canonical)
    if advance_rule:
        target_status, evidence_fields = advance_rule
        has_evidence = all(fm_value(fm, field) and fm_value(fm, field) != "null" for field in evidence_fields)
        count_matches = True
        if target_status == "frozen":
            try:
                count_matches = int(fm_value(fm, "scope_included_count")) == included_count
            except ValueError:
                count_matches = False
        if has_evidence and count_matches:
            findings.append(
                make_finding(
                    "advisory",
                    "SCOPE_STATUS_ADVANCE_AVAILABLE",
                    rel(workbench),
                    f"{rel(workbench)}: 已具备从 {canonical} 晋升到 {target_status} 的证据字段，可由人工确认后更新 scope_status。",
                    {
                        "current_status": canonical,
                        "suggested_status": target_status,
                        "evidence_fields": evidence_fields,
                    },
                )
            )

    if canonical not in SCOPE_LOCKED_STATES:
        return findings, included_count

    expected_raw = fm_value(fm, "scope_included_count")
    frozen_at = fm_value(fm, "scope_frozen_at")
    freeze_ref = fm_value(fm, "scope_freeze_ref")
    if not expected_raw or not frozen_at or not freeze_ref:
        findings.append(
            make_finding(
                "advisory",
                "SCOPE_FREEZE_BASELINE_MISSING",
                rel(workbench),
                f"{rel(workbench)}: scope_status={scope_status} 已进入冻结/发布阶段，但缺 scope_included_count、scope_frozen_at 或 scope_freeze_ref。",
                {
                    "scope_status": scope_status,
                    "missing_fields": [
                        field
                        for field, value in {
                            "scope_included_count": expected_raw,
                            "scope_frozen_at": frozen_at,
                            "scope_freeze_ref": freeze_ref,
                        }.items()
                        if not value or value == "null"
                    ],
                    "current_included_count": included_count,
                },
            )
        )
        return findings, included_count

    try:
        expected_count = int(expected_raw)
    except ValueError:
        findings.append(
            make_finding(
                "advisory",
                "SCOPE_FREEZE_BASELINE_INVALID",
                rel(workbench),
                f"{rel(workbench)}: scope_included_count={expected_raw} 不是整数，无法比较冻结后新增范围项。",
                {"actual": expected_raw, "current_included_count": included_count},
            )
        )
        return findings, included_count

    if included_count > expected_count:
        findings.append(
            make_finding(
                "p1",
                "SCOPE_ADDED_AFTER_FREEZE",
                rel(scope_doc),
                f"{rel(scope_doc)}: 当前纳入类范围项数量 {included_count} 超过冻结基准 {expected_count}；冻结后新增范围必须先走人工评审和范围变更记录。",
                {
                    "iteration": iteration,
                    "scope_status": scope_status,
                    "expected_included_count": expected_count,
                    "current_included_count": included_count,
                    "scope_freeze_ref": freeze_ref,
                    "scope_frozen_at": frozen_at,
                },
            )
        )

    return findings, included_count


def check_frontmatter(
    path: Path,
    template: str,
    iteration: str,
    expected_iteration_root: str,
    expected_baseline_root: str,
) -> list[dict]:
    text = read_text(path)
    fm = frontmatter(text)
    findings: list[dict] = []
    if not fm:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_FRONTMATTER_MISSING",
                rel(path),
                f"{rel(path)}: 缺 frontmatter，后续无法稳定读取迭代元数据。",
            )
        )
        return findings

    actual_iteration = fm_value(fm, "iteration")
    if not actual_iteration:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_FIELD_MISSING",
                rel(path),
                f"{rel(path)}: 缺 iteration 字段。",
            )
        )
    elif actual_iteration != iteration:
        findings.append(
            make_finding(
                "p1",
                "ITERATION_FIELD_MISMATCH",
                rel(path),
                f"{rel(path)}: iteration={actual_iteration}，但当前迭代为 {iteration}。",
                {"expected": iteration, "actual": actual_iteration},
            )
        )

    findings.extend(check_metadata_integrity(path, fm))

    if template in DOUBLE_TIME_REQUIRED_TEMPLATES:
        missing_time_fields = [field for field in DOUBLE_TIME_FIELDS if not fm_value(fm, field) or fm_value(fm, field) == "null"]
        if missing_time_fields:
            findings.append(
                make_finding(
                    "advisory",
                    "DOUBLE_TIME_MISSING",
                    rel(path),
                    f"{rel(path)}: 缺 observed_at / recorded_at 双时间字段，无法区分事实时间和写入时间。",
                    {"missing_fields": missing_time_fields},
                )
            )

    if template in MEMORY_ANCHOR_REQUIRED_TEMPLATES:
        findings.extend(
            check_memory_anchor_contract(
                path,
                fm,
                iteration,
                expected_iteration_root,
                expected_baseline_root,
                MEMORY_ANCHOR_FIELDS,
            )
        )

    if template == "README.md":
        scope_status = fm_value(fm, "scope_status")
        if not scope_status:
            findings.append(
                make_finding(
                    "advisory",
                    "SCOPE_STATUS_MISSING",
                    rel(path),
                    f"{rel(path)}: 迭代工作台缺 scope_status。",
                )
            )
        findings.extend(check_agent_assignments_contract(path, fm))

    return findings


def check_write_boundary_contract(path: Path, iteration: str) -> list[dict]:
    text = read_text(path)
    missing: list[str] = []
    for key, markers in WRITE_BOUNDARY_REQUIRED_MARKERS.items():
        expected_markers = [marker.format(iteration=iteration) for marker in markers]
        if not all(marker in text for marker in expected_markers):
            missing.append(key)
    if not missing:
        return []
    return [
        make_finding(
            "advisory",
            "WRITE_BOUNDARY_CONTRACT_INCOMPLETE",
            rel(path),
            f"{rel(path)}: 智能体写入边界未完整声明默认迭代写区、基线归集例外、manifest 先登记或封版保护。",
            {"missing_contract_parts": missing},
        )
    ]


def check_release_collection_contract(path: Path, iteration: str) -> list[dict]:
    text = read_text(path)
    missing: list[str] = []
    for key, markers in RELEASE_COLLECTION_REQUIRED_MARKERS.items():
        expected_markers = [marker.format(iteration=iteration) for marker in markers]
        if not all(marker in text for marker in expected_markers):
            missing.append(key)
    if not missing:
        return []
    return [
        make_finding(
            "advisory",
            "RELEASE_COLLECTION_CONTRACT_INCOMPLETE",
            rel(path),
            f"{rel(path)}: 封版归集清单未完整声明归集目标、归集原则、台账列或完成条件。",
            {"missing_contract_parts": missing},
        )
    ]


def check_material_manifest_contract(path: Path, iteration: str) -> list[dict]:
    text = read_text(path)
    missing: list[str] = []
    for key, markers in MATERIAL_MANIFEST_REQUIRED_MARKERS.items():
        expected_markers = [marker.format(iteration=iteration) for marker in markers]
        if not all(marker in text for marker in expected_markers):
            missing.append(key)
    if not missing:
        return []
    return [
        make_finding(
            "advisory",
            "MATERIAL_MANIFEST_CONTRACT_INCOMPLETE",
            rel(path),
            f"{rel(path)}: 材料迁移 manifest 未完整声明先登记、台账列、逐行执行、引用更新或搬迁后检查。",
            {"missing_contract_parts": missing},
        )
    ]


def check_carryover_ledger_contract(path: Path, iteration: str) -> list[dict]:
    text = read_text(path)
    missing: list[str] = []
    for key, markers in CARRYOVER_LEDGER_REQUIRED_MARKERS.items():
        expected_markers = [marker.format(iteration=iteration) for marker in markers]
        if not all(marker in text for marker in expected_markers):
            missing.append(key)
    if not missing:
        return []
    return [
        make_finding(
            "advisory",
            "CARRYOVER_LEDGER_CONTRACT_INCOMPLETE",
            rel(path),
            f"{rel(path)}: 遗留项台账未完整声明目标、台账列、carryover_count 规则、close_decision 或 review 晋升闭环。",
            {"missing_contract_parts": missing},
        )
    ]


def git_root_for(path: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def git_changed_paths_under(path: Path) -> list[str]:
    git_root = git_root_for(path)
    if not git_root:
        return []
    try:
        target = path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain=v1", "-z", "--", target],
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []

    changed: list[str] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            entry = raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
        if len(entry) < 4:
            continue
        changed.append(entry[3:])
    return changed


def baseline_write_authorized(release_doc: Path, scope_status: str) -> tuple[bool, dict]:
    if canonical_scope_status(scope_status) not in {"released", "reviewed"}:
        return False, {"reason": "scope_status_not_released", "scope_status": scope_status or None}
    if not release_doc.exists():
        return False, {"reason": "release_doc_missing", "scope_status": scope_status or None}

    fm = frontmatter(read_text(release_doc))
    authorized = normalize_status(fm_value(fm, "baseline_write_authorized"))
    if authorized in {normalize_status(item) for item in BASELINE_WRITE_AUTH_TRUE}:
        return True, {"reason": "authorized_by_release_collection", "baseline_write_authorized": authorized}
    return False, {
        "reason": "baseline_write_authorized_missing",
        "scope_status": scope_status or None,
        "baseline_write_authorized": authorized or None,
    }


def check_baseline_write_contract(
    project_root: Path,
    management_root: Path,
    iteration: str,
    scope_status: str,
) -> tuple[list[dict], int]:
    baseline_root = project_root / "基线"
    changed_paths = git_changed_paths_under(baseline_root)
    if not changed_paths:
        return [], 0

    release_doc = management_root / RELEASE_COLLECTION_TEMPLATE.format(iteration=iteration)
    authorized, auth_detail = baseline_write_authorized(release_doc, scope_status)
    if authorized:
        return [], len(changed_paths)

    return [
        make_finding(
            "p1",
            "BASELINE_WRITE_WITHOUT_RELEASE",
            rel(baseline_root),
            "检测到基线区存在未提交变更，但当前迭代没有封版归集显式授权；基线改写必须先走封版归集清单和人工拍板。",
            {
                "iteration": iteration,
                "changed_paths": changed_paths,
                **auth_detail,
            },
        )
    ], len(changed_paths)


def is_closed_status(status: str) -> bool:
    return normalize_status(status) in {normalize_status(item) for item in CARRYOVER_CLOSED_STATUSES}


def check_carryover_contract(iteration_root: Path) -> tuple[list[dict], int]:
    docs = markdown_index(iteration_root)
    findings: list[dict] = []
    checked = 0
    for path, text in docs:
        fm = frontmatter(text)
        if not fm:
            continue
        carryover_to = fm_value(fm, "carryover_to")
        carryover_count_raw = fm_value(fm, "carryover_count")
        if not carryover_to or carryover_to == "null" or not carryover_count_raw:
            continue
        checked += 1
        if is_closed_status(fm_value(fm, "status")):
            continue
        try:
            carryover_count = int(carryover_count_raw)
        except ValueError:
            findings.append(
                make_finding(
                    "advisory",
                    "CARRYOVER_COUNT_INVALID",
                    rel(path),
                    f"{rel(path)}: carryover_count={carryover_count_raw} 不是整数，无法判断跨迭代遗留时长。",
                    {"carryover_to": carryover_to, "carryover_count": carryover_count_raw},
                )
            )
            continue
        if carryover_count > CARRYOVER_LIMIT:
            findings.append(
                make_finding(
                    "advisory",
                    "CARRYOVER_TOO_LONG",
                    rel(path),
                    f"{rel(path)}: 已连续遗留 {carryover_count} 个迭代，超过 V9.5 建议上限 {CARRYOVER_LIMIT}；月末 review 需要拍板关闭、拆分或升级。",
                    {
                        "carryover_to": carryover_to,
                        "carryover_count": carryover_count,
                        "limit": CARRYOVER_LIMIT,
                        "status": fm_value(fm, "status") or None,
                    },
                )
            )

    return findings, checked


def review_doc_paths(management_root: Path, iteration: str) -> list[Path]:
    return [management_root / template.format(iteration=iteration) for template in REVIEW_DOC_TEMPLATES]


def find_review_doc(management_root: Path, iteration: str) -> Path | None:
    for path in review_doc_paths(management_root, iteration):
        if path.exists():
            return path
    return None


def review_required_for_status(scope_status: str) -> bool:
    normalized = normalize_status(scope_status)
    if not normalized:
        return False
    return normalized in {normalize_status(item) for item in REVIEW_TRIGGER_STATUSES}


def check_review_contract(path: Path, iteration: str, expected_iteration_root: str) -> list[dict]:
    text = read_text(path)
    fm = frontmatter(text)
    findings: list[dict] = []

    if not fm:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_REVIEW_FRONTMATTER_MISSING",
                rel(path),
                f"{rel(path)}: 缺 frontmatter，review 无法进入规则/eval/skill 晋升闭环。",
            )
        )
        return findings

    actual_iteration = fm_value(fm, "iteration")
    if actual_iteration and actual_iteration != iteration:
        findings.append(
            make_finding(
                "p1",
                "ITERATION_REVIEW_FIELD_MISMATCH",
                rel(path),
                f"{rel(path)}: iteration={actual_iteration}，但当前迭代为 {iteration}。",
                {"expected": iteration, "actual": actual_iteration},
            )
        )
    elif not actual_iteration:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_REVIEW_FIELD_MISSING",
                rel(path),
                f"{rel(path)}: 缺 iteration 字段。",
            )
        )

    missing_time_fields = [field for field in DOUBLE_TIME_FIELDS if not fm_value(fm, field) or fm_value(fm, field) == "null"]
    if missing_time_fields:
        findings.append(
            make_finding(
                "advisory",
                "DOUBLE_TIME_MISSING",
                rel(path),
                f"{rel(path)}: 缺 observed_at / recorded_at 双时间字段，无法区分复盘事实时间和写入时间。",
                {"missing_fields": missing_time_fields},
            )
        )

    findings.extend(
        check_memory_anchor_contract(
            path,
            fm,
            iteration,
            expected_iteration_root,
            required_fields=["valid_for"],
        )
    )

    doc_type = normalize_status(fm_value(fm, "type"))
    if doc_type and doc_type != "iteration_review":
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_REVIEW_TYPE_UNEXPECTED",
                rel(path),
                f"{rel(path)}: type={doc_type}，建议使用 iteration_review。",
                {"expected": "iteration_review", "actual": doc_type},
            )
        )
    elif not doc_type:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_REVIEW_TYPE_MISSING",
                rel(path),
                f"{rel(path)}: 缺 type: iteration_review。",
            )
        )

    missing_sections = [section for section in REVIEW_REQUIRED_SECTIONS if section not in text]
    if missing_sections:
        findings.append(
            make_finding(
                "advisory",
                "ITERATION_REVIEW_PROMOTION_LOOP_INCOMPLETE",
                rel(path),
                f"{rel(path)}: review 缺规则/eval/skill 晋升闭环章节。",
                {"missing_sections": missing_sections},
            )
        )

    if "## 遗留项回填" in text:
        missing_carryover_markers = [
            marker.format(iteration=iteration)
            for marker in REVIEW_CARRYOVER_REQUIRED_MARKERS
            if marker.format(iteration=iteration) not in text
        ]
        if missing_carryover_markers:
            findings.append(
                make_finding(
                    "advisory",
                    "ITERATION_REVIEW_CARRYOVER_LOOP_INCOMPLETE",
                    rel(path),
                    f"{rel(path)}: review 的遗留项回填缺台账链接、close_decision 或关闭/拆分/升级动作。",
                    {"missing_markers": missing_carryover_markers},
                )
            )

    if "## 状态晋升建议回填" in text:
        missing_advance_markers = [
            marker.format(iteration=iteration)
            for marker in REVIEW_SCOPE_ADVANCE_REQUIRED_MARKERS
            if marker.format(iteration=iteration) not in text
        ]
        if missing_advance_markers:
            findings.append(
                make_finding(
                    "advisory",
                    "ITERATION_REVIEW_SCOPE_ADVANCE_LOOP_INCOMPLETE",
                    rel(path),
                    f"{rel(path)}: review 的状态晋升建议回填缺规则 ID、current_status、suggested_status 或接受/拒绝/延后决定。",
                    {"missing_markers": missing_advance_markers},
                )
            )

    return findings


def accepted_scope_advance_decisions(review_doc: Path) -> list[dict]:
    if not review_doc.exists():
        return []
    text = read_text(review_doc)
    decisions: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "SCOPE_STATUS_ADVANCE_AVAILABLE" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        decision = cells[4]
        if normalize_status(decision) != "接受":
            continue
        decisions.append(
            {
                "rule": cells[0],
                "current_status": cells[1],
                "suggested_status": cells[2],
                "decision": decision,
            }
        )
    return decisions


def review_checker_eval_decisions(review_doc: Path) -> list[dict]:
    if not review_doc.exists():
        return []
    decisions: list[dict] = []
    for line in read_text(review_doc).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "checker/eval" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        decision = cells[1]
        if normalize_status(decision) not in AFFIRMATIVE_REVIEW_DECISIONS:
            continue
        decisions.append(
            {
                "item": cells[0],
                "decision": decision,
                "eval_status": cells[2],
            }
        )
    return decisions


def review_skill_runbook_decisions(review_doc: Path) -> list[dict]:
    if not review_doc.exists():
        return []
    decisions: list[dict] = []
    for line in read_text(review_doc).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "skill/runbook" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        decision = cells[1]
        if normalize_status(decision) not in AFFIRMATIVE_REVIEW_DECISIONS:
            continue
        decisions.append(
            {
                "item": cells[0],
                "decision": decision,
                "eval_status": cells[2],
            }
        )
    return decisions


def review_v9_body_decisions(review_doc: Path) -> list[dict]:
    if not review_doc.exists():
        return []
    decisions: list[dict] = []
    for line in read_text(review_doc).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "V9 正文" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        decision = cells[1]
        if normalize_status(decision) not in AFFIRMATIVE_REVIEW_DECISIONS:
            continue
        decisions.append(
            {
                "item": cells[0],
                "decision": decision,
                "eval_status": cells[2],
            }
        )
    return decisions


def markdown_section_text(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    rest = text[start + len(heading):]
    next_heading = re.search(r"\n##\s+", rest)
    if next_heading:
        return rest[:next_heading.start()]
    return rest


def review_has_done_eval(review_doc: Path) -> bool:
    if not review_doc.exists():
        return False
    text = read_text(review_doc)
    section = markdown_section_text(text, "## Eval 回填")
    if not section:
        return False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 4 and normalize_status(cells[3]) == "done":
            return True
    return False


def review_has_done_skill_writeback(review_doc: Path) -> bool:
    if not review_doc.exists():
        return False
    section = markdown_section_text(read_text(review_doc), "## Skill 回填")
    if not section:
        return False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        skill_name, _, needs_new, status = cells[:4]
        if normalize_status(skill_name) in {"skill / runbook", "无", "none", "n/a", "待填写"}:
            continue
        if normalize_status(needs_new) in {"是", "yes", "true"} and normalize_status(status) == "done":
            return True
    return False


def review_has_done_rule_candidate(review_doc: Path) -> bool:
    if not review_doc.exists():
        return False
    section = markdown_section_text(read_text(review_doc), "## 规则晋升候选")
    if not section:
        return False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rule_name, _, target_shape, status = cells[:4]
        if normalize_status(rule_name) in {"候选规则", "无", "none", "n/a", "待填写"}:
            continue
        target = normalize_status(target_shape)
        if "v9" in target or "正文" in target or "rule" in target or "checker" in target:
            if normalize_status(status) == "done":
                return True
    return False


def review_has_done_checker_eval_decision(review_doc: Path) -> bool:
    return any(
        normalize_status(decision.get("eval_status", "")) == "done"
        for decision in review_checker_eval_decisions(review_doc)
    )


def check_review_eval_writeback(review_doc: Path) -> list[dict]:
    findings: list[dict] = []
    for decision in review_checker_eval_decisions(review_doc):
        if normalize_status(decision.get("eval_status", "")) == "done" and review_has_done_eval(review_doc):
            continue
        findings.append(
            make_finding(
                "advisory",
                "REVIEW_ACCEPTED_RULE_WITHOUT_EVAL",
                rel(review_doc),
                f"{rel(review_doc)} 已决定进入 checker/eval，但 eval_status 未完成或 Eval 回填缺 done 行。",
                decision,
            )
        )
    return findings


def check_review_decision_matrix(review_doc: Path) -> list[dict]:
    findings: list[dict] = []
    for decision in review_v9_body_decisions(review_doc):
        if review_has_done_checker_eval_decision(review_doc):
            continue
        findings.append(
            make_finding(
                "advisory",
                "REVIEW_DECISION_MATRIX_INCONSISTENT",
                rel(review_doc),
                f"{rel(review_doc)} 已决定进入 V9 正文，但 checker/eval 未同步为接受且 done。",
                {
                    "v9_body_decision": decision,
                    "checker_eval_decisions": review_checker_eval_decisions(review_doc),
                },
            )
        )
    return findings


def check_review_v9_body_writeback(review_doc: Path) -> list[dict]:
    findings: list[dict] = []
    for decision in review_v9_body_decisions(review_doc):
        if (
            normalize_status(decision.get("eval_status", "")) == "done"
            and review_has_done_eval(review_doc)
            and review_has_done_rule_candidate(review_doc)
        ):
            continue
        findings.append(
            make_finding(
                "advisory",
                "REVIEW_ACCEPTED_V9_BODY_WITHOUT_EVIDENCE",
                rel(review_doc),
                f"{rel(review_doc)} 已决定进入 V9 正文，但缺 eval_status done、Eval 回填 done 或规则晋升候选 done 记录。",
                decision,
            )
        )
    return findings


def check_review_skill_writeback(review_doc: Path) -> list[dict]:
    findings: list[dict] = []
    for decision in review_skill_runbook_decisions(review_doc):
        if review_has_done_skill_writeback(review_doc):
            continue
        findings.append(
            make_finding(
                "advisory",
                "REVIEW_ACCEPTED_SKILL_WITHOUT_WRITEBACK",
                rel(review_doc),
                f"{rel(review_doc)} 已决定进入 skill/runbook，但 Skill 回填缺新增项 done 记录。",
                decision,
            )
        )
    return findings


def check_review_scope_advance_writeback(workbench: Path, review_doc: Path) -> list[dict]:
    if not workbench.exists() or not review_doc.exists():
        return []
    fm = frontmatter(read_text(workbench))
    actual_status = canonical_scope_status(fm_value(fm, "scope_status"))
    findings: list[dict] = []
    for decision in accepted_scope_advance_decisions(review_doc):
        suggested = canonical_scope_status(decision.get("suggested_status", "")) or normalize_status(decision.get("suggested_status", ""))
        if suggested and actual_status != suggested:
            findings.append(
                make_finding(
                    "advisory",
                    "SCOPE_STATUS_ACCEPTED_BUT_NOT_APPLIED",
                    rel(workbench),
                    f"{rel(review_doc)} 已接受 scope_status 晋升到 {decision.get('suggested_status')}，但工作台当前状态仍为 {fm_value(fm, 'scope_status')}。",
                    {
                        "accepted_decision": decision,
                        "actual_scope_status": fm_value(fm, "scope_status") or None,
                    },
                )
            )
    return findings


def html_render_signal(path: Path) -> tuple[bool, dict]:
    try:
        text = read_text(path)
    except UnicodeDecodeError:
        return True, {"reason": "non_utf8_html"}

    stripped = text.strip()
    if not stripped:
        return False, {"reason": "empty_file", "bytes": path.stat().st_size}

    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
    body = body_match.group(1) if body_match else text
    without_code = re.sub(r"<!--.*?-->", " ", body, flags=re.DOTALL)
    without_code = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", without_code, flags=re.IGNORECASE | re.DOTALL)
    visible_text = re.sub(r"<[^>]+>", " ", without_code)
    visible_text = html.unescape(re.sub(r"\s+", " ", visible_text)).strip()

    if len(visible_text) >= 4:
        return True, {"reason": "visible_text", "visible_chars": len(visible_text)}

    if re.search(r"<script\b[^>]*\bsrc\s*=", body, re.IGNORECASE):
        return True, {"reason": "script_src"}

    inline_scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", body, flags=re.IGNORECASE | re.DOTALL)
    if any(len(script.strip()) >= 40 for script in inline_scripts):
        return True, {"reason": "inline_script"}

    if re.search(r"<(img|svg|canvas|video|iframe)\b", body, re.IGNORECASE):
        return True, {"reason": "visual_element"}

    return False, {"reason": "no_render_signal", "bytes": path.stat().st_size}


def check_html_entry_render_signal(path: Path, obj: str) -> list[dict]:
    if path.suffix.lower() not in VISUAL_ARTIFACT_EXTS:
        return []
    ok, detail = html_render_signal(path)
    if ok:
        return []
    return [
        make_finding(
            "advisory",
            "VISUAL_ARTIFACT_BLANK_OR_SHELL_EMPTY",
            obj,
            f"{obj}: HTML 入口缺少可见文本、脚本或视觉元素等渲染信号；可能是空白页或空壳页。",
            detail,
        )
    ]


def visual_config_from_workbench(workbench: Path) -> dict:
    text = read_text(workbench)
    fm = frontmatter(text)
    root = fm_value(fm, "prototype_root")
    preview_status = ""
    match = re.search(r"^\s+preview_status:\s*(.+)$", fm, re.MULTILINE)
    if match:
        preview_status = clean_value(match.group(1))

    entries: list[str] = []
    in_entries = False
    for line in fm.splitlines():
        if re.match(r"^\s+entries:\s*$", line):
            in_entries = True
            continue
        if in_entries:
            item = re.match(r"^\s+-\s+(.+?)\s*$", line)
            if item:
                entries.append(clean_value(item.group(1)))
                continue
            if line and not line.startswith(" "):
                break
            if re.match(r"^\s+\w", line):
                break

    return {
        "root": root,
        "entries": entries,
        "preview_status": preview_status,
        "coverage_status": fm_value(fm, "prototype_coverage_status"),
        "requirement_total": fm_value(fm, "prototype_requirement_total"),
        "covered_count": fm_value(fm, "prototype_covered_count"),
        "partial_count": fm_value(fm, "prototype_partial_count"),
        "missing_count": fm_value(fm, "prototype_missing_count"),
        "coverage_checked_at": fm_value(fm, "prototype_coverage_checked_at"),
        "coverage_ref": fm_value(fm, "prototype_coverage_ref"),
    }


def prototype_coverage_contract(workbench: Path, cfg: dict) -> tuple[list[dict], dict]:
    """Validate requirement-level prototype coverage independently from preview reachability."""
    empty_stats = {
        "prototype_requirements_total": 0,
        "prototype_requirements_covered": 0,
        "prototype_requirements_partial": 0,
        "prototype_requirements_missing": 0,
    }
    if not cfg.get("root"):
        return [], empty_stats

    required = {
        "prototype_coverage_status": cfg.get("coverage_status"),
        "prototype_requirement_total": cfg.get("requirement_total"),
        "prototype_covered_count": cfg.get("covered_count"),
        "prototype_partial_count": cfg.get("partial_count"),
        "prototype_missing_count": cfg.get("missing_count"),
        "prototype_coverage_checked_at": cfg.get("coverage_checked_at"),
        "prototype_coverage_ref": cfg.get("coverage_ref"),
    }
    missing_fields = [key for key, value in required.items() if value in {None, ""}]
    if len(missing_fields) == len(required):
        return [
            make_finding(
                "advisory",
                "PROTOTYPE_COVERAGE_AUDIT_MISSING",
                rel(workbench),
                "已声明原型入口，但缺正式需求到原型的覆盖审计；入口可打开不等于需求已覆盖。",
            )
        ], empty_stats
    if missing_fields:
        return [
            make_finding(
                "advisory",
                "PROTOTYPE_COVERAGE_FIELDS_INCOMPLETE",
                rel(workbench),
                "原型覆盖审计字段不完整，无法形成可复核的需求覆盖结论。",
                {"missing_fields": missing_fields},
            )
        ], empty_stats

    try:
        total = int(str(cfg["requirement_total"]))
        covered = int(str(cfg["covered_count"]))
        partial = int(str(cfg["partial_count"]))
        missing = int(str(cfg["missing_count"]))
    except (TypeError, ValueError):
        return [
            make_finding(
                "p1",
                "PROTOTYPE_COVERAGE_COUNTS_INVALID",
                rel(workbench),
                "原型覆盖数量字段必须是非负整数。",
                {"values": required},
            )
        ], empty_stats

    stats = {
        "prototype_requirements_total": total,
        "prototype_requirements_covered": covered,
        "prototype_requirements_partial": partial,
        "prototype_requirements_missing": missing,
    }
    findings: list[dict] = []
    if min(total, covered, partial, missing) < 0 or covered + partial + missing != total:
        findings.append(
            make_finding(
                "p1",
                "PROTOTYPE_COVERAGE_COUNTS_INVALID",
                rel(workbench),
                "原型覆盖数量必须非负，且 covered + partial + missing 必须等于 total。",
                stats,
            )
        )
        return findings, stats

    expected_status = "complete" if partial == 0 and missing == 0 else "partial"
    if cfg.get("coverage_status") != expected_status:
        findings.append(
            make_finding(
                "p1",
                "PROTOTYPE_COVERAGE_STATUS_INCONSISTENT",
                rel(workbench),
                f"prototype_coverage_status 应为 {expected_status}，不得用状态字段掩盖部分覆盖或缺失。",
                {"declared": cfg.get("coverage_status"), "expected": expected_status, **stats},
            )
        )
    if missing > 0:
        findings.append(
            make_finding(
                "p1",
                "PROTOTYPE_REQUIREMENT_GAPS",
                rel(workbench),
                f"当前原型仍有 {missing}/{total} 条正式需求无直接覆盖证据。",
                {"coverage_ref": cfg.get("coverage_ref"), **stats},
            )
        )
    if partial > 0:
        findings.append(
            make_finding(
                "advisory",
                "PROTOTYPE_REQUIREMENT_PARTIAL",
                rel(workbench),
                f"当前原型有 {partial}/{total} 条正式需求仅部分覆盖，需按字段、动作和状态闭环补齐。",
                {"coverage_ref": cfg.get("coverage_ref"), **stats},
            )
        )
    return findings, stats


def int_fm_value(fm: str, key: str) -> int | None:
    value = fm_value(fm, key)
    if value in {None, ""}:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def requirement_status_rows(text: str, heading: str, status_column: int) -> dict[int, str]:
    """Parse requirement-id/status rows from one Markdown section."""
    marker = text.find(heading)
    if marker < 0:
        return {}
    section = text[marker:]
    next_heading = re.search(r"\n## (?!#)", section[len(heading):])
    if next_heading:
        section = section[: len(heading) + next_heading.start()]
    rows: dict[int, str] = {}
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) <= status_column:
            continue
        requirement_ids = [int(item) for item in re.findall(r"\d+", cells[0])]
        status = normalize_status(cells[status_column])
        if status not in {"complete", "partial", "missing", "完整", "部分", "缺失"}:
            continue
        status = {"完整": "complete", "部分": "partial", "缺失": "missing"}.get(status, status)
        for requirement_id in requirement_ids:
            rows[requirement_id] = status
    return rows


def iteration_fact_chain_contract(
    iteration_root: Path,
    management_root: Path,
    iteration: str,
) -> tuple[list[dict], dict]:
    """Cross-check scope, Vault mapping, prototype audit, module actions and review facts."""
    stats = {"fact_chain_docs_checked": 0, "fact_chain_gap_requirements": 0}
    workbench = management_root / "README.md"
    if not workbench.exists():
        return [], stats
    workbench_text = read_text(workbench)
    workbench_fm = frontmatter(workbench_text)
    if int_fm_value(workbench_fm, "scope_requirement_total") is None:
        return [], stats

    scope_audit = management_root / f"{iteration}-需求范围核对.md"
    scope_draft = management_root / f"{iteration}-需求范围划定草案.md"
    change_ledger = management_root / f"{iteration}-模块变更台账.md"
    review = management_root / f"{iteration}-review.md"
    required_paths = [scope_audit, scope_draft, change_ledger]
    missing_paths = [rel(path) for path in required_paths if not path.exists()]
    if missing_paths:
        return [
            make_finding(
                "p1",
                "ITERATION_FACT_CHAIN_INCONSISTENT",
                rel(management_root),
                "迭代已启用三轴事实契约，但缺必要的事实链文档。",
                {"missing_paths": missing_paths},
            )
        ], stats

    texts = {path: read_text(path) for path in required_paths}
    fms = {path: frontmatter(texts[path]) for path in required_paths}
    stats["fact_chain_docs_checked"] = 4 + (1 if review.exists() else 0)
    mismatches: list[dict] = []

    scope_statuses = {
        "workbench": canonical_scope_status(fm_value(workbench_fm, "scope_status")),
        "scope_audit": canonical_scope_status(fm_value(fms[scope_audit], "scope_status")),
        "scope_draft": canonical_scope_status(fm_value(fms[scope_draft], "scope_status")),
        "change_ledger": canonical_scope_status(fm_value(fms[change_ledger], "scope_status")),
    }
    if len(set(scope_statuses.values())) != 1 or not all(scope_statuses.values()):
        mismatches.append({"kind": "scope_status", "values": scope_statuses})

    count_keys = [
        "scope_requirement_total",
        "vault_mapping_existing_count",
        "vault_mapping_strengthen_count",
        "vault_mapping_new_module_count",
    ]
    for key in count_keys:
        values = {
            "workbench": int_fm_value(workbench_fm, key),
            "scope_audit": int_fm_value(fms[scope_audit], key),
            "scope_draft": int_fm_value(fms[scope_draft], key),
        }
        if None in values.values() or len(set(values.values())) != 1:
            mismatches.append({"kind": key, "values": values})

    scope_total = int_fm_value(workbench_fm, "scope_requirement_total") or 0
    vault_counts = [int_fm_value(workbench_fm, key) or 0 for key in count_keys[1:]]
    if sum(vault_counts) != scope_total:
        mismatches.append({"kind": "vault_mapping_sum", "scope_total": scope_total, "parts": vault_counts})

    prototype_keys = [
        "prototype_requirement_total",
        "prototype_covered_count",
        "prototype_partial_count",
        "prototype_missing_count",
    ]
    for key in prototype_keys:
        values = {
            "workbench": int_fm_value(workbench_fm, key),
            "scope_audit": int_fm_value(fms[scope_audit], key),
        }
        if None in values.values() or len(set(values.values())) != 1:
            mismatches.append({"kind": key, "values": values})

    audit_rows = requirement_status_rows(texts[scope_audit], "## 7. 原型覆盖审计", 3)
    audit_counts = {
        "complete": sum(1 for status in audit_rows.values() if status == "complete"),
        "partial": sum(1 for status in audit_rows.values() if status == "partial"),
        "missing": sum(1 for status in audit_rows.values() if status == "missing"),
    }
    declared_counts = {
        "complete": int_fm_value(workbench_fm, "prototype_covered_count") or 0,
        "partial": int_fm_value(workbench_fm, "prototype_partial_count") or 0,
        "missing": int_fm_value(workbench_fm, "prototype_missing_count") or 0,
    }
    if len(audit_rows) != scope_total or audit_counts != declared_counts:
        mismatches.append(
            {"kind": "prototype_detail_counts", "rows": len(audit_rows), "audit": audit_counts, "declared": declared_counts}
        )

    expected_gaps = {rid: status for rid, status in audit_rows.items() if status in {"partial", "missing"}}
    stats["fact_chain_gap_requirements"] = len(expected_gaps)
    module_rows: dict[int, set[str]] = {}
    module_scope: dict[str, str] = {}
    for module_readme in sorted(iteration_root.glob("*/README.md")):
        if module_readme.parent == management_root:
            continue
        module_text = read_text(module_readme)
        rows = requirement_status_rows(module_text, "## 725 原型覆盖回填", 2)
        if not rows:
            continue
        module_scope[rel(module_readme)] = canonical_scope_status(fm_value(frontmatter(module_text), "scope_status"))
        for rid, status in rows.items():
            module_rows.setdefault(rid, set()).add(status)

    for rid, expected_status in expected_gaps.items():
        if expected_status not in module_rows.get(rid, set()):
            mismatches.append(
                {"kind": "module_coverage_writeback", "requirement_id": rid, "expected": expected_status, "actual": sorted(module_rows.get(rid, set()))}
            )
    invalid_module_scope = [path for path, status in module_scope.items() if status != "scoped"]
    if invalid_module_scope:
        mismatches.append({"kind": "module_scope_status", "paths": invalid_module_scope})

    ledger_rows = requirement_status_rows(texts[change_ledger], "## 原型覆盖缺口执行台账", 2)
    for rid, expected_status in expected_gaps.items():
        if ledger_rows.get(rid) != expected_status:
            mismatches.append(
                {"kind": "change_ledger_writeback", "requirement_id": rid, "expected": expected_status, "actual": ledger_rows.get(rid)}
            )

    if review.exists():
        review_fm = frontmatter(read_text(review))
        review_scope_value = fm_value(review_fm, "scope_status_at_review")
        if review_scope_value and normalize_status(review_scope_value) != "null":
            review_scope = canonical_scope_status(review_scope_value)
            if review_scope != scope_statuses["workbench"]:
                mismatches.append(
                    {"kind": "review_scope_status", "workbench": scope_statuses["workbench"], "review": review_scope}
                )

    if not mismatches:
        return [], stats
    return [
        make_finding(
            "p1",
            "ITERATION_FACT_CHAIN_INCONSISTENT",
            rel(management_root),
            "范围、Vault 承接、原型覆盖、模块执行台账或 Review 之间存在事实漂移。",
            {"mismatches": mismatches},
        )
    ], stats


def visual_artifacts(iteration_root: Path) -> list[Path]:
    artifacts: list[Path] = []
    for path in iteration_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(iteration_root).parts):
            continue
        if path.suffix.lower() in VISUAL_ARTIFACT_EXTS:
            artifacts.append(path)
    return sorted(artifacts)


def markdown_index(iteration_root: Path) -> list[tuple[Path, str]]:
    docs: list[tuple[Path, str]] = []
    for path in sorted(iteration_root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(iteration_root).parts):
            continue
        docs.append((path, read_text(path)))
    return docs


def has_preview_record(artifact: Path, iteration_root: Path, docs: list[tuple[Path, str]]) -> bool:
    artifact_rel = artifact.relative_to(iteration_root).as_posix()
    artifact_name = artifact.name
    for _doc_path, text in docs:
        if artifact_rel not in text and artifact_name not in text:
            continue
        lowered = text.lower()
        if any(marker.lower() in lowered for marker in PREVIEW_MARKERS):
            return True
    return False


def check_declared_prototype_preview(workbench: Path) -> tuple[list[dict], int, dict]:
    cfg = visual_config_from_workbench(workbench)
    root_value = cfg["root"]
    if not root_value:
        return [], 0, {}

    root = Path(root_value).expanduser()
    findings, coverage_stats = prototype_coverage_contract(workbench, cfg)
    if not root.exists():
        findings.extend([
            make_finding(
                "advisory",
                "PROTOTYPE_ROOT_MISSING",
                root.as_posix(),
                f"声明的 prototype_root 不存在：{root.as_posix()}",
            )
        ])
        return findings, 0, coverage_stats

    entries = cfg["entries"] or ["index.html"]
    checked = 0
    for entry in entries:
        path = root / entry
        checked += 1
        if not path.exists():
            findings.append(
                make_finding(
                    "p1",
                    "VISUAL_ARTIFACT_ENTRY_MISSING",
                    path.as_posix(),
                    f"声明的原型入口不存在：{path.as_posix()}",
                    {"prototype_root": root.as_posix(), "entry": entry},
                )
            )
            continue
        findings.extend(check_html_entry_render_signal(path, path.as_posix()))

    preview_status = cfg["preview_status"]
    if checked and preview_status not in {"checked", "not_applicable"}:
        findings.append(
            make_finding(
                "advisory",
                "VISUAL_PREVIEW_PENDING",
                root.as_posix(),
                "原型入口已声明，但 preview_status 尚未为 checked；进入验收前应使用 flyfish viewer 或等价预览完成检查。",
                {"prototype_root": root.as_posix(), "entries": entries, "preview_status": preview_status or None},
            )
        )

    return findings, checked, coverage_stats


def check_visual_preview(iteration_root: Path, management_root: Path) -> tuple[list[dict], int, dict]:
    workbench = management_root / "README.md"
    if workbench.exists():
        declared_findings, declared_count, coverage_stats = check_declared_prototype_preview(workbench)
        if declared_count or declared_findings:
            return declared_findings, declared_count, coverage_stats

    artifacts = visual_artifacts(iteration_root)
    if not artifacts:
        return [], 0, {}

    docs = markdown_index(iteration_root)
    findings: list[dict] = []
    for artifact in artifacts:
        findings.extend(check_html_entry_render_signal(artifact, rel(artifact)))
        if has_preview_record(artifact, iteration_root, docs):
            continue
        findings.append(
            make_finding(
                "advisory",
                "VISUAL_PREVIEW_MISSING",
                rel(artifact),
                f"{rel(artifact)}: 视觉产物缺 preview 记录；Handoff 或迭代文档应声明 flyfish viewer 检查结果或不适用原因。",
                {"artifact_type": artifact.suffix.lower().lstrip(".")},
            )
        )
    return findings, len(artifacts), {}


def check_project(project_root: Path) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    stats = {
        "managed_docs_found": 0,
        "managed_docs_expected": len(REQUIRED_MANAGEMENT_DOCS),
        "iteration_docs_scanned": 0,
        "visual_artifacts_checked": 0,
        "prototype_requirements_total": 0,
        "prototype_requirements_covered": 0,
        "prototype_requirements_partial": 0,
        "prototype_requirements_missing": 0,
        "fact_chain_docs_checked": 0,
        "fact_chain_gap_requirements": 0,
        "review_docs_found": 0,
        "review_contracts_checked": 0,
        "double_time_docs_checked": 0,
        "agent_assignments_checked": 0,
        "memory_anchor_docs_checked": 0,
        "write_boundary_contracts_checked": 0,
        "release_collection_contracts_checked": 0,
        "material_manifest_contracts_checked": 0,
        "carryover_ledger_contracts_checked": 0,
        "scope_status_checked": 0,
        "included_scope_items": 0,
        "baseline_changed_paths": 0,
        "carryover_docs_checked": 0,
    }

    readme = project_root / "README.md"
    if not readme.exists():
        findings.append(
            make_finding(
                "p1",
                "PROJECT_README_MISSING",
                rel(readme),
                f"项目根 README 不存在，无法解析当前迭代：{rel(readme)}",
            )
        )
        return findings, {"current_iteration": None, "iteration_root": None, "management_root": None, "stats": stats}

    text = read_text(readme)
    iteration, hint = current_iteration_from_readme(text)
    if not iteration:
        findings.append(
            make_finding(
                "p1",
                "CURRENT_ITERATION_UNDECLARED",
                rel(readme),
                f"{rel(readme)}: 未解析到当前迭代。",
            )
        )
        return findings, {"current_iteration": None, "iteration_root": None, "management_root": None, "stats": stats}

    iteration_root = project_root / "迭代" / f"{iteration}迭代"
    if not iteration_root.exists():
        findings.append(
            make_finding(
                "p1",
                "ITERATION_ENTRY_MISSING",
                rel(iteration_root),
                f"{rel(readme)} 指向当前迭代 {iteration}，但目录不存在：{rel(iteration_root)}",
                {"iteration": iteration, "hint": hint},
            )
        )
        return findings, {
            "current_iteration": iteration,
            "iteration_root": rel(iteration_root),
            "management_root": rel(iteration_root / "迭代管理"),
            "stats": stats,
        }

    management_root = iteration_root / "迭代管理"
    expected_iteration_root = rel(iteration_root)
    expected_baseline_root = rel(project_root / "基线")
    if not management_root.exists():
        findings.append(
            make_finding(
                "p1",
                "ITERATION_MANAGEMENT_DIR_MISSING",
                rel(management_root),
                f"当前迭代缺迭代管理目录：{rel(management_root)}",
                {"iteration": iteration},
            )
        )
        return findings, {
            "current_iteration": iteration,
            "iteration_root": rel(iteration_root),
            "management_root": rel(management_root),
            "stats": stats,
        }

    workbench_scope_status = ""
    for path, template, label in management_doc_paths(management_root, iteration):
        if not path.exists():
            findings.append(
                make_finding(
                    "p1",
                    "ITERATION_MANAGEMENT_DOC_MISSING",
                    rel(path),
                    f"当前迭代缺{label}：{rel(path)}",
                    {"iteration": iteration, "required_doc": path.name, "label": label},
                )
            )
            continue

        stats["managed_docs_found"] += 1
        if template in FRONTMATTER_REQUIRED_DOCS:
            stats["iteration_docs_scanned"] += 1
            if template == "README.md":
                workbench_scope_status = fm_value(frontmatter(read_text(path)), "scope_status")
                stats["agent_assignments_checked"] += 1
            if template in DOUBLE_TIME_REQUIRED_TEMPLATES:
                stats["double_time_docs_checked"] += 1
            if template in MEMORY_ANCHOR_REQUIRED_TEMPLATES:
                stats["memory_anchor_docs_checked"] += 1
            findings.extend(check_frontmatter(path, template, iteration, expected_iteration_root, expected_baseline_root))
            if template == WRITE_BOUNDARY_TEMPLATE:
                stats["write_boundary_contracts_checked"] += 1
                findings.extend(check_write_boundary_contract(path, iteration))
            if template == RELEASE_COLLECTION_TEMPLATE:
                stats["release_collection_contracts_checked"] += 1
                findings.extend(check_release_collection_contract(path, iteration))
            if template == MATERIAL_MANIFEST_TEMPLATE:
                stats["material_manifest_contracts_checked"] += 1
                findings.extend(check_material_manifest_contract(path, iteration))
            if template == CARRYOVER_LEDGER_TEMPLATE:
                stats["carryover_ledger_contracts_checked"] += 1
                findings.extend(check_carryover_ledger_contract(path, iteration))

    workbench = management_root / "README.md"
    scope_doc = management_root / f"{iteration}-需求范围划定草案.md"
    if workbench.exists():
        stats["scope_status_checked"] = 1
        scope_findings, included_scope_items = check_scope_state_contract(workbench, scope_doc, iteration)
        stats["included_scope_items"] = included_scope_items
        findings.extend(scope_findings)

        fact_chain_findings, fact_chain_stats = iteration_fact_chain_contract(iteration_root, management_root, iteration)
        stats.update(fact_chain_stats)
        findings.extend(fact_chain_findings)

    baseline_findings, baseline_changed_paths = check_baseline_write_contract(
        project_root,
        management_root,
        iteration,
        workbench_scope_status,
    )
    stats["baseline_changed_paths"] = baseline_changed_paths
    findings.extend(baseline_findings)

    preview_findings, visual_count, coverage_stats = check_visual_preview(iteration_root, management_root)
    stats["visual_artifacts_checked"] = visual_count
    stats.update(coverage_stats)
    findings.extend(preview_findings)

    carryover_findings, carryover_docs_checked = check_carryover_contract(iteration_root)
    stats["carryover_docs_checked"] = carryover_docs_checked
    findings.extend(carryover_findings)

    review_doc = find_review_doc(management_root, iteration)
    if review_doc:
        stats["review_docs_found"] = 1
        stats["review_contracts_checked"] = 1
        stats["double_time_docs_checked"] += 1
        stats["memory_anchor_docs_checked"] += 1
        findings.extend(check_review_contract(review_doc, iteration, expected_iteration_root))
        findings.extend(check_review_scope_advance_writeback(workbench, review_doc))
        findings.extend(check_review_eval_writeback(review_doc))
        findings.extend(check_review_decision_matrix(review_doc))
        findings.extend(check_review_v9_body_writeback(review_doc))
        findings.extend(check_review_skill_writeback(review_doc))
    elif review_required_for_status(workbench_scope_status):
        findings.append(
            make_finding(
                "advisory",
                "REVIEW_MISSING_AFTER_RELEASE",
                rel(management_root),
                "迭代已进入发布/复盘状态，但缺 review.md 或 {iteration}-review.md；无法沉淀规则/eval/skill 晋升闭环。",
                {
                    "iteration": iteration,
                    "scope_status": workbench_scope_status,
                    "expected": [rel(path) for path in review_doc_paths(management_root, iteration)],
                },
            )
        )

    return findings, {
        "current_iteration": iteration,
        "iteration_root": rel(iteration_root),
        "management_root": rel(management_root),
        "stats": stats,
    }


def summarize(findings: list[dict], stats: dict) -> dict:
    def count(severity: str) -> int:
        return sum(1 for item in findings if item["severity"] == severity)

    worst = min((item["severity"] for item in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    return {
        "total": len(findings),
        "p0": count("p0"),
        "p1": count("p1"),
        "advisory": count("advisory"),
        "worst": worst,
        **stats,
    }


def build_report(project_root: Path) -> dict:
    findings, context = check_project(project_root)
    stats = context.pop("stats")
    return {
        "check": CHECK_NAME,
        "source": SOURCE,
        "generated_at": now_iso(),
        **context,
        "summary": summarize(findings, stats),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="10-项目", help="项目根目录，默认 10-项目")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="发现 p0/p1 时退出码 1")
    args = parser.parse_args()

    report = build_report(Path(args.project_root))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"# {CHECK_NAME}")
        print(
            "summary: "
            f"total={s['total']} p0={s['p0']} p1={s['p1']} advisory={s['advisory']} "
            f"iteration={report.get('current_iteration') or '-'} "
            f"managed_docs={s['managed_docs_found']}/{s['managed_docs_expected']}"
        )
        for finding in report["findings"]:
            print(f"[{finding['severity']}] {finding['rule_id']} | {finding['message']}")

    if args.strict and (report["summary"]["p0"] > 0 or report["summary"]["p1"] > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
