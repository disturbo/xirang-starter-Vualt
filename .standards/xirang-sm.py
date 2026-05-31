#!/usr/bin/env python3
"""
xirang-sm.py — Xi Rang V8 Subagent 状态机库
v1.0 · 2026-05-17 | 息壤 V8.5.0

将 V8 子Agent生命周期的 10 态状态机编码为可执行 Python 库。
来源：息壤V8-子Agent生命周期.md §1~§6

功能：
  1. 状态定义 + 合法转移表
  2. SubagentSM 类：创建实例、推进状态、校验转移合法性
  3. 事件自动追加到 智能体事件.jsonl
  4. heartbeat 文件管理
  5. 父 Agent 追踪表管理（status.json）
  6. CLI 模式：查询/推进/校验

用法（库模式）：
  from xirang_sm import SubagentSM, TaskTracker
  sm = SubagentSM(sub_id="sub-01", parent="qingmeisu", task_id="T-20260517-01")
  sm.transition("RUNNING")
  sm.heartbeat(progress="正在生成第2页", pct=0.4)
  sm.transition("SUCCESS", result_path="10-项目/.../page.html")

用法（CLI 模式）：
  python3 .standards/xirang-sm.py --status                    # 查看所有活跃 sub
  python3 .standards/xirang-sm.py --advance sub-01 RUNNING    # 推进状态
  python3 .standards/xirang-sm.py --validate sub-01 SUCCESS   # 校验是否可转移
  python3 .standards/xirang-sm.py --graph                     # 输出状态转移图（Mermaid）
  python3 .standards/xirang-sm.py --check-timeout             # 检查超时的 sub
"""

import sys
import os
import json
import time
import datetime
from pathlib import Path
from typing import Optional

# === 状态定义（对齐 V8 子Agent生命周期 §1） ===

STATES = {
    "CREATED":          {"label": "已spawn",   "terminal": False, "desc": "已spawn，等待开始执行"},
    "RUNNING":          {"label": "执行中",    "terminal": False, "desc": "正在执行中"},
    "PARTIAL":          {"label": "中途汇报",  "terminal": False, "desc": "Agent Pool中途汇报（RUNNING子模式）"},
    "SUCCESS":          {"label": "执行成功",  "terminal": False, "desc": "执行成功，产出结果"},
    "FAILED":           {"label": "执行出错",  "terminal": False, "desc": "执行出错（网络/超限/权限等）"},
    "TIMEOUT":          {"label": "超时",      "terminal": False, "desc": "超过限定时间未完成"},
    "RETRYING":         {"label": "重试中",    "terminal": False, "desc": "父Agent正在重试（<=2次）"},
    "RETRY_EXHAUSTED":  {"label": "重试耗尽",  "terminal": False, "desc": "重试2次仍失败"},
    "RECLAIMED":        {"label": "回收自做",  "terminal": False, "desc": "父Agent放弃spawn，收回任务自己做"},
    "ESCALATED":        {"label": "升级不死鸟","terminal": False, "desc": "升级到不死鸟Phoenix（L1错误升级链）"},
    "COLLECTED":        {"label": "已回收",    "terminal": False, "desc": "父Agent已验证结果并合稿"},
    "DESTROYED":        {"label": "已销毁",    "terminal": True,  "desc": "子Agent进程/上下文已释放（终态）"},
}

# === 合法状态转移表（对齐 V8 子Agent生命周期 §1 状态定义表） ===

TRANSITIONS = {
    "CREATED":         ["RUNNING", "DESTROYED"],
    "RUNNING":         ["SUCCESS", "FAILED", "TIMEOUT", "PARTIAL"],
    "PARTIAL":         ["RUNNING"],  # 父 send_msg 后继续
    "SUCCESS":         ["COLLECTED"],
    "FAILED":          ["RETRYING", "RECLAIMED"],
    "TIMEOUT":         ["RETRYING", "RECLAIMED"],
    "RETRYING":        ["SUCCESS", "RETRY_EXHAUSTED"],
    "RETRY_EXHAUSTED": ["RECLAIMED", "ESCALATED"],
    "RECLAIMED":       ["COLLECTED"],
    "ESCALATED":       ["COLLECTED"],  # 不死鸟处理后也需要 COLLECTED
    "COLLECTED":       ["DESTROYED"],
    "DESTROYED":       [],  # 终态，不可转移
}

# === 错误类型枚举（对齐 §2.3 重试决策矩阵） ===

ERROR_TYPES = {
    "E-NETWORK":    {"strategy": "直接重试",     "desc": "网络错误"},
    "E-CONTEXT":    {"strategy": "拆更细",       "desc": "上下文超限"},
    "E-MODEL":      {"strategy": "换强模型",     "desc": "模型能力不足"},
    "E-PERMISSION": {"strategy": "回收自做",     "desc": "权限不足"},
    "E-TIMEOUT":    {"strategy": "拆更细/延时",  "desc": "超时"},
    "E-FORMAT":     {"strategy": "直接重试",     "desc": "格式错误"},
    "E-BRAND":      {"strategy": "回收父Agent修","desc": "品牌违规"},
    "E-CONFLICT":   {"strategy": "换路径重试",   "desc": "路径冲突"},
}

# === 超时量化表（对齐 §6.1） ===

TIMEOUT_TABLE = {
    "readonly":     {"default": 60,   "max": 120,  "heartbeat_interval": None, "desc": "只读采集"},
    "lightweight":  {"default": 120,  "max": 180,  "heartbeat_interval": None, "desc": "轻量生成"},
    "single_page":  {"default": 300,  "max": 450,  "heartbeat_interval": 60,   "desc": "单页HTML生成"},
    "batch":        {"default": 300,  "max": 600,  "heartbeat_interval": 60,   "desc": "多文件批量"},
    "complex_prd":  {"default": 600,  "max": 900,  "heartbeat_interval": 120,  "desc": "复杂PRD段落"},
    "web_fetch":    {"default": 300,  "max": 600,  "heartbeat_interval": 60,   "desc": "联网搜集"},
    "agent_pool":   {"default": 300,  "max": 600,  "heartbeat_interval": None, "desc": "Agent Pool多轮（每轮）"},
}

# === Vault 路径常量 ===

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).parent.parent))
TEMP_DIR = VAULT_ROOT / "_temp"
EVENTS_FILE = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"


def now_iso() -> str:
    """当前时间 ISO 格式"""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def append_event(event: dict):
    """追加事件到 智能体事件.jsonl（原子行写入）"""
    event.setdefault("ts", now_iso())
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[WARN] 无法写入事件流: {e}", file=sys.stderr)


class SubagentSM:
    """
    单个子 Agent 的状态机实例。

    用法：
        sm = SubagentSM(sub_id="sub-01", parent="qingmeisu", task_id="T-20260517-01")
        sm.transition("RUNNING")
        sm.heartbeat(progress="生成中", pct=0.3)
        sm.transition("SUCCESS", result_path="path/to/output.html")
    """

    def __init__(
        self,
        sub_id: str,
        parent: str,
        task_id: str,
        model: str = "sonnet",
        task_type: str = "single_page",
        timeout_override: Optional[int] = None,
    ):
        self.sub_id = sub_id
        self.parent = parent
        self.task_id = task_id
        self.model = model
        self.task_type = task_type
        self.state = "CREATED"
        self.retries = 0
        self.max_retries = 2
        self.created_at = now_iso()
        self.last_transition_at = self.created_at
        self.elapsed_sec = 0
        self.token_used = 0
        self.result_path: Optional[str] = None
        self.error_code: Optional[str] = None
        self.error_detail: Optional[str] = None

        # 超时配置
        timeout_cfg = TIMEOUT_TABLE.get(task_type, TIMEOUT_TABLE["single_page"])
        self.timeout_sec = timeout_override or timeout_cfg["default"]
        self.timeout_max = timeout_cfg["max"]
        self.heartbeat_interval = timeout_cfg["heartbeat_interval"]
        self.last_heartbeat_at: Optional[str] = None

        # 记录事件
        append_event({
            "type": "sub_spawn",
            "agent": parent,
            "sub_id": sub_id,
            "task_id": task_id,
            "model": model,
            "task_type": task_type,
            "timeout_sec": self.timeout_sec,
        })

    def can_transition(self, target: str) -> bool:
        """检查是否可以转移到目标状态"""
        if target not in STATES:
            return False
        return target in TRANSITIONS.get(self.state, [])

    def transition(
        self,
        target: str,
        result_path: Optional[str] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        token_used: Optional[int] = None,
    ) -> dict:
        """
        推进状态。返回转移结果。
        如果不合法，抛出 ValueError。
        """
        if not self.can_transition(target):
            valid = TRANSITIONS.get(self.state, [])
            raise ValueError(
                f"非法状态转移: {self.state} -> {target}。"
                f"当前态 [{self.state}] 允许转为: {valid}"
            )

        old_state = self.state
        self.state = target
        self.last_transition_at = now_iso()

        # 更新字段
        if result_path:
            self.result_path = result_path
        if error_code:
            self.error_code = error_code
            self.error_detail = error_detail
        if token_used:
            self.token_used += token_used

        # 重试计数
        if target == "RETRYING":
            self.retries += 1

        # 记录事件
        event = {
            "type": "sub_transition",
            "agent": self.parent,
            "sub_id": self.sub_id,
            "task_id": self.task_id,
            "from": old_state,
            "to": target,
        }
        if error_code:
            event["error_code"] = error_code
        if result_path:
            event["result_path"] = result_path
        append_event(event)

        return {
            "sub_id": self.sub_id,
            "from": old_state,
            "to": target,
            "at": self.last_transition_at,
            "valid": True,
        }

    def heartbeat(self, progress: str, pct: Optional[float] = None):
        """写心跳文件到 _temp/{task-id}/heartbeat.json"""
        self.last_heartbeat_at = now_iso()
        hb_data = {
            "sub_id": self.sub_id,
            "parent_id": self.parent,
            "last_heartbeat": self.last_heartbeat_at,
            "progress": progress,
            "tokens_used_so_far": self.token_used,
        }
        if pct is not None:
            hb_data["pct"] = pct

        hb_dir = TEMP_DIR / self.task_id
        hb_dir.mkdir(parents=True, exist_ok=True)
        hb_file = hb_dir / "heartbeat.json"
        with open(hb_file, "w", encoding="utf-8") as f:
            json.dump(hb_data, f, ensure_ascii=False, indent=2)

    def is_timeout(self) -> bool:
        """检查是否超时（基于创建时间）"""
        if self.state != "RUNNING":
            return False
        created = datetime.datetime.fromisoformat(self.created_at.replace("+08:00", "+08:00"))
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        elapsed = (now - created).total_seconds()
        return elapsed > self.timeout_sec

    def is_heartbeat_timeout(self) -> bool:
        """检查心跳是否超时（间隔x3无心跳=判死）"""
        if self.state != "RUNNING" or not self.heartbeat_interval:
            return False
        if not self.last_heartbeat_at:
            # 从未心跳：用 created_at 作为起点
            ref_time = self.created_at
        else:
            ref_time = self.last_heartbeat_at

        ref_dt = datetime.datetime.fromisoformat(ref_time.replace("+08:00", "+08:00"))
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        elapsed = (now - ref_dt).total_seconds()
        return elapsed > self.heartbeat_interval * 3

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "sub_id": self.sub_id,
            "parent": self.parent,
            "task_id": self.task_id,
            "state": self.state,
            "model": self.model,
            "task_type": self.task_type,
            "retries": self.retries,
            "timeout_sec": self.timeout_sec,
            "created_at": self.created_at,
            "last_transition_at": self.last_transition_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "elapsed_sec": self.elapsed_sec,
            "token_used": self.token_used,
            "result_path": self.result_path,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }

    def __repr__(self):
        return f"<SubagentSM {self.sub_id} state={self.state} parent={self.parent}>"


class TaskTracker:
    """
    父 Agent 追踪表管理器（对齐 §3 父Agent内部状态追踪）。

    管理一个 task 下的所有子 Agent 状态。
    持久化到 _temp/{task-id}/status.json。
    """

    def __init__(self, task_id: str, parent_agent: str, mode: str = "fan_out"):
        self.task_id = task_id
        self.parent_agent = parent_agent
        self.mode = mode
        self.created_at = now_iso()
        self.subs: dict[str, SubagentSM] = {}
        self._status_file = TEMP_DIR / task_id / "status.json"

    def spawn(
        self,
        sub_id: str,
        model: str = "sonnet",
        task_type: str = "single_page",
        timeout_override: Optional[int] = None,
    ) -> SubagentSM:
        """创建一个新的子 Agent 并加入追踪"""
        if len(self.active_subs()) >= 6:
            raise RuntimeError(
                f"并行上限已达 6 个活跃子Agent。"
                f"当前活跃: {[s.sub_id for s in self.active_subs()]}"
            )
        if sub_id in self.subs:
            raise ValueError(f"sub_id '{sub_id}' 已存在")

        sm = SubagentSM(
            sub_id=sub_id,
            parent=self.parent_agent,
            task_id=self.task_id,
            model=model,
            task_type=task_type,
            timeout_override=timeout_override,
        )
        self.subs[sub_id] = sm
        self._persist()
        return sm

    def active_subs(self) -> list:
        """返回所有非终态的子 Agent"""
        terminal_states = {"DESTROYED", "COLLECTED", "ESCALATED"}
        return [s for s in self.subs.values() if s.state not in terminal_states]

    def all_collected(self) -> bool:
        """是否所有 sub 都已 COLLECTED 或更高"""
        done_states = {"COLLECTED", "DESTROYED", "ESCALATED"}
        return all(s.state in done_states for s in self.subs.values())

    def check_timeouts(self) -> list:
        """检查所有超时的子 Agent"""
        timed_out = []
        for sm in self.subs.values():
            if sm.state == "RUNNING":
                if sm.is_timeout() or sm.is_heartbeat_timeout():
                    timed_out.append(sm)
        return timed_out

    def summary(self) -> dict:
        """汇总统计"""
        state_counts = {}
        for sm in self.subs.values():
            state_counts[sm.state] = state_counts.get(sm.state, 0) + 1
        total_tokens = sum(sm.token_used for sm in self.subs.values())
        return {
            "task_id": self.task_id,
            "parent": self.parent_agent,
            "mode": self.mode,
            "total_subs": len(self.subs),
            "active_subs": len(self.active_subs()),
            "state_counts": state_counts,
            "total_tokens": total_tokens,
            "all_collected": self.all_collected(),
        }

    def _persist(self):
        """持久化追踪表到 _temp/{task-id}/status.json"""
        self._status_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "task_id": self.task_id,
            "parent_agent": self.parent_agent,
            "mode": self.mode,
            "created_at": self.created_at,
            "subs": [sm.to_dict() for sm in self.subs.values()],
        }
        with open(self._status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, task_id: str) -> "TaskTracker":
        """从 status.json 恢复追踪表（崩溃恢复）"""
        status_file = TEMP_DIR / task_id / "status.json"
        if not status_file.exists():
            raise FileNotFoundError(f"追踪表不存在: {status_file}")

        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        tracker = cls(
            task_id=data["task_id"],
            parent_agent=data["parent_agent"],
            mode=data.get("mode", "fan_out"),
        )
        tracker.created_at = data["created_at"]

        for sub_data in data.get("subs", []):
            sm = SubagentSM(
                sub_id=sub_data["sub_id"],
                parent=sub_data["parent"],
                task_id=task_id,
                model=sub_data.get("model", "sonnet"),
                task_type=sub_data.get("task_type", "single_page"),
            )
            # 恢复状态字段
            sm.state = sub_data["state"]
            sm.retries = sub_data.get("retries", 0)
            sm.created_at = sub_data.get("created_at", tracker.created_at)
            sm.last_transition_at = sub_data.get("last_transition_at", sm.created_at)
            sm.last_heartbeat_at = sub_data.get("last_heartbeat_at")
            sm.elapsed_sec = sub_data.get("elapsed_sec", 0)
            sm.token_used = sub_data.get("token_used", 0)
            sm.result_path = sub_data.get("result_path")
            sm.error_code = sub_data.get("error_code")
            sm.error_detail = sub_data.get("error_detail")
            tracker.subs[sm.sub_id] = sm

        return tracker

    def destroy_all(self):
        """销毁所有已回收的子 Agent + 清理 _temp"""
        for sm in self.subs.values():
            if sm.state == "COLLECTED":
                sm.transition("DESTROYED")

        # 清理 _temp 目录
        task_dir = TEMP_DIR / self.task_id
        if task_dir.exists():
            import shutil
            shutil.rmtree(task_dir)

        # 最终事件
        append_event({
            "type": "task_cleanup",
            "agent": self.parent_agent,
            "task_id": self.task_id,
            "subs_destroyed": len(self.subs),
        })


def generate_mermaid() -> str:
    """生成状态转移图的 Mermaid 代码"""
    lines = ["stateDiagram-v2"]
    lines.append("    [*] --> CREATED : spawn")

    for src, targets in TRANSITIONS.items():
        for tgt in targets:
            label = ""
            if src == "CREATED" and tgt == "RUNNING":
                label = " : 开始执行"
            elif src == "RUNNING" and tgt == "SUCCESS":
                label = " : 完成"
            elif src == "RUNNING" and tgt == "FAILED":
                label = " : 出错"
            elif src == "RUNNING" and tgt == "TIMEOUT":
                label = " : 超时"
            elif src == "RUNNING" and tgt == "PARTIAL":
                label = " : 中途汇报"
            elif src == "PARTIAL" and tgt == "RUNNING":
                label = " : send_msg继续"
            elif src == "FAILED" and tgt == "RETRYING":
                label = " : L0.5重试"
            elif src == "TIMEOUT" and tgt == "RETRYING":
                label = " : L0.5重试"
            elif src == "RETRYING" and tgt == "SUCCESS":
                label = " : 重试成功"
            elif src == "RETRYING" and tgt == "RETRY_EXHAUSTED":
                label = " : 重试耗尽"
            elif src == "RETRY_EXHAUSTED" and tgt == "RECLAIMED":
                label = " : 回收自做"
            elif src == "RETRY_EXHAUSTED" and tgt == "ESCALATED":
                label = " : 升级不死鸟"
            elif tgt == "COLLECTED":
                label = " : 验证合稿"
            elif tgt == "DESTROYED":
                label = " : 释放"
            lines.append(f"    {src} --> {tgt}{label}")

    lines.append("    DESTROYED --> [*]")
    lines.append("")
    lines.append("    note right of CREATED : 父Agent spawn后进入")
    lines.append("    note right of RETRYING : 最多重试2次")
    lines.append("    note right of DESTROYED : 终态，上下文释放")

    return "\n".join(lines)


def print_transition_table():
    """打印完整的状态转移表"""
    print("息壤 V8 子Agent 状态转移表")
    print("=" * 70)
    print(f"{'当前状态':<18} {'可转为':<50} {'说明'}")
    print("-" * 70)
    for state, targets in TRANSITIONS.items():
        info = STATES[state]
        targets_str = ", ".join(targets) if targets else "(终态)"
        print(f"{state:<18} {targets_str:<50} {info['desc']}")


def cli_main():
    """CLI 入口"""
    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print("""xirang-sm.py -- 息壤 V8 子Agent 状态机库 (CLI)

用法:
  python3 .standards/xirang-sm.py --table           打印状态转移表
  python3 .standards/xirang-sm.py --graph           输出 Mermaid 状态图
  python3 .standards/xirang-sm.py --validate S1 S2  校验转移 S1->S2 是否合法
  python3 .standards/xirang-sm.py --errors          列出所有错误类型
  python3 .standards/xirang-sm.py --timeouts        列出超时量化表
  python3 .standards/xirang-sm.py --states          列出所有状态
  python3 .standards/xirang-sm.py --demo            运行一个完整生命周期 demo

库模式:
  from xirang_sm import SubagentSM, TaskTracker
  # 见文件头注释
""")
        return

    if "--table" in sys.argv:
        print_transition_table()
        return

    if "--graph" in sys.argv:
        print(generate_mermaid())
        return

    if "--states" in sys.argv:
        print("息壤 V8 子Agent 状态定义（10+2 态）")
        print("=" * 60)
        for i, (name, info) in enumerate(STATES.items(), 1):
            terminal = " [终态]" if info["terminal"] else ""
            print(f"  {i:>2}. {name:<18} {info['label']:<8} {info['desc']}{terminal}")
        return

    if "--errors" in sys.argv:
        print("息壤 V8 错误类型枚举（8 种）")
        print("=" * 60)
        for code, info in ERROR_TYPES.items():
            print(f"  {code:<14} {info['desc']:<12} 策略: {info['strategy']}")
        return

    if "--timeouts" in sys.argv:
        print("息壤 V8 超时量化表（§6.1）")
        print("=" * 70)
        print(f"{'类型':<14} {'默认(s)':<8} {'上限(s)':<8} {'心跳间隔':<10} {'说明'}")
        print("-" * 70)
        for name, cfg in TIMEOUT_TABLE.items():
            hb = f"{cfg['heartbeat_interval']}s" if cfg['heartbeat_interval'] else "不需要"
            print(f"{name:<14} {cfg['default']:<8} {cfg['max']:<8} {hb:<10} {cfg['desc']}")
        return

    if "--validate" in sys.argv:
        idx = sys.argv.index("--validate")
        if idx + 2 >= len(sys.argv):
            print("用法: --validate <当前态> <目标态>", file=sys.stderr)
            sys.exit(2)
        src = sys.argv[idx + 1].upper()
        tgt = sys.argv[idx + 2].upper()
        if src not in STATES:
            print(f"[ERROR] 未知状态: {src}", file=sys.stderr)
            sys.exit(1)
        if tgt not in STATES:
            print(f"[ERROR] 未知状态: {tgt}", file=sys.stderr)
            sys.exit(1)
        valid = tgt in TRANSITIONS.get(src, [])
        if valid:
            print(f"[OK] {src} -> {tgt} 合法")
        else:
            allowed = TRANSITIONS.get(src, [])
            print(f"[REJECT] {src} -> {tgt} 非法。{src} 允许转为: {allowed}")
            sys.exit(1)
        return

    if "--demo" in sys.argv:
        print("--- 息壤 SM Demo: Fan-Out 模式 3 子Agent ---\n")
        tracker = TaskTracker(
            task_id="DEMO-001",
            parent_agent="qingmeisu",
            mode="fan_out"
        )
        print(f"创建 TaskTracker: {tracker.task_id}, 模式={tracker.mode}")

        # Spawn 3 个子 Agent
        for i in range(1, 4):
            sm = tracker.spawn(f"sub-{i:02d}", model="sonnet", task_type="single_page")
            print(f"  Spawn: {sm.sub_id} -> {sm.state}")

        # 全部开始执行
        for sm in tracker.subs.values():
            sm.transition("RUNNING")
            print(f"  {sm.sub_id}: CREATED -> RUNNING")

        # sub-01 成功
        tracker.subs["sub-01"].transition("SUCCESS", result_path="output/page1.html", token_used=8000)
        print(f"  sub-01: RUNNING -> SUCCESS (8000 tokens)")

        # sub-02 失败 -> 重试 -> 成功
        tracker.subs["sub-02"].transition("FAILED", error_code="E-NETWORK")
        print(f"  sub-02: RUNNING -> FAILED (E-NETWORK)")
        tracker.subs["sub-02"].transition("RETRYING")
        print(f"  sub-02: FAILED -> RETRYING (第{tracker.subs['sub-02'].retries}次)")
        tracker.subs["sub-02"].transition("SUCCESS", result_path="output/page2.html", token_used=12000)
        print(f"  sub-02: RETRYING -> SUCCESS (12000 tokens)")

        # sub-03 超时 -> 回收
        tracker.subs["sub-03"].transition("TIMEOUT")
        print(f"  sub-03: RUNNING -> TIMEOUT")
        tracker.subs["sub-03"].transition("RECLAIMED")
        print(f"  sub-03: TIMEOUT -> RECLAIMED (父Agent自己做)")

        # 全部回收
        for sm in tracker.subs.values():
            if sm.state in ("SUCCESS", "RECLAIMED"):
                sm.transition("COLLECTED")
                print(f"  {sm.sub_id}: -> COLLECTED")

        # 汇总
        print(f"\n汇总: {json.dumps(tracker.summary(), ensure_ascii=False, indent=2)}")

        # 销毁
        print(f"\n检查: all_collected = {tracker.all_collected()}")
        print("--- Demo 完成 ---")
        return

    print(f"未知参数: {sys.argv[1:]}。用 --help 查看用法。", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    cli_main()
