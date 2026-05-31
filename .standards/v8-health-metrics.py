#!/usr/bin/env python3
"""
v8-health-metrics.py -- V8 运行健康指标采集与报告生成
v1.0 | 2026-05-17 | 息壤 V8.5.0

数据源：
  - 02-项目管理/任务卡/T-*.md          task card（门禁/状态/成本/交付物）
  - 02-项目管理/pre-write-check.log.jsonl   pre-write-check 日志
  - 02-项目管理/智能体状态/智能体事件.jsonl  事件流
  - 50-经验/Agent协作方法论/V8-实战案例-*.md  retrospective

用法：
  python3 .standards/v8-health-metrics.py [--json] [--output <path>]

输出：
  无 --output 时打印到 stdout
  有 --output 时写入指定路径的 Markdown 报告
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter

# === 路径配置 ===
VAULT_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = VAULT_ROOT / "02-项目管理" / "tasks"
PWC_LOG = VAULT_ROOT / "02-项目管理" / "pre-write-check.log.jsonl"
EVENTS_LOG = VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl"
RETRO_DIR = VAULT_ROOT / "50-经验" / "Agent协作方法论"

TZ = timezone(timedelta(hours=8))


def parse_task_cards():
    """解析所有 V8.2 相关 task card"""
    cards = []
    for f in sorted(TASKS_DIR.glob("T-*.md")):
        content = f.read_text(encoding="utf-8")
        # 提取 frontmatter
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)

        card = {"file": f.name}

        # 基本字段（strip inline YAML comments）
        for key in ["task_id", "title", "status", "task_size", "module", "owner"]:
            m = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
            if m:
                val = m.group(1).strip()
                # 去除 YAML 行内注释（# 后面的内容）
                if "#" in val:
                    val = val.split("#")[0].strip()
                card[key] = val
            else:
                card[key] = None

        # 门禁
        gates = {}
        for gate in ["pre_start", "pre_write", "cost_fuse", "handoff"]:
            m = re.search(rf"^\s*{gate}:\s*(.+)$", fm, re.MULTILINE)
            gates[gate] = m.group(1).strip() if m else "pending"
        card["gates"] = gates

        # deliverables state
        states = re.findall(r"state:\s*(\w+)", fm)
        card["deliverable_states"] = states

        # 成本
        m = re.search(r"cost_ceiling_cny:\s*([\d.]+)", fm)
        card["budget_cny"] = float(m.group(1)) if m else 0
        m = re.search(r"cost_cny:\s*([\d.]+)", fm)
        card["actual_cny"] = float(m.group(1)) if m else 0

        # completed_at
        m = re.search(r"completed_at:\s*(.+)$", fm, re.MULTILINE)
        card["completed_at"] = m.group(1).strip() if m else None

        cards.append(card)

    return cards


def parse_pwc_log():
    """解析 pre-write-check 日志"""
    entries = []
    if not PWC_LOG.exists():
        return entries
    for line in PWC_LOG.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def parse_events():
    """解析 智能体事件.jsonl"""
    entries = []
    if not EVENTS_LOG.exists():
        return entries
    for line in EVENTS_LOG.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def parse_retrospectives():
    """解析 retrospective 文件"""
    retros = []
    for f in sorted(RETRO_DIR.glob("V8-实战案例-*-retrospective.md")):
        content = f.read_text(encoding="utf-8")
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        retro = {"file": f.name}
        for key in ["task_id", "module", "created"]:
            m = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
            retro[key] = m.group(1).strip() if m else None
        retros.append(retro)
    return retros


def compute_metrics(cards, pwc_log, events, retros):
    """计算健康指标"""
    metrics = {}

    # 筛选 V8.2 样本 + 演习 task cards（T-20260518-05 及以后）
    v8_cards = [c for c in cards if c.get("task_id", "").startswith("T-20260518-")
                and c.get("task_id", "") >= "T-20260518-05"]

    # 1. 门禁通过率
    total_gates = 0
    passed_gates = 0
    for c in v8_cards:
        for g, val in c["gates"].items():
            total_gates += 1
            if val.startswith("passed") or val.startswith("verified"):
                passed_gates += 1
    metrics["gate_pass_rate"] = round(passed_gates / total_gates * 100, 1) if total_gates > 0 else 0

    # 2. pre-write-check 首次通过率（从日志看，file 不含 "violation" 的 pass 记录）
    pwc_files = {}
    for entry in pwc_log:
        f = entry.get("file", "")
        if f not in pwc_files:
            pwc_files[f] = entry.get("status", "")
    first_pass = sum(1 for s in pwc_files.values() if s == "pass")
    total_pwc = len(pwc_files)
    metrics["pwc_first_pass_rate"] = round(first_pass / total_pwc * 100, 1) if total_pwc > 0 else 0

    # 3. 路径越权率
    path_violations = sum(1 for e in pwc_log if "path" in str(e.get("violation_types", [])))
    total_writes = len(pwc_log)
    metrics["path_violation_rate"] = round(path_violations / total_writes * 100, 1) if total_writes > 0 else 0

    # 4. Task 完成率
    done_cards = [c for c in v8_cards if c.get("status") == "done"]
    metrics["task_completion_rate"] = round(len(done_cards) / len(v8_cards) * 100, 1) if v8_cards else 0

    # 5. Deliverable verified 率
    all_states = []
    for c in v8_cards:
        all_states.extend(c.get("deliverable_states", []))
    verified = sum(1 for s in all_states if s == "verified")
    metrics["deliverable_verified_rate"] = round(verified / len(all_states) * 100, 1) if all_states else 0

    # 6. 成本利用率（平均）
    cost_utils = []
    for c in v8_cards:
        if c["budget_cny"] > 0:
            cost_utils.append(c["actual_cny"] / c["budget_cny"] * 100)
    metrics["avg_cost_utilization"] = round(sum(cost_utils) / len(cost_utils), 1) if cost_utils else 0

    # 7. Retrospective 数量
    metrics["retrospective_count"] = len(retros)
    metrics["retrospective_target"] = 5

    # 8. 事件流统计
    event_types = Counter(e.get("type", "unknown") for e in events)
    metrics["event_count"] = len(events)
    metrics["event_types"] = dict(event_types.most_common(10))

    # 9. V8.2 task card 数量
    metrics["v8_task_count"] = len(v8_cards)
    metrics["v8_done_count"] = len(done_cards)

    # 10. 故障演习统计
    drill_cards = [c for c in v8_cards if "故障演习" in (c.get("title") or "")]
    metrics["drill_count"] = len(drill_cards)
    metrics["drill_done"] = sum(1 for c in drill_cards if c.get("status") == "done")

    # 11. PRD 类型覆盖
    prd_cards = [c for c in v8_cards if "PRD" in (c.get("title") or "")]
    metrics["prd_sample_count"] = len(prd_cards)

    return metrics


def health_score(metrics):
    """计算综合健康分（0-100）"""
    score = 0
    weights = {
        "gate_pass_rate": 25,       # 25 分
        "pwc_first_pass_rate": 15,  # 15 分
        "path_violation_rate": 15,  # 15 分（0% = 满分）
        "task_completion_rate": 15, # 15 分
        "deliverable_verified_rate": 10, # 10 分
        "retrospective_pct": 10,    # 10 分
        "drill_pct": 10,            # 10 分
    }

    score += metrics["gate_pass_rate"] / 100 * weights["gate_pass_rate"]
    score += metrics["pwc_first_pass_rate"] / 100 * weights["pwc_first_pass_rate"]
    score += (100 - metrics["path_violation_rate"]) / 100 * weights["path_violation_rate"]
    score += metrics["task_completion_rate"] / 100 * weights["task_completion_rate"]
    score += metrics["deliverable_verified_rate"] / 100 * weights["deliverable_verified_rate"]

    retro_pct = min(metrics["retrospective_count"] / metrics["retrospective_target"], 1.0)
    score += retro_pct * weights["retrospective_pct"]

    drill_pct = min(metrics["drill_done"] / max(metrics["drill_count"], 1), 1.0)
    score += drill_pct * weights["drill_pct"]

    return round(score, 1)


def generate_report(metrics, score):
    """生成 Markdown 健康报告"""
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    report = f"""---
title: V8 运行健康报告
type: 健康报告
version: "1.0"
maturity: draft
created: {today}
generated: {now}
health_score: {score}
status: generated
tags: [V8, 健康指标, 熵报告]
---

# V8 运行健康报告

> 生成时间：{now} | 健康分：**{score}/100**

---

## 综合健康分

| 分数 | 等级 | 说明 |
|:---:|:---:|------|
| {score} | {"优秀" if score >= 90 else "良好" if score >= 75 else "及格" if score >= 60 else "需改进"} | {"全部指标达标" if score >= 90 else "主要指标达标，个别需改进" if score >= 75 else "部分指标未达标" if score >= 60 else "多项指标未达标"} |

## 核心指标

| 指标 | 数值 | 阈值 | 状态 |
|------|:---:|:---:|:---:|
| 门禁通过率 | {metrics['gate_pass_rate']}% | >= 90% | {"pass" if metrics['gate_pass_rate'] >= 90 else "warn" if metrics['gate_pass_rate'] >= 70 else "fail"} |
| pre-write-check 首次通过率 | {metrics['pwc_first_pass_rate']}% | >= 80% | {"pass" if metrics['pwc_first_pass_rate'] >= 80 else "warn" if metrics['pwc_first_pass_rate'] >= 60 else "fail"} |
| 路径越权率 | {metrics['path_violation_rate']}% | 0% | {"pass" if metrics['path_violation_rate'] == 0 else "fail"} |
| 任务完成率 | {metrics['task_completion_rate']}% | >= 95% | {"pass" if metrics['task_completion_rate'] >= 95 else "warn" if metrics['task_completion_rate'] >= 80 else "fail"} |
| 交付物 verified 率 | {metrics['deliverable_verified_rate']}% | >= 95% | {"pass" if metrics['deliverable_verified_rate'] >= 95 else "warn" if metrics['deliverable_verified_rate'] >= 80 else "fail"} |
| 成本利用率（均值） | {metrics['avg_cost_utilization']}% | <= 80% | {"pass" if metrics['avg_cost_utilization'] <= 80 else "warn" if metrics['avg_cost_utilization'] <= 100 else "fail"} |

## 样本量

| 指标 | 实际 | 目标 | 达成 |
|------|:---:|:---:|:---:|
| Retrospective | {metrics['retrospective_count']} | {metrics['retrospective_target']} | {"是" if metrics['retrospective_count'] >= metrics['retrospective_target'] else "否"} |
| 故障演习 | {metrics['drill_done']}/{metrics['drill_count']} | 5/5 | {"是" if metrics['drill_done'] >= 5 else "否"} |
| PRD 样本 | {metrics['prd_sample_count']} | 3+ | {"是" if metrics['prd_sample_count'] >= 3 else "否"} |
| V8 Task Cards | {metrics['v8_done_count']}/{metrics['v8_task_count']} | - | 信息 |

## 事件流统计

| 事件类型 | 数量 |
|----------|:---:|
"""
    for etype, count in sorted(metrics.get("event_types", {}).items(), key=lambda x: -x[1]):
        report += f"| {etype} | {count} |\n"

    report += f"""
总事件数：{metrics['event_count']}

## 趋势判断

| 维度 | 趋势 | 建议 |
|------|------|------|
| 门禁自动化 | 从 0% 到 {metrics['gate_pass_rate']}% | {"已稳定，保持" if metrics['gate_pass_rate'] >= 90 else "需继续提升"} |
| 路径安全 | {"零越权" if metrics['path_violation_rate'] == 0 else "存在越权"} | {"已稳定" if metrics['path_violation_rate'] == 0 else "需修复"} |
| 成本控制 | 利用率 {metrics['avg_cost_utilization']}% | {"极低利用率，说明还未接入真实 cost 事件" if metrics['avg_cost_utilization'] < 5 else "正常"} |
| 样本覆盖 | {metrics['retrospective_count']}/5 retro + {metrics['drill_done']}/5 drill | {"全部达成" if metrics['retrospective_count'] >= 5 and metrics['drill_done'] >= 5 else "继续积累"} |

## 下一步建议

1. {"cost event 真数据接入（当前利用率为 0，说明 Agent 未自动写 task_cost 事件）" if metrics['avg_cost_utilization'] < 5 else "成本监控正常运行"}
2. {"定期生成报告（建议每周一次）" }
3. {"Hermes 将本报告纳入熵报告体系"}

---

*本报告由 `.standards/v8-health-metrics.py` 自动生成*
"""
    return report


def main():
    output_json = "--json" in sys.argv
    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    # 采集数据
    cards = parse_task_cards()
    pwc_log = parse_pwc_log()
    events = parse_events()
    retros = parse_retrospectives()

    # 计算指标
    metrics = compute_metrics(cards, pwc_log, events, retros)
    score = health_score(metrics)
    metrics["health_score"] = score

    if output_json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    elif output_path:
        report = generate_report(metrics, score)
        Path(output_path).write_text(report, encoding="utf-8")
        print(f"[OK] 健康报告已生成: {output_path} (score: {score}/100)")
    else:
        report = generate_report(metrics, score)
        print(report)


if __name__ == "__main__":
    main()
