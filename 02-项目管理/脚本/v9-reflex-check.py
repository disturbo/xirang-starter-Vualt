#!/usr/bin/env python3
"""
v9-reflex-check.py — V9 第一反射器 MVP 聚合器（子任务 2/3/4）

职责：汇集多个巡检源 → 归一到统一 severity schema → 去重+冷却 → 写 health-latest.json。

防污染硬约束（对标 6-18 反思 Hindsight `_converted` 污染教训）：
  本脚本【只写】02-项目管理/巡检/ 目录，绝不写看板 / 运行日志 / 模块文档。
  正式看板/日志的提升由会话启动 checklist 人工确认后进行，不由本脚本自动写。

信号源：
  1. project-ops-check.py --json   任务卡 + 运行日志巡检（子任务 1 已 JSON 化）
  2. agent-state-lint.py --json    Agent 状态文件 schema 校验
  3. 内置 heartbeat 检查            status=busy 但 last_heartbeat 超时（子任务 2）
  4. v9-policy-conflict-check.py    规范管辖权索引 + 冲突扫描（动作 C）

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(".")
SCRIPT_DIR = ROOT / "02-项目管理" / "脚本"
STATUS_DIR = ROOT / "02-项目管理" / "智能体状态"
INSPECT_DIR = ROOT / "02-项目管理" / "巡检"  # 唯一允许写入的输出目录
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
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_local().tzinfo)
    return dt


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
        m_status = re.search(r"^status:\s*(.+)$", text, re.MULTILINE)
        m_hb = re.search(r"^last_heartbeat:\s*(.+)$", text, re.MULTILINE)
        m_id = re.search(r"^agent_id:\s*(.+)$", text, re.MULTILINE)
        if not m_status:
            continue
        status = m_status.group(1).strip().strip('"').strip("'")
        agent_id = m_id.group(1).strip().strip('"').strip("'") if m_id else path.stem
        if status != "busy":
            continue  # 只关心 busy 卡死；idle/standby/retired 无需心跳告警
        hb = parse_iso(m_hb.group(1)) if m_hb else None
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


def build_report(findings: list[dict], today: date, now: datetime, cfg: dict, sources_run: list[dict]) -> dict:
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
    parser.add_argument("--quiet", action="store_true", help="只写文件，不打印（launchd 用）")
    parser.add_argument("--strict", action="store_true", help="有 active 发现时退出码 1")
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    now = now_local()
    cfg = {"stale_heartbeat_hours": args.stale_heartbeat_hours, "cooldown_hours": args.cooldown_hours}

    # 每个源显式记录"跑没跑/什么状态/几条"，使"跑了干净"可与"没跑/静默崩"区分（自省）。
    source_specs = [
        ("project-ops", lambda: collect_project_ops(today)),
        ("agent-state", collect_agent_state),
        ("heartbeat", lambda: collect_heartbeat(now, args.stale_heartbeat_hours)),
        ("policy-conflict", collect_policy_conflicts),
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

    # 排序：先按严重度，再按 source
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f.get("source", ""), f["rule_id"]))

    # 只写巡检目录（防污染硬约束）；read-modify-write 全程持锁、原子替换（Codex P1）
    with state_lock():
        state = read_state()
        new_state = apply_cooldown(findings, now, args.cooldown_hours, state)
        report = build_report(findings, today, now, cfg, sources_run)
        atomic_write_json(HEALTH_LATEST, report)
        atomic_write_json(REFLEX_STATE, new_state)

    if not args.quiet:
        s = report["summary"]
        print(f"# V9 第一反射器巡检 ({today})")
        print(f"快照: {HEALTH_LATEST}")
        srcs = " ".join(f"{r['source']}={r['status']}({r['findings']})" for r in report["sources_run"])
        flag = "" if not report["sources_failed"] else f"  ⚠ 异常源: {report['sources_failed']}"
        print(f"源: {srcs}{flag}")
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
