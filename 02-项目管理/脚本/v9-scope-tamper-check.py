#!/usr/bin/env python3
"""
v9-scope-tamper-check.py — V9.4.1 write_scope 越权检测器（事后发现）

动机（吸收用户检核 finding P1 #2）：
  pre-write hook 只能拦 Write/Edit 改 write_scope，拦不住 Bash 直写状态文件。
  本检测器作为反射器事后兜底：**比较"任务卡授权范围"与"状态文件实际 write_scope"**，
  发现实际 scope 超出授权 → SCOPE_ESCALATION(p1)。
  注意：不是"见到 ./ 就报错"，而是"实际 scope 不被任一授权路径覆盖"才报。

用法：
  python3 v9-scope-tamper-check.py --status <状态文件> --task-root <_temp目录> --json
  python3 v9-scope-tamper-check.py --json          # 扫全部 agent 状态文件（反射器用）

输出统一 severity schema（p0/p1/advisory），与其它 v9-*-check 一致，便于接入反射器。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(".")
STATUS_DIR = ROOT / "02-项目管理" / "智能体状态"
DEFAULT_TASK_ROOT = ROOT / "_temp"
CHECK_NAME = "v9-scope-tamper-check"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[4:end] if end != -1 else ""


def fm_value(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def norm_path(p: str) -> str:
    """归一化路径用于覆盖比较：去引号/空白、去 ./ 与前导斜杠、去尾斜杠。根目录归一为 ''。"""
    p = p.strip().strip('"').strip("'")
    p = p.lstrip("./").lstrip("/")
    p = p.rstrip("/")
    if p in {".", ""}:
        return ""  # 代表整个 vault 根
    return p


def parse_scope_list(raw: str) -> list[str]:
    return [norm_path(x) for x in raw.split(",") if x.strip()]


def read_authorized_paths(card_path: Path) -> list[str] | None:
    if not card_path.exists():
        return None
    text = card_path.read_text(encoding="utf-8")
    # 匹配 authorized_paths: 下的 - "xxx" 列表项
    m = re.search(r"authorized_paths:\s*\n((?:\s*-\s*.+\n?)*)", text)
    if not m:
        # 也兼容任务卡 paths.allowed_write_roots
        m = re.search(r"allowed_write_roots:\s*\n((?:\s*-\s*.+\n?)*)", text)
    if not m:
        return []
    items = re.findall(r"^\s*-\s*(.+?)\s*$", m.group(1), re.MULTILINE)
    return [norm_path(x) for x in items]


def covered_by(scope_entry: str, authorized: list[str]) -> bool:
    """scope_entry 是否被某个授权路径覆盖（在其之内）。"""
    for a in authorized:
        if a == "":
            return True  # 授权即根（理论上不该出现），覆盖一切
        if scope_entry == a or scope_entry.startswith(a.rstrip("/") + "/") or scope_entry == a.rstrip("/"):
            return True
    return False


def check_status_file(status_path: Path, task_root: Path) -> list[dict]:
    findings: list[dict] = []
    text = status_path.read_text(encoding="utf-8")
    fm = frontmatter(text)
    if not fm:
        return findings
    agent_id = fm_value(fm, "agent_id") or status_path.stem
    scope_raw = fm_value(fm, "write_scope")
    task_id = fm_value(fm, "current_task_id")
    rel = str(status_path)

    if scope_raw in {"", "null", "None"}:
        return findings  # 无 scope（idle 等），无需比较

    scope_list = parse_scope_list(scope_raw)

    if not task_id or task_id in {"null", "None"}:
        # busy 有 scope 却无任务卡支撑——无法比对授权，给 advisory
        findings.append({
            "severity": "advisory", "rule_id": "SCOPE_NO_TASK", "object": agent_id,
            "message": f"{agent_id}: write_scope={scope_raw} 但无 current_task_id，无法核对授权。",
        })
        return findings

    card = task_root / task_id / "task-card.yaml"
    authorized = read_authorized_paths(card)
    if authorized is None:
        findings.append({
            "severity": "advisory", "rule_id": "SCOPE_CARD_MISSING", "object": agent_id,
            "message": f"{agent_id}: 找不到任务卡 {card}，无法核对授权。",
        })
        return findings

    # 逐条比较：任一 scope 项不被授权覆盖 = 越权扩张
    escalated = [s for s in scope_list if not covered_by(s, authorized)]
    if escalated:
        shown = ["(vault根)" if s == "" else s for s in escalated]
        findings.append({
            "severity": "p1", "rule_id": "SCOPE_ESCALATION", "object": agent_id,
            "message": (f"{agent_id}: write_scope 含未授权路径 {shown}（授权范围 "
                        f"{[a or '(根)' for a in authorized]}），疑似 Bash 直写扩权。"),
            "detail": {"write_scope": scope_list, "authorized": authorized, "escalated": escalated},
        })
    return findings


def build_report(findings: list[dict]) -> dict:
    def c(sev): return sum(1 for f in findings if f["severity"] == sev)
    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "summary": {"total": len(findings), "p0": c("p0"), "p1": c("p1"), "advisory": c("advisory"), "worst": worst},
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", help="单个状态文件路径；缺省扫描全部 agent 状态文件")
    parser.add_argument("--task-root", default=str(DEFAULT_TASK_ROOT), help="任务卡 _temp 根目录")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="有 p0/p1 时退出码 1")
    args = parser.parse_args()

    task_root = Path(args.task_root)
    findings: list[dict] = []
    if args.status:
        findings = check_status_file(Path(args.status), task_root)
    elif STATUS_DIR.exists():
        for sp in sorted(STATUS_DIR.glob("*.md")):
            findings.extend(check_status_file(sp, task_root))

    report = build_report(findings)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"# {CHECK_NAME}")
        for f in findings:
            print(f"  [{f['severity']}] {f['rule_id']} | {f['message']}")
        if not findings:
            print("  no scope tampering detected")

    blocking = report["summary"]["p0"] + report["summary"]["p1"]
    return 1 if (blocking and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
