#!/usr/bin/env python3
"""
v9-reflex-check.py — V9 第一反射器 MVP 聚合器（子任务 2/3/4）

职责：汇集多个巡检源 → 归一到统一 severity schema → 去重+冷却 → 写运行态 health-latest.json。

防污染硬约束（对标 6-18 反思 Hindsight `_converted` 污染教训）：
  本脚本默认只写 Vault 外的运行态目录，绝不写看板 / 运行日志 / 模块文档。
  正式看板/日志的提升由会话启动 checklist 人工确认后进行，不由本脚本自动写。

信号源：
  1. project-ops-check.py --json   任务卡 + 运行日志巡检（子任务 1 已 JSON 化）
  2. agent-state-lint.py --json    Agent 状态文件 schema 校验
  3. 内置 heartbeat 检查            status=busy 但 last_heartbeat 超时（子任务 2）
  4. v9-policy-conflict-check.py    规范管辖权索引 + 冲突扫描（动作 C）
  5. v9-starter-leak-check.py       starter 分发泄漏扫描（V9.4）
  6. v9-task-state-check.py         任务验收状态扫描（V9.4）
  7. v9-scope-tamper-check.py       write_scope Bash 旁路扩权扫描（V9.4.1）
  8. v9-handoff-check.py            Handoff 可接手性扫描（V9.4.2）
  9. v9-iteration-ops-check.py      月度迭代 Ops 结构扫描（V9.5）
 10. frontmatter-lint.py            全库结构债务聚合（不把历史债伪装成绿色）

运行时自检（不计入九个治理信号源）：
  - 第一反射器与 GBrain launchd 是否实际加载
  - GBrain CLI、lint 正反行为契约、bge-m3、autopilot 是否可用
  - sync / dream 是否在承诺窗口内真实成功
  - doc-gardening v2 影子报告是否按周保持新鲜
  - skill 多入口是否存在未声明的版本遮蔽

统一 severity：p0（阻断）/ p1（结构性）/ advisory（提示）。
  agent-state-lint 的 error→p1、warning→advisory。

去重 + 冷却（子任务 4）：
  幂等键默认 = "{rule_id}:{object}"。
  LOG_GAP 特例 = "LOG_GAP:run_logs"（缺失日期窗口会滚动，键须稳定；具体日期放 detail）。
  冷却窗口内（默认 24h）同键不重复"上报"，仅累加 count；超窗口重新置为 active。

用法：
  python3 02-项目管理/脚本/v9-reflex-check.py
  python3 02-项目管理/脚本/v9-reflex-check.py --today 2026-06-25 --stale-heartbeat-hours 24
  python3 02-项目管理/脚本/v9-reflex-check.py --quiet      # 仅写文件不打印（launchd 用）
  python3 02-项目管理/脚本/v9-reflex-check.py --strict     # 有 active 发现时退出码 1
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(".")
SCRIPT_DIR = ROOT / "02-项目管理" / "脚本"
STATUS_DIR = ROOT / "02-项目管理" / "智能体状态"


def runtime_inspect_dir() -> Path:
    explicit = os.environ.get("XIRANG_V9_INSPECT_DIR")
    if explicit:
        return Path(explicit).expanduser()
    runtime_root = os.environ.get("XIRANG_V9_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root).expanduser() / "巡检"
    return Path.home() / ".xirang" / "v9-runtime" / "巡检"


INSPECT_DIR = runtime_inspect_dir()  # Vault 外运行态输出目录
HEALTH_LATEST = INSPECT_DIR / "health-latest.json"
REFLEX_STATE = INSPECT_DIR / "reflex-state.json"

CHECK_NAME = "v9-reflex-check"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@contextlib.contextmanager
def state_lock():
    """互斥锁，保证 launchd 与手动跑并发时 read-modify-write 不丢 count（Codex P1）。"""
    INSPECT_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(INSPECT_DIR / ".reflex.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write_json(path: Path, data) -> None:
    """临时文件 + os.replace 原子替换，避免写半截 JSON（Codex P1）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state() -> dict:
    try:
        return json.loads(REFLEX_STATE.read_text(encoding="utf-8")) if REFLEX_STATE.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def parse_iso(value: str) -> datetime | None:
    value = value.strip().strip('"').strip("'")
    if not value or value in {"null", "None"}:
        return None
    try:
        # macOS system Python 3.9 does not accept the RFC 3339 `Z` suffix.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_local().tzinfo)
    return dt


def frontmatter(text: str) -> str:
    """Return only the first YAML frontmatter block; embedded examples are not state."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[4:end] if end != -1 else ""


def frontmatter_value(text: str, key: str) -> str:
    fm = frontmatter(text)
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else ""


def make_finding(severity: str, rule_id: str, obj: str, message: str, source: str, detail=None) -> dict:
    f = {
        "severity": severity,
        "rule_id": rule_id,
        "object": obj,
        "message": message,
        "source": source,
    }
    if detail is not None:
        f["detail"] = detail
    return f


# ---------- 源 1：project-ops-check ----------
def collect_project_ops(today: date) -> list[dict]:
    script = SCRIPT_DIR / "project-ops-check.py"
    if not script.exists():
        return [make_finding("p1", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--today", today.isoformat(), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"project-ops-check 执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "project-ops",
            )
        )
    return findings


# ---------- 源 2：agent-state-lint ----------
def collect_agent_state() -> list[dict]:
    script = ROOT / ".standards" / "agent-state-lint.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--validate", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"agent-state-lint 执行失败：{exc}", "reflex")]

    sev_map = {"error": "p1", "warning": "advisory"}
    findings = []
    for agent_id, issues in data.get("details", {}).items():
        for issue in issues:
            sev = sev_map.get(issue.get("severity", "warning"), "advisory")
            field = issue.get("field", "?")
            findings.append(
                make_finding(
                    sev,
                    f"STATE_{field}",
                    agent_id,
                    f"{agent_id}: {issue.get('message', '')}",
                    "agent-state",
                )
            )
    return findings


# ---------- 源 3：heartbeat 超时（子任务 2）----------
def collect_heartbeat(now: datetime, stale_hours: int) -> list[dict]:
    if not STATUS_DIR.exists():
        return []
    findings = []
    cutoff = now - timedelta(hours=stale_hours)
    for path in sorted(STATUS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        status = frontmatter_value(text, "status")
        if not status:
            continue
        agent_id = frontmatter_value(text, "agent_id") or path.stem
        if status != "busy":
            continue  # 只关心 busy 卡死；idle/standby/retired 无需心跳告警
        last_heartbeat = frontmatter_value(text, "last_heartbeat")
        hb = parse_iso(last_heartbeat) if last_heartbeat else None
        if hb is None:
            findings.append(
                make_finding("p1", "HEARTBEAT_MISSING", agent_id,
                             f"{agent_id}: status=busy 但缺 last_heartbeat。", "heartbeat")
            )
        elif hb < cutoff:
            age_h = round((now - hb).total_seconds() / 3600, 1)
            findings.append(
                make_finding("p1", "STALE_HEARTBEAT", agent_id,
                             f"{agent_id}: status=busy 但心跳已 {age_h}h 未更新（阈值 {stale_hours}h），疑似卡死。",
                             "heartbeat", detail={"last_heartbeat": hb.isoformat(), "age_hours": age_h})
            )
    return findings


# ---------- 源 4：规范管辖权/冲突扫描（动作 C）----------
def collect_policy_conflicts() -> list[dict]:
    script = SCRIPT_DIR / "v9-policy-conflict-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"规范冲突扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "policy-conflict",
                detail=item.get("detail"),
            )
        )
    return findings


# ---------- 源 5：starter 分发泄漏扫描（V9.4）----------
def collect_starter_leaks() -> list[dict]:
    script = SCRIPT_DIR / "v9-starter-leak-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"starter 泄漏扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "starter-leak",
                detail=item.get("detail"),
            )
        )
    return findings


# ---------- 源 6：任务验收状态扫描（V9.4）----------
def collect_task_state() -> list[dict]:
    script = SCRIPT_DIR / "v9-task-state-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"任务验收状态扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "task-state",
                detail=item.get("detail"),
            )
        )
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    missing = int(summary.get("done_missing_review_status", 0) or 0)
    awaiting = int(summary.get("awaiting_review", 0) or 0)
    # submitted/reviewing 是正常 HITL 队列，不是 Agent 可自行偿还的治理债。
    # 只有结构缺失才进入 health finding；完整队列由 task-review-queue.json 承载。
    if missing:
        findings.append(make_finding(
            "advisory", "TASK_REVIEW_DEBT", "formal-task-cards",
            f"正式任务卡存在验收结构债：done 缺 review_status={missing}；"
            f"另有正常 HITL 待评审队列={awaiting}（不计治理债）。",
            "task-state", detail=summary,
        ))
    return findings


def collect_frontmatter_lint() -> list[dict]:
    script = ROOT / ".standards/frontmatter-lint.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "frontmatter-lint")]
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--all", "--json"],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"Frontmatter lint 执行失败：{exc}", "frontmatter-lint")]
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    errors = int(summary.get("errors", 0) or 0)
    warnings = int(summary.get("warnings", 0) or 0)
    info = int(summary.get("info", 0) or 0)
    if not (errors or warnings or info):
        return []
    return [make_finding(
        "advisory", "FRONTMATTER_LINT_DEBT", "vault-markdown",
        f"全库 Frontmatter 治理债未清零：errors={errors} warnings={warnings} info={info}。",
        "frontmatter-lint", detail=summary,
    )]


# ---------- 源 7：write_scope Bash 旁路扩权扫描（V9.4.1）----------
def collect_scope_tamper() -> list[dict]:
    script = SCRIPT_DIR / "v9-scope-tamper-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"write_scope 扩权扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "scope-tamper",
                detail=item.get("detail"),
            )
        )
    return findings


# ---------- 源 8：Handoff 可接手性扫描（V9.4.2）----------
def collect_handoff() -> list[dict]:
    script = SCRIPT_DIR / "v9-handoff-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"Handoff 可接手性扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "handoff",
                detail=item.get("detail"),
            )
        )
    return findings


# ---------- 源 9：月度迭代 Ops 结构扫描（V9.5）----------
def collect_iteration_ops() -> list[dict]:
    script = SCRIPT_DIR / "v9-iteration-ops-check.py"
    if not script.exists():
        return [make_finding("advisory", "SOURCE_MISSING", str(script), f"巡检源缺失：{script}", "reflex")]
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--project-root", "10-项目", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [make_finding("p1", "SOURCE_FAILED", str(script), f"月度迭代 Ops 扫描执行失败：{exc}", "reflex")]

    findings = []
    for item in data.get("findings", []):
        findings.append(
            make_finding(
                item.get("severity", "advisory"),
                item.get("rule_id", "UNKNOWN"),
                item.get("object", ""),
                item.get("message", ""),
                "iteration-ops",
                detail=item.get("detail"),
            )
        )
    return findings


# ---------- 运行时自检：防止“文件还在，但机制已经停了” ----------
def _runtime_check(name: str, status: str, detail: str = "") -> dict:
    item = {"check": name, "status": status}
    if detail:
        item["detail"] = detail
    return item


def _launchd_info(label: str) -> tuple[bool, str, str]:
    launchctl = os.environ.get("XIRANG_LAUNCHCTL", "/bin/launchctl")
    try:
        out = subprocess.run(
            [launchctl, "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "unknown", str(exc)
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode != 0:
        return False, "not_loaded", text.strip()[-500:]
    match = re.search(r"^\s*state\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    return True, match.group(1).strip() if match else "loaded", ""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _gbrain_lint_contract(gbrain: Path) -> tuple[bool, str]:
    """Probe detector behavior so a package upgrade cannot silently restore false positives."""
    embedded_content = (
        "---\ntitle: Test\ntype: guide\ncreated: 2026-07-18\n---\n\n"
        "# Guide\n\n```markdown\n# Example\n```\n\nClosing guidance.\n"
    )
    wrapped_content = "```markdown\n# Wrapped page\n```\n"
    date_format_content = (
        "---\ntitle: Date formats\ntype: guide\ncreated: 2026-07-18\n---\n\n"
        "| Field | Format |\n|---|---|\n| Created date | YYYY-MM-DD |\n"
    )
    unfilled_date_content = (
        "---\ntitle: Unfilled\ntype: guide\ncreated: YYYY-MM-DD\n---\n\n# Unfilled\n"
    )
    valid_flow_array_content = (
        "---\ntitle: Flow array\ntype: guide\ncreated: 2026-07-18\n"
        "aliases: [\"A\", \"B\", \"C\"]\n---\n\n# Flow array\n"
    )
    malformed_nested_content = (
        "---\ntitle: \"Outer \"Inner\" Value\"\ntype: guide\ncreated: 2026-07-18\n"
        "---\n\n# Malformed nested quotes\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="v9-gbrain-lint-") as tmp:
            embedded = Path(tmp) / "embedded.md"
            wrapped = Path(tmp) / "wrapped.md"
            date_format = Path(tmp) / "date-format.md"
            unfilled_date = Path(tmp) / "unfilled.md"
            valid_flow_array = Path(tmp) / "valid-flow-array.md"
            malformed_nested = Path(tmp) / "malformed-nested.md"
            embedded.write_text(embedded_content, encoding="utf-8")
            wrapped.write_text(wrapped_content, encoding="utf-8")
            date_format.write_text(date_format_content, encoding="utf-8")
            unfilled_date.write_text(unfilled_date_content, encoding="utf-8")
            valid_flow_array.write_text(valid_flow_array_content, encoding="utf-8")
            malformed_nested.write_text(malformed_nested_content, encoding="utf-8")
            embedded_proc = subprocess.run(
                [str(gbrain), "lint", str(embedded)],
                capture_output=True, text=True, timeout=15,
            )
            wrapped_proc = subprocess.run(
                [str(gbrain), "lint", str(wrapped)],
                capture_output=True, text=True, timeout=15,
            )
            date_format_proc = subprocess.run(
                [str(gbrain), "lint", str(date_format)],
                capture_output=True, text=True, timeout=15,
            )
            unfilled_date_proc = subprocess.run(
                [str(gbrain), "lint", str(unfilled_date)],
                capture_output=True, text=True, timeout=15,
            )
            valid_flow_array_proc = subprocess.run(
                [str(gbrain), "lint", str(valid_flow_array)],
                capture_output=True, text=True, timeout=15,
            )
            malformed_nested_proc = subprocess.run(
                [str(gbrain), "lint", str(malformed_nested)],
                capture_output=True, text=True, timeout=15,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"probe_failed:{exc}"

    embedded_output = (embedded_proc.stdout or "") + (embedded_proc.stderr or "")
    wrapped_output = (wrapped_proc.stdout or "") + (wrapped_proc.stderr or "")
    date_format_output = (date_format_proc.stdout or "") + (date_format_proc.stderr or "")
    unfilled_date_output = (unfilled_date_proc.stdout or "") + (unfilled_date_proc.stderr or "")
    valid_flow_array_output = (valid_flow_array_proc.stdout or "") + (valid_flow_array_proc.stderr or "")
    malformed_nested_output = (malformed_nested_proc.stdout or "") + (malformed_nested_proc.stderr or "")
    return_codes = {
        "embedded": embedded_proc.returncode,
        "wrapped": wrapped_proc.returncode,
        "date_format": date_format_proc.returncode,
        "unfilled": unfilled_date_proc.returncode,
        "valid_flow_array": valid_flow_array_proc.returncode,
        "malformed_nested": malformed_nested_proc.returncode,
    }
    if any(return_codes.values()):
        return False, "probe_exit=" + ",".join(f"{key}:{value}" for key, value in return_codes.items())
    if "code-fence-wrap" in embedded_output:
        return False, "embedded_markdown_false_positive"
    if "code-fence-wrap" not in wrapped_output:
        return False, "whole_page_wrapper_missed"
    if "placeholder-date" in date_format_output:
        return False, "date_format_false_positive"
    if "placeholder-date" not in unfilled_date_output:
        return False, "unfilled_date_missed"
    if "frontmatter-nested-quotes" in valid_flow_array_output:
        return False, "flow_sequence_false_positive"
    if "frontmatter-nested-quotes" not in malformed_nested_output:
        return False, "malformed_nested_quotes_missed"
    return True, "fence=calibrated placeholder_date=calibrated nested_quotes=calibrated"


def _maintenance_freshness(
    action: str, path: Path, now: datetime, stale_hours: int,
) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    data = _read_json(path)
    name = f"gbrain_{action}_freshness"
    if not data:
        findings.append(make_finding(
            "p1", f"GBRAIN_{action.upper()}_NEVER_SUCCEEDED", str(path),
            f"GBrain {action} 尚无机器可读的成功记录。", "runtime-liveness",
        ))
        return findings, _runtime_check(name, "failed", "state_missing")

    status = str(data.get("status", "unknown"))
    updated = parse_iso(str(data.get("updated_at", "")))
    last_success = parse_iso(str(data.get("last_success_at", "")))
    if last_success is None and status == "success":
        last_success = updated  # backward-compatible state written before 2026-07-18
    if status == "running" and updated and now - updated <= timedelta(hours=stale_hours):
        return findings, _runtime_check(name, "running", data.get("updated_at", ""))
    if last_success is None:
        findings.append(make_finding(
            "p1", f"GBRAIN_{action.upper()}_NEVER_SUCCEEDED", str(path),
            f"GBrain {action} 尚无真实成功记录；最近状态为 {status}：{data.get('reason', 'unknown')}。",
            "runtime-liveness", detail=data,
        ))
        return findings, _runtime_check(name, "failed", status)

    age = now - last_success
    if age > timedelta(hours=stale_hours):
        age_h = round(age.total_seconds() / 3600, 1)
        findings.append(make_finding(
            "p1", f"GBRAIN_{action.upper()}_STALE", str(path),
            f"GBrain {action} 已 {age_h}h 未成功（阈值 {stale_hours}h）。",
            "runtime-liveness",
            detail={"last_success_at": last_success.isoformat(), "last_attempt_status": status, "age_hours": age_h},
        ))
        return findings, _runtime_check(name, "stale", f"age_hours={age_h}")
    detail = f"last_success={last_success.isoformat()} last_attempt={status}"
    return findings, _runtime_check(name, "ok", detail)


def collect_runtime_liveness(
    now: datetime, sync_stale_hours: int, dream_stale_hours: int, entropy_stale_hours: int,
) -> tuple[list[dict], list[dict]]:
    """Validate live mechanisms rather than the presence of their files."""
    findings: list[dict] = []
    checks: list[dict] = []
    home = Path.home()

    gbrain = Path(os.environ.get("XIRANG_GBRAIN_CLI", str(home / ".npm-global/bin/gbrain")))
    gbrain_identity_ok = False
    if not gbrain.is_file() or not os.access(gbrain, os.X_OK):
        findings.append(make_finding(
            "p1", "GBRAIN_CLI_MISSING", str(gbrain), "GBrain CLI 不存在或不可执行。", "runtime-liveness",
        ))
        checks.append(_runtime_check("gbrain_cli", "failed", str(gbrain)))
    else:
        try:
            proc = subprocess.run([str(gbrain), "--version"], capture_output=True, text=True, timeout=15)
            version = ((proc.stdout or "") + (proc.stderr or "")).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            proc = None
            version = str(exc)
        expected_version = os.environ.get("XIRANG_GBRAIN_EXPECTED_VERSION", "gbrain 0.33.0")
        if proc is None or proc.returncode != 0 or not version.startswith("gbrain "):
            findings.append(make_finding(
                "p1", "GBRAIN_CLI_UNHEALTHY", str(gbrain),
                f"GBrain CLI 自检失败：{version[-300:]}", "runtime-liveness",
            ))
            checks.append(_runtime_check("gbrain_cli", "failed", version[-300:]))
        elif version.splitlines()[0] != expected_version:
            findings.append(make_finding(
                "p1", "GBRAIN_VERSION_DRIFT", str(gbrain),
                f"GBrain 版本/包身份漂移：期望 {expected_version}，实际 {version.splitlines()[0]}。",
                "runtime-liveness",
            ))
            checks.append(_runtime_check("gbrain_cli", "failed", version.splitlines()[0]))
        else:
            checks.append(_runtime_check("gbrain_cli", "ok", version.splitlines()[0]))
            gbrain_identity_ok = True

    if gbrain_identity_ok:
        lint_contract_ok, lint_contract_detail = _gbrain_lint_contract(gbrain)
        if not lint_contract_ok:
            findings.append(make_finding(
                "p1", "GBRAIN_LINT_CONTRACT_BROKEN", str(gbrain),
                f"GBrain lint 行为契约失效：{lint_contract_detail}。",
                "runtime-liveness",
            ))
            checks.append(_runtime_check("gbrain_lint_contract", "failed", lint_contract_detail))
        else:
            checks.append(_runtime_check("gbrain_lint_contract", "ok", lint_contract_detail))

    ollama = Path(os.environ.get("XIRANG_OLLAMA_CLI", "/opt/homebrew/bin/ollama"))
    try:
        model_proc = subprocess.run([str(ollama), "list"], capture_output=True, text=True, timeout=15)
        model_ok = model_proc.returncode == 0 and any(
            line.startswith("bge-m3:") for line in model_proc.stdout.splitlines()
        )
        model_detail = ((model_proc.stdout or "") + (model_proc.stderr or ""))[-300:]
    except (OSError, subprocess.SubprocessError) as exc:
        model_ok = False
        model_detail = str(exc)
    if not model_ok:
        findings.append(make_finding(
            "p1", "SEMANTIC_MODEL_UNAVAILABLE", "ollama:bge-m3",
            "Ollama bge-m3 不可用，语义嵌入链无法工作。", "runtime-liveness",
            detail={"probe": model_detail},
        ))
        checks.append(_runtime_check("semantic_model", "failed", model_detail))
    else:
        checks.append(_runtime_check("semantic_model", "ok", "ollama:bge-m3"))

    event_file = STATUS_DIR / "智能体事件.jsonl"
    last_recall: dict | None = None
    try:
        for line in event_file.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                row.get("event") == "semantic_recall"
                and row.get("source") in {"session_start", "task_start"}
                and row.get("status") == "success"
                and row.get("contract_hit") is True
            ):
                last_recall = row
    except OSError:
        last_recall = None
    recall_at = parse_iso(str((last_recall or {}).get("ts", "")))
    if recall_at is None or now - recall_at > timedelta(days=7):
        detail = "no_successful_session_or_task_recall" if recall_at is None else f"stale_at={recall_at.isoformat()}"
        findings.append(make_finding(
            "p1", "SEMANTIC_RECALL_NOT_CONSUMED", str(event_file),
            "最近 7 天没有 SessionStart/任务开始成功消费当前 GBrain 契约的证据。",
            "runtime-liveness", detail={"reason": detail},
        ))
        checks.append(_runtime_check("semantic_recall_consumption", "failed", detail))
    else:
        checks.append(_runtime_check(
            "semantic_recall_consumption", "ok",
            f"last={recall_at.isoformat()} source={last_recall.get('source')} task={last_recall.get('task_id', '')}",
        ))

    loaded, state, error = _launchd_info("com.xirang.v9reflex")
    if not loaded:
        findings.append(make_finding(
            "p1", "RUNTIME_SCHEDULER_NOT_LOADED", "com.xirang.v9reflex",
            "运行时调度器未加载：com.xirang.v9reflex。", "runtime-liveness",
            detail={"state": state, "error": error},
        ))
        checks.append(_runtime_check("v9reflex_launchd", "failed", state))
    else:
        checks.append(_runtime_check("v9reflex_launchd", "ok", state))

    # GBrain has one scheduler owner: cron one-shot sync/dream. A long-running
    # autopilot holds the PGLite lock and makes both cron jobs time out.
    autopilot_loaded, autopilot_state, _ = _launchd_info("com.gbrain.autopilot")
    if autopilot_loaded:
        findings.append(make_finding(
            "p1", "GBRAIN_SCHEDULER_CONFLICT", "com.gbrain.autopilot",
            f"GBrain 常驻 autopilot 与 cron 单次任务冲突（state={autopilot_state}）。",
            "runtime-liveness",
        ))
        checks.append(_runtime_check("gbrain_scheduler_owner", "failed", "autopilot_and_cron"))
    else:
        checks.append(_runtime_check("gbrain_scheduler_owner", "ok", "cron_only"))

    crontab = os.environ.get("XIRANG_CRONTAB", "/usr/bin/crontab")
    try:
        cron_proc = subprocess.run([crontab, "-l"], capture_output=True, text=True, timeout=10)
        cron_text = cron_proc.stdout if cron_proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        cron_text = ""
    required_cron = tuple(
        str(home / f".gbrain/maintenance-run.sh {action}")
        for action in ("sync", "dream")
    )
    missing_cron = [item for item in required_cron if item not in cron_text]
    if missing_cron:
        findings.append(make_finding(
            "p1", "GBRAIN_CRON_MISSING", "user-crontab",
            f"GBrain 单一调度源缺少任务：{', '.join(missing_cron)}。", "runtime-liveness",
        ))
        checks.append(_runtime_check("gbrain_cron", "failed", ",".join(missing_cron)))
    else:
        checks.append(_runtime_check("gbrain_cron", "ok", "sync=30m dream=6h"))

    for action, hours in (("sync", sync_stale_hours), ("dream", dream_stale_hours)):
        state_path = Path(os.environ.get(
            f"XIRANG_GBRAIN_{action.upper()}_STATE",
            str(home / f".gbrain/maintenance-{action}.json"),
        ))
        action_findings, check = _maintenance_freshness(action, state_path, now, hours)
        findings.extend(action_findings)
        checks.append(check)

    contract_verifier = Path(os.environ.get(
        "XIRANG_GBRAIN_CONTRACT_VERIFY", str(home / ".gbrain/verify-runtime-contract.py"),
    ))
    try:
        verify_proc = subprocess.run(
            [sys.executable, str(contract_verifier), "--json"],
            capture_output=True, text=True, timeout=180,
        )
        verify_data = json.loads(verify_proc.stdout) if verify_proc.stdout else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        verify_proc = None
        verify_data = {"failures": [f"probe_error:{exc}"]}
    if verify_proc is None or verify_proc.returncode != 0 or verify_data.get("status") != "success":
        findings.append(make_finding(
            "p1", "GBRAIN_CURRENT_REVISION_NOT_CONSUMED", str(contract_verifier),
            "GBrain 未证明当前运行时契约同时可直接读取并被语义检索消费。", "runtime-liveness",
            detail=verify_data,
        ))
        checks.append(_runtime_check("gbrain_current_contract", "failed", ",".join(verify_data.get("failures", []))))
    else:
        checks.append(_runtime_check(
            "gbrain_current_contract", "ok",
            f"revision={verify_data.get('source_updated')} body_sha256={verify_data.get('source_body_sha256')}",
        ))

    llm_wiki_checker = Path(os.environ.get(
        "XIRANG_LLM_WIKI_CHECKER", str(ROOT / ".standards/scripts/llm_wiki_check.py"),
    ))
    try:
        wiki_proc = subprocess.run(
            [sys.executable, str(llm_wiki_checker), "--json"],
            capture_output=True, text=True, timeout=180,
        )
        wiki_data = json.loads(wiki_proc.stdout) if wiki_proc.stdout else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        wiki_proc = None
        wiki_data = {"errors": [f"probe_error:{exc}"]}
    wiki_errors = wiki_data.get("errors", []) if isinstance(wiki_data.get("errors"), list) else []
    if (
        wiki_proc is None or wiki_proc.returncode != 0 or wiki_data.get("status") != "success"
        or not wiki_data.get("prototype_root")
    ):
        findings.append(make_finding(
            "p1", "LLM_WIKI_PROTOTYPE_NOT_CONSUMED", str(llm_wiki_checker),
            f"LLM Wiki 尚未完整消费当前原型事实（errors={len(wiki_errors)}）。", "runtime-liveness",
            detail={"prototype_root": wiki_data.get("prototype_root"), "error_count": len(wiki_errors), "sample": wiki_errors[:10]},
        ))
        checks.append(_runtime_check("llm_wiki", "failed", f"errors={len(wiki_errors)}"))
    else:
        checks.append(_runtime_check("llm_wiki", "ok", str(wiki_data.get("prototype_root"))))

    phoenix_executor = Path(os.environ.get(
        "XIRANG_V9_PHOENIX_SCRIPT", str(ROOT / "02-项目管理/脚本/v9-phoenix.py"),
    ))
    phoenix_wrapper = ROOT / ".standards/v9-reflex-run.sh"
    phoenix_state_path = Path(os.environ.get(
        "XIRANG_V9_PHOENIX_STATE", str(home / ".xirang/v9-runtime/巡检/phoenix-latest.json"),
    ))
    phoenix_state = _read_json(phoenix_state_path)
    try:
        wrapper_text = phoenix_wrapper.read_text(encoding="utf-8")
    except OSError:
        wrapper_text = ""
    phoenix_issues = []
    if not phoenix_executor.is_file():
        phoenix_issues.append("executor_missing")
    if '"$PHOENIX_SCRIPT" --apply-safe' not in wrapper_text:
        phoenix_issues.append("scheduler_integration_missing")
    phoenix_generated = parse_iso(str((phoenix_state or {}).get("generated_at", "")))
    if not phoenix_state:
        phoenix_issues.append("state_missing")
    elif phoenix_state.get("status") not in {"success", "degraded"}:
        phoenix_issues.append(f"state={phoenix_state.get('status')}")
    elif phoenix_generated is None or now - phoenix_generated > timedelta(hours=24):
        phoenix_issues.append("state_stale")
    safety = (phoenix_state or {}).get("safety") if isinstance((phoenix_state or {}).get("safety"), dict) else {}
    if phoenix_state and not all(safety.get(key) is False for key in (
        "source_note_edits", "gate_changes", "self_acceptance", "manifest_changes",
    )):
        phoenix_issues.append("safety_contract_invalid")
    if phoenix_issues:
        findings.append(make_finding(
            "p1", "PHOENIX_RUNTIME_INVALID", str(phoenix_state_path),
            f"Phoenix 执行链异常：{','.join(phoenix_issues)}。", "runtime-liveness",
            detail={"issues": phoenix_issues, "state": phoenix_state},
        ))
        checks.append(_runtime_check("phoenix_capability", "failed", ",".join(phoenix_issues)))
    else:
        checks.append(_runtime_check(
            "phoenix_capability", "ok",
            f"mode={phoenix_state.get('mode')} repairs={phoenix_state.get('repairs_applied')} "
            f"upgrade_candidates={phoenix_state.get('upgrade_candidates')}",
        ))

    # Hermes One owns the deterministic weekly entropy-v2 shadow job. Check
    # the scheduler, desired job contract, and last real execution separately;
    # a fresh report alone must not be allowed to hide a dead generator.
    hermes_heartbeat = Path(os.environ.get(
        "XIRANG_HERMES_CRON_HEARTBEAT", str(home / ".hermes/cron/ticker_heartbeat"),
    ))
    if not hermes_heartbeat.is_file():
        findings.append(make_finding(
            "p1", "HERMES_CRON_SCHEDULER_MISSING", str(hermes_heartbeat),
            "Hermes One cron ticker 心跳不存在。", "runtime-liveness",
        ))
        checks.append(_runtime_check("hermes_cron_scheduler", "failed", "heartbeat_missing"))
    else:
        heartbeat_age = now - datetime.fromtimestamp(hermes_heartbeat.stat().st_mtime, tz=now.tzinfo)
        if heartbeat_age > timedelta(minutes=5):
            age_m = round(heartbeat_age.total_seconds() / 60, 1)
            findings.append(make_finding(
                "p1", "HERMES_CRON_SCHEDULER_STALE", str(hermes_heartbeat),
                f"Hermes One cron ticker 已 {age_m} 分钟未刷新（阈值 5 分钟）。",
                "runtime-liveness", detail={"age_minutes": age_m},
            ))
            checks.append(_runtime_check("hermes_cron_scheduler", "stale", f"age_minutes={age_m}"))
        else:
            checks.append(_runtime_check("hermes_cron_scheduler", "ok", "ticker_fresh"))

    hermes_jobs_path = Path(os.environ.get(
        "XIRANG_HERMES_CRON_JOBS", str(home / ".hermes/cron/jobs.json"),
    ))
    entropy_job_id = os.environ.get("XIRANG_ENTROPY_JOB_ID", "328aa7f7b498")
    jobs_data = _read_json(hermes_jobs_path) or {}
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else []
    entropy_job = next((item for item in jobs if isinstance(item, dict) and item.get("id") == entropy_job_id), None)
    entropy_job_issues: list[str] = []
    if entropy_job is None:
        entropy_job_issues.append("job_missing")
    else:
        schedule = entropy_job.get("schedule") if isinstance(entropy_job.get("schedule"), dict) else {}
        if entropy_job.get("enabled") is not True:
            entropy_job_issues.append("disabled")
        if entropy_job.get("state") not in {"scheduled", "running"}:
            entropy_job_issues.append(f"state={entropy_job.get('state')}")
        if schedule.get("expr") != "0 9 * * 1":
            entropy_job_issues.append(f"schedule={schedule.get('expr')}")
        if entropy_job.get("no_agent") is not True:
            entropy_job_issues.append("not_no_agent")
        if entropy_job.get("script") != "v9-entropy-shadow.py":
            entropy_job_issues.append(f"script={entropy_job.get('script')}")
        if entropy_job.get("last_status") == "error":
            entropy_job_issues.append("last_status=error")
    if entropy_job_issues:
        findings.append(make_finding(
            "p1", "ENTROPY_SCHEDULER_INVALID", str(hermes_jobs_path),
            f"Hermes 熵检测任务配置或状态异常：{', '.join(entropy_job_issues)}。",
            "runtime-liveness", detail={"job_id": entropy_job_id, "issues": entropy_job_issues},
        ))
        checks.append(_runtime_check("entropy_scheduler", "failed", ",".join(entropy_job_issues)))
    else:
        checks.append(_runtime_check("entropy_scheduler", "ok", f"job={entropy_job_id} weekly_monday_09:00"))

    entropy_state_path = Path(os.environ.get(
        "XIRANG_ENTROPY_JOB_STATE", str(home / ".hermes/cron/entropy-shadow-state.json"),
    ))
    entropy_state = _read_json(entropy_state_path)
    entropy_last_success = parse_iso(str((entropy_state or {}).get("last_success_at", "")))
    entropy_status = str((entropy_state or {}).get("status", "missing"))
    entropy_state_issue = ""
    if not entropy_state:
        entropy_state_issue = "state_missing"
    elif entropy_status == "failed":
        entropy_state_issue = f"last_run_failed:{entropy_state.get('reason', 'unknown')}"
    elif entropy_last_success is None:
        entropy_state_issue = "never_succeeded"
    elif now - entropy_last_success > timedelta(hours=entropy_stale_hours):
        entropy_state_issue = f"stale_hours={round((now - entropy_last_success).total_seconds() / 3600, 1)}"
    elif entropy_state.get("detector_version") != "2.0.0" or entropy_state.get("mode") != "shadow":
        entropy_state_issue = (
            f"identity={entropy_state.get('detector_version')}/{entropy_state.get('mode')}"
        )
    if entropy_state_issue:
        findings.append(make_finding(
            "p1", "ENTROPY_JOB_STATE_INVALID", str(entropy_state_path),
            f"熵检测最近执行状态异常：{entropy_state_issue}。", "runtime-liveness",
            detail=entropy_state or {},
        ))
        checks.append(_runtime_check("entropy_job_state", "failed", entropy_state_issue))
    else:
        checks.append(_runtime_check(
            "entropy_job_state", "ok",
            f"last_success={entropy_last_success.isoformat()} last_attempt={entropy_status}",
        ))

    entropy_queue_path = Path(os.environ.get(
        "XIRANG_ENTROPY_GOVERNANCE_QUEUE",
        str(home / ".xirang/v9-runtime/治理/entropy-governance-queue.json"),
    ))
    entropy_queue = _read_json(entropy_queue_path)
    queue_issues: list[str] = []
    queue_metrics = (entropy_queue or {}).get("metrics", {})
    if not entropy_queue:
        queue_issues.append("queue_missing")
    elif entropy_queue.get("policy") != "human_confirmation_with_default_defer; unresolved_findings_remain_backlog; source_notes_never_auto_modified":
        queue_issues.append("unsafe_policy")
    required_metrics = {
        "pending_confirmation", "confirmed_for_action", "deferred", "archived", "rejected", "resolved",
        "previous_open", "current_open", "new_since_previous",
        "resolved_since_previous", "net_backlog_delta",
    }
    if not isinstance(queue_metrics, dict) or not required_metrics.issubset(queue_metrics):
        queue_issues.append("metrics_incomplete")
    queue_updated = parse_iso(str((entropy_queue or {}).get("updated_at", "")))
    if queue_updated is None:
        queue_issues.append("updated_at_missing")
    elif now - queue_updated > timedelta(hours=entropy_stale_hours):
        queue_issues.append(f"stale_hours={round((now - queue_updated).total_seconds() / 3600, 1)}")
    detected_confirmed = int(((entropy_queue or {}).get("source_summary") or {}).get("confirmed", -1))
    represented = sum(int(queue_metrics.get(key, 0)) for key in (
        "pending_confirmation", "confirmed_for_action", "deferred", "archived", "rejected",
    )) if isinstance(queue_metrics, dict) else -1
    if detected_confirmed < 0 or represented != detected_confirmed:
        queue_issues.append(f"consumer_mismatch={represented}/{detected_confirmed}")
    if queue_issues:
        findings.append(make_finding(
            "p1", "ENTROPY_GOVERNANCE_CONSUMER_INVALID", str(entropy_queue_path),
            f"熵报告未被安全、完整地消费到人工确认队列：{', '.join(queue_issues)}。", "runtime-liveness",
            detail={"issues": queue_issues, "metrics": queue_metrics},
        ))
        checks.append(_runtime_check("entropy_governance_queue", "failed", ",".join(queue_issues)))
    else:
        checks.append(_runtime_check(
            "entropy_governance_queue", "ok",
            f"backlog={queue_metrics.get('current_open')} deferred={queue_metrics.get('deferred')} net_delta={queue_metrics.get('net_backlog_delta')}",
        ))
        backlog = int(queue_metrics.get("current_open", 0) or 0)
        if backlog:
            findings.append(make_finding(
                "advisory", "ENTROPY_GOVERNANCE_BACKLOG", str(entropy_queue_path),
                f"熵治理队列仍有 {backlog} 条未解决项（deferred 仍计入积压）。",
                "runtime-liveness", detail={"metrics": queue_metrics},
            ))

    entropy_dir = Path(os.environ.get(
        "XIRANG_ENTROPY_SHADOW_DIR", str(ROOT / "50-经验/Agent进化/熵报告-影子"),
    ))
    reports = sorted(entropy_dir.glob("影子熵报告-*.md"), key=lambda p: p.stat().st_mtime) if entropy_dir.exists() else []
    if not reports:
        findings.append(make_finding(
            "p1", "ENTROPY_SHADOW_MISSING", str(entropy_dir),
            "doc-gardening v2 尚无影子报告。", "runtime-liveness",
        ))
        checks.append(_runtime_check("entropy_shadow", "failed", "report_missing"))
    else:
        latest = reports[-1]
        age = now - datetime.fromtimestamp(latest.stat().st_mtime, tz=now.tzinfo)
        if age > timedelta(hours=entropy_stale_hours):
            age_h = round(age.total_seconds() / 3600, 1)
            findings.append(make_finding(
                "p1", "ENTROPY_SHADOW_STALE", str(latest),
                f"doc-gardening v2 影子报告已 {age_h}h 未更新（阈值 {entropy_stale_hours}h）。",
                "runtime-liveness", detail={"age_hours": age_h},
            ))
            checks.append(_runtime_check("entropy_shadow", "stale", f"age_hours={age_h}"))
        else:
            checks.append(_runtime_check("entropy_shadow", "ok", str(latest)))

    skill_checker = Path(os.environ.get(
        "XIRANG_SKILL_SHADOW_CHECKER",
        str(SCRIPT_DIR / "v9-skill-shadow-check.py"),
    ))
    try:
        skill_proc = subprocess.run(
            [sys.executable, str(skill_checker), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        skill_data = json.loads(skill_proc.stdout) if skill_proc.stdout else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        skill_proc = None
        skill_data = {"summary": {"p1": 1}, "error": str(exc)}
    skill_summary = skill_data.get("summary") if isinstance(skill_data.get("summary"), dict) else {}
    if skill_proc is None or skill_proc.returncode != 0 or int(skill_summary.get("p1", 0)) > 0:
        findings.append(make_finding(
            "p1", "SKILL_VERSION_SHADOW", str(skill_checker),
            "skill 多入口存在未声明的版本遮蔽，平台解析结果不唯一。", "runtime-liveness",
            detail=skill_data,
        ))
        checks.append(_runtime_check(
            "skill_shadow", "failed", f"p1={skill_summary.get('p1', 'unknown')}",
        ))
    else:
        checks.append(_runtime_check(
            "skill_shadow", "ok",
            f"skills={skill_summary.get('skills_scanned', 0)} explicit_variants={skill_summary.get('explicit_variant_groups', 0)}",
        ))

    freeze_checker = Path(os.environ.get(
        "XIRANG_FREEZE_OBSERVATION_CHECKER",
        str(SCRIPT_DIR / "v9-freeze-observation.py"),
    ))
    try:
        freeze_proc = subprocess.run(
            [sys.executable, str(freeze_checker), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        freeze_data = json.loads(freeze_proc.stdout) if freeze_proc.stdout else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        freeze_proc = None
        freeze_data = {"status": "blocked", "error": str(exc)}
    freeze_status = freeze_data.get("status")
    if freeze_proc is None or freeze_proc.returncode != 0 or freeze_status == "blocked":
        findings.append(make_finding(
            "p1", "FREEZE_OBSERVATION_BLOCKED", str(freeze_checker),
            "V9 冻结期观测指标未全部通过，不得解冻或进入 V9.6。", "runtime-liveness",
            detail=freeze_data.get("today", freeze_data),
        ))
        checks.append(_runtime_check("freeze_observation", "failed", str(freeze_status or "error")))
    else:
        checks.append(_runtime_check(
            "freeze_observation", "ok",
            f"status={freeze_status} streak={freeze_data.get('consecutive_pass_days', 0)}/{freeze_data.get('required_consecutive_days', 14)}",
        ))

    return findings, checks


# ---------- 去重 + 冷却（子任务 4）----------
def dedup_key(finding: dict) -> str:
    rule = finding["rule_id"]
    if rule == "LOG_GAP":
        return "LOG_GAP:run_logs"  # Codex 建议：滚动日期窗口须用稳定键
    return f"{rule}:{finding['object']}"


def apply_cooldown(findings: list[dict], now: datetime, cooldown_hours: int, state: dict) -> dict:
    """active 判定（Codex P1：严重度升级穿透冷却）：
       1. p0 永远 active（穿透冷却）；
       2. 严重度比上次变重（rank 数值更小）→ active；
       3. 否则按冷却窗：超过 cooldown 才 active。
    """
    now_iso = now.isoformat(timespec="seconds")
    cooldown = timedelta(hours=cooldown_hours)
    new_state: dict = {}

    for f in findings:
        key = dedup_key(f)
        prev = state.get(key, {})
        first_seen = prev.get("first_seen", now_iso)
        last_reported = parse_iso(prev.get("last_reported", "")) if prev.get("last_reported") else None
        count = prev.get("count", 0)

        cur_rank = SEVERITY_ORDER.get(f["severity"], 9)
        prev_rank = SEVERITY_ORDER.get(prev.get("severity"), 9)
        is_p0 = f["severity"] == "p0"
        worsened = bool(prev) and cur_rank < prev_rank          # 数值更小 = 更严重；首次出现不算升级
        cooled = last_reported is None or (now - last_reported) >= cooldown
        active = is_p0 or worsened or cooled

        reason = "p0" if is_p0 else "escalated" if worsened else "cooled" if cooled else "suppressed"
        f["key"] = key
        f["first_seen"] = first_seen
        f["suppressed"] = not active
        f["active_reason"] = reason if active else None
        f["seen_count"] = count + 1

        new_state[key] = {
            "first_seen": first_seen,
            "last_reported": now_iso if active else prev.get("last_reported", now_iso),
            "count": count + 1,
            "severity": f["severity"],
        }

    return new_state


def build_report(
    findings: list[dict], today: date, now: datetime, cfg: dict,
    sources_run: list[dict], runtime_checks: list[dict],
) -> dict:
    def count(sev): return sum(1 for f in findings if f["severity"] == sev)
    def acount(sev): return sum(1 for f in findings if f["severity"] == sev and not f.get("suppressed"))
    active = [f for f in findings if not f.get("suppressed")]
    # active 中最严重的（checklist 判断用）
    worst_active = min((f["severity"] for f in active), key=lambda s: SEVERITY_ORDER.get(s, 9)) if active else None
    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    sources_failed = [r["source"] for r in sources_run if r["status"] != "ok"]
    return {
        "check": CHECK_NAME,
        "generated_at": now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "config": cfg,
        # 自省：每个源跑没跑/状态/几条。区分"跑了干净(ok,0)"与"没跑(missing)/崩了(failed)"。
        "sources_run": sources_run,
        "sources_ok": len(sources_run) - len(sources_failed),
        "sources_failed": sources_failed,
        "runtime_checks": runtime_checks,
        "summary": {
            "total": len(findings),
            "p0": count("p0"),
            "p1": count("p1"),
            "advisory": count("advisory"),
            "active": len(active),
            "active_p0": acount("p0"),
            "active_p1": acount("p1"),
            "active_advisory": acount("advisory"),
            "suppressed": len(findings) - len(active),
            "worst": worst,
            "worst_active": worst_active,
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--stale-heartbeat-hours", type=int, default=24)
    parser.add_argument("--cooldown-hours", type=int, default=24)
    parser.add_argument("--gbrain-sync-stale-hours", type=int, default=2)
    parser.add_argument("--gbrain-dream-stale-hours", type=int, default=8)
    parser.add_argument("--entropy-stale-hours", type=int, default=216)
    parser.add_argument("--quiet", action="store_true", help="只写文件，不打印（launchd 用）")
    parser.add_argument("--strict", action="store_true", help="有 active 发现时退出码 1")
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    now = now_local()
    cfg = {
        "stale_heartbeat_hours": args.stale_heartbeat_hours,
        "cooldown_hours": args.cooldown_hours,
        "gbrain_sync_stale_hours": args.gbrain_sync_stale_hours,
        "gbrain_dream_stale_hours": args.gbrain_dream_stale_hours,
        "entropy_stale_hours": args.entropy_stale_hours,
    }

    # 每个源显式记录"跑没跑/什么状态/几条"，使"跑了干净"可与"没跑/静默崩"区分（自省）。
    source_specs = [
        ("project-ops", lambda: collect_project_ops(today)),
        ("agent-state", collect_agent_state),
        ("heartbeat", lambda: collect_heartbeat(now, args.stale_heartbeat_hours)),
        ("policy-conflict", collect_policy_conflicts),
        ("starter-leak", collect_starter_leaks),
        ("task-state", collect_task_state),
        ("scope-tamper", collect_scope_tamper),
        ("handoff", collect_handoff),
        ("iteration-ops", collect_iteration_ops),
        ("frontmatter-lint", collect_frontmatter_lint),
    ]
    findings: list[dict] = []
    sources_run: list[dict] = []
    for name, fn in source_specs:
        try:
            fs = fn()
        except Exception as exc:  # 源静默崩溃也要显式记录，而非从快照里消失
            fs = [make_finding("p1", "SOURCE_CRASHED", name, f"{name} 采集异常：{exc}", name)]
        status = "ok"
        for f in fs:
            if f["rule_id"] in ("SOURCE_FAILED", "SOURCE_CRASHED"):
                status = "failed"
            elif f["rule_id"] == "SOURCE_MISSING":
                status = "missing"
        sources_run.append({"source": name, "status": status, "findings": len(fs)})
        findings += fs

    runtime_findings, runtime_checks = collect_runtime_liveness(
        now, args.gbrain_sync_stale_hours, args.gbrain_dream_stale_hours, args.entropy_stale_hours,
    )
    findings += runtime_findings

    # 排序：先按严重度，再按 source
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f.get("source", ""), f["rule_id"]))

    # 只写 Vault 外运行态目录（防污染硬约束）；read-modify-write 全程持锁、原子替换（Codex P1）
    with state_lock():
        state = read_state()
        new_state = apply_cooldown(findings, now, args.cooldown_hours, state)
        report = build_report(findings, today, now, cfg, sources_run, runtime_checks)
        atomic_write_json(HEALTH_LATEST, report)
        atomic_write_json(REFLEX_STATE, new_state)

    if not args.quiet:
        s = report["summary"]
        print(f"# V9 第一反射器巡检 ({today})")
        print(f"快照: {HEALTH_LATEST}")
        srcs = " ".join(f"{r['source']}={r['status']}({r['findings']})" for r in report["sources_run"])
        flag = "" if not report["sources_failed"] else f"  ⚠ 异常源: {report['sources_failed']}"
        print(f"源: {srcs}{flag}")
        runtime = " ".join(f"{r['check']}={r['status']}" for r in report["runtime_checks"])
        print(f"运行时: {runtime}")
        print(f"汇总: total={s['total']} p0={s['p0']} p1={s['p1']} advisory={s['advisory']} "
              f"| active={s['active']}(p0={s['active_p0']}/p1={s['active_p1']}/adv={s['active_advisory']}) "
              f"suppressed={s['suppressed']} worst_active={s['worst_active']}")
        active = [f for f in findings if not f.get("suppressed")]
        if active:
            print("\n[active] 待上报（冷却窗口外或严重度升级）：")
            for f in active:
                print(f"  [{f['severity']}] {f['rule_id']} ({f.get('active_reason')}) | {f['message']}")
        else:
            print("\n[active] 无新增待上报项（全部在冷却窗口内或无异常）。")

    # 退出码：p0 active 始终非零；--strict 时任意 active 非零
    active_p0 = any(f["severity"] == "p0" and not f.get("suppressed") for f in findings)
    if active_p0:
        return 1
    if args.strict and any(not f.get("suppressed") for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
