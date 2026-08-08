#!/usr/bin/env python3
"""Generate the EXAMPLE module registry from module README frontmatter.

The registry is a derived index. Module README.md frontmatter remains the
source of truth.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


VAULT = Path("$HOME/Desktop/obsidianVault")
MODULE_ROOT = VAULT / "10-项目" / "基线"
DEFAULT_OUTPUT = MODULE_ROOT / "module-registry.json"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
MODULE_DIR_RE = re.compile(r"^\d{2}-.+")
MATURITY_ORDER = [
    "骨架占位",
    "业务理解已完成",
    "业务理解+流程已完成",
    "已设计方案",
    "已发布PRD",
]
DONE_STATUSES = {"done", "已完成"}


def parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}

    data: dict[str, Any] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key.strip()] = [
                item.strip().strip('"').strip("'")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        else:
            data[key.strip()] = value.strip('"').strip("'")
    return data


def discover_readmes() -> list[Path]:
    return sorted(
        path / "README.md"
        for path in MODULE_ROOT.iterdir()
        if (
            path.is_dir()
            and MODULE_DIR_RE.match(path.name)
            and not path.name.startswith("00-")
            and (path / "README.md").exists()
        )
    )


def has_design_file(module_dir: Path) -> bool:
    return (module_dir / "设计方案.md").exists() or any(module_dir.glob("设计方案*.md"))


def build_module(readme: Path) -> dict[str, Any]:
    module_dir = readme.parent
    frontmatter = parse_frontmatter(readme)
    module_id = frontmatter.get("module_id", module_dir.name[:2])
    module_name = frontmatter.get("module_name", module_dir.name[3:])
    return {
        "module_id": str(module_id),
        "module_name": str(module_name),
        "directory": module_dir.name,
        "status": str(frontmatter.get("status", "")),
        "maturity": str(frontmatter.get("maturity", "")),
        "prd_status": str(frontmatter.get("prd_status", "")),
        "prototype_status": str(frontmatter.get("prototype_status", "")),
        "design_status": str(frontmatter.get("design_status", "")),
        "kb_layer": str(frontmatter.get("kb_layer", "")),
        "baseline_version": str(frontmatter.get("baseline_version", "")),
        "baseline_status": str(frontmatter.get("baseline_status", "")),
        "current_iteration": str(frontmatter.get("current_iteration", "")),
        "iteration_status": str(frontmatter.get("iteration_status", "")),
        "iteration_record": str(frontmatter.get("iteration_record", "")),
        "updated": str(frontmatter.get("updated", "")),
        "deliverables": {
            "readme": True,
            "summary": (module_dir / "资料摘要.md").exists(),
            "prd": (module_dir / "PRD.md").exists(),
            "design": has_design_file(module_dir),
        },
    }


def generated_at(modules: list[dict[str, Any]]) -> str:
    dates = sorted(
        str(module.get("updated", ""))
        for module in modules
        if re.match(r"^\d{4}-\d{2}-\d{2}$", str(module.get("updated", "")))
    )
    if not dates:
        return "unknown"
    return f"{dates[-1]}T00:00:00+08:00"


def build_registry() -> dict[str, Any]:
    modules = [build_module(readme) for readme in discover_readmes()]
    module_ids = {module["module_id"] for module in modules}
    numeric_ids = sorted(int(module_id) for module_id in module_ids if module_id.isdigit())
    max_id = max(numeric_ids, default=0)
    expected_ids = [f"{num:02d}" for num in range(1, max_id + 1)]
    temporary_modules = sorted(path.name for path in MODULE_ROOT.glob("TBD-*") if path.is_dir())

    maturity_counts = Counter(module["maturity"] for module in modules)
    status_counts = Counter(module["status"] for module in modules)
    iteration_status_counts = Counter(module["iteration_status"] for module in modules)

    return {
        "_meta": {
            "schema_version": "2026-06-11",
            "generated_by": "generate_module_registry.py",
            "generated_at": generated_at(modules),
            "source": "10-项目/基线/*/README.md frontmatter",
            "module_count": len(modules),
            "temporary_modules": temporary_modules,
            "excluded_or_unassigned_module_ids": [
                module_id for module_id in expected_ids if module_id not in module_ids
            ],
            "note": "本文件为派生索引；权威状态以各模块 README.md frontmatter 为准。缺号不代表遗漏，需结合业务范围确认。",
        },
        "summary": {
            "maturity_distribution": {
                level: maturity_counts.get(level, 0) for level in MATURITY_ORDER
            },
            "status_distribution": dict(sorted(status_counts.items())),
            "iteration_status_distribution": dict(sorted(iteration_status_counts.items())),
            "prd_done": sum(
                1
                for module in modules
                if module["prd_status"] in DONE_STATUSES or module["deliverables"]["prd"]
            ),
            "prototype_done": sum(
                1 for module in modules if module["prototype_status"] in DONE_STATUSES
            ),
            "summary_ready": sum(1 for module in modules if module["deliverables"]["summary"]),
        },
        "modules": modules,
    }


def dumps_registry(registry: dict[str, Any]) -> str:
    return json.dumps(registry, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate 10-项目/基线/module-registry.json from README frontmatter."
    )
    parser.add_argument("--write", action="store_true", help="Write the registry file.")
    parser.add_argument("--check", action="store_true", help="Fail if the registry file is stale.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path.")
    args = parser.parse_args()

    content = dumps_registry(build_registry())

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != content:
            print(f"[fail] registry stale: {args.output}", file=sys.stderr)
            return 1
        print(f"[pass] registry up to date: {args.output}")
        return 0

    if args.write:
        args.output.write_text(content, encoding="utf-8")
        print(f"[write] {args.output}")
        return 0

    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
