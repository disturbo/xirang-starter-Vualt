#!/usr/bin/env python3
"""Verify that every portable XiRang standard has one explicit source mapping."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTABLE_ROOTS = (
    Path("payload/30-规范"),
    Path("payload/50-经验/Agent协作方法论"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(root: Path, manifest_path: Path) -> dict:
    findings: list[str] = []
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "findings": [f"manifest:{exc}"]}
    mappings = document.get("mappings") if document.get("schema_version") == 1 else None
    if not isinstance(mappings, list):
        return {"ok": False, "findings": ["manifest:schema"]}
    expected = {
        path.relative_to(root).as_posix()
        for directory in PORTABLE_ROOTS
        for path in (root / directory).glob("*")
        if path.is_file() and path.name != ".gitkeep"
    }
    targets: dict[str, dict] = {}
    for row in mappings:
        if not isinstance(row, dict):
            findings.append("mapping:not-object")
            continue
        source = str(row.get("source") or "")
        target = str(row.get("target") or "")
        if not source or not target or target in targets:
            findings.append(f"mapping:invalid:{target}")
            continue
        targets[target] = row
        path = root / target
        if not path.is_file():
            findings.append(f"target:missing:{target}")
        elif sha256(path) != str(row.get("target_sha256") or ""):
            findings.append(f"target:hash:{target}")
    missing = sorted(expected - set(targets))
    extra = sorted(set(targets) - expected)
    findings.extend(f"mapping:missing:{path}" for path in missing)
    findings.extend(f"mapping:extra:{path}" for path in extra)
    return {
        "ok": not findings,
        "mappings": len(targets),
        "expected": len(expected),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else root / "manifests/portable-standards.json"
    result = check(root, manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
