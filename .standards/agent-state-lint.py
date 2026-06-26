#!/usr/bin/env python3
"""
agent-state-lint.py -- V8.5 Agent 状态文件 Schema 验证与修复工具
v1.0.0 | 2026-05-24 | 息壤 V8.5.0

用法:
  python3 .standards/agent-state-lint.py --validate          # 校验所有状态文件
  python3 .standards/agent-state-lint.py --validate --json   # JSON 输出
  python3 .standards/agent-state-lint.py --fix               # 自动补齐缺失字段
  python3 .standards/agent-state-lint.py --fix --agent claudian  # 只修单个

退出码:
  0 = 全部通过
  1 = 有违规（--validate 模式）或修复失败（--fix 模式）

跨平台调用:
  bash -c "cd $VAULT_ROOT && python3 .standards/agent-state-lint.py --validate --json"
"""

import sys
import os
import json
import argparse
import re
from pathlib import Path
from datetime import datetime, date

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
STATE_DIR = VAULT_ROOT / "02-项目管理" / "智能体状态"
SCHEMA_PATH = VAULT_ROOT / ".standards" / "schemas" / "agent-state.schema.json"

# Agent 文件名映射
AGENT_FILES = {
    "claudian": "Claudian.md",
    "xiaochong": "阿莫西林.md",
    "toubao": "头孢.md",
    "workbuddy": "WorkBuddy.md",
    "qingmeisu": "青霉素.md",
    "hongmeisu": "红霉素.md",
}

# V8.5 必须字段及默认值（用于 --fix）
REQUIRED_FIELDS_DEFAULTS = {
    "agent_id": None,  # 无默认值，必须已存在
    "agent_name": None,
    "agent_role": None,
    "platform": None,
    "status": "idle",
    "current_task": "null",
    "current_task_id": "null",
    "last_heartbeat": None,  # 由修复时生成
    "spawn_count": "0",
    "active_subtasks": "[]",
    "cooldown_until": "null",
    "cost_tracking": None,  # 结构体，特殊处理
    "tags": None,  # 保留现有
}

COST_TRACKING_DEFAULTS = {
    "session_tokens": "0",
    "session_cost_cny": "0.0",
    "weekly_tokens": "0",
    "weekly_cost_cny": "0.0",
    "model_used": "null",
    "last_reset": None,  # 当天日期
}

VALID_STATUS = {"idle", "busy", "cooling", "error", "standby", "retired"}
VALID_PLATFORMS = {"Codebuddy", "OpenClaw", "Hermes", "Codex", "Claudian"}
VALID_AGENT_IDS = {"claudian", "xiaochong", "toubao", "workbuddy", "qingmeisu", "hongmeisu"}


def parse_frontmatter(filepath: Path) -> tuple[dict, str, str]:
    """解析 YAML frontmatter，返回 (fields_dict, frontmatter_raw, body)"""
    content = filepath.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}, "", content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, "", content

    fm_raw = parts[1].strip()
    body = parts[2]
    fields = {}

    for line in fm_raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 处理嵌套（cost_tracking 子字段）
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            fields[key] = value

    return fields, fm_raw, body


def load_schema() -> dict:
    """加载 JSON Schema 文件"""
    if SCHEMA_PATH.exists():
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {}


def validate_against_schema(fields: dict, schema: dict) -> list[dict]:
    """基于 JSON Schema 定义进行验证（标准库实现，无需 jsonschema 依赖）"""
    errors = []
    properties = schema.get("properties", {})

    # 1. required 字段检查
    for field in schema.get("required", []):
        if field not in fields:
            errors.append({"severity": "error", "field": field, "message": f"缺失必须字段: {field}（schema required）"})

    # 2. enum 值域检查
    for field, prop_schema in properties.items():
        if field not in fields:
            continue
        value = fields[field].strip("\"'")
        if "enum" in prop_schema:
            if value not in prop_schema["enum"]:
                errors.append({
                    "severity": "error", "field": field,
                    "message": f"值 '{value}' 不在允许范围: {prop_schema['enum']}"
                })

    # 3. type 检查（基础类型）
    for field, prop_schema in properties.items():
        if field not in fields:
            continue
        value = fields[field].strip("\"'")
        expected_type = prop_schema.get("type")
        if expected_type == "integer" or (isinstance(expected_type, list) and "integer" in expected_type):
            if value not in ("null", ""):
                try:
                    int(value)
                except ValueError:
                    errors.append({"severity": "error", "field": field, "message": f"应为整数，实际: '{value}'"})
        if expected_type == "array" or (isinstance(expected_type, list) and "array" in expected_type):
            if not (value.startswith("[") or value == "null" or value == ""):
                errors.append({"severity": "warning", "field": field, "message": f"应为数组格式，实际: '{value}'"})

    # 4. minimum 检查
    for field, prop_schema in properties.items():
        if field not in fields:
            continue
        if "minimum" in prop_schema:
            value = fields[field].strip("\"'")
            try:
                if int(value) < prop_schema["minimum"]:
                    errors.append({"severity": "error", "field": field, "message": f"值 {value} 小于最小值 {prop_schema['minimum']}"})
            except ValueError:
                pass

    return errors


def validate_agent_file(filepath: Path) -> list[dict]:
    """验证单个 Agent 状态文件，返回违规列表"""
    errors = []
    agent_filename = filepath.name

    if not filepath.exists():
        errors.append({"severity": "error", "field": "_file", "message": f"文件不存在: {filepath}"})
        return errors

    fields, fm_raw, body = parse_frontmatter(filepath)

    if not fields:
        errors.append({"severity": "error", "field": "_frontmatter", "message": "无法解析 YAML frontmatter"})
        return errors

    # 加载并使用 JSON Schema 进行验证
    schema = load_schema()
    if schema:
        schema_errors = validate_against_schema(fields, schema)
        errors.extend(schema_errors)
    else:
        # 降级：schema 文件不存在时用旧逻辑
        for field in ["agent_id", "agent_name", "agent_role", "platform", "status"]:
            if field not in fields:
                errors.append({"severity": "error", "field": field, "message": f"缺失必须字段: {field}"})

        if "status" in fields and fields["status"] not in VALID_STATUS:
            errors.append({"severity": "error", "field": "status", "message": f"非法状态值: {fields['status']}"})

        if "platform" in fields:
            platform = fields["platform"].strip("\"'")
            if platform not in VALID_PLATFORMS:
                errors.append({"severity": "warning", "field": "platform", "message": f"非标准平台: {platform}"})

        if "agent_id" in fields:
            agent_id = fields["agent_id"].strip("\"'")
            if agent_id not in VALID_AGENT_IDS:
                errors.append({"severity": "error", "field": "agent_id", "message": f"非法 agent_id: {agent_id}"})

    # V8.5 新增字段检查（schema 可能不含这些作为 required，单独检查）
    for field in ["current_task_id", "active_subtasks"]:
        if field not in fields:
            # 检查是否已被 schema required 报过
            already_reported = any(e["field"] == field for e in errors)
            if not already_reported:
                errors.append({"severity": "warning", "field": field, "message": f"缺失 V8.5 字段: {field}（可用 --fix 补齐）"})

    # 检查 cost_tracking 存在
    if "cost_tracking" not in fm_raw:
        errors.append({"severity": "warning", "field": "cost_tracking", "message": "缺失 cost_tracking 块"})

    return errors


def fix_agent_file(filepath: Path) -> dict:
    """修复单个 Agent 状态文件，补齐缺失字段"""
    result = {"file": str(filepath.name), "fixed_fields": [], "status": "ok"}

    if not filepath.exists():
        result["status"] = "error"
        result["message"] = "文件不存在"
        return result

    content = filepath.read_text(encoding="utf-8")
    if not content.startswith("---"):
        result["status"] = "error"
        result["message"] = "无 frontmatter"
        return result

    parts = content.split("---", 2)
    if len(parts) < 3:
        result["status"] = "error"
        result["message"] = "frontmatter 格式异常"
        return result

    fm_lines = parts[1].strip().split("\n")
    body = parts[2]

    # 提取现有字段名
    existing_fields = set()
    for line in fm_lines:
        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key = line.split(":")[0].strip()
            existing_fields.add(key)

    # 需要补齐的字段
    additions = []
    today = date.today().isoformat()
    now = datetime.now().astimezone().isoformat()

    if "current_task_id" not in existing_fields:
        additions.append("current_task_id: null")
        result["fixed_fields"].append("current_task_id")

    if "active_subtasks" not in existing_fields:
        additions.append("active_subtasks: []")
        result["fixed_fields"].append("active_subtasks")

    if "spawn_count" not in existing_fields:
        additions.append("spawn_count: 0")
        result["fixed_fields"].append("spawn_count")

    if "cooldown_until" not in existing_fields:
        additions.append("cooldown_until: null")
        result["fixed_fields"].append("cooldown_until")

    if "last_heartbeat" not in existing_fields:
        additions.append(f'last_heartbeat: "{now}"')
        result["fixed_fields"].append("last_heartbeat")

    # 检查 cost_tracking
    has_cost_tracking = "cost_tracking" in existing_fields
    if not has_cost_tracking:
        cost_block = [
            "cost_tracking:",
            "  session_tokens: 0",
            "  session_cost_cny: 0.0",
            "  weekly_tokens: 0",
            "  weekly_cost_cny: 0.0",
            "  model_used: null",
            f'  last_reset: "{today}"',
        ]
        additions.extend(cost_block)
        result["fixed_fields"].append("cost_tracking")

    if not additions:
        result["message"] = "无需修复"
        return result

    # 在 frontmatter 末尾追加（tags 前面）
    # 找到 tags 行的位置，在其前面插入
    insert_idx = len(fm_lines)
    for i, line in enumerate(fm_lines):
        if line.startswith("tags:"):
            insert_idx = i
            break

    new_fm_lines = fm_lines[:insert_idx] + additions + fm_lines[insert_idx:]
    new_content = "---\n" + "\n".join(new_fm_lines) + "\n---" + body

    filepath.write_text(new_content, encoding="utf-8")
    result["message"] = f"已补齐 {len(result['fixed_fields'])} 个字段"
    return result


def main():
    parser = argparse.ArgumentParser(description="V8.5 Agent 状态文件验证与修复")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", action="store_true", help="校验所有状态文件")
    group.add_argument("--fix", action="store_true", help="自动补齐缺失字段")
    parser.add_argument("--agent", type=str, help="只操作指定 agent_id")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    # 确定要处理的文件
    if args.agent:
        if args.agent not in AGENT_FILES:
            print(json.dumps({"error": f"未知 agent_id: {args.agent}", "valid": list(AGENT_FILES.keys())}))
            sys.exit(1)
        targets = {args.agent: AGENT_FILES[args.agent]}
    else:
        targets = AGENT_FILES

    if args.validate:
        all_errors = {}
        total_errors = 0
        total_warnings = 0

        for agent_id, filename in targets.items():
            filepath = STATE_DIR / filename
            errors = validate_agent_file(filepath)
            all_errors[agent_id] = errors
            total_errors += sum(1 for e in errors if e["severity"] == "error")
            total_warnings += sum(1 for e in errors if e["severity"] == "warning")

        if args.json:
            output = {
                "agents_checked": len(targets),
                "errors": total_errors,
                "warnings": total_warnings,
                "details": all_errors,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(f"=== Agent 状态文件验证 ===")
            print(f"检查 {len(targets)} 个文件")
            print(f"错误: {total_errors} | 警告: {total_warnings}")
            print()
            for agent_id, errors in all_errors.items():
                if errors:
                    print(f"[{agent_id}] ({AGENT_FILES[agent_id]})")
                    for e in errors:
                        icon = "x" if e["severity"] == "error" else "!"
                        print(f"  [{icon}] {e['field']}: {e['message']}")
                    print()
                else:
                    print(f"[{agent_id}] OK")

        sys.exit(1 if total_errors > 0 else 0)

    elif args.fix:
        results = []
        for agent_id, filename in targets.items():
            filepath = STATE_DIR / filename
            result = fix_agent_file(filepath)
            result["agent_id"] = agent_id
            results.append(result)

        if args.json:
            print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        else:
            print("=== Agent 状态文件修复 ===")
            for r in results:
                if r["fixed_fields"]:
                    print(f"[{r['agent_id']}] 修复: {', '.join(r['fixed_fields'])}")
                else:
                    print(f"[{r['agent_id']}] {r.get('message', 'OK')}")

        has_error = any(r["status"] == "error" for r in results)
        sys.exit(1 if has_error else 0)


if __name__ == "__main__":
    main()
