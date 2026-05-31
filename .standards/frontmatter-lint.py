#!/usr/bin/env python3
"""
frontmatter-lint.py — 息壤 V8 Frontmatter 深度校验
v1.0 · 2026-05-17 | 息壤 V8.5.0

扫描 vault 中 .md 文件的 frontmatter 合规性（按文件类型分 Schema）。

用法：
  python3 .standards/frontmatter-lint.py [--path 10-项目/] [--fix] [--json]
  python3 .standards/frontmatter-lint.py --all        # 扫描全 vault

检查项：
  1. Frontmatter 存在性（必须以 --- 开头和闭合）
  2. 按文件类型匹配 Schema（方法论/规范/MOC/智能体状态/项目文档）
  3. 字段值合法性（status/maturity/type 枚举校验）
  4. 交叉引用：parent 字段指向的文件是否存在
  5. tags 格式校验
"""

import sys
import os
import re
import json
import glob
from pathlib import Path

# === Schema 定义（按文件类型） ===

# 通用必填字段
BASE_REQUIRED = ["title", "created", "tags"]

# 各类型文件的必填字段
SCHEMAS = {
    "方法论": {
        "required": ["title", "version", "status", "maturity", "type", "created", "tags"],
        "path_pattern": "50-经验/Agent协作方法论/",
    },
    "规范": {
        "required": ["title", "version", "status", "created", "tags"],
        "path_pattern": "30-规范/",
    },
    "MOC": {
        "required": ["title", "type", "created", "tags"],
        "path_pattern": "00-MOC/",
    },
    "智能体状态": {
        "required": ["agent_id", "agent_name", "status", "agent_role", "platform", "last_heartbeat"],
        "path_pattern": "02-项目管理/智能体状态/",
    },
    "项目文档": {
        "required": ["title", "status", "created", "tags"],
        "path_pattern": "10-项目/",
    },
    "决策": {
        "required": ["title", "status", "created", "tags", "type"],
        "path_pattern": "40-决策/",
    },
    "经验": {
        "required": ["title", "created", "tags"],
        "path_pattern": "50-经验/",
    },
}

# 枚举约束
VALID_STATUS = ["草稿", "正式", "废弃", "WIP", "归档"]
VALID_MATURITY = ["草稿", "正式", "试行", "归档"]
VALID_TYPE = ["方法论", "方法论指南", "规范", "MOC", "决策", "PRD", "方案", "复盘", "经验", "模板"]

# 跳过的目录
SKIP_DIRS = ["_archive", "_temp", ".obsidian", ".trash", ".standards", "node_modules"]

# 跳过的文件
SKIP_FILES = ["README.md", "CHANGELOG.md"]


def parse_frontmatter(content: str) -> tuple[dict | None, str | None]:
    """解析 frontmatter，返回 (字段dict, 错误信息)"""
    if not content.startswith("---"):
        return None, "文件缺少 YAML frontmatter"

    end = content.find("\n---", 3)
    if end == -1:
        return None, "frontmatter 块未闭合"

    fm_text = content[4:end]  # skip first "---\n"
    fields = {}

    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 简单 key: value 解析（不处理嵌套 YAML）
        match = re.match(r'^(\w[\w_-]*)\s*:\s*(.*)', line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            # 去除引号
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            fields[key] = value

    return fields, None


def detect_schema(filepath: str, fields: dict) -> tuple[str, dict]:
    """根据文件路径+frontmatter字段判断应使用哪个 Schema"""
    # 智能体状态 目录下但有 title 字段的是规范文档（如 EVENT-SPEC.md），不是 agent 状态文件
    if "智能体状态/" in filepath:
        if "agent_id" in fields:
            return "智能体状态", SCHEMAS["智能体状态"]
        # 不是 agent 状态文件，按"规范"处理
        return "规范", SCHEMAS["规范"]

    for schema_name, schema in SCHEMAS.items():
        if schema_name == "智能体状态":
            continue  # 已在上面处理
        if schema["path_pattern"] in filepath:
            return schema_name, schema
    # 默认使用基础 schema
    return "通用", {"required": BASE_REQUIRED, "path_pattern": ""}


def validate_field_values(fields: dict, schema_name: str) -> list[dict]:
    """校验字段值的合法性"""
    violations = []

    # status 枚举校验
    if "status" in fields and fields["status"]:
        status_val = fields["status"]
        if status_val not in VALID_STATUS and schema_name != "智能体状态":
            violations.append({
                "type": "enum_invalid",
                "severity": "warning",
                "message": f"status 值 '{status_val}' 不在枚举中",
                "suggestion": f"允许值: {', '.join(VALID_STATUS)}"
            })

    # maturity 枚举校验
    if "maturity" in fields and fields["maturity"]:
        maturity_val = fields["maturity"]
        if maturity_val not in VALID_MATURITY:
            violations.append({
                "type": "enum_invalid",
                "severity": "warning",
                "message": f"maturity 值 '{maturity_val}' 不在枚举中",
                "suggestion": f"允许值: {', '.join(VALID_MATURITY)}"
            })

    # type 枚举校验
    if "type" in fields and fields["type"]:
        type_val = fields["type"]
        if type_val not in VALID_TYPE and schema_name not in ("智能体状态", "MOC"):
            violations.append({
                "type": "enum_invalid",
                "severity": "info",
                "message": f"type 值 '{type_val}' 不在标准枚举中",
                "suggestion": f"标准值: {', '.join(VALID_TYPE)}"
            })

    # tags 格式校验
    if "tags" in fields:
        tags_val = fields["tags"]
        if tags_val and not (tags_val.startswith("[") or tags_val.startswith("-")):
            violations.append({
                "type": "format_invalid",
                "severity": "warning",
                "message": f"tags 格式不正确: '{tags_val}'",
                "suggestion": "tags 应为 YAML 数组格式: [tag1, tag2] 或 - tag1"
            })

    # version 格式校验
    if "version" in fields and fields["version"]:
        ver = fields["version"]
        if not re.match(r'^\d+(\.\d+)*[a-zA-Z]?$', ver):
            violations.append({
                "type": "format_invalid",
                "severity": "info",
                "message": f"version 格式不规范: '{ver}'",
                "suggestion": "建议格式: X.Y 或 X.Y.Z（如 1.0, 8.1.2L）"
            })

    # created 日期格式校验
    if "created" in fields and fields["created"]:
        date_val = fields["created"]
        if not re.match(r'^\d{4}-\d{2}-\d{2}', date_val):
            violations.append({
                "type": "format_invalid",
                "severity": "warning",
                "message": f"created 日期格式不规范: '{date_val}'",
                "suggestion": "应为 YYYY-MM-DD 格式"
            })

    return violations


def check_parent_ref(fields: dict, filepath: str, vault_root: str) -> list[dict]:
    """检查 parent 字段引用的文件是否存在"""
    violations = []
    if "parent" not in fields or not fields["parent"]:
        return violations

    parent_ref = fields["parent"]
    # 去除 wikilink 格式
    parent_ref = parent_ref.strip("[]").replace("[[", "").replace("]]", "")

    if not parent_ref:
        return violations

    # 尝试在 vault 中查找
    # 直接路径
    if not parent_ref.endswith(".md"):
        parent_ref += ".md"

    # 在同目录下查找
    same_dir = os.path.join(os.path.dirname(filepath), parent_ref)
    # 在 vault 根目录查找
    from_root = os.path.join(vault_root, parent_ref)

    # 尝试 glob 搜索
    found = False
    if os.path.isfile(same_dir):
        found = True
    elif os.path.isfile(from_root):
        found = True
    else:
        # 模糊查找
        pattern = os.path.join(vault_root, f"**/{os.path.basename(parent_ref)}")
        matches = glob.glob(pattern, recursive=True)
        if matches:
            found = True

    if not found:
        violations.append({
            "type": "broken_ref",
            "severity": "warning",
            "message": f"parent 引用 '{fields['parent']}' 指向的文件不存在",
            "suggestion": "检查文件是否被移动或重命名"
        })

    return violations


def scan_file(filepath: str, vault_root: str = ".") -> list[dict]:
    """扫描单个文件的 frontmatter 违规"""
    violations = []

    # 跳过非 .md 文件
    if not filepath.endswith(".md"):
        return violations

    # 跳过特殊文件
    basename = os.path.basename(filepath)
    if basename in SKIP_FILES:
        return violations

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return violations

    # 空文件跳过
    if not content.strip():
        return violations

    # 1. 解析 frontmatter
    fields, error = parse_frontmatter(content)
    if error:
        violations.append({
            "file": filepath,
            "line": 1,
            "type": "fm_missing",
            "severity": "error",
            "message": error,
            "suggestion": "添加标准 YAML frontmatter 块"
        })
        return violations

    # 2. 确定 schema
    schema_name, schema = detect_schema(filepath, fields)

    # 3. 必填字段检查
    for field in schema["required"]:
        if field not in fields or not fields[field] or fields[field] == "null":
            violations.append({
                "file": filepath,
                "line": 1,
                "type": "fm_field_missing",
                "severity": "error" if field in ("title", "agent_id") else "warning",
                "message": f"[{schema_name}] 缺少必填字段 '{field}'",
                "suggestion": f"在 frontmatter 中添加 {field}: <值>"
            })

    # 4. 字段值合法性
    value_violations = validate_field_values(fields, schema_name)
    for v in value_violations:
        v["file"] = filepath
        v["line"] = 1
        violations.append(v)

    # 5. parent 引用检查
    ref_violations = check_parent_ref(fields, filepath, vault_root)
    for v in ref_violations:
        v["file"] = filepath
        v["line"] = 1
        violations.append(v)

    return violations


def scan_vault(target_path: str = ".", vault_root: str = ".") -> list[dict]:
    """扫描指定路径下所有 .md 文件"""
    all_violations = []

    pattern = os.path.join(target_path, "**/*.md")
    for filepath in glob.glob(pattern, recursive=True):
        # 跳过排除目录
        should_skip = False
        for skip_dir in SKIP_DIRS:
            if f"/{skip_dir}/" in filepath or f"\\{skip_dir}\\" in filepath:
                should_skip = True
                break
        if should_skip:
            continue

        violations = scan_file(filepath, vault_root)
        all_violations.extend(violations)

    return all_violations


def main():
    target = "."
    vault_root = "."
    output_json = "--json" in sys.argv
    scan_all = "--all" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--path" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]
        if arg == "--vault-root" and i + 1 < len(sys.argv):
            vault_root = sys.argv[i + 1]

    if scan_all:
        target = "."

    violations = scan_vault(target, vault_root)

    # 统计
    errors = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    infos = [v for v in violations if v["severity"] == "info"]

    if output_json:
        result = {
            "status": "fail" if errors else "pass",
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "info": len(infos),
                "files_scanned": len(set(v["file"] for v in violations)) if violations else 0
            },
            "violations": violations
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"[pass] Frontmatter 合规检查通过（扫描路径: {target}）")
            sys.exit(0)

        print(f"Frontmatter 合规扫描结果（{target}）：")
        print(f"  Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}")
        print()

        for v in sorted(violations, key=lambda x: (x["severity"] != "error", x["file"], x.get("line", 0))):
            severity_icon = {"error": "[E]", "warning": "[W]", "info": "[I]"}[v["severity"]]
            line_info = f":{v.get('line', '')}" if v.get("line") else ""
            print(f"  {severity_icon} {v['file']}{line_info} — {v['message']}")

        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
