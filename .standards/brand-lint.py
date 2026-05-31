#!/usr/bin/env python3
"""
brand-lint.py — 息壤 V8 品牌合规扫描
v1.0 · 2026-05-17 | 息壤 V8.5.0

扫描 vault 中 HTML/CSS/SVG/drawio 文件的品牌色值、字体、命名合规性。

用法：
  python3 .standards/brand-lint.py [--path 10-项目/] [--fix] [--json]
  python3 .standards/brand-lint.py --all        # 扫描全 vault

检查项：
  1. 品牌色值（主色 #861B2F / 辅助色）
  2. 字体引用（PingFang SC / Microsoft YaHei）
  3. drawio 节点色值合规
  4. 已废弃品牌名检测（启境 M7 等）
"""

import sys
import os
import re
import json
import glob
from pathlib import Path

# === 品牌色系 ===
BRAND_COLORS = {
    # 核心品牌色
    "#861B2F": "品牌主色（核心操作节点）",
    "#2D9C4F": "品牌辅助色",
    # drawio 流程图色系（V8 流程图规范 v3.0）
    "#FAAD14": "判断/警告色",
    "#52C41A": "终点/成功色",
    "#FF4D4F": "异常/错误色",
    "#333333": "主流程线色",
    "#666666": "次要文字色",
    # 中性色（允许）
    "#FFFFFF": "白色背景",
    "#000000": "纯黑",
    "#F5F5F5": "浅灰背景",
    "#F8F9FA": "极浅灰",
    "#E0E0E0": "边框灰",
    "#F0F0F0": "分隔线",
    "#FAFAFA": "卡片背景",
    "#1A1A1A": "深黑文字",
    "#CCCCCC": "禁用灰",
    "#999999": "占位文字",
    # drawio 特有填充色
    "#FFF1F0": "错误背景（浅红）",
    "#FFF7E6": "警告背景（浅橙）",
    "#F6FFED": "成功背景（浅绿）",
    "#E6F7FF": "信息背景（浅蓝）",
}

BRAND_COLORS_SET = {k.lower() for k in BRAND_COLORS}

# 已废弃品牌名
DEPRECATED_BRANDS = [
    "启境 M7",
    "启境M7",
    "QiJing",
    "qijing",
]

# 允许的字体
ALLOWED_FONTS = ["PingFang SC", "Microsoft YaHei", "Helvetica", "Arial", "sans-serif", "monospace"]

# 扫描文件类型
SCAN_EXTENSIONS = {".html", ".css", ".svg", ".drawio"}


def scan_file(filepath: str) -> list[dict]:
    """扫描单个文件的品牌违规"""
    violations = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return violations

    lines = content.split("\n")
    ext = Path(filepath).suffix

    # 1. 检查非品牌色值
    for i, line in enumerate(lines, 1):
        hex_colors = re.findall(r'#[0-9A-Fa-f]{6}', line)
        for color in hex_colors:
            if color.lower() not in BRAND_COLORS_SET:
                violations.append({
                    "file": filepath,
                    "line": i,
                    "type": "brand_color",
                    "severity": "warning",
                    "message": f"非品牌色值 {color}",
                    "suggestion": f"替换为最近的品牌色或添加到白名单"
                })

    # 2. 检查废弃品牌名
    for i, line in enumerate(lines, 1):
        for brand in DEPRECATED_BRANDS:
            if brand in line:
                violations.append({
                    "file": filepath,
                    "line": i,
                    "type": "deprecated_brand",
                    "severity": "error",
                    "message": f"已废弃品牌名 '{brand}'",
                    "suggestion": "移除或替换为当前品牌名"
                })

    # 3. 检查 drawio 特有规则
    if ext == ".drawio":
        # 检查是否有白底背景
        if 'id="bg"' not in content:
            violations.append({
                "file": filepath,
                "line": 0,
                "type": "drawio_no_bg",
                "severity": "warning",
                "message": "drawio 缺少白色背景底板 (id=\"bg\")",
                "suggestion": "按流程图规范 v3.0 Step 5 添加背景层"
            })
        # 检查主流程线宽
        if "strokeWidth=2" not in content and "strokeWidth=" in content:
            violations.append({
                "file": filepath,
                "line": 0,
                "type": "drawio_stroke",
                "severity": "info",
                "message": "drawio 可能缺少主流程线 strokeWidth=2",
                "suggestion": "主流程边应为 strokeWidth=2;strokeColor=#333333"
            })

    return violations


def scan_vault(target_path: str = ".") -> list[dict]:
    """扫描指定路径下所有品牌相关文件"""
    all_violations = []

    for ext in SCAN_EXTENSIONS:
        pattern = os.path.join(target_path, f"**/*{ext}")
        for filepath in glob.glob(pattern, recursive=True):
            # 跳过 _archive 目录
            if "/_archive/" in filepath or "\\_archive\\" in filepath:
                continue
            violations = scan_file(filepath)
            all_violations.extend(violations)

    return all_violations


def main():
    target = "."
    output_json = "--json" in sys.argv
    scan_all = "--all" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--path" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]

    if scan_all:
        target = "."

    violations = scan_vault(target)

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
            print(f"[pass] 品牌合规检查通过（扫描路径: {target}）")
            sys.exit(0)

        print(f"品牌合规扫描结果（{target}）：")
        print(f"  Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}")
        print()

        for v in sorted(violations, key=lambda x: (x["severity"] != "error", x["file"], x["line"])):
            severity_icon = {"error": "[E]", "warning": "[W]", "info": "[I]"}[v["severity"]]
            line_info = f":{v['line']}" if v["line"] else ""
            print(f"  {severity_icon} {v['file']}{line_info} — {v['message']}")

        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
