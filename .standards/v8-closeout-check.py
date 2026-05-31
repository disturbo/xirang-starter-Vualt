#!/usr/bin/env python3
"""
v8-closeout-check.py -- V8.5 Phase 2: 收工完成度自动验证
v1.0.0 | 2026-05-26 | 息壤 V8.5

在 v8_end / gate-enforce pre-end 时被调用，检查 M4/M5 任务的收工条件。
Agent 不可绕过：由 gate-enforce.py pre-end 自动调用。

用法:
  python3 .standards/v8-closeout-check.py --task-id T-xxx --agent claudian [--gear M4] [--json]

退出码:
  0 = 全部通过（或仅有 advisory 级别问题）
  1 = 有 P1 级别问题，建议阻断收工
  2 = 参数错误

检查项:
  [P1] MISSING_DELIVERABLES  - 无产物写入记录（事件流中无 file_write）
  [P1] KANBAN_NOT_UPDATED    - 看板未更新该任务状态
  [P1] NO_RUN_LOG            - 当日运行日志未记录
  [P2] UNCOLLECTED_SUBTASKS  - 子任务未全部收集
  [P2] NO_HANDOFF            - M5 任务无 Handoff 记录
  [P2] SCOPE_VIOLATION       - 写入了 write_scope 之外的文件
"""
from __future__ import annotations

import sys
import os
import json
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "$VAULT_ROOT"))
EVENT_FILE = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"
LOG_DIR = VAULT_ROOT / "02-项目管理" / "运行日志"
KANBAN_FILE = VAULT_ROOT / "00-MOC" / "多智能体协作看板.md"
AGENT_STATUS_DIR = VAULT_ROOT / "02-项目管理" / "智能体状态"

TZ = timezone(timedelta(hours=8))


@dataclass
class CheckResult:
    """检查结果"""
    priority: int       # 1=P1(建议阻断), 2=P2(advisory)
    rule_id: str
    message: str
    details: dict = None

    def to_dict(self) -> dict:
        d = {"priority": self.priority, "rule_id": self.rule_id, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


def _read_events_for_task(task_id: str) -> list[dict]:
    """从事件流中读取该任务的所有事件"""
    events = []
    if not EVENT_FILE.exists():
        return events
    try:
        with open(EVENT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("task_id") == task_id:
                        events.append(ev)
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass
    return events


def _get_file_writes_for_task(task_id: str) -> list[str]:
    """获取该任务的所有 file_write 事件中的文件路径"""
    events = _read_events_for_task(task_id)
    files = []
    for ev in events:
        if ev.get("event") == "file_write":
            f = ev.get("file", "")
            if f:
                files.append(f)
    return files


def _parse_authorized_paths(card_file) -> list:
    """从任务卡 YAML 中解析 authorized_paths 列表"""
    try:
        content = card_file.read_text(encoding="utf-8")
        section = re.search(r'authorized_paths:\s*\n((?:\s+-\s+.+\n?)*)', content)
        if not section:
            return []
        paths = re.findall(r"^\s+-\s+(.+)$", section.group(1), re.MULTILINE)
        return [p.strip().strip('"').strip("'") for p in paths if p.strip()]
    except (IOError, OSError):
        return []


def _get_task_start_time(task_id: str) -> float:
    """从事件流获取 task_start 事件的时间戳（epoch seconds）"""
    from datetime import datetime, timezone, timedelta
    events = _read_events_for_task(task_id)
    for ev in events:
        if ev.get("event") == "task_start":
            ts_str = ev.get("ts", "")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    return dt.timestamp()
                except (ValueError, TypeError):
                    pass
    # Fallback: 0 means any file counts
    return 0.0


def _get_write_scope(agent: str) -> str:
    """从状态文件读取当前 write_scope"""
    agent_files = {
        "claudian": "Claudian.md",
        "dongfeng": "Claudian.md",
        "workbuddy": "WorkBuddy.md",
        "xiaochong": "阿莫西林.md",
        "toubao": "头孢.md",
        "hongmeisu": "红霉素.md",
    }
    filename = agent_files.get(agent)
    if not filename:
        return ""
    status_file = AGENT_STATUS_DIR / filename
    if not status_file.exists():
        return ""
    try:
        content = status_file.read_text(encoding="utf-8")
        m = re.search(r'^write_scope:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
        if m:
            val = m.group(1).strip()
            if val == "null":
                return ""
            return val
    except IOError:
        pass
    return ""


def _check_deliverables(task_id: str) -> list[CheckResult]:
    """检查是否有产物写入（事件流 + 文件系统双重检测）"""
    results = []

    # 主检测：事件流 file_write 记录
    files = _get_file_writes_for_task(task_id)
    if files:
        return results  # 有事件记录，通过

    # Fallback：文件系统扫描（覆盖 Bash 写入场景）
    # 1. 从任务卡读取 authorized_paths
    card_file = VAULT_ROOT / "_temp" / task_id / "task-card.yaml"
    if card_file.exists():
        authorized = _parse_authorized_paths(card_file)
        task_start_ts = _get_task_start_time(task_id)

        for auth_path in authorized:
            full = VAULT_ROOT / auth_path
            if full.is_dir():
                # 目录：检查是否有 mtime > task_start 的文件
                try:
                    for f in full.rglob("*"):
                        if f.is_file() and f.stat().st_mtime > task_start_ts:
                            return results  # 找到产物
                except (OSError, PermissionError):
                    continue
            elif full.is_file():
                # 精确文件：检查 mtime
                try:
                    if full.stat().st_mtime > task_start_ts:
                        return results  # 找到产物
                except (OSError, PermissionError):
                    continue

    # 2. 检查 _temp/{task_id}/ 下是否有非元数据文件
    temp_dir = VAULT_ROOT / "_temp" / task_id
    if temp_dir.exists():
        for f in temp_dir.iterdir():
            if f.is_file() and f.name != "task-card.yaml":
                return results  # 有 scratch 产物
        for d in temp_dir.iterdir():
            if d.is_dir() and d.name != "subtasks":
                try:
                    if any(d.iterdir()):
                        return results
                except (OSError, PermissionError):
                    continue

    # 事件流和文件系统均无产物
    results.append(CheckResult(
        priority=1,
        rule_id="MISSING_DELIVERABLES",
        message=f"任务 {task_id} 无产物记录（事件流和文件系统均无交付物）。",
        details={"task_id": task_id, "file_write_count": 0,
                 "fallback": "filesystem scan found no files in authorized_paths after task_start"}
    ))
    return results


def _check_kanban(task_id: str) -> list[CheckResult]:
    """检查看板是否有该任务记录"""
    results = []
    if not KANBAN_FILE.exists():
        return results
    try:
        content = KANBAN_FILE.read_text(encoding="utf-8")
        if task_id not in content:
            results.append(CheckResult(
                priority=1,
                rule_id="KANBAN_NOT_UPDATED",
                message=f"看板中未找到任务 {task_id} 的记录。M4 任务必须登记看板。",
                details={"task_id": task_id, "kanban_path": str(KANBAN_FILE.relative_to(VAULT_ROOT))}
            ))
    except IOError:
        pass
    return results


def _check_run_log() -> list[CheckResult]:
    """检查当日运行日志是否存在"""
    results = []
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.md"
    if not log_file.exists():
        results.append(CheckResult(
            priority=1,
            rule_id="NO_RUN_LOG",
            message=f"当日运行日志 {today}.md 不存在。",
            details={"expected_path": str(log_file.relative_to(VAULT_ROOT))}
        ))
    return results


def _check_subtasks(task_id: str) -> list[CheckResult]:
    """检查是否有未收集的子任务"""
    results = []
    subtasks_dir = VAULT_ROOT / "_temp" / task_id / "subtasks"
    if not subtasks_dir.exists():
        return results

    active = []
    for f in subtasks_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            state = data.get("state", "")
            if state not in ("COLLECTED", "DESTROYED"):
                active.append(data.get("sub_id", f.stem))
        except (json.JSONDecodeError, IOError):
            continue

    if active:
        results.append(CheckResult(
            priority=2,
            rule_id="UNCOLLECTED_SUBTASKS",
            message=f"{len(active)} 个子任务未收集: {active[:5]}",
            details={"count": len(active), "sub_ids": active}
        ))
    return results


def _check_handoff(task_id: str, gear: str) -> list[CheckResult]:
    """M5 必须有 Handoff；M4 有下游时需要"""
    results = []
    if gear != "M5":
        return results  # M4 的 Handoff 不强制

    # 检查看板中是否有 Handoff 记录
    if KANBAN_FILE.exists():
        try:
            content = KANBAN_FILE.read_text(encoding="utf-8")
            # 查找 Handoff 区域中是否有该任务
            handoff_section = re.search(
                r'## Handoff.*?(?=\n## |\Z)', content, re.DOTALL
            )
            if handoff_section:
                if task_id in handoff_section.group():
                    return results  # 有 Handoff
            # 没找到
            results.append(CheckResult(
                priority=2,
                rule_id="NO_HANDOFF",
                message=f"M5 任务 {task_id} 看板中无 Handoff 记录。M5 必写 Handoff。",
                details={"task_id": task_id, "gear": gear}
            ))
        except IOError:
            pass
    return results


def _check_scope_violation(task_id: str, agent: str) -> list[CheckResult]:
    """检查是否有写入超出 write_scope 的文件"""
    results = []
    write_scope = _get_write_scope(agent)
    if not write_scope:
        return results

    # 解析 scope 为路径前缀列表（支持目录和精确文件）
    scope_entries = [s.strip() for s in write_scope.split(",") if s.strip()]

    # 获取该任务的所有写入文件
    files = _get_file_writes_for_task(task_id)
    violations = []
    for f in files:
        in_scope = False
        for entry in scope_entries:
            # 目录匹配（以 / 结尾）
            if entry.endswith("/"):
                if f.startswith(entry):
                    in_scope = True
                    break
            else:
                # 精确文件匹配
                if f == entry or f.startswith(entry + "/"):
                    in_scope = True
                    break
        if not in_scope:
            violations.append(f)

    if violations:
        results.append(CheckResult(
            priority=2,
            rule_id="SCOPE_VIOLATION",
            message=f"{len(violations)} 个文件写入超出 write_scope: {violations[:5]}",
            details={
                "write_scope": write_scope,
                "violations": violations,
                "count": len(violations)
            }
        ))
    return results


def run_closeout_check(task_id: str, agent: str, gear: str = "M4") -> list[CheckResult]:
    """执行全部收工检查，返回结果列表"""
    all_results = []

    all_results.extend(_check_deliverables(task_id))
    all_results.extend(_check_kanban(task_id))
    all_results.extend(_check_run_log())
    all_results.extend(_check_subtasks(task_id))
    all_results.extend(_check_handoff(task_id, gear))
    all_results.extend(_check_scope_violation(task_id, agent))

    return all_results


def main():
    parser = argparse.ArgumentParser(
        description="V8.5 收工完成度验证（gate-enforce pre-end 调用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
退出码:
  0 = 全部通过或仅 P2 advisory
  1 = 有 P1 级别问题（建议阻断收工）
  2 = 参数错误

示例:
  python3 .standards/v8-closeout-check.py --task-id T-20260526-80 --agent claudian --json
"""
    )
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument("--agent", required=True, help="Agent ID")
    parser.add_argument("--gear", default="M4", choices=["M4", "M5"], help="档位")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args()

    results = run_closeout_check(args.task_id, args.agent, args.gear)

    # 分类
    p1_issues = [r for r in results if r.priority <= 1]
    p2_advisories = [r for r in results if r.priority > 1]

    if args.json:
        output = {
            "check": "closeout",
            "task_id": args.task_id,
            "agent": args.agent,
            "gear": args.gear,
            "passed": len(p1_issues) == 0,
            "p1_count": len(p1_issues),
            "p2_count": len(p2_advisories),
            "issues": [r.to_dict() for r in p1_issues],
            "advisories": [r.to_dict() for r in p2_advisories],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if p1_issues:
            for r in p1_issues:
                print(f"[P{r.priority}-BLOCK] {r.rule_id}: {r.message}", file=sys.stderr)
        if p2_advisories:
            for r in p2_advisories:
                print(f"[P{r.priority}] {r.rule_id}: {r.message}", file=sys.stderr)
        if not results:
            print("[CLOSEOUT] 全部通过", file=sys.stderr)

    sys.exit(1 if p1_issues else 0)


if __name__ == "__main__":
    main()
