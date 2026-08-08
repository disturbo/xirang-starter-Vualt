#!/usr/bin/env python3
"""为 lint 明确认定缺少 title 的基线文件做最小化、可审计修复。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE_ROOT = ROOT / "10-项目" / "基线"
LINTER = ROOT / ".standards" / "frontmatter-lint.py"


def lint_targets() -> list[Path]:
    result = subprocess.run(
        ["python3", str(LINTER), "--path", str(BASELINE_ROOT), "--vault-root", str(ROOT), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    paths = {
        (ROOT / item["file"]).resolve()
        for item in payload["violations"]
        if item["severity"] == "error"
        and item["type"] == "fm_field_missing"
        and item["message"].endswith("'title'")
    }
    targets = sorted(paths)
    for path in targets:
        if not path.is_relative_to(BASELINE_ROOT.resolve()):
            raise RuntimeError(f"拒绝处理基线外路径：{path}")
    return targets


def inferred_title(path: Path) -> str:
    title = path.stem
    if title.endswith(".excalidraw"):
        title = title[: -len(".excalidraw")]
    return title


def patched_content(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise RuntimeError(f"缺少可修复的 frontmatter：{path}")
    lines = content.splitlines()
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise RuntimeError(f"frontmatter 未闭合：{path}") from exc
    if any(re.match(r"^title\s*:", line) for line in lines[1:closing]):
        raise RuntimeError(f"lint 与文件状态不一致，title 已存在：{path}")
    title_value = json.dumps(inferred_title(path), ensure_ascii=False)
    lines.insert(1, f"title: {title_value}")
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写入；默认只预览")
    args = parser.parse_args()

    targets = lint_targets()
    for path in targets:
        relative = path.relative_to(ROOT)
        print(f"{relative}\ttitle={inferred_title(path)}")
        if args.apply:
            path.write_text(patched_content(path), encoding="utf-8")
    print(json.dumps({"mode": "apply" if args.apply else "check", "target_count": len(targets)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
