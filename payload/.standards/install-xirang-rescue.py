#!/usr/bin/env python3
"""Atomically deploy the governed Xi Rang rescue bundle to ~/.xirang/bin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = SOURCE_ROOT.parent
SOURCE_FILES = (
    "xirang_state.py",
    "xirang_recovery_roots.py",
    "xirang_state_migrate.py",
    "xirang_state_backup.py",
    "xirang-rescue.py",
)
ENTRYPOINT = "xirang-rescue.py"
INSTALL_METADATA = "xirang-rescue-install.json"
INCIDENT_HELPER = "xirang-state-rescue.py"
INCIDENT_MARKERS = (
    "2026-08-23 Xi Rang double-task incident",
    'BLOCKED_TASK = "T-20260823-210800-1e37"',
    'INVALID_SUCCESSOR = "T-20260823-213027-4485"',
)


class InstallError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def expected_metadata() -> dict:
    registry = WORKSPACE_ROOT / ".xirang/contract/recovery-roots.yaml"
    if not registry.is_file():
        raise InstallError(f"workspace recovery registry is missing: {registry}")
    return {
        "schema_version": 1,
        "workspace_id": workspace_id(WORKSPACE_ROOT),
        "workspace_root": str(WORKSPACE_ROOT),
        "registry_path": str(registry),
        "registry_sha256": sha256(registry),
        "files": {name: sha256(SOURCE_ROOT / name) for name in SOURCE_FILES},
    }


def metadata_bytes() -> bytes:
    return (json.dumps(expected_metadata(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def verify_incident_helper(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"cannot verify legacy incident helper: {path}: {exc}") from exc
    if not all(marker in content for marker in INCIDENT_MARKERS):
        raise InstallError(f"refusing to remove an unrecognized legacy helper: {path}")


def install(destination: Path) -> dict:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    actions: list[dict] = []
    expected = expected_metadata()

    # Dependencies first and the entrypoint last keep every visible entrypoint
    # either on its complete old bundle or complete new bundle.
    for name in (*SOURCE_FILES[:-1], INSTALL_METADATA, ENTRYPOINT):
        target = destination / name
        payload = metadata_bytes() if name == INSTALL_METADATA else (SOURCE_ROOT / name).read_bytes()
        mode = 0o700 if name == ENTRYPOINT else 0o600
        existed = target.exists()
        content_changed = not target.is_file() or target.read_bytes() != payload
        mode_changed = target.is_file() and (target.stat().st_mode & 0o777) != mode
        if content_changed:
            atomic_write(target, payload, mode)
        elif mode_changed:
            os.chmod(target, mode)
        if content_changed or mode_changed:
            actions.append({
                "path": str(target),
                "operation": "update" if existed else "add",
                "sha256": hashlib.sha256(payload).hexdigest(),
            })

    incident = destination / INCIDENT_HELPER
    if incident.exists() or incident.is_symlink():
        if not incident.is_file() or incident.is_symlink():
            raise InstallError(f"legacy incident helper is not a regular file: {incident}")
        verify_incident_helper(incident)
        incident.unlink()
        actions.append({"path": str(incident), "operation": "delete", "sha256": None})
    cache = destination / "__pycache__"
    if cache.is_dir():
        for bytecode in sorted(cache.glob("xirang-state-rescue.*.pyc")):
            bytecode.unlink()
            actions.append({"path": str(bytecode), "operation": "delete", "sha256": None})
        try:
            cache.rmdir()
        except OSError:
            pass

    result = check(destination)
    return {**result, "actions": actions}


def check(destination: Path) -> dict:
    destination = destination.expanduser().resolve()
    expected = expected_metadata()
    drift: list[str] = []
    for name, expected_hash in expected["files"].items():
        target = destination / name
        if not target.is_file() or sha256(target) != expected_hash:
            drift.append(name)
    metadata = destination / INSTALL_METADATA
    if not metadata.is_file() or metadata.read_bytes() != metadata_bytes():
        drift.append(INSTALL_METADATA)
    if (destination / INCIDENT_HELPER).exists():
        drift.append(INCIDENT_HELPER)
    cache = destination / "__pycache__"
    if cache.is_dir() and any(cache.glob("xirang-state-rescue.*.pyc")):
        drift.append("__pycache__/xirang-state-rescue.*.pyc")
    if drift:
        raise InstallError(f"rescue installation drift: {sorted(set(drift))}")
    return {
        "ok": True,
        "destination": str(destination),
        "workspace_id": expected["workspace_id"],
        "files": expected["files"],
        "incident_helper_retired": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify the Xi Rang rescue bundle")
    parser.add_argument("action", choices=("install", "check"))
    parser.add_argument("--destination", type=Path, default=Path.home() / ".xirang/bin")
    args = parser.parse_args()
    try:
        result = install(args.destination) if args.action == "install" else check(args.destination)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
