#!/usr/bin/env python3
"""
path-lint.py — 息壤 V8 路径规范校验
v1.0 · 2026-05-17 | 息壤 V8.5.0

扫描 vault 中文件的路径合规性（目录结构、命名规范、孤儿检测）。

用法：
  python3 .standards/path-lint.py [--path 10-项目/] [--json]
  python3 .standards/path-lint.py --all        # 扫描全 vault

检查项：
  1. 模块结构合规（每个顶级目录下的文件必须在正确分区）
  2. 文件命名规范（中文/英文/连字符，禁止空格和特殊字符）
  3. 深度检测（目录嵌套不超过 5 层）
  4. 孤儿文件检测（不属于任何模块的散落文件）
  5. 空目录检测
  6. 大文件预警（单文件 >500KB）
"""

import sys
import os
import re
import json
import glob
from pathlib import Path

# === 目录结构规范 ===

# 允许的顶级目录
ALLOWED_TOP_DIRS = {
    "00-MOC",       # 地图/索引
    "02-项目管理",  # 项目管理
    "10-项目",      # 项目实体
    "20-资料",      # 资料/领域知识
    "30-规范",      # 规范文档
    "40-决策",      # 决策记录
    "50-经验",      # 经验总结
    "60-归档",      # 归档
    "90-模板",      # 模板库
    "知识库工程化",  # 知识库治理工程
    "_temp",        # 临时目录
}

# 特殊文件（允许在根目录）
ALLOWED_ROOT_FILES = {
    "README.md",
    "CHANGELOG.md",
    ".gitignore",
    ".gitattributes",
}

# 隐藏目录（跳过扫描）
HIDDEN_DIRS = {".obsidian", ".git", ".trash", ".claude", ".standards", "node_modules", ".gbrain"}

# 最大目录深度
MAX_DEPTH = 5

# 允许超过默认深度的结构化产物路径
ALLOWED_DEEP_PATH_PATTERNS = [
    re.compile(r'(^|/)10-项目/基线/[^/]+/助手端原型/(pages|styles)/'),
]

# 大文件阈值（字节）
LARGE_FILE_THRESHOLD = 500 * 1024  # 500KB

# 不同文件类型/资料层的体积阈值。资料源图片和 PDF 是证据资产，阈值更高。
SOURCE_ASSET_PATTERN = re.compile(r'(^|/)20-资料/(业务文件|来源-原始PDF|参考系统|会议纪要|外部系统接口)/')
MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff", ".bmp", ".pdf"}

# 文件名禁止字符（除了常规中英文数字连字符下划线点号）
FORBIDDEN_CHARS_PATTERN = re.compile(r'[<>:"|?*\\]')

# 推荐命名模式
RECOMMENDED_NAME_PATTERN = re.compile(
    r'^[一-鿿\w][一-鿿\w\s\-\.()（）·]*$',
    re.UNICODE
)


def check_file_naming(filepath: str) -> list[dict]:
    """检查文件命名规范"""
    violations = []
    basename = Path(filepath).stem
    ext = Path(filepath).suffix

    # 检查禁止字符
    if FORBIDDEN_CHARS_PATTERN.search(basename):
        violations.append({
            "file": filepath,
            "line": 0,
            "type": "naming_forbidden_char",
            "severity": "error",
            "message": f"文件名含禁止字符: '{basename}'",
            "suggestion": "移除 < > : \" | ? * \\ 等特殊字符"
        })

    # 检查连续空格
    if "  " in basename:
        violations.append({
            "file": filepath,
            "line": 0,
            "type": "naming_double_space",
            "severity": "warning",
            "message": f"文件名含连续空格: '{basename}'",
            "suggestion": "使用单空格或连字符 - 替代"
        })

    # 检查首尾空格
    if basename != basename.strip():
        violations.append({
            "file": filepath,
            "line": 0,
            "type": "naming_trim",
            "severity": "error",
            "message": f"文件名首尾有空格: '{basename}'",
            "suggestion": "去除文件名首尾空格"
        })

    return violations


def check_depth(filepath: str, base_path: str = ".") -> list[dict]:
    """检查目录嵌套深度"""
    violations = []
    rel_path = os.path.relpath(filepath, base_path)
    rel_path_posix = Path(rel_path).as_posix()
    depth = len(Path(rel_path).parts)

    if any(pattern.search(rel_path_posix) for pattern in ALLOWED_DEEP_PATH_PATTERNS):
        return violations

    if depth > MAX_DEPTH:
        violations.append({
            "file": filepath,
            "line": 0,
            "type": "depth_exceeded",
            "severity": "warning",
            "message": f"目录嵌套深度 {depth} 层（上限 {MAX_DEPTH}）",
            "suggestion": f"考虑扁平化目录结构，当前路径: {rel_path}"
        })

    return violations


def check_large_file(filepath: str) -> list[dict]:
    """检查大文件"""
    violations = []
    try:
        size = os.path.getsize(filepath)
        rel_path_posix = Path(filepath).as_posix()
        ext = Path(filepath).suffix.lower()
        threshold = LARGE_FILE_THRESHOLD

        if SOURCE_ASSET_PATTERN.search(rel_path_posix) and ext in MEDIA_EXTENSIONS:
            threshold = 10 * 1024 * 1024
        elif ext == ".md":
            threshold = 1024 * 1024
        elif ext in MEDIA_EXTENSIONS:
            threshold = 2 * 1024 * 1024

        if size > threshold:
            size_kb = size / 1024
            violations.append({
                "file": filepath,
                "line": 0,
                "type": "large_file",
                "severity": "info",
                "message": f"大文件预警: {size_kb:.0f}KB（阈值 {threshold // 1024}KB）",
                "suggestion": "考虑拆分或压缩，大文件影响 vault 性能和同步"
            })
    except OSError:
        pass

    return violations


def check_orphan_root_files(vault_root: str = ".") -> list[dict]:
    """检查 vault 根目录散落文件"""
    violations = []

    for item in os.listdir(vault_root):
        full_path = os.path.join(vault_root, item)

        # 跳过隐藏目录和文件
        if item.startswith("."):
            continue

        # 检查根目录文件
        if os.path.isfile(full_path):
            if item not in ALLOWED_ROOT_FILES:
                violations.append({
                    "file": full_path,
                    "line": 0,
                    "type": "orphan_root",
                    "severity": "warning",
                    "message": f"根目录散落文件: '{item}'",
                    "suggestion": "移动到对应模块目录（90-inbox/ 或具体分区）"
                })

        # 检查顶级目录是否在允许列表
        if os.path.isdir(full_path):
            if item not in ALLOWED_TOP_DIRS:
                violations.append({
                    "file": full_path,
                    "line": 0,
                    "type": "unknown_top_dir",
                    "severity": "info",
                    "message": f"非标准顶级目录: '{item}'",
                    "suggestion": f"标准目录: {', '.join(sorted(ALLOWED_TOP_DIRS))}"
                })

    return violations


def find_empty_dirs(target_path: str = ".") -> list[dict]:
    """检测空目录"""
    violations = []

    for root, dirs, files in os.walk(target_path):
        # 跳过隐藏目录
        dirs[:] = [d for d in dirs if d not in HIDDEN_DIRS and not d.startswith(".")]

        if os.path.normpath(root) == os.path.normpath("./_temp"):
            continue

        if not dirs and not files:
            violations.append({
                "file": root,
                "line": 0,
                "type": "empty_dir",
                "severity": "info",
                "message": f"空目录: '{root}'",
                "suggestion": "删除空目录或添加内容"
            })

    return violations


def scan_vault(target_path: str = ".", vault_root: str = ".") -> list[dict]:
    """扫描指定路径"""
    all_violations = []

    # 1. 根目录检查（仅全 vault 扫描时）
    if target_path == "." or target_path == vault_root:
        all_violations.extend(check_orphan_root_files(vault_root))

    # 2. 遍历所有文件
    for root, dirs, files in os.walk(target_path):
        # 跳过隐藏目录
        dirs[:] = [d for d in dirs if d not in HIDDEN_DIRS and not d.startswith(".")]

        for fname in files:
            filepath = os.path.join(root, fname)

            # 跳过隐藏文件
            if fname.startswith("."):
                continue

            # 命名检查
            all_violations.extend(check_file_naming(filepath))

            # 深度检查
            all_violations.extend(check_depth(filepath, vault_root))

            # 大文件检查
            all_violations.extend(check_large_file(filepath))

    # 3. 空目录检查
    all_violations.extend(find_empty_dirs(target_path))

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
                "total_files": sum(1 for _ in glob.glob(os.path.join(target, "**/*"), recursive=True))
            },
            "violations": violations
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"[pass] 路径规范检查通过（扫描路径: {target}）")
            sys.exit(0)

        print(f"路径规范扫描结果（{target}）：")
        print(f"  Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}")
        print()

        for v in sorted(violations, key=lambda x: (x["severity"] != "error", x["file"])):
            severity_icon = {"error": "[E]", "warning": "[W]", "info": "[I]"}[v["severity"]]
            print(f"  {severity_icon} {v['file']} — {v['message']}")

        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
