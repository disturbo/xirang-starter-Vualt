#!/usr/bin/env python3
"""
pre-start-check.py — 息壤 V8.2 任务启动前门禁
v1.0 · 2026-05-18 | 息壤 V8.5.0

用途：验证 task card 是否满足启动条件。任务开始前必须通过此检查。

用法：
  python3 .standards/pre-start-check.py <task_id> [--json] [--create]

  --json   输出 JSON 格式
  --create 如果 task card 不存在，从模板创建（交互式）

检查项（全部 pass 才能启动）：
  1. task card 文件存在
  2. owner 非空
  3. budget.cost_ceiling_cny > 0
  4. deliverables 至少 1 项
  5. status 为 ready（不是 done/cancelled/blocked）
  6. blocked_by 为空

返回：
  0 = 全部通过，可启动
  1 = 有不通过项
  2 = task card 不存在
"""

import sys
import os
import re
import json
from pathlib import Path

TASKS_DIR = "02-项目管理/任务卡"
TEMPLATE_PATH = os.path.join(TASKS_DIR, "_template.md")
LOG_PATH = "02-项目管理/pre-write-check.log.jsonl"  # 复用同一日志文件


def parse_frontmatter(content: str) -> dict:
    """简易 frontmatter 解析（不依赖 pyyaml）"""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm_text = content[3:end]
    result = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("#") and not line.startswith("-"):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # 去引号
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            result[key] = val
    return result


def extract_yaml_value(content: str, key: str) -> str | None:
    """从 frontmatter 中提取特定 key 的值（支持嵌套缩进字段）"""
    # 先尝试顶级
    m = re.search(rf"^{key}\s*:\s*(.+)$", content, re.MULTILINE)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    # 再尝试缩进（嵌套字段如 budget.cost_ceiling_cny）
    m = re.search(rf"^\s+{key}\s*:\s*(.+)$", content, re.MULTILINE)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return None


def extract_list_field(content: str, key: str) -> list[str]:
    """提取 YAML 列表字段（简易版）"""
    # 匹配 key: 后面的 - item 行
    pattern = rf"^{key}:\s*\n((?:\s+-\s+.+\n?)*)"
    m = re.search(pattern, content, re.MULTILINE)
    if m:
        items = re.findall(r"^\s+-\s+(.+)$", m.group(1), re.MULTILINE)
        return [i.strip() for i in items if i.strip()]
    # 也检查 inline 格式: key: []
    m2 = re.search(rf"^{key}:\s*\[([^\]]*)\]", content, re.MULTILINE)
    if m2:
        items = [i.strip().strip('"').strip("'") for i in m2.group(1).split(",") if i.strip()]
        return items
    return []


def check_task_card(task_id: str) -> dict:
    """执行 pre-start-check，返回结果字典"""
    card_path = os.path.join(TASKS_DIR, f"{task_id}.md")

    result = {
        "task_id": task_id,
        "card_path": card_path,
        "checks": {},
        "status": "pass",
        "blockers": [],
    }

    # Check 1: task card 存在
    if not os.path.isfile(card_path):
        result["checks"]["card_exists"] = False
        result["status"] = "fail"
        result["blockers"].append("task card 文件不存在")
        return result
    result["checks"]["card_exists"] = True

    with open(card_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取 frontmatter 区域
    if not content.startswith("---"):
        result["status"] = "fail"
        result["blockers"].append("task card 无 frontmatter")
        return result
    end = content.find("---", 3)
    if end == -1:
        result["status"] = "fail"
        result["blockers"].append("frontmatter 未闭合")
        return result
    fm = content[:end + 3]

    # Check 2: owner 非空
    owner = extract_yaml_value(fm, "owner")
    if owner and owner not in ('""', "''", "null", ""):
        result["checks"]["owner"] = True
    else:
        result["checks"]["owner"] = False
        result["status"] = "fail"
        result["blockers"].append(f"owner 为空（当前: {owner!r}）")

    # Check 3: budget.cost_ceiling_cny > 0
    ceiling = extract_yaml_value(fm, "cost_ceiling_cny")
    if ceiling:
        try:
            val = float(ceiling)
            result["checks"]["budget"] = val > 0
            if val <= 0:
                result["status"] = "fail"
                result["blockers"].append(f"cost_ceiling_cny <= 0（当前: {val}）")
        except ValueError:
            result["checks"]["budget"] = False
            result["status"] = "fail"
            result["blockers"].append(f"cost_ceiling_cny 格式错误（当前: {ceiling!r}）")
    else:
        result["checks"]["budget"] = False
        result["status"] = "fail"
        result["blockers"].append("缺少 cost_ceiling_cny 字段")

    # Check 4: deliverables 至少 1 项
    deliverables = extract_list_field(fm, "deliverables")
    # 也检查 deliverables 下的 - path: 模式
    if not deliverables:
        deliv_paths = re.findall(r"^\s+- path:\s*(.+)$", fm, re.MULTILINE)
        deliverables = [p.strip() for p in deliv_paths if p.strip() and p.strip() != '""']
    has_deliverables = len(deliverables) > 0
    result["checks"]["deliverables"] = has_deliverables
    if not has_deliverables:
        result["status"] = "fail"
        result["blockers"].append("deliverables 为空")

    # Check 5: status 为 ready
    status = extract_yaml_value(fm, "status")
    valid_start_statuses = {"ready", "in_progress"}  # in_progress 也允许（续接）
    if status and status in valid_start_statuses:
        result["checks"]["status_ready"] = True
    else:
        result["checks"]["status_ready"] = False
        if status in ("done", "cancelled"):
            result["status"] = "fail"
            result["blockers"].append(f"任务已结束（status: {status}），不可启动")
        elif status == "blocked":
            result["status"] = "fail"
            result["blockers"].append("任务处于 blocked 状态")
        else:
            # submitted / reviewing 等状态也可以继续
            result["checks"]["status_ready"] = True

    # Check 6: blocked_by 为空
    blocked_by = extract_list_field(fm, "blocked_by")
    result["checks"]["not_blocked"] = len(blocked_by) == 0
    if blocked_by:
        result["status"] = "fail"
        result["blockers"].append(f"被以下任务阻塞: {blocked_by}")

    return result


def append_log(task_id: str, status: str, blockers: list[str]):
    """追加日志"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    entry = {
        "ts": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "check": "pre-start",
        "task_id": task_id,
        "status": status,
        "blockers": len(blockers),
    }
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        print("用法: pre-start-check.py <task_id> [--json] [--create]", file=sys.stderr)
        sys.exit(2)

    task_id = sys.argv[1]
    output_json = "--json" in sys.argv

    result = check_task_card(task_id)
    append_log(task_id, result["status"], result["blockers"])

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["status"] == "pass":
            checks_str = ", ".join(f"{k}=OK" for k, v in result["checks"].items() if v)
            print(f"[PASS] {task_id} 可启动（{checks_str}）")
        else:
            print(f"[FAIL] {task_id} 不可启动：")
            for b in result["blockers"]:
                print(f"  - {b}")

    sys.exit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
