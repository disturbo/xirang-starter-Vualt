#!/usr/bin/env python3
"""Lightweight checks for the LLM Wiki module-entry convention.

Flags:
  --strict            Treat warnings as failures.
  --non-placeholder   Validate maturity vs actual content depth.
                      (教训库 E-20260512: 假信号 → 非骨架摘要必须有 流程/字段/状态)
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


VAULT = Path("$VAULT_ROOT")
SANDBOX_PAGES = Path("$VAULT_ROOT/../sandbox/prototype/pages")
MODULE_ROOT = VAULT / "10-项目" / "{项目名}"

MODULE_PATTERN = re.compile(r"^\d{2}-.+")
WIKILINK_PATTERN = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
ALIAS_PATTERN = re.compile(r"^aliases:\s*\[([^\]]*)\]", re.MULTILINE)
PROTO_PATTERN = re.compile(r"`([a-z][a-z0-9_-]+/[a-z0-9_.-]+\.html)`")

REQUIRED_SECTIONS = [
    "## 当前状态",
    "## 交付物清单",
    "## 原型映射",
    "## 待办",
]

# maturity 五档枚举及其等级
MATURITY_LEVELS = {
    "骨架占位": 0,
    "业务理解已完成": 1,
    "业务理解+流程已完成": 2,
    "已设计方案": 3,
    "已发布PRD": 4,
}

# 资料摘要深度校验关键词（教训库要求的"三节"）
DEPTH_KEYWORDS = ["流程", "字段", "状态"]
DEPTH_MIN_LINES_LEVEL2 = 80  # 业务理解+流程已完成 最低行数
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def discover_modules() -> list[str]:
    return sorted(
        path.name
        for path in MODULE_ROOT.iterdir()
        if path.is_dir() and MODULE_PATTERN.match(path.name)
    )


def build_note_index() -> set[str]:
    index: set[str] = set()
    for path in VAULT.rglob("*.md"):
        if ".obsidian" in path.parts or ".git" in path.parts:
            continue
        rel = path.relative_to(VAULT).with_suffix("")
        index.add(path.stem)
        index.add(str(rel))
        index.add(str(rel).replace("\\", "/"))
    return index


def link_exists(target: str, note_index: set[str]) -> bool:
    normalized = target.strip().strip("/")
    if not normalized:
        return True
    if normalized in note_index:
        return True
    if any(item.endswith(f"/{normalized}") for item in note_index):
        return True
    if normalized.endswith(".md"):
        without_suffix = normalized[:-3]
        return (
            without_suffix in note_index
            or any(item.endswith(f"/{without_suffix}") for item in note_index)
            or (VAULT / normalized).exists()
        )
    return False


def extract_frontmatter_field(text: str, field: str) -> str | None:
    """从 YAML frontmatter 中提取指定字段的值。"""
    m = FRONTMATTER_PATTERN.match(text)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def check_readme(module: str, note_index: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    readme = MODULE_ROOT / module / "README.md"
    if not readme.exists():
        return [f"{module}: missing README.md"], warnings

    text = readme.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"{module}: missing section {section}")

    for match in PROTO_PATTERN.finditer(text):
        rel = match.group(1)
        if not (SANDBOX_PAGES / rel).exists():
            errors.append(f"{module}: prototype path not found: {rel}")

    for match in WIKILINK_PATTERN.finditer(text):
        target = match.group(1)
        if not link_exists(target, note_index):
            errors.append(f"{module}: broken wikilink [[{target}]]")

    if "资料摘要" in text and not (MODULE_ROOT / module / "资料摘要.md").exists():
        warnings.append(f"{module}: README mentions 资料摘要 but module has no 资料摘要.md")

    if "PRD | 已有" in text and not (MODULE_ROOT / module / "PRD.md").exists():
        warnings.append(f"{module}: README says PRD exists but PRD.md is missing")

    return errors, warnings


def check_maturity(module: str) -> tuple[list[str], list[str]]:
    """--non-placeholder 模式：校验 maturity 与实际内容的一致性。"""
    errors: list[str] = []
    warnings: list[str] = []
    readme = MODULE_ROOT / module / "README.md"
    if not readme.exists():
        return errors, warnings

    text = readme.read_text(encoding="utf-8")
    maturity = extract_frontmatter_field(text, "maturity")

    # 1. maturity 字段必须存在
    if maturity is None:
        errors.append(f"{module}: missing frontmatter field 'maturity'")
        return errors, warnings

    # 2. maturity 值必须是五档之一
    level = MATURITY_LEVELS.get(maturity)
    if level is None:
        errors.append(
            f"{module}: invalid maturity '{maturity}' "
            f"(valid: {', '.join(MATURITY_LEVELS)})"
        )
        return errors, warnings

    # 3. 骨架占位 → 跳过内容深度校验
    if level == 0:
        return errors, warnings

    # --- Level >= 1: 业务理解已完成 ---
    summary_path = MODULE_ROOT / module / "资料摘要.md"
    if not summary_path.exists():
        warnings.append(
            f"{module}: maturity='{maturity}' but 资料摘要.md missing"
        )
        summary_text = ""
        summary_lines = 0
    else:
        summary_text = summary_path.read_text(encoding="utf-8")
        summary_lines = len(summary_text.splitlines())

    # --- Level >= 2: 业务理解+流程已完成 ---
    if level >= 2:
        if summary_lines < DEPTH_MIN_LINES_LEVEL2:
            errors.append(
                f"{module}: maturity='{maturity}' but 资料摘要 only "
                f"{summary_lines} lines (need >={DEPTH_MIN_LINES_LEVEL2})"
            )
        # 教训库要求的"三节"关键词检查
        hits = [kw for kw in DEPTH_KEYWORDS if kw in summary_text]
        if len(hits) < 2:
            errors.append(
                f"{module}: maturity='{maturity}' but 资料摘要 missing "
                f"depth keywords (found: {hits}, need >=2 of {DEPTH_KEYWORDS})"
            )

    # --- Level >= 3: 已设计方案 ---
    if level >= 3:
        has_design = (MODULE_ROOT / module / "设计方案.md").exists()
        has_prd = (MODULE_ROOT / module / "PRD.md").exists()
        if not has_design and not has_prd:
            errors.append(
                f"{module}: maturity='{maturity}' but neither "
                f"设计方案.md nor PRD.md exists"
            )

    # --- Level >= 4: 已发布PRD ---
    if level >= 4:
        prd_path = MODULE_ROOT / module / "PRD.md"
        if not prd_path.exists():
            errors.append(
                f"{module}: maturity='已发布PRD' but PRD.md missing"
            )

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM Wiki module-entry convention checker."
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat warnings as failures.",
    )
    parser.add_argument(
        "--non-placeholder", action="store_true",
        help="Validate maturity vs actual content depth "
             "(e.g. 非骨架摘要 must have 流程/字段/状态).",
    )
    args = parser.parse_args()

    modules = discover_modules()
    note_index = build_note_index()
    errors: list[str] = []
    warnings: list[str] = []

    for module in modules:
        module_errors, module_warnings = check_readme(module, note_index)
        errors.extend(module_errors)
        warnings.extend(module_warnings)

        if args.non_placeholder:
            mat_errors, mat_warnings = check_maturity(module)
            errors.extend(mat_errors)
            warnings.extend(mat_warnings)

    if warnings:
        print("LLM Wiki check warnings:")
        for warning in warnings:
            print(f"  ⚠ {warning}")
        print()

    if errors or (args.strict and warnings):
        print("LLM Wiki check FAILED:")
        for error in errors:
            print(f"  ✗ {error}")
        if args.strict and warnings:
            print("  (--strict: warnings treated as failures)")
        return 1

    print("LLM Wiki check passed. ✓")
    print(f"  modules: {len(modules)}")
    print(f"  required sections: {len(REQUIRED_SECTIONS)}")
    print(f"  warnings: {len(warnings)}")
    if args.non_placeholder:
        # 统计 maturity 分布
        dist: dict[str, int] = {}
        for mod in modules:
            readme = MODULE_ROOT / mod / "README.md"
            if readme.exists():
                text = readme.read_text(encoding="utf-8")
                mat = extract_frontmatter_field(text, "maturity")
                if mat:
                    dist[mat] = dist.get(mat, 0) + 1
        print("  maturity distribution:")
        for mat_name in MATURITY_LEVELS:
            count = dist.get(mat_name, 0)
            if count:
                bar = "█" * count
                print(f"    {mat_name}: {count} {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
