#!/usr/bin/env python3
"""
vault-health-check.py — Obsidian Vault 健康检查脚本
用途：检测孤立文件、状态不一致、过时元数据
建议：每周运行一次，或在大批量修改后执行

用法：
  python3 02-项目管理/脚本/vault-health-check.py [--fix-dates]

选项：
  --fix-dates  自动将 frontmatter updated 同步为文件实际修改日期
"""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

VAULT = Path(".")
SKIP_DIRS = {".obsidian", ".claude", ".standards", ".git", ".prompt-src", ".skills", "node_modules"}
MODULE_ROOT = VAULT / "10-项目" / "基线"
ORPHAN_SKIP_PREFIXES = ("60-归档/冷归档/",)
SENSITIVE_VALUE_PATTERNS = (
    ("密码字段疑似明文口令", re.compile(r"(默认密码|VPN密码|密码)[^\n]{0,40}(`[^`\n]*(?:@|\d{4})[^`\n]*`|[A-Za-z]+@[A-Za-z0-9]+)")),
    ("微信推送 target 疑似真实 ID", re.compile(r"\b[\w-]+@im\.wechat\b")),
    ("微信推送 bot account 疑似真实 ID", re.compile(r"\b\d{8,}-im-bot\b")),
    ("URL query token 疑似明文令牌", re.compile(r"\btoken=[A-Za-z0-9._-]{16,}\b", re.IGNORECASE)),
)
SENSITIVE_SCAN_EXTENSIONS = {".csv", ".json", ".md"}
EXPECTED_REPEAT_STEMS = {
    "README",
    "_index",
    "_MOC",
    "_template",
    "PRD",
    "资料摘要",
    "知识沉淀",
    "设计方案",
}
TEMP_MODULE_PREFIXES = ("TBD-", "TMP-")
CRITICAL_LINK_FILES = (
    Path("00-MOC/🏠-Home.md"),
    Path("00-MOC/工作台.md"),
    Path("00-MOC/LLM-Wiki-MOC.md"),
    Path("00-MOC/Skill-Inventory.md"),
    Path("00-MOC/待办汇总.md"),
    Path("00-MOC/多智能体协作看板.md"),
    Path("00-MOC/项目运营笔记本.md"),
    Path("00-MOC/示例项目EXAMPLE-MOC.md"),
    Path("02-项目管理/_MOC.md"),
    Path("02-项目管理/任务卡/_MOC.md"),
    Path("02-项目管理/运行日志/_index.md"),
    Path("02-项目管理/项目文档/_index.md"),
    Path("02-项目管理/项目文档/示例项目EXAMPLE 项目首页.md"),
    Path("02-项目管理/项目文档/项目日历.md"),
    Path("20-资料/README.md"),
    Path("20-资料/业务文件/📋-资料索引.md"),
    Path("20-资料/会议纪要/需求调研纪要索引.md"),
    Path("20-资料/外部系统接口/外部系统接口索引.md"),
    Path("30-规范/README.md"),
    Path("30-规范/agent-paths.md"),
    Path("30-规范/agents-registry.md"),
    Path("50-经验/_MOC.md"),
    Path("知识库工程化/README.md"),
)
ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".docx",
    ".drawio",
    ".excalidraw",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pptx",
    ".svg",
    ".webp",
    ".xlsx",
}
PLACEHOLDER_LINK_HINTS = (
    "{{",
    "}}",
    "xxx",
    "YYYY",
    "MM",
    "DD",
    "wikilink",
    "文件名",
    "模块名",
    "路径/",
    "任务ID",
)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# --- 工具函数 ---

def iter_md_files(root=VAULT):
    """遍历 vault 中所有 .md 文件"""
    for path in root.rglob("*.md"):
        if any(part.startswith(".") or part in SKIP_DIRS for part in path.parts):
            continue
        yield path

def iter_files(root=VAULT):
    """遍历 vault 中所有公开文件。"""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") or part in SKIP_DIRS for part in path.parts):
            continue
        yield path

def parse_frontmatter(path):
    """解析 YAML frontmatter，返回 dict"""
    try:
        content = path.read_text(encoding="utf-8")
    except:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in content[3:end].strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm

def normalize_wikilink_target(raw_link):
    """从 wikilink 原文中抽出目标文件部分。"""
    target = raw_link.replace(r"\|", "|").strip()
    target = target.split("|", 1)[0]
    target = target.split("#", 1)[0]
    return target.strip()

def find_wikilinks(content):
    """提取文件中所有 wikilink 目标"""
    links = []
    for match in WIKILINK_RE.finditer(content):
        target = normalize_wikilink_target(match.group(1))
        if target:
            links.append(target)
    return links

def iter_wikilinks_with_lines(content):
    """逐个返回 wikilink 原文、目标与行号。"""
    for match in WIKILINK_RE.finditer(content):
        raw_link = match.group(1)
        target = normalize_wikilink_target(raw_link)
        line_no = content.count("\n", 0, match.start()) + 1
        yield raw_link, target, line_no

def build_stem_map():
    """构建文件名→路径的映射"""
    stems = {}
    for path in iter_md_files():
        rel = path.relative_to(VAULT)
        stem = str(rel)[:-3] if str(rel).endswith(".md") else str(rel)
        name = path.stem
        stems.setdefault(name, []).append(rel)
        stems.setdefault(str(stem), []).append(rel)
    return stems

def parse_inline_aliases(raw_aliases):
    """解析 frontmatter 中的 aliases 简写。"""
    if not raw_aliases:
        return []

    value = raw_aliases.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]

    aliases = []
    for item in value.split(","):
        alias = item.strip().strip('"').strip("'")
        if alias:
            aliases.append(alias)
    return aliases

def build_link_target_map():
    """构建关键链接检查所需的目标索引。"""
    targets = {}
    for path in iter_md_files():
        rel = path.relative_to(VAULT)
        rel_str = rel.as_posix()
        no_ext = rel_str[:-3] if rel_str.endswith(".md") else rel_str
        keys = {path.stem, no_ext}
        fm = parse_frontmatter(path)

        title = fm.get("title", "").strip()
        if title:
            keys.add(title)

        for alias in parse_inline_aliases(fm.get("aliases", "")):
            keys.add(alias)

        if path.name == "README.md" and rel.parent.as_posix() != ".":
            keys.add(rel.parent.as_posix())

        for key in keys:
            targets.setdefault(key, []).append(rel)
    return targets

def is_placeholder_link(target):
    """跳过模板/示例型占位链接。"""
    return any(hint in target for hint in PLACEHOLDER_LINK_HINTS)

def existing_file_candidates(target, source):
    """生成附件/显式文件链接的候选路径。"""
    clean = target.strip().strip("/")
    source_parent = source.parent
    return [
        VAULT / clean,
        VAULT / source_parent / clean,
    ]

def link_target_exists(target, source, target_map):
    """判断 wikilink 目标是否能解析到现有 Markdown 或附件。"""
    clean = target.strip().strip("/")
    if not clean:
        return True

    if clean.startswith(("http://", "https://", "mailto:")):
        return True

    no_ext = clean[:-3] if clean.endswith(".md") else clean
    source_relative = (source.parent / no_ext).as_posix()

    if no_ext in target_map or source_relative in target_map:
        return True

    suffix = Path(clean).suffix.lower()
    if suffix and suffix != ".md":
        if suffix in ATTACHMENT_EXTENSIONS:
            return any(candidate.exists() for candidate in existing_file_candidates(clean, source))
        return True

    return any((candidate / "README.md").exists() for candidate in existing_file_candidates(no_ext, source))

# --- 检查项 ---

def check_orphans():
    """检查完全孤立文件（零连接）"""
    print("\n## 1. 孤立文件检查")

    all_files = set()
    file_links = {}  # path -> outgoing links

    for path in iter_md_files():
        rel = path.relative_to(VAULT)
        if str(rel).startswith(ORPHAN_SKIP_PREFIXES):
            continue
        all_files.add(rel)
        try:
            content = path.read_text(encoding="utf-8")
            file_links[rel] = find_wikilinks(content)
        except:
            file_links[rel] = []

    stem_map = build_stem_map()
    incoming = set()

    for source, links in file_links.items():
        for link in links:
            link = link.rstrip("\\").strip()
            if link in stem_map:
                for target in stem_map[link]:
                    if target != source:
                        incoming.add(target)

    orphans = [f for f in all_files if f not in incoming and len(file_links.get(f, [])) == 0]

    if orphans:
        print(f"  发现 {len(orphans)} 个完全孤立文件：")
        for f in sorted(orphans):
            print(f"    - {f}")
    else:
        print("  ✅ 无完全孤立文件")

    return orphans

def check_status_consistency():
    """检查模块 README frontmatter 与实际文件的一致性"""
    print("\n## 2. 状态一致性检查")

    issues = []
    prd_file_expected_statuses = {"done", "review", "已完成", "评审中"}
    empty_statuses = {"", "N/A", "—"}
    if not MODULE_ROOT.exists():
        print("  ⚠️ 模块目录不存在")
        return issues

    for module_dir in sorted(MODULE_ROOT.iterdir()):
        if not module_dir.is_dir():
            continue
        readme = module_dir / "README.md"
        if not readme.exists():
            continue

        fm = parse_frontmatter(readme)
        module_name = fm.get("module_name", module_dir.name)

        # 检查 PRD 状态
        prd_status = fm.get("prd_status", "")
        prd_exists = (module_dir / "PRD.md").exists()

        if prd_status in prd_file_expected_statuses and not prd_exists:
            issues.append(f"  ❌ {module_dir.name}: prd_status={prd_status} 但 PRD.md 不存在")
        elif prd_exists and prd_status in empty_statuses:
            prd_lines = len((module_dir / "PRD.md").read_text(encoding="utf-8").split("\n"))
            if prd_lines > 50:
                issues.append(f"  ⚠️ {module_dir.name}: PRD.md 有 {prd_lines} 行但 prd_status={prd_status or '(空)'}")

    if issues:
        print(f"  发现 {len(issues)} 处不一致：")
        for i in issues:
            print(i)
    else:
        print("  ✅ 模块状态与实际文件一致")

    return issues

def check_stale_dates():
    """检查 frontmatter updated 日期是否严重滞后"""
    print("\n## 3. 日期新鲜度检查")

    stale = []
    threshold = timedelta(days=30)

    for path in iter_md_files(MODULE_ROOT):
        fm = parse_frontmatter(path)
        updated_str = fm.get("updated", "")
        if not updated_str:
            continue

        try:
            # 处理多种日期格式
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
                try:
                    updated_date = datetime.strptime(updated_str[:10], "%Y-%m-%d")
                    break
                except:
                    continue
            else:
                continue

            file_mtime = datetime.fromtimestamp(path.stat().st_mtime)
            gap = file_mtime - updated_date

            if gap > threshold:
                rel = path.relative_to(VAULT)
                stale.append((rel, updated_str, file_mtime.strftime("%Y-%m-%d"), gap.days))
        except:
            continue

    if stale:
        print(f"  发现 {len(stale)} 个文件 updated 日期滞后超过 30 天：")
        for rel, fm_date, actual, days in sorted(stale, key=lambda x: -x[3])[:10]:
            print(f"    - {rel}: frontmatter={fm_date}, 实际修改={actual} (差{days}天)")
    else:
        print("  ✅ 日期均在合理范围")

    return stale

def check_naming_conflicts():
    """检查同名文件冲突"""
    print("\n## 4. 命名冲突检查")

    name_map = {}
    for path in iter_md_files():
        name = path.stem
        name_map.setdefault(name, []).append(path.relative_to(VAULT))

    conflicts = {k: v for k, v in name_map.items() if len(v) >= 3 and k not in EXPECTED_REPEAT_STEMS}

    if conflicts:
        print(f"  {len(conflicts)} 组同名文件（≥3个）：")
        for name, paths in sorted(conflicts.items(), key=lambda x: -len(x[1])):
            print(f"    - '{name}' × {len(paths)}")
    else:
        print("  ✅ 无严重命名冲突")

    return conflicts

def check_module_registry():
    """检查模块 README 的编号登记是否唯一。"""
    print("\n## 5. 模块编号登记检查")

    issues = []
    module_ids = {}

    if not MODULE_ROOT.exists():
        print("  ⚠️ 模块目录不存在")
        return issues

    for module_dir in sorted(MODULE_ROOT.iterdir()):
        if not module_dir.is_dir():
            continue
        readme = module_dir / "README.md"
        if not readme.exists():
            continue

        fm = parse_frontmatter(readme)
        module_id = (fm.get("module_id") or fm.get("module_no") or "").strip()
        if not module_id:
            issues.append(f"  ❌ {module_dir.name}: 缺少 module_id/module_no")
            continue

        module_ids.setdefault(module_id, []).append(module_dir.name)

        if not (re.fullmatch(r"\d{2}", module_id) or module_id.startswith(TEMP_MODULE_PREFIXES)):
            issues.append(f"  ⚠️ {module_dir.name}: module_id={module_id} 不是两位数字或临时编号")

        prefix = module_dir.name.split("-", 1)[0]
        if re.fullmatch(r"\d{2}", prefix) and re.fullmatch(r"\d{2}", module_id) and prefix != module_id:
            issues.append(f"  ⚠️ {module_dir.name}: 目录编号 {prefix} 与 module_id={module_id} 不一致")

    for module_id, dirs in sorted(module_ids.items()):
        if len(dirs) > 1:
            issues.append(f"  ❌ module_id={module_id} 重复: {', '.join(dirs)}")

    if issues:
        print(f"  发现 {len(issues)} 处模块编号问题：")
        for issue in issues:
            print(issue)
    else:
        print("  ✅ 模块编号登记唯一")

    return issues

def check_critical_links():
    """检查关键入口页中的 wikilink 是否仍能解析。"""
    print("\n## 6. 关键入口链接检查")

    issues = []
    target_map = build_link_target_map()

    for rel in CRITICAL_LINK_FILES:
        path = VAULT / rel
        if not path.exists():
            issues.append(f"  ❌ 缺少关键入口文件: {rel}")
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            issues.append(f"  ❌ {rel}: 无法读取 ({exc})")
            continue

        for raw_link, target, line_no in iter_wikilinks_with_lines(content):
            if not target or is_placeholder_link(target):
                continue

            if r"\|" in raw_link:
                issues.append(f"  ❌ {rel}:{line_no}: 使用了转义管道 \\|，应写为 [[目标|别名]]")

            if not link_target_exists(target, rel, target_map):
                issues.append(f"  ❌ {rel}:{line_no}: 缺失链接目标 [[{target}]]")

    if issues:
        print(f"  发现 {len(issues)} 处关键入口链接问题：")
        for issue in issues:
            print(issue)
    else:
        print("  ✅ 关键入口链接可解析")

    return issues

def check_sensitive_values():
    """检查公开文本资料中是否残留疑似明文敏感值。"""
    print("\n## 7. 敏感值检查")

    issues = []
    for path in iter_files():
        if path.suffix.lower() not in SENSITIVE_SCAN_EXTENSIONS:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue

        rel = path.relative_to(VAULT)
        for line_no, line in enumerate(content.splitlines(), 1):
            for label, pattern in SENSITIVE_VALUE_PATTERNS:
                if pattern.search(line):
                    issues.append(f"  ❌ {rel}:{line_no}: {label}")

    if issues:
        print(f"  发现 {len(issues)} 处敏感信息残留：")
        for issue in issues:
            print(issue)
    else:
        print("  ✅ 未发现疑似明文敏感值")

    return issues

# --- 主函数 ---

def main():
    print("=" * 60)
    print("  Vault 健康检查报告")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  路径: {VAULT.resolve()}")
    print("=" * 60)

    orphans = check_orphans()
    status_issues = check_status_consistency()
    stale = check_stale_dates()
    conflicts = check_naming_conflicts()
    module_issues = check_module_registry()
    critical_link_issues = check_critical_links()
    sensitive_issues = check_sensitive_values()

    # 汇总
    print("\n" + "=" * 60)
    print("  汇总")
    print("=" * 60)
    total_issues = len(orphans) + len(status_issues) + len(stale) + len(conflicts) + len(module_issues) + len(critical_link_issues) + len(sensitive_issues)
    print(f"  孤立文件: {len(orphans)}")
    print(f"  状态不一致: {len(status_issues)}")
    print(f"  日期滞后: {len(stale)}")
    print(f"  命名冲突: {len(conflicts)} 组")
    print(f"  模块编号问题: {len(module_issues)}")
    print(f"  关键入口链接问题: {len(critical_link_issues)}")
    print(f"  敏感值问题: {len(sensitive_issues)}")
    print(f"  {'✅ Vault 健康' if total_issues == 0 else f'⚠️ 发现 {total_issues} 项问题需关注'}")

if __name__ == "__main__":
    main()
