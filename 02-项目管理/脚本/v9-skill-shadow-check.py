#!/usr/bin/env python3
"""Detect same-name skill copies that can shadow different versions across platforms."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path


DEFAULT_ROOTS = [
    Path(".skills"),
    Path.home() / ".skills-manager/skills",
    Path.home() / ".codex/skills",
    Path.home() / ".hermes/skills",
    Path.home() / ".openclaw/skills",
    Path.home() / ".workbuddy/skills",
    Path.home() / ".claude/skills",
    Path.home() / ".agents/skills",
    Path.home() / "Library/Application Support/hermes-desktop/skills",
    Path.home() / "Library/Application Support/reasonix/global-workspace/.agents/skills",
    Path.home() / "Library/Application Support/reasonix/global-workspace/.claude/skills",
]
VERSION_RE = re.compile(r"(?i)\bv?\d+\.\d+(?:\.\d+)?\b")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    description = ""
    name = path.parent.name
    version = ""
    shadow_group = ""
    variant = ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        frontmatter = text[4:end] if end >= 0 else ""
        for line in frontmatter.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("description:"):
                description = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("version:"):
                version = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("x-v9-shadow-group:"):
                shadow_group = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("x-v9-variant:"):
                variant = line.split(":", 1)[1].strip().strip('"\'')
    versions = [version] if version else VERSION_RE.findall(description)
    return {
        "name": name,
        "path": str(path.parent),
        "realpath": str(path.parent.resolve()),
        "sha256": sha256(path),
        "declared_versions": versions,
        "shadow_group": shadow_group,
        "variant": variant,
    }


def scan(roots: list[Path]) -> dict:
    by_name: dict[str, list[dict]] = {}
    scanned_roots: list[str] = []
    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        scanned_roots.append(str(root))
        for child in sorted(root.iterdir()):
            skill = child / "SKILL.md"
            if not skill.is_file():
                continue
            try:
                item = metadata(skill)
            except (OSError, UnicodeDecodeError):
                continue
            item["root"] = str(root)
            by_name.setdefault(str(item["name"]), []).append(item)

    findings: list[dict] = []
    for name, entries in sorted(by_name.items()):
        realpaths = {item["realpath"] for item in entries}
        if len(realpaths) <= 1:
            continue
        hashes = {item["sha256"] for item in entries}
        versions = {version for item in entries for version in item["declared_versions"]}
        groups = {item["shadow_group"] for item in entries}
        variants = [item["variant"] for item in entries]
        explicitly_partitioned = (
            len(groups) == 1
            and "" not in groups
            and len(versions) == 1
            and "" not in variants
            and len(set(variants)) == len(entries)
        )
        if explicitly_partitioned:
            continue
        if len(hashes) > 1 or len(versions) > 1:
            severity = "p1"
            rule = "SKILL_VERSION_SHADOW"
            message = f"同名 skill '{name}' 解析到不同实体且内容/版本不一致。"
        else:
            severity = "advisory"
            rule = "SKILL_DUPLICATE_COPY"
            message = f"同名 skill '{name}' 存在相同内容的多份实体副本；建议收敛为软链主副本。"
        findings.append({
            "severity": severity, "rule_id": rule, "object": name,
            "message": message, "detail": {"entries": entries},
        })
    count = lambda severity: sum(item["severity"] == severity for item in findings)
    return {
        "check": "v9-skill-shadow-check",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "roots": scanned_roots,
        "summary": {
            "total": len(findings), "p0": count("p0"), "p1": count("p1"),
            "advisory": count("advisory"),
            "worst": "p1" if count("p1") else ("advisory" if findings else None),
            "skills_scanned": sum(len(items) for items in by_name.values()),
            "unique_skills": len(by_name),
            "explicit_variant_groups": sum(
                1
                for entries in by_name.values()
                if len({item["realpath"] for item in entries}) > 1
                and len({item["shadow_group"] for item in entries}) == 1
                and "" not in {item["shadow_group"] for item in entries}
                and len({version for item in entries for version in item["declared_versions"]}) == 1
                and all(item["variant"] for item in entries)
                and len({item["variant"] for item in entries}) == len(entries)
            ),
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    roots = args.roots or DEFAULT_ROOTS
    report = scan(roots)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"skill shadow: {report['summary']}")
        for item in report["findings"]:
            print(f"[{item['severity']}] {item['rule_id']}: {item['object']}")
    blocking = report["summary"]["p1"] or (args.strict and report["summary"]["advisory"])
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
