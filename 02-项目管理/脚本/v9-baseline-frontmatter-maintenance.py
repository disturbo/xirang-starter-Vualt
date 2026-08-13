#!/usr/bin/env python3
"""为 lint 明确认定的缺失字段做确定性、可审计的元数据迁移。"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LINTER = ROOT / ".standards" / "frontmatter-lint.py"
FIELD_ORDER = ["title", "created", "status", "maturity", "version", "tags"]


def lint_targets() -> dict[Path, set[str]]:
    result = subprocess.run(
        [sys.executable, str(LINTER), "--vault-root", str(ROOT), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    targets: dict[Path, set[str]] = {}
    for item in payload["violations"]:
        if item["type"] != "fm_field_missing":
            continue
        match = re.search(r"'([^']+)'$", item["message"])
        if not match or match.group(1) not in FIELD_ORDER:
            continue
        path = (ROOT / item["file"]).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"拒绝处理 Vault 外路径：{path}")
        targets.setdefault(path, set()).add(match.group(1))
    return targets


def inferred_title(path: Path) -> str:
    title = path.stem
    if title.endswith(".excalidraw"):
        title = title[: -len(".excalidraw")]
    return title


def existing_fields(content: str) -> dict[str, str]:
    closing = content.find("\n---", 3)
    fields: dict[str, str] = {}
    for line in content[4:closing].splitlines():
        match = re.match(r"^(\w[\w_-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip('"\'')
    return fields


def inferred_created(path: Path, fields: dict[str, str]) -> str:
    for key in ("updated", "date", "last_updated", "observed_at", "recorded_at", "completed_at", "created_at"):
        match = re.match(r"(\d{4}-\d{2}-\d{2})", fields.get(key, ""))
        if match:
            return match.group(1)
    filename_date = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", path.stem)
    if filename_date:
        return "-".join(filename_date.groups())
    birthtime = getattr(path.stat(), "st_birthtime", path.stat().st_mtime)
    return datetime.fromtimestamp(birthtime).date().isoformat()


def inferred_status(path: Path) -> str:
    relative = path.relative_to(ROOT).as_posix()
    if relative.startswith("40-决策/"):
        return "accepted"
    if "/迭代/" in relative:
        return "in_progress"
    return "active"


def inferred_version(path: Path, fields: dict[str, str]) -> str:
    source = f"{path.stem} {fields.get('title', '')}"
    match = re.search(r"(?i)(?:^|[-_\s])v?(\d+(?:\.\d+)+)", source)
    return match.group(1) if match else "1.0"


def inferred_value(path: Path, field: str, fields: dict[str, str]) -> str:
    if field == "title":
        return json.dumps(inferred_title(path), ensure_ascii=False)
    if field == "created":
        return inferred_created(path, fields)
    if field == "status":
        return inferred_status(path)
    if field == "maturity":
        return "stable"
    if field == "version":
        return json.dumps(inferred_version(path, fields), ensure_ascii=False)
    if field == "tags":
        relative = path.relative_to(ROOT).as_posix()
        tag = next((value for prefix, value in {
            "00-MOC/": "MOC",
            "02-项目管理/": "项目管理",
            "10-项目/": "项目文档",
            "20-资料/": "资料",
            "30-规范/": "规范",
            "40-决策/": "决策",
            "50-经验/": "经验",
            "60-归档/": "归档",
        }.items() if relative.startswith(prefix)), "Vault")
        return json.dumps([tag], ensure_ascii=False)
    raise RuntimeError(f"不支持的字段：{field}")


def patched_content(path: Path, missing: set[str]) -> tuple[str, dict[str, str]]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise RuntimeError(f"缺少可修复的 frontmatter：{path}")
    lines = content.splitlines()
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise RuntimeError(f"frontmatter 未闭合：{path}") from exc
    fields = existing_fields(content)
    additions = {field: inferred_value(path, field, fields) for field in FIELD_ORDER if field in missing}
    for field in reversed(FIELD_ORDER):
        if field in additions:
            lines.insert(closing, f"{field}: {additions[field]}")
    return "\n".join(lines) + ("\n" if content.endswith("\n") else ""), additions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写入；默认只预览")
    args = parser.parse_args()

    targets = lint_targets()
    field_counts = {field: 0 for field in FIELD_ORDER}
    samples = []
    for path, missing in sorted(targets.items()):
        content, additions = patched_content(path, missing)
        for field in additions:
            field_counts[field] += 1
        if len(samples) < 12:
            samples.append({"path": str(path.relative_to(ROOT)), "additions": additions})
        if args.apply:
            path.write_text(content, encoding="utf-8")
    print(json.dumps({
        "mode": "apply" if args.apply else "check",
        "target_count": len(targets),
        "field_counts": field_counts,
        "samples": samples,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
