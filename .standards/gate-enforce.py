#!/usr/bin/env python3
"""
gate-enforce.py -- V9 统一门禁入口
v1.1.0 | 2026-05-28 | 息壤 V9.0

将分散的合规检查统一为单一入口，嵌入 v8-handshake.sh 生命周期函数。
P0 = 硬阻断（exit 1），P1-P3 = advisory（stderr 警告，exit 0）。

用法:
  python3 .standards/gate-enforce.py pre-start  --agent <id> --gear <M4/M5> --write-scope <paths> [--task-id T-xxx] [--json]
  python3 .standards/gate-enforce.py pre-spawn  --task-id <id> --sub-id <id> --agent <id> --model <m> --type <t> --write-scope <paths> [--json]
  python3 .standards/gate-enforce.py pre-write  --file <path> [--task-id T-xxx] [--write-scope <paths>] [--json]
  python3 .standards/gate-enforce.py pre-end    --task-id <id> --agent <id> [--json]

退出码:
  0 = 全部通过（P1-P3 advisory 输出到 stderr）
  1 = P0 违规 — 硬阻断
  2 = 参数错误

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/gate-enforce.py pre-write --file '10-项目/test.md' --write-scope '10-项目/' --json"
"""
from __future__ import annotations

import sys
import os
import json
import re
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "$VAULT_ROOT"))
STANDARDS_DIR = VAULT_ROOT / ".standards"
EVENT_FILE = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"
AGENT_STATUS_DIR = VAULT_ROOT / "02-项目管理" / "智能体状态"
TASKS_DIR = VAULT_ROOT / "02-项目管理" / "任务卡"

TZ = timezone(timedelta(hours=8))

# 禁止写入路径（除非 M4+ 显式声明在 write_scope 中）
FORBIDDEN_PATHS = ["00-MOC/", "30-规范/", "40-决策/", ".standards/"]

# Agent ID -> 中文名映射
AGENT_STATUS_FILES = {
    "claudian": "Claudian.md",
    "dongfeng": "Claudian.md",  # backward compat alias
    "xiaochong": "阿莫西林.md",
    "toubao": "头孢.md",
    "workbuddy": "WorkBuddy.md",
    "qingmeisu": "青霉素.md",
    "hongmeisu": "红霉素.md",
}


@dataclass
class GateResult:
    """门禁检查结果"""
    priority: int       # 0=P0 硬阻断, 1-3=advisory
    rule_id: str        # e.g. "AGENT_BUSY", "FUSE_BLOWN"
    message: str        # 人类可读描述
    source: str         # 来源工具 (e.g. "cost-fuse.py", "internal")
    details: dict = None

    def to_dict(self) -> dict:
        d = {"priority": self.priority, "rule_id": self.rule_id,
             "message": self.message, "source": self.source}
        if self.details:
            d["details"] = self.details
        return d


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def _run_tool(script: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """调用 .standards/ 下的工具脚本"""
    script_path = STANDARDS_DIR / script
    if not script_path.exists():
        return 98, "", f"工具不存在: {script}"
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)] + args,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(VAULT_ROOT)
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 99, "", f"工具超时: {script}"
    except OSError as e:
        return 98, "", f"工具调用失败: {script}: {e}"


def _read_agent_status(agent: str) -> str | None:
    """读取 Agent 状态文件中的 status 字段"""
    filename = AGENT_STATUS_FILES.get(agent)
    if not filename:
        return None
    status_file = AGENT_STATUS_DIR / filename
    if not status_file.exists():
        return None
    try:
        content = status_file.read_text(encoding="utf-8")
        m = re.search(r"^status:\s*(.+)$", content, re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    except IOError:
        pass
    return None


def _find_task_card(task_id: str) -> Path | None:
    """按优先级查找任务卡文件

    查找顺序:
      1. _temp/{task_id}/task-card.yaml (V8.5 标准位置, handshake 创建)
      2. 02-项目管理/任务卡/ 下的 {task_id}.md (legacy 兼容)
    """
    # V8.5 标准位置
    v85_card = VAULT_ROOT / "_temp" / task_id / "task-card.yaml"
    if v85_card.exists():
        return v85_card
    # Legacy 兼容
    if TASKS_DIR.exists():
        for path in TASKS_DIR.rglob(f"{task_id}.md"):
            if path.is_file():
                return path
    return None


def _read_task_card_field(task_id: str, field: str) -> str | None:
    """从 task card 读取字段值"""
    card_path = _find_task_card(task_id)
    if not card_path:
        return None
    try:
        content = card_path.read_text(encoding="utf-8")
        m = re.search(rf"^{field}\s*:\s*(.+)$", content, re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"').strip("'")
        # 也检查缩进字段
        m = re.search(rf"^\s+{field}\s*:\s*(.+)$", content, re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    except IOError:
        pass
    return None


def _read_task_card_list(task_id: str, field: str) -> list[str]:
    """从 task card 读取列表字段"""
    card_path = _find_task_card(task_id)
    if not card_path:
        return []
    try:
        content = card_path.read_text(encoding="utf-8")
        pattern = rf"^{field}:\s*\n((?:\s+-\s+.+\n?)*)"
        m = re.search(pattern, content, re.MULTILINE)
        if m:
            items = re.findall(r"^\s+-\s+(.+)$", m.group(1), re.MULTILINE)
            return [i.strip().strip('"').strip("'") for i in items]
        # 空列表形式: field: []
        m = re.search(rf"^{field}:\s*\[\]", content, re.MULTILINE)
        if m:
            return []
    except IOError:
        pass
    return []


def _path_in_scope(file_path: str, write_scope: str) -> bool:
    """检查文件路径是否在 write_scope 允许范围内
    
    scope 支持两种形式:
      - 目录: "10-项目/" → 匹配该目录下所有文件
      - 精确文件: ".standards/gate-enforce.py" → 仅匹配该文件
    """
    if not write_scope or not write_scope.strip():
        return True  # 无 scope 限制 → 允许
    scopes = [s.strip() for s in write_scope.split(",") if s.strip()]
    if not scopes:
        return True
    # 规范化路径: 去掉开头的 ./ 但保留 .xxx/ 形式的隐藏目录
    def normalize(p: str) -> str:
        if p.startswith("./"):
            p = p[2:]
        return p.lstrip("/")
    
    norm_file = normalize(file_path)
    for scope in scopes:
        norm_scope = normalize(scope)
        # 目录匹配（以 / 结尾）
        if norm_scope.endswith("/"):
            if norm_file.startswith(norm_scope):
                return True
        else:
            # 精确文件匹配
            if norm_file == norm_scope:
                return True
            # 也支持 scope 作为前缀目录（无尾 / 但确实是目录场景）
            if norm_file.startswith(norm_scope + "/"):
                return True
    return False


def _path_is_forbidden(file_path: str, write_scope: str) -> bool:
    """检查是否写入禁止目录（M4+ 显式声明在 write_scope 中则放行）"""
    norm_file = file_path.lstrip("/").lstrip("./")
    for forbidden in FORBIDDEN_PATHS:
        if norm_file.startswith(forbidden):
            # 检查 write_scope 是否显式包含此路径
            if write_scope:
                scopes = [s.strip().lstrip("/").lstrip("./") for s in write_scope.split(",")]
                for scope in scopes:
                    if norm_file.startswith(scope) or forbidden.startswith(scope):
                        return False  # 显式声明，放行
            return True  # 未声明，禁止
    return False


def log_gate_event(gate: str, task_id: str | None, agent: str | None,
                   results: list[GateResult]):
    """追加 gate_check 事件到事件流（非阻塞）"""
    try:
        p0_list = [r.to_dict() for r in results if r.priority == 0]
        advisory_list = [r.to_dict() for r in results if r.priority > 0]
        event = {
            "ts": now_iso(),
            "event": "gate_check",
            "gate": gate,
            "task_id": task_id or "",
            "agent": agent or "",
            "result": "block" if p0_list else "pass",
            "p0_count": len(p0_list),
            "advisory_count": len(advisory_list),
            "violations": p0_list[:5],  # 最多记 5 条
        }
        EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (IOError, OSError):
        pass  # 日志写入失败不影响门禁结果


# === 子命令实现 ===

def cmd_pre_start(args) -> list[GateResult]:
    """pre-start 门禁：任务激活前检查"""
    results = []

    # P0: Agent 是否已 busy
    agent_status = _read_agent_status(args.agent)
    if agent_status and agent_status == "busy":
        results.append(GateResult(
            priority=0, rule_id="AGENT_BUSY",
            message=f"Agent {args.agent} 状态为 busy，不可重复激活",
            source="internal"
        ))

    # P0: 如果提供了 task_id，检查 task card
    if args.task_id:
        # blocked_by 非空
        blocked = _read_task_card_list(args.task_id, "blocked_by")
        if blocked:
            results.append(GateResult(
                priority=0, rule_id="TASK_BLOCKED",
                message=f"任务 {args.task_id} 被阻塞: {blocked}",
                source="internal",
                details={"blocked_by": blocked}
            ))

        # status 为 done/cancelled
        card_status = _read_task_card_field(args.task_id, "status")
        if card_status in ("done", "cancelled", "completed"):
            results.append(GateResult(
                priority=0, rule_id="TASK_CANCELLED",
                message=f"任务 {args.task_id} 状态为 {card_status}，不可激活",
                source="internal"
            ))

    # P1: cost-fuse 预警（如果有 task_id）
    if args.task_id:
        exit_code, stdout, _ = _run_tool("cost-fuse.py", [args.task_id, "--json"])
        if exit_code == 0 and stdout:
            try:
                fuse_data = json.loads(stdout)
                if fuse_data.get("pct", 0) >= 60:
                    results.append(GateResult(
                        priority=1, rule_id="BUDGET_WARNING",
                        message=f"预算已用 {fuse_data['pct']:.0f}% ({fuse_data['cost_cny']:.2f}/{fuse_data['ceiling_cny']:.2f} CNY)",
                        source="cost-fuse.py",
                        details=fuse_data
                    ))
            except json.JSONDecodeError:
                pass

    return results


def cmd_pre_spawn(args) -> list[GateResult]:
    """pre-spawn 门禁：子任务创建前检查"""
    results = []

    # P0: task_id 为空
    if not args.task_id or not args.task_id.strip():
        results.append(GateResult(
            priority=0, rule_id="NO_TASK_ID",
            message="task_id 为空，禁止创建幽灵子任务",
            source="internal"
        ))
        return results  # 无 task_id 则后续检查无意义

    # P0: cost-fuse 熔断
    exit_code, stdout, _ = _run_tool("cost-fuse.py", [args.task_id, "--json"])
    if exit_code == 1:
        # fuse blown
        fuse_msg = "成本已达上限，熔断"
        try:
            fuse_data = json.loads(stdout)
            fuse_msg = f"成本熔断: {fuse_data.get('cost_cny', '?')}/{fuse_data.get('ceiling_cny', '?')} CNY ({fuse_data.get('pct', '?')}%)"
        except json.JSONDecodeError:
            fuse_data = {}
        results.append(GateResult(
            priority=0, rule_id="FUSE_BLOWN",
            message=fuse_msg,
            source="cost-fuse.py",
            details=fuse_data if 'fuse_data' in dir() else {}
        ))

    # P0: write_scope 越权检查（子任务 scope 必须在允许范围内）
    # 这里检查子任务 scope 是否包含禁止路径
    if args.write_scope:
        child_scopes = [s.strip() for s in args.write_scope.split(",") if s.strip()]
        for scope in child_scopes:
            norm_scope = scope.lstrip("/").lstrip("./")
            for forbidden in FORBIDDEN_PATHS:
                if norm_scope.startswith(forbidden):
                    results.append(GateResult(
                        priority=0, rule_id="SCOPE_VIOLATION",
                        message=f"子任务 write_scope '{scope}' 包含禁止路径 '{forbidden}'",
                        source="internal"
                    ))
                    break

    # P1/P2: spawn-budget-check advisory
    exit_code, stdout, _ = _run_tool("spawn-budget-check.py", [
        "check", "--task-id", args.task_id, "--type", args.type,
        "--model", args.model, "--json"
    ])
    if exit_code == 1:
        # 黄灯：建议降级
        try:
            budget_data = json.loads(stdout)
            recommended = budget_data.get("model_recommended", "haiku")
            results.append(GateResult(
                priority=2, rule_id="MODEL_DOWNGRADE",
                message=f"预算紧张，建议降级到 {recommended}",
                source="spawn-budget-check.py",
                details=budget_data
            ))
        except json.JSONDecodeError:
            pass
    elif exit_code == 2 and not any(r.rule_id == "FUSE_BLOWN" for r in results):
        # 红灯（如果不是已经有 FUSE_BLOWN）
        try:
            budget_data = json.loads(stdout)
            results.append(GateResult(
                priority=0, rule_id="BUDGET_EXHAUSTED",
                message=f"所有模型均超预算: {budget_data.get('reason', '')}",
                source="spawn-budget-check.py",
                details=budget_data
            ))
        except json.JSONDecodeError:
            results.append(GateResult(
                priority=0, rule_id="BUDGET_EXHAUSTED",
                message="所有模型均超预算",
                source="spawn-budget-check.py"
            ))

    return results


def cmd_pre_write(args) -> list[GateResult]:
    """pre-write 门禁：文件写入前检查"""
    results = []
    file_path = args.file

    # P1: V9 声明缺失检测 — 无 task_id 且路径非豁免
    V9_EXEMPT_PATHS = ["02-项目管理/运行日志/", "02-项目管理/智能体状态/"]
    if not args.task_id:
        norm_file = file_path.lstrip("/")
        if norm_file.startswith("./"):
            norm_file = norm_file[2:]
        is_exempt = any(norm_file.startswith(ep) for ep in V9_EXEMPT_PATHS)
        if not is_exempt:
            results.append(GateResult(
                priority=1, rule_id="WRITE_WITHOUT_V9_PREFIX",
                message=f"写入 '{file_path}' 时未提供 task_id — 可能缺少 V9 写入声明",
                source="internal"
            ))

    # P0: 路径越权（write_scope 外）
    if args.write_scope and not _path_in_scope(file_path, args.write_scope):
        results.append(GateResult(
            priority=0, rule_id="SCOPE_EXCEEDED",
            message=f"路径 '{file_path}' 不在 write_scope '{args.write_scope}' 范围内",
            source="internal"
        ))

    # P0: 禁止路径
    if _path_is_forbidden(file_path, args.write_scope or ""):
        results.append(GateResult(
            priority=0, rule_id="PATH_FORBIDDEN",
            message=f"路径 '{file_path}' 在禁止目录中（需在 write_scope 显式声明）",
            source="internal"
        ))

    # P1-P3: 调用 pre-write-check.py（仅当文件已存在时）
    full_path = VAULT_ROOT / file_path
    if full_path.exists():
        check_args = [file_path, "--json"]
        if args.task_id:
            check_args += ["--task-id", args.task_id]
        exit_code, stdout, _ = _run_tool("pre-write-check.py", check_args)
        if exit_code == 1 and stdout:
            try:
                check_data = json.loads(stdout)
                violations = check_data.get("violations", [])
                for v in violations:
                    # 映射 pre-write-check 违规到优先级
                    if isinstance(v, str):
                        # 旧格式：纯字符串违规
                        results.append(GateResult(
                            priority=1, rule_id="PRE_WRITE_VIOLATION",
                            message=v, source="pre-write-check.py"
                        ))
                        continue
                    vtype = v.get("type", "")
                    if vtype == "path":
                        # 已经在上面检查过，跳过重复
                        continue
                    elif vtype == "frontmatter":
                        results.append(GateResult(
                            priority=1, rule_id="FRONTMATTER_MISSING",
                            message=v.get("message", "Frontmatter 不完整"),
                            source="pre-write-check.py"
                        ))
                    elif vtype == "emoji":
                        results.append(GateResult(
                            priority=2, rule_id="EMOJI_DETECTED",
                            message=v.get("message", "检测到装饰性 emoji"),
                            source="pre-write-check.py"
                        ))
                    elif vtype == "brand":
                        results.append(GateResult(
                            priority=3, rule_id="BRAND_COLOR",
                            message=v.get("message", "品牌色不合规"),
                            source="pre-write-check.py"
                        ))
            except json.JSONDecodeError:
                pass

    return results


def cmd_pre_end(args) -> list[GateResult]:
    """pre-end 门禁：任务关闭前检查"""
    results = []

    # P0: Agent 已经是 idle（重复关闭）
    agent_status = _read_agent_status(args.agent)
    if agent_status and agent_status == "idle":
        results.append(GateResult(
            priority=0, rule_id="ALREADY_IDLE",
            message=f"Agent {args.agent} 已为 idle，不可重复关闭",
            source="internal"
        ))

    # P1: 未收集子任务
    if args.task_id:
        exit_code, stdout, _ = _run_tool("subtask-query.py", [
            "--task", args.task_id, "--json"
        ])
        if exit_code == 0 and stdout:
            try:
                query_data = json.loads(stdout)
                records = query_data.get("records", [])
                active = [r for r in records if r.get("state") not in
                          ("COLLECTED", "DESTROYED")]
                if active:
                    results.append(GateResult(
                        priority=1, rule_id="UNCOLLECTED_SUBTASKS",
                        message=f"{len(active)} 个子任务未收集: {[r['sub_id'] for r in active[:3]]}",
                        source="subtask-query.py",
                        details={"count": len(active), "sub_ids": [r["sub_id"] for r in active]}
                    ))
            except json.JSONDecodeError:
                pass

    # P0/P1/P2: 收工完成度验证（v8-closeout-check.py）
    # Fix 5: MISSING_DELIVERABLES / KANBAN_NOT_UPDATED / NO_RUN_LOG 升级为 P0 硬阻断
    ESCALATE_TO_P0 = {"MISSING_DELIVERABLES", "KANBAN_NOT_UPDATED", "NO_RUN_LOG"}

    if args.task_id:
        gear = getattr(args, 'gear', 'M4') or 'M4'
        exit_code, stdout, _ = _run_tool("v8-closeout-check.py", [
            "--task-id", args.task_id,
            "--agent", args.agent,
            "--gear", gear,
            "--json"
        ])
        if exit_code in (0, 1) and stdout:
            try:
                check_data = json.loads(stdout)
                for issue in check_data.get("issues", []):
                    rule_id = issue.get("rule_id", "CLOSEOUT_FAIL")
                    # 关键收工条件升级为 P0 硬阻断
                    effective_priority = 0 if rule_id in ESCALATE_TO_P0 else issue.get("priority", 1)
                    results.append(GateResult(
                        priority=effective_priority,
                        rule_id=rule_id,
                        message=issue.get("message", "收工检查未通过"),
                        source="v8-closeout-check.py",
                        details=issue.get("details")
                    ))
                for adv in check_data.get("advisories", []):
                    rule_id = adv.get("rule_id", "CLOSEOUT_ADVISORY")
                    effective_priority = 0 if rule_id in ESCALATE_TO_P0 else adv.get("priority", 2)
                    results.append(GateResult(
                        priority=effective_priority,
                        rule_id=rule_id,
                        message=adv.get("message", "收工建议"),
                        source="v8-closeout-check.py",
                        details=adv.get("details")
                    ))
            except json.JSONDecodeError:
                pass

    return results


# === 主函数 ===

def main():
    parser = argparse.ArgumentParser(
        description="V8.5 Phase 4 统一门禁入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
退出码:
  0 = 全部通过（P1-P3 advisory 输出到 stderr）
  1 = P0 违规 — 硬阻断
  2 = 参数错误
"""
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pre-start
    p_start = subparsers.add_parser("pre-start", help="任务激活前门禁")
    p_start.add_argument("--agent", required=True, help="Agent ID")
    p_start.add_argument("--gear", required=True, choices=["M4", "M5"], help="档位")
    p_start.add_argument("--write-scope", required=True, help="声明的写入范围")
    p_start.add_argument("--task-id", help="任务 ID（如有 task card）")
    p_start.add_argument("--json", action="store_true", default=True)

    # pre-spawn
    p_spawn = subparsers.add_parser("pre-spawn", help="子任务创建前门禁")
    p_spawn.add_argument("--task-id", required=True, help="父任务 ID")
    p_spawn.add_argument("--sub-id", required=True, help="子任务 ID")
    p_spawn.add_argument("--agent", required=True, help="父 Agent ID")
    p_spawn.add_argument("--model", required=True, help="请求模型")
    p_spawn.add_argument("--type", required=True, help="任务类型")
    p_spawn.add_argument("--write-scope", required=True, help="子任务写入范围")
    p_spawn.add_argument("--json", action="store_true", default=True)

    # pre-write
    p_write = subparsers.add_parser("pre-write", help="文件写入前门禁")
    p_write.add_argument("--file", required=True, help="目标文件路径（相对 vault root）")
    p_write.add_argument("--task-id", help="当前任务 ID")
    p_write.add_argument("--write-scope", help="声明的写入范围")
    p_write.add_argument("--json", action="store_true", default=True)

    # pre-end
    p_end = subparsers.add_parser("pre-end", help="任务关闭前门禁")
    p_end.add_argument("--task-id", required=True, help="任务 ID")
    p_end.add_argument("--agent", required=True, help="Agent ID")
    p_end.add_argument("--gear", choices=["M4", "M5"], default="M4", help="档位（影响收工检查严格度）")
    p_end.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args()

    # 路由到子命令
    handlers = {
        "pre-start": cmd_pre_start,
        "pre-spawn": cmd_pre_spawn,
        "pre-write": cmd_pre_write,
        "pre-end": cmd_pre_end,
    }

    results = handlers[args.command](args)

    # 分类
    p0_violations = [r for r in results if r.priority == 0]
    advisories = [r for r in results if r.priority > 0]

    # 日志事件
    task_id = getattr(args, "task_id", None)
    agent = getattr(args, "agent", None)
    log_gate_event(args.command, task_id, agent, results)

    # 输出
    if args.json:
        output = {
            "gate": args.command,
            "passed": len(p0_violations) == 0,
            "p0_count": len(p0_violations),
            "advisory_count": len(advisories),
            "violations": [r.to_dict() for r in p0_violations],
            "advisories": [r.to_dict() for r in advisories],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if p0_violations:
            for r in p0_violations:
                print(f"[P0-BLOCK] {r.rule_id}: {r.message}", file=sys.stderr)
        if advisories:
            for r in advisories:
                print(f"[P{r.priority}] {r.rule_id}: {r.message}", file=sys.stderr)
        if not results:
            print("[GATE] 全部通过", file=sys.stderr)

    # 退出码
    sys.exit(1 if p0_violations else 0)


if __name__ == "__main__":
    main()
