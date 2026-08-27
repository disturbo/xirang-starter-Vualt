#!/usr/bin/env python3
"""Verify an extracted XiRang package, including a safe post-Obsidian-open mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


VERSION = "9.7.2"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PACKAGE_ROOT / ".xirang/distribution/package-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(raw: str) -> Path:
    value = PurePosixPath(raw)
    if not raw or value.is_absolute() or ".." in value.parts or "." in value.parts or "\\" in raw:
        raise ValueError(f"unsafe manifest path: {raw!r}")
    return Path(*value.parts)


OBSIDIAN_MUTABLE_FILES = {
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".obsidian/community-plugins.json",
    ".obsidian/core-plugins.json",
    ".obsidian/templates.json",
    ".obsidian/types.json",
    ".obsidian/workspace.json",
}


def runtime_mutable(logical: str, plugin_ids: set[str]) -> bool:
    if logical in OBSIDIAN_MUTABLE_FILES:
        return True
    match = re.fullmatch(r"\.obsidian/plugins/([^/]+)/(?:data|markdown-states)\.json", logical)
    if match:
        return match.group(1) in plugin_ids
    return bool(re.search(r"(?:^|/)__pycache__/[^/]+\.pyc$", logical))


def verify(*, exact: bool = False) -> dict:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {"ok": False, "status": "manifest_invalid", "error": str(exc)}
    if manifest.get("schema_version") != 1 or manifest.get("version") != VERSION:
        return {"ok": False, "status": "manifest_invalid", "error": "version or schema mismatch"}
    expected: set[str] = set()
    findings: list[str] = []
    mutable_changes: list[str] = []
    for row in manifest.get("files") or []:
        try:
            relative = safe_relative(str(row.get("path") or ""))
        except (AttributeError, TypeError, ValueError) as exc:
            findings.append(str(exc))
            continue
        logical = relative.as_posix()
        if logical in expected:
            findings.append(f"duplicate:{logical}")
            continue
        expected.add(logical)
        path = PACKAGE_ROOT / relative
        if not path.is_file() or path.is_symlink():
            findings.append(f"missing-or-unsafe:{logical}")
            continue
        if path.stat().st_size != int(row.get("size", -1)) or sha256(path) != row.get("sha256"):
            if not exact and logical in OBSIDIAN_MUTABLE_FILES:
                mutable_changes.append(f"hash:{logical}")
            else:
                findings.append(f"hash:{logical}")
    actual = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    allowed = expected | {MANIFEST.relative_to(PACKAGE_ROOT).as_posix()}
    plugin_ids = {
        PurePosixPath(logical).parts[2]
        for logical in expected
        if logical.startswith(".obsidian/plugins/") and len(PurePosixPath(logical).parts) >= 4
    }
    for logical in sorted(actual - allowed):
        if not exact and runtime_mutable(logical, plugin_ids):
            mutable_changes.append(f"extra:{logical}")
        else:
            findings.append(f"extra:{logical}")
    for logical in sorted(allowed - actual):
        findings.append(f"missing:{logical}")
    return {
        "ok": not findings,
        "status": (
            "complete_verified" if not findings and not mutable_changes
            else "safe_open_verified" if not findings
            else "manifest_invalid"
        ),
        "version": VERSION,
        "root": str(PACKAGE_ROOT),
        "files": len(expected),
        "findings": findings,
        "allowed_runtime_changes": mutable_changes,
        "verification_strength": "exact_archive_closure" if not mutable_changes else "immutable_files_plus_registered_obsidian_runtime",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("auto", "exact"), default="auto")
    args = parser.parse_args()
    result = verify(exact=args.mode == "exact")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 2)
