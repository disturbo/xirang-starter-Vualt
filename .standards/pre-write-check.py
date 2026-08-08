#!/usr/bin/env python3
"""
pre-write-check.py — 息壤 V8 产物合规检查脚本
v1.0 · 2026-05-17 · 来自商务补偿 retrospective | 息壤 V8.5.0

用途：任何 Agent 写文件前/后调用，检查四项合规：
  1. emoji 检测
  2. frontmatter 完整性
  3. 品牌色值
  4. 路径白名单

用法：
  python3 .standards/pre-write-check.py <file> [--fix] [--json] [--task-id T-XXXXXXXX-NN]

返回：
  0 = 全部通过
  1 = 有违规（违规列表输出到 stderr）
  2 = 文件不存在或不可读
"""

import sys
import re
import json
import os
from pathlib import Path

# === 配置 ===
BRAND_MAIN = "#861B2F"
BRAND_ACCENT = "#2D9C4F"
BRAND_FONT = "PingFang SC"
BRAND_FONT_FALLBACK = "Microsoft YaHei"

ALLOWED_PATHS = [
    "10-项目/",
    "02-项目管理/",
    "50-经验/",
    "20-资料/",
    "60-归档/",
    "90-模板/",
    "_temp/",
    ".standards/",
]

FORBIDDEN_PATHS = [
    "00-MOC/",
    "30-规范/",
    "40-决策/",
]

REQUIRED_FRONTMATTER = ["title", "version", "status", "maturity", "type", "created", "tags"]

# _temp/ 暂存区规则（V8.2.0L 收口定稿）
# - YAML 补办清单（_temp/cowork-handoff/{task-id}.md）：需要 task_id + agent + status
# - 暂存区产物（_temp/cowork-handoff/{task-id}/*.md）：frontmatter 必须有但放宽 maturity/version 为 warning
# - 暂存区 HTML：emoji/品牌色照常检查，无 frontmatter 要求
TEMP_HANDOFF_REQUIRED = ["task_id", "agent", "status"]
TEMP_PRODUCT_REQUIRED = ["title", "type", "created"]  # maturity/version 为 warning 不阻断
TEMP_PRODUCT_WARNED = ["version", "maturity"]  # 缺失时发 warning 但不 fail

# emoji unicode ranges (simplified — covers most common emoji)
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"  # Misc Symbols, Dingbats, Emoticons, etc
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols Extended-A
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols
    "\U00002600-\U000027BF"  # Misc symbols (includes some emoji)
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "]+"
)

# Agent 角色名 emoji (保留，不在检测范围)
AGENT_EMOJI_EXCEPTIONS = {
    "\U0001F41B",  # 🐛 小虫
    "\U0001F43E",  # 🐾 青霉素
    "\U0001F415",  # 🐕 红霉素
    "\U0001F30A",  # 🌊 头孢
    "\U000026A1",  # ⚡ Claudian
    "\U0001F6E0",  # 🛠️ WorkBuddy
}


def check_emoji(content: str, filepath: str) -> list[str]:
    """检查文件是否包含 emoji"""
    violations = []
    # Skip markdown files that are methodology docs (they document agent emoji)
    if "50-经验/Agent协作方法论" in filepath:
        return violations

    matches = EMOJI_PATTERN.findall(content)
    for m in set(matches):
        # 逐字符检查：多字符匹配中可能混合例外字符
        non_exception_chars = [c for c in m if c not in AGENT_EMOJI_EXCEPTIONS]
        if non_exception_chars:
            codes = " ".join(f"U+{ord(c):04X}" for c in non_exception_chars)
            violations.append(f"emoji: 发现 emoji 字符 '{m}' ({codes})")
    return violations


def check_frontmatter(content: str, filepath: str) -> list[str]:
    """检查 frontmatter 完整性（仅 .md 文件，按文件类型匹配不同 schema）"""
    violations = []
    warnings = []
    if not filepath.endswith('.md'):
        return violations
    if not content.startswith("---"):
        violations.append("frontmatter: 文件缺少 YAML frontmatter（不以 '---' 开头）")
        return violations

    end = content.find("---", 3)
    if end == -1:
        violations.append("frontmatter: frontmatter 块未闭合（缺少结束的 '---'）")
        return violations

    fm = content[3:end]

    # 根据文件类型选择必填字段
    if "智能体状态/" in filepath:
        # 智能体状态 files only need: agent_id + status
        required = ["agent_id", "status"]
    elif "_temp/cowork-handoff/" in filepath and not re.search(r"_temp/cowork-handoff/[^/]+/", filepath):
        # YAML 补办清单（顶级文件如 _temp/cowork-handoff/T-xxx.md）
        required = TEMP_HANDOFF_REQUIRED
    elif "_temp/" in filepath:
        # 暂存区产物：核心字段必须有，maturity/version 放宽为 warning
        required = TEMP_PRODUCT_REQUIRED
        for field in TEMP_PRODUCT_WARNED:
            if not re.search(rf"^{field}\s*:", fm, re.MULTILINE):
                warnings.append(f"frontmatter: [warning] 暂存区产物建议补充 '{field}'（不阻断）")
    else:
        required = REQUIRED_FRONTMATTER

    for field in required:
        if not re.search(rf"^{field}\s*:", fm, re.MULTILINE):
            violations.append(f"frontmatter: 缺少必填字段 '{field}'")

    # Warnings 输出但不计入 violations（不影响退出码）
    for w in warnings:
        print(f"  [warn] {w}", file=sys.stderr)

    return violations


def check_brand(content: str, filepath: str) -> list[str]:
    """检查品牌色值合规"""
    violations = []
    # Only check HTML/CSS files for brand colors
    if not (filepath.endswith(".html") or filepath.endswith(".css") or "html" in content[:100].lower()):
        return violations

    # Check for hardcoded hex colors that aren't brand colors
    hex_colors = re.findall(r'#[0-9A-Fa-f]{6}', content)
    brand_set = {BRAND_MAIN.lower(), BRAND_ACCENT.lower(), "#ffffff", "#000000", "#f5f5f5", "#f8f9fa", "#e0e0e0", "#333333", "#666666", "#999999", "#cccccc", "#f0f0f0", "#fafafa", "#1a1a1a"}
    for c in set(hex_colors):
        if c.lower() not in brand_set:
            violations.append(f"brand: 发现非品牌色值 '{c}'（允许: {BRAND_MAIN}, {BRAND_ACCENT}, 中性色）")
    return violations


def check_path(filepath: str) -> list[str]:
    """检查路径是否在白名单/黑名单"""
    violations = []
    # Check forbidden paths
    for forbidden in FORBIDDEN_PATHS:
        if filepath.startswith(forbidden):
            violations.append(f"path: 禁止写入 '{forbidden}' 目录（v8.1.1L 约束）")

    # Check if file is in allowed area (match absolute or relative paths)
    from pathlib import Path
    fp = str(Path(filepath).resolve())
    in_allowed = any(
        fp.endswith(a) or ('/' + a) in fp or fp.startswith(a)
        for a in ALLOWED_PATHS
    )
    if not in_allowed:
        violations.append(f"path: 路径不在白名单中 ({', '.join(ALLOWED_PATHS)})")

    return violations


LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "02-项目管理", "pre-write-check.log.jsonl")


def append_log(filepath: str, status: str, violations: list[str], warnings: list[str] | None = None):
    """每次检查追加一行 JSONL 日志，使拦截次数可采集"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    entry = {
        "ts": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "file": filepath,
        "status": status,
        "violations": len(violations),
        "violation_types": list({v.split(":")[0] for v in violations}) if violations else [],
        "warnings": len(warnings) if warnings else 0,
    }
    try:
        log_dir = os.path.dirname(LOG_PATH)
        os.makedirs(log_dir, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志写入失败不应阻断主流程


def check_task_path(filepath: str, task_id: str) -> list[str]:
    """检查文件路径是否在 task card 的 allowed_write_roots 内（per-task 路径隔离）"""
    violations = []
    task_card_path = os.path.join("02-项目管理", "tasks", f"{task_id}.md")
    if not os.path.isfile(task_card_path):
        violations.append(f"task_path: task card 不存在 ({task_card_path})")
        return violations

    with open(task_card_path, "r", encoding="utf-8") as f:
        card_content = f.read()

    # 提取 allowed_write_roots
    allowed_roots = []
    in_allowed_section = False
    for line in card_content.split("\n"):
        if "allowed_write_roots:" in line:
            in_allowed_section = True
            continue
        if in_allowed_section:
            stripped = line.strip()
            if stripped.startswith("- "):
                root = stripped[2:].strip()
                allowed_roots.append(root)
            elif stripped and not stripped.startswith("#"):
                break  # 离开 allowed_write_roots 列表

    if not allowed_roots:
        violations.append(f"task_path: task card 未定义 allowed_write_roots")
        return violations

    # 检查 filepath 是否在任意一个 allowed root 下
    fp_normalized = filepath.replace("\\", "/")
    in_allowed = any(fp_normalized.startswith(root) for root in allowed_roots)
    if not in_allowed:
        violations.append(
            f"task_path: 路径越权！文件 '{filepath}' 不在 task {task_id} 的 "
            f"allowed_write_roots ({', '.join(allowed_roots)}) 内"
        )

    return violations


def main():
    if len(sys.argv) < 2:
        print("用法: pre-write-check.py <file> [--fix] [--json] [--task-id T-XXX]", file=sys.stderr)
        sys.exit(2)

    filepath = sys.argv[1]
    output_json = "--json" in sys.argv

    # 解析 --task-id 参数
    task_id = None
    if "--task-id" in sys.argv:
        idx = sys.argv.index("--task-id")
        if idx + 1 < len(sys.argv):
            task_id = sys.argv[idx + 1]

    if not os.path.isfile(filepath):
        msg = f"文件不存在或不可读: {filepath}"
        if output_json:
            print(json.dumps({"status": "error", "message": msg}))
        else:
            print(msg, file=sys.stderr)
        append_log(filepath, "error", [msg])
        sys.exit(2)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    all_violations = []
    all_violations.extend(check_emoji(content, filepath))
    all_violations.extend(check_frontmatter(content, filepath))
    all_violations.extend(check_brand(content, filepath))
    all_violations.extend(check_path(filepath))

    # per-task 路径隔离检查（仅在提供 --task-id 时启用）
    if task_id:
        all_violations.extend(check_task_path(filepath, task_id))

    # 日志化：每次执行都记录
    log_status = "pass" if not all_violations else "fail"
    append_log(filepath, log_status, all_violations)

    if output_json:
        result = {
            "status": "pass" if not all_violations else "fail",
            "file": filepath,
            "violations": all_violations,
            "passed_checks": [
                "emoji" if not any("emoji:" in v for v in all_violations) else None,
                "frontmatter" if not any("frontmatter:" in v for v in all_violations) else None,
                "brand" if not any("brand:" in v for v in all_violations) else None,
                "path" if not any("path:" in v for v in all_violations) else None,
            ]
        }
        result["passed_checks"] = [c for c in result["passed_checks"] if c]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1 if all_violations else 0)
    else:
        if all_violations:
            print(f"[fail] {filepath} — {len(all_violations)} 项违规：")
            for v in all_violations:
                print(f"  - {v}")
            sys.exit(1)
        else:
            print(f"[pass] {filepath}")
            sys.exit(0)


if __name__ == "__main__":
    main()
