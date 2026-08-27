#!/usr/bin/env python3
"""Check the portable Vault's internal links and required XiRang semantics."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+\.md(?:#[^)]+)?)\)")


def check(root: Path) -> dict:
    findings: list[str] = []
    markdown = sorted(root.rglob("*.md"))
    by_stem: dict[str, list[Path]] = {}
    for path in markdown:
        by_stem.setdefault(path.stem, []).append(path)

    def resolve(current: Path, raw: str) -> bool:
        target = unquote(raw.split("|", 1)[0].split("#", 1)[0]).strip()
        if not target:
            return True
        logical = Path(target if Path(target).suffix else target + ".md")
        candidates = [root / logical, current.parent / logical]
        if any(path.is_file() for path in candidates):
            return True
        return logical.suffix == ".md" and len(by_stem.get(logical.stem, [])) == 1

    for path in markdown:
        text = path.read_text(encoding="utf-8")
        for raw in WIKILINK.findall(text):
            if not resolve(path, raw):
                findings.append(f"wikilink:{path.relative_to(root)}:{raw}")
        for raw in MARKDOWN_LINK.findall(text):
            target = raw.split("#", 1)[0]
            if "://" not in target and not resolve(path, target):
                findings.append(f"markdown-link:{path.relative_to(root)}:{raw}")

    flow = root / "30-规范/流程图绘制规范.md"
    flow_text = flow.read_text(encoding="utf-8") if flow.is_file() else ""
    if "飞书" not in flow_text or "会议纪要" not in flow_text:
        findings.append("semantics:feishu-meeting-flowchart")
    human_flow = root / "30-规范/面向人的五步协作视图.md"
    if not human_flow.is_file() or (root / "30-规范/Agent任务五阶段工作流.md").exists():
        findings.append("semantics:human-five-step-view")
    method = root / "50-经验/Agent协作方法论/息壤方法论-V9.md"
    method_text = method.read_text(encoding="utf-8") if method.is_file() else ""
    for phrase in ("面向人的五步协作视图", "机器运行阶段", "最小息壤包"):
        if phrase not in method_text:
            findings.append(f"semantics:methodology:{phrase}")
    return {"ok": not findings, "markdown_files": len(markdown), "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT / "payload")
    args = parser.parse_args()
    result = check(args.root.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
