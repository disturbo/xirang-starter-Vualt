#!/usr/bin/env python3
"""Safely add the portable Obsidian and workspace-Skill layer to a target Vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = PACKAGE_ROOT / ".xirang/distribution/obsidian-presets"
BASELINES = PACKAGE_ROOT / ".xirang/distribution/extras-baselines.json"
FILE_ROOTS = (
    Path(".skills"),
    Path(".obsidian/plugins"),
    Path(".obsidian/themes"),
    Path(".obsidian/snippets"),
)
MERGED_CONFIGS = (
    Path(".obsidian/app.json"),
    Path(".obsidian/appearance.json"),
    Path(".obsidian/community-plugins.json"),
    Path(".obsidian/core-plugins.json"),
    Path(".obsidian/templates.json"),
)
ADD_IF_MISSING = (
    Path(".obsidian/types.json"),
    Path(".obsidian/workspace.json"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def has_symlink_ancestor(target: Path, relative: Path) -> bool:
    current = target
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def source_files() -> list[Path]:
    result: list[Path] = []
    for root in FILE_ROOTS:
        source_root = PACKAGE_ROOT / root
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"source contains symlink: {path.relative_to(PACKAGE_ROOT)}")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                result.append(path.relative_to(PACKAGE_ROOT))
    return result


def portable_presets() -> dict[Path, object]:
    result: dict[Path, object] = {}
    if not PRESET_ROOT.is_dir() or PRESET_ROOT.is_symlink():
        raise RuntimeError("portable Obsidian preset directory is missing or unsafe")
    for source in sorted(PRESET_ROOT.glob("*.json")):
        plugin_id = source.stem
        manifest = PACKAGE_ROOT / ".obsidian/plugins" / plugin_id / "manifest.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise RuntimeError(f"preset has no bundled plugin: {plugin_id}")
        value = read_json(source)
        if not isinstance(value, dict):
            raise RuntimeError(f"preset must be an object: {plugin_id}")
        result[Path(".obsidian/plugins") / plugin_id / "data.json"] = value
    return result


def known_prior_hashes() -> dict[str, set[str]]:
    value = read_json(BASELINES)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("portable extras baseline manifest is invalid")
    result: dict[str, set[str]] = {}
    for baseline in value.get("baselines") or []:
        if not isinstance(baseline, dict) or not baseline.get("version") or not isinstance(baseline.get("files"), dict):
            raise RuntimeError("portable extras baseline row is invalid")
        for logical, checksum in baseline["files"].items():
            relative = Path(str(logical))
            if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", str(checksum)):
                raise RuntimeError(f"portable extras baseline path/hash is invalid: {logical}")
            if not (PACKAGE_ROOT / relative).is_file():
                raise RuntimeError(f"portable extras baseline points to missing current file: {logical}")
            result.setdefault(relative.as_posix(), set()).add(str(checksum))
    return result


def ordered_union(existing: list, bundled: list) -> list:
    result = list(existing)
    for item in bundled:
        if item not in result:
            result.append(item)
    return result


def merge_config(relative: Path, existing: object, bundled: object) -> object:
    logical = relative.as_posix()
    if logical == ".obsidian/community-plugins.json":
        if not isinstance(existing, list) or not isinstance(bundled, list):
            raise ValueError("community-plugins.json must be an array")
        return ordered_union(existing, bundled)
    if not isinstance(existing, dict) or not isinstance(bundled, dict):
        raise ValueError(f"{logical} must be an object")
    merged = dict(existing)
    if logical == ".obsidian/appearance.json":
        for key, value in bundled.items():
            if key == "enabledCssSnippets":
                merged[key] = ordered_union(list(existing.get(key) or []), list(value or []))
            else:
                merged.setdefault(key, value)
    elif logical == ".obsidian/app.json":
        for key, value in bundled.items():
            if key == "userIgnoreFilters":
                merged[key] = ordered_union(list(existing.get(key) or []), list(value or []))
            else:
                merged.setdefault(key, value)
    elif logical == ".obsidian/core-plugins.json":
        for key, value in bundled.items():
            merged.setdefault(key, value)
    elif logical == ".obsidian/templates.json":
        for key, value in bundled.items():
            merged.setdefault(key, value)
    return merged


def plan(target: Path) -> dict:
    additions: list[str] = []
    current: list[str] = []
    conflicts: list[str] = []
    config_updates: dict[str, object] = {}
    preset_updates: dict[str, object] = {}
    preset_preserved: list[str] = []
    managed_updates: list[str] = []
    prior_hashes = known_prior_hashes()
    for relative in source_files():
        source = PACKAGE_ROOT / relative
        destination = target / relative
        logical = relative.as_posix()
        if has_symlink_ancestor(target, relative):
            conflicts.append(f"symlink:{logical}")
        elif not destination.exists():
            additions.append(logical)
        elif destination.is_file() and not destination.is_symlink():
            destination_hash = sha256(destination)
            if sha256(source) == destination_hash:
                current.append(logical)
            elif destination_hash in prior_hashes.get(logical, set()):
                managed_updates.append(logical)
            else:
                conflicts.append(f"content:{logical}")
        else:
            conflicts.append(f"type:{logical}")
    for relative in ADD_IF_MISSING:
        source = PACKAGE_ROOT / relative
        destination = target / relative
        logical = relative.as_posix()
        if has_symlink_ancestor(target, relative):
            conflicts.append(f"symlink:{logical}")
        elif not destination.exists():
            additions.append(logical)
        elif destination.is_file() and not destination.is_symlink():
            current.append(logical)
        else:
            conflicts.append(f"type:{logical}")
    for relative in MERGED_CONFIGS:
        source = PACKAGE_ROOT / relative
        destination = target / relative
        logical = relative.as_posix()
        if has_symlink_ancestor(target, relative):
            conflicts.append(f"symlink:{logical}")
            continue
        if not destination.exists():
            additions.append(logical)
            config_updates[logical] = read_json(source)
            continue
        if not destination.is_file() or destination.is_symlink():
            conflicts.append(f"type:{logical}")
            continue
        try:
            merged = merge_config(relative, read_json(destination), read_json(source))
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
            conflicts.append(f"invalid:{logical}:{type(exc).__name__}")
            continue
        if merged == read_json(destination):
            current.append(logical)
        else:
            config_updates[logical] = merged
    for relative, value in portable_presets().items():
        destination = target / relative
        logical = relative.as_posix()
        if has_symlink_ancestor(target, relative):
            conflicts.append(f"symlink:{logical}")
        elif not destination.exists():
            preset_updates[logical] = value
        elif destination.is_file() and not destination.is_symlink():
            try:
                read_json(destination)
            except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
                conflicts.append(f"invalid:{logical}:{type(exc).__name__}")
            else:
                preset_preserved.append(logical)
        else:
            conflicts.append(f"type:{logical}")
    return {
        "ok": not conflicts,
        "status": "ready" if not conflicts else "assistance_required",
        "target": str(target),
        "additions": sorted(set(additions)),
        "config_updates": sorted(config_updates),
        "managed_updates": sorted(managed_updates),
        "preset_additions": sorted(preset_updates),
        "preset_preserved": sorted(preset_preserved),
        "current_count": len(set(current)),
        "conflicts": sorted(set(conflicts)),
        "_config_values": config_updates,
        "_preset_values": preset_updates,
    }


def apply(target: Path) -> dict:
    prepared = plan(target)
    config_values = prepared.pop("_config_values")
    preset_values = prepared.pop("_preset_values")
    if not prepared["ok"]:
        return prepared
    additions = [Path(value) for value in prepared["additions"]]
    managed_updates = [Path(value) for value in prepared["managed_updates"]]
    if not additions and not managed_updates and not config_values and not preset_values:
        return {**prepared, "status": "current_verified", "backup": None}
    backup = target / ".xirang/install/extras-backups" / (
        datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    backup.mkdir(parents=True, exist_ok=False)
    added: list[Path] = []
    saved: list[Path] = []
    try:
        for logical in config_values:
            relative = Path(logical)
            destination = target / relative
            if destination.is_file():
                backup_path = backup / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_path)
                saved.append(relative)
        for relative in managed_updates:
            destination = target / relative
            backup_path = backup / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup_path)
            saved.append(relative)
        for relative in additions:
            source = PACKAGE_ROOT / relative
            destination = target / relative
            atomic_bytes(destination, source.read_bytes(), stat.S_IMODE(source.stat().st_mode) or 0o644)
            added.append(relative)
        for relative in managed_updates:
            source = PACKAGE_ROOT / relative
            atomic_bytes(target / relative, source.read_bytes(), stat.S_IMODE(source.stat().st_mode) or 0o644)
        for logical, value in preset_values.items():
            relative = Path(logical)
            destination = target / relative
            atomic_json(destination, value)
            added.append(relative)
        for logical, value in config_values.items():
            relative = Path(logical)
            destination = target / relative
            if relative not in added:
                atomic_json(destination, value)
        atomic_json(
            backup / "manifest.json",
            {
                "schema_version": 1,
                "target": str(target),
                "added": [path.as_posix() for path in added],
                "saved": [path.as_posix() for path in saved],
            },
        )
    except Exception:
        for relative in reversed(added):
            destination = target / relative
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
        for relative in saved:
            source = backup / relative
            atomic_bytes(target / relative, source.read_bytes(), stat.S_IMODE(source.stat().st_mode) or 0o644)
        raise
    return {**prepared, "status": "extras_installed", "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        result = {"ok": False, "status": "unsafe_target", "target": str(target)}
    else:
        target.mkdir(parents=True, exist_ok=True) if args.action == "apply" else None
        result = plan(target) if args.action == "plan" else apply(target)
    result.pop("_config_values", None)
    result.pop("_preset_values", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
