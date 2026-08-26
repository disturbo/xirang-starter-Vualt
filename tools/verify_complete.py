#!/usr/bin/env python3
"""Verify the extracted XiRang complete-Vault package without changing it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath


VERSION = "9.7.0"
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


def verify() -> dict:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {"ok": False, "status": "manifest_invalid", "error": str(exc)}
    if manifest.get("schema_version") != 1 or manifest.get("version") != VERSION:
        return {"ok": False, "status": "manifest_invalid", "error": "version or schema mismatch"}
    expected: set[str] = set()
    findings: list[str] = []
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
            findings.append(f"hash:{logical}")
    actual = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    allowed = expected | {MANIFEST.relative_to(PACKAGE_ROOT).as_posix()}
    for logical in sorted(actual - allowed):
        findings.append(f"extra:{logical}")
    for logical in sorted(allowed - actual):
        findings.append(f"missing:{logical}")
    return {
        "ok": not findings,
        "status": "complete_verified" if not findings else "manifest_invalid",
        "version": VERSION,
        "root": str(PACKAGE_ROOT),
        "files": len(expected),
        "findings": findings,
    }


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 2)
