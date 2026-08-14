#!/usr/bin/env python3
from __future__ import annotations

"""
frontmatter-lint.py — 息壤 V9 Frontmatter 深度校验
v1.2 · 2026-07-22 | 息壤 V9

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
    "任务卡": {
        "required": ["task_id", "title", "status", "created"],
        "path_pattern": "02-项目管理/任务卡/",
    },
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
VALID_STATUS = [
    "草稿", "正式", "废弃", "WIP", "归档",
    "draft", "active", "proposed", "generated", "completed", "implemented",
    "ready", "in_progress", "pending", "submitted", "review", "accepted",
    "pilot", "official", "retired", "archived", "deprecated", "cancelled", "blocked",
]
VALID_MATURITY = [
    "草稿", "正式", "试行", "归档",
    "骨架占位", "业务理解已完成", "业务理解+流程已完成", "已设计方案", "已发布PRD",
    "draft", "partial", "partial_implementation", "implemented", "verified", "stable",
    "complete", "closed", "blocked", "stale", "archived",
]
VALID_TYPE = ["方法论", "方法论指南", "规范", "MOC", "决策", "PRD", "方案", "复盘", "经验", "模板"]
TASK_STATUS = ["ready", "in_progress", "blocked", "done", "cancelled", "submitted"]
TASK_MATURITY = ["draft", "partial", "complete", "implemented", "verified", "stale", "closed", "blocked"]

# 跳过的目录
SKIP_DIRS = ["_archive", "_temp", ".obsidian", ".trash", ".standards", "node_modules"]

# 跳过的文件
SKIP_FILES = ["README.md", "CHANGELOG.md"]

DEFAULT_TAGS_BY_PREFIX = {
    "00-MOC/": "MOC",
    "02-项目管理/": "项目管理",
    "10-项目/": "项目文档",
    "20-资料/": "资料",
    "30-规范/": "规范",
    "40-决策/": "决策",
    "50-经验/": "经验",
    "60-归档/": "归档",
    "90-模板/": "模板",
}


def parse_frontmatter(content: str) -> tuple[dict | None, str | None]:
    """解析 frontmatter，返回 (字段dict, 错误信息)"""
    if not content.startswith("---"):
        return None, "文件缺少 YAML frontmatter"

    end = content.find("\n---", 3)
    if end == -1:
        return None, "frontmatter 块未闭合"

    fm_text = content[4:end]  # skip first "---\n"
    fields = {}

    lines = fm_text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 只读顶层 key；嵌套 deliverables/gates 不得覆盖顶层 status/type。
        match = re.match(r'^(\w[\w_-]*)\s*:\s*(.*)', line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            # 去除引号
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if not value:
                # 多行列表/映射是有值字段，不应被误报为缺失。
                for child in lines[index + 1:]:
                    if not child.strip() or child.lstrip().startswith("#"):
                        continue
                    if child[:1].isspace():
                        value = "- block-list" if child.lstrip().startswith("-") else "{block-map}"
                    break
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

    if "02-项目管理/任务卡/" in filepath and os.path.basename(filepath).startswith("T-"):
        return "任务卡", SCHEMAS["任务卡"]

    for schema_name, schema in SCHEMAS.items():
        if schema_name == "智能体状态":
            continue  # 已在上面处理
        if schema_name == "任务卡":
            continue  # 只匹配正式 T-*.md 任务卡
        if schema["path_pattern"] in filepath:
            return schema_name, schema
    # 默认使用基础 schema
    return "通用", {"required": BASE_REQUIRED, "path_pattern": ""}


def validate_field_values(fields: dict, schema_name: str) -> list[dict]:
    """校验字段值的合法性"""
    violations = []

    # status 枚举校验
    if "status" in fields and fields["status"] and schema_name == "任务卡":
        status_val = fields["status"]
        allowed_status = TASK_STATUS
        if status_val not in allowed_status:
            violations.append({
                "type": "enum_invalid",
                "severity": "warning",
                "message": f"status 值 '{status_val}' 不在枚举中",
                "suggestion": f"允许值: {', '.join(allowed_status)}"
            })

    # maturity 枚举校验
    if "maturity" in fields and fields["maturity"] and schema_name == "任务卡":
        maturity_val = fields["maturity"]
        allowed_maturity = TASK_MATURITY
        if maturity_val not in allowed_maturity:
            violations.append({
                "type": "enum_invalid",
                "severity": "warning",
                "message": f"maturity 值 '{maturity_val}' 不在枚举中",
                "suggestion": f"允许值: {', '.join(allowed_maturity)}"
            })

    # V9 的 type 是开放分类（例如 task_card、运行日志、技术文档）。
    # 必填性由 schema 校验，不再用 V8 的封闭十项枚举制造误报。

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
        if not re.match(r'^[vV]?\d+(\.\d+)*[a-zA-Z]?$', ver):
            violations.append({
                "type": "format_invalid",
                "severity": "info",
                "message": f"version 格式不规范: '{ver}'",
                "suggestion": "建议格式: X.Y 或 vX.Y.Z（如 1.0, v8.1.2L）"
            })

    # created 日期格式校验
    if "created" in fields and fields["created"]:
        date_val = fields["created"]
        is_template = bool(re.fullmatch(r'\{\{[^{}]+\}\}|YYYY-MM-DD', date_val))
        if not is_template and not re.match(r'^\d{4}-\d{2}-\d{2}', date_val):
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
    if not Path(parent_ref).suffix:
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
        value_present = field in fields and bool(fields[field]) and fields[field] != "null"
        if field == "created" and not value_present:
            value_present = any(
                bool(fields.get(alias)) and fields.get(alias) != "null"
                for alias in ("created_at", "date")
            )
        if not value_present:
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


def scan_vault(
    target_path: str = ".", vault_root: str = ".", *, include_stats: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """扫描指定路径下所有 .md 文件"""
    all_violations = []
    files_scanned = 0

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

        files_scanned += 1
        violations = scan_file(filepath, vault_root)
        all_violations.extend(violations)

    if include_stats:
        return all_violations, files_scanned
    return all_violations


def normalized_rel(filepath: str) -> str:
    return filepath.replace("\\", "/").removeprefix("./")


def safe_fix_updates(filepath: str, violations: list[dict]) -> dict[str, str]:
    """Return only values derivable from the governed path, never guessed semantics."""
    rel = normalized_rel(filepath)
    if rel.startswith("10-项目/基线/"):
        return {}

    missing = {
        match.group(1)
        for item in violations
        if item.get("type") == "fm_field_missing"
        if (match := re.search(r"'([^']+)'$", item.get("message", "")))
    }
    updates: dict[str, str] = {}
    if "tags" in missing:
        for prefix, tag in DEFAULT_TAGS_BY_PREFIX.items():
            if rel.startswith(prefix):
                updates["tags"] = f"[{tag}]"
                break
    if "type" in missing and rel.startswith("40-决策/"):
        updates["type"] = "决策"
    return updates


def set_top_level_fields(content: str, updates: dict[str, str]) -> str:
    if not updates or not content.startswith("---"):
        return content
    had_trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return content

    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(?:null|~)?\s*$")
        replaced = False
        for index in range(1, closing):
            if pattern.fullmatch(lines[index]):
                lines[index] = f"{key}: {value}"
                replaced = True
                break
        if not replaced:
            lines.insert(closing, f"{key}: {value}")
            closing += 1
    return "\n".join(lines) + ("\n" if had_trailing_newline else "")


def release_protected_paths(root: Path = Path(".")) -> set[str]:
    manifest = root / "02-项目管理" / "巡检" / "v9-release-manifest.json"
    if not manifest.is_file():
        return set()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    paths: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str):
                paths.add(normalized_rel(path))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return paths


def apply_safe_fixes(violations: list[dict]) -> list[str]:
    grouped: dict[str, list[dict]] = {}
    for item in violations:
        grouped.setdefault(item["file"], []).append(item)

    changed: list[str] = []
    protected = release_protected_paths()
    for filepath, items in sorted(grouped.items()):
        if normalized_rel(filepath) in protected:
            continue
        updates = safe_fix_updates(filepath, items)
        if not updates:
            continue
        path = Path(filepath)
        content = path.read_text(encoding="utf-8")
        updated = set_top_level_fields(content, updates)
        if updated != content:
            path.write_text(updated, encoding="utf-8")
            changed.append(filepath)
    return changed


def main():
    target = "."
    vault_root = "."
    output_json = "--json" in sys.argv
    scan_all = "--all" in sys.argv
    fix = "--fix" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--path" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]
        if arg == "--vault-root" and i + 1 < len(sys.argv):
            vault_root = sys.argv[i + 1]

    if scan_all:
        target = "."

    violations, files_scanned = scan_vault(target, vault_root, include_stats=True)
    fixed_files: list[str] = []
    if fix:
        fixed_files = apply_safe_fixes(violations)
        violations, files_scanned = scan_vault(target, vault_root, include_stats=True)

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
                "files_scanned": files_scanned
            },
            "fixed_files": fixed_files,
            "violations": violations
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not violations:
            if fixed_files:
                print(f"[fixed] 已安全修复 {len(fixed_files)} 个文件")
            print(f"[pass] Frontmatter 合规检查通过（扫描路径: {target}）")
            sys.exit(0)

        print(f"Frontmatter 合规扫描结果（{target}）：")
        if fixed_files:
            print(f"  Safe fixes: {len(fixed_files)} files")
        print(f"  Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}")
        print()

        for v in sorted(violations, key=lambda x: (x["severity"] != "error", x["file"], x.get("line", 0))):
            severity_icon = {"error": "[E]", "warning": "[W]", "info": "[I]"}[v["severity"]]
            line_info = f":{v.get('line', '')}" if v.get("line") else ""
            print(f"  {severity_icon} {v['file']}{line_info} — {v['message']}")

        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
