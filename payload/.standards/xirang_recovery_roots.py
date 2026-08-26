#!/usr/bin/env python3
"""Registered recovery-root resolver and destructive-safe restore canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any


class RecoveryRootError(RuntimeError):
    pass


def _scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    return value


def load_registry(path: Path) -> dict[str, Any]:
    """Parse the deliberately small, mapping-only recovery registry."""
    result: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, result)]
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise RecoveryRootError(f"registry indentation error at line {number}")
        key, separator, value = raw.strip().partition(":")
        if not separator or not key:
            raise RecoveryRootError(f"registry syntax error at line {number}")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    required = {
        "primary.objects", "primary.manifests", "primary.audit", "fallback.root",
    }
    missing = []
    for dotted in required:
        cursor: Any = result
        for part in dotted.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if not cursor:
            missing.append(dotted)
    if missing:
        raise RecoveryRootError(f"registry missing fields: {missing}")
    return result


def registered_paths(registry: dict[str, Any]) -> dict[str, Path]:
    return {
        "primary.objects": Path(registry["primary"]["objects"]).expanduser().resolve(),
        "primary.manifests": Path(registry["primary"]["manifests"]).expanduser().resolve(),
        "primary.audit": Path(registry["primary"]["audit"]).expanduser().resolve(),
        "fallback.root": Path(registry["fallback"]["root"]).expanduser().resolve(),
    }


def recovery_layouts(registry: dict[str, Any]) -> dict[str, dict[str, Path]]:
    roots = registered_paths(registry)
    fallback = roots["fallback.root"]
    return {
        "primary": {
            "objects": roots["primary.objects"],
            "manifests": roots["primary.manifests"],
            "audit": roots["primary.audit"],
        },
        "fallback": {
            "objects": fallback / "objects",
            "manifests": fallback / "manifests",
            "audit": fallback / "audit",
        },
    }


def forbidden_paths(registry: dict[str, Any]) -> dict[str, Path]:
    raw = registry.get("forbidden_roots") or {}
    return {
        str(name): Path(str(value)).expanduser().resolve()
        for name, value in raw.items()
        if value
    } if isinstance(raw, dict) else {}


def require_registered(
    candidate: Path, registry: dict[str, Any], *, kind: str | None = None,
) -> str:
    resolved = candidate.expanduser().resolve()
    if kind is not None and kind not in {"objects", "manifests", "audit"}:
        raise RecoveryRootError(f"unknown recovery path kind: {kind}")
    candidates = (
        {
            f"{tier}.{entry_kind}": root
            for tier, layout in recovery_layouts(registry).items()
            for entry_kind, root in layout.items()
            if kind is None or entry_kind == kind
        }
        if kind is not None
        else registered_paths(registry)
    )
    for name, root in candidates.items():
        if resolved == root or root in resolved.parents:
            return name
    raise RecoveryRootError(f"unregistered recovery path rejected: {resolved}")


def _validate_layout(
    name: str, layout: dict[str, Path], registry: dict[str, Any], vault: Path,
    *, initialize: bool = False, required_bytes: int = 0,
) -> dict[str, Any]:
    minimum = max(int(registry.get("minimum_free_bytes") or 0), int(required_bytes))
    forbidden = forbidden_paths(registry)
    checks: dict[str, Any] = {}
    for kind, path in layout.items():
        if initialize and not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise RecoveryRootError(f"registered recovery root missing: {name}.{kind}={path}")
        if path == vault or vault in path.parents or path in vault.parents:
            raise RecoveryRootError(f"recovery root overlaps Vault: {name}.{kind}={path}")
        for forbidden_name, forbidden_path in forbidden.items():
            if path == forbidden_path or path in forbidden_path.parents or forbidden_path in path.parents:
                raise RecoveryRootError(
                    f"recovery root overlaps {forbidden_name}: {name}.{kind}={path}"
                )
        free = shutil.disk_usage(path).free
        if free < minimum:
            raise RecoveryRootError(f"insufficient free space: {name}.{kind}={free}")
        if not os.access(path, os.W_OK):
            raise RecoveryRootError(f"registered recovery root is not writable: {name}.{kind}")
        checks[kind] = {"path": str(path), "free_bytes": free, "writable": True}
    if len(set(layout.values())) != len(layout):
        raise RecoveryRootError(f"{name} recovery objects/manifests/audit roots must be distinct")
    return checks


def select_layout(
    registry_path: Path, vault: Path, *, required_bytes: int = 0,
    initialize_fallback: bool = False,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    vault = vault.expanduser().resolve()
    errors: dict[str, str] = {}
    for name, layout in recovery_layouts(registry).items():
        try:
            checks = _validate_layout(
                name, layout, registry, vault,
                initialize=name == "fallback" and initialize_fallback,
                required_bytes=required_bytes,
            )
            return {"tier": name, **layout, "checks": checks}
        except (OSError, RecoveryRootError) as exc:
            errors[name] = str(exc)
    raise RecoveryRootError(f"no registered recovery layout is usable: {errors}")


def _write_probe(directory: Path, payload: bytes) -> tuple[Path, str]:
    target = directory / f".xirang-recovery-canary-{secrets.token_hex(6)}"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != hashlib.sha256(payload).hexdigest():
        raise RecoveryRootError(f"canary hash mismatch: {target}")
    return target, digest


def doctor(registry_path: Path, vault: Path, *, drill: bool = False) -> dict[str, Any]:
    registry = load_registry(registry_path)
    vault = vault.expanduser().resolve()
    checks: dict[str, Any] = {}
    layouts = recovery_layouts(registry)
    for name, layout in layouts.items():
        checks[name] = _validate_layout(
            name, layout, registry, vault, initialize=name == "fallback",
        )

    drill_result: dict[str, Any] = {}
    if drill:
        payload = f"xirang-recovery-drill:{registry.get('workspace_id')}".encode()
        for name, layout in layouts.items():
            object_path = manifest_path = restore_path = None
            try:
                object_path, object_sha = _write_probe(layout["objects"], payload)
                manifest_payload = json.dumps({
                    "schema_version": 1, "object": str(object_path), "sha256": object_sha,
                    "workspace_id": registry.get("workspace_id"), "tier": name,
                }, ensure_ascii=False, sort_keys=True).encode()
                manifest_path, manifest_sha = _write_probe(layout["manifests"], manifest_payload)
                declared = json.loads(manifest_path.read_text(encoding="utf-8"))
                if declared["sha256"] != hashlib.sha256(object_path.read_bytes()).hexdigest():
                    raise RecoveryRootError("restore drill manifest/object mismatch")
                restore_path = layout["objects"] / f".xirang-restore-canary-{secrets.token_hex(6)}"
                if restore_path.exists():
                    raise RecoveryRootError("restore drill target unexpectedly exists")
                restore_path.write_bytes(object_path.read_bytes())
                with restore_path.open("rb") as handle:
                    os.fsync(handle.fileno())
                restored_sha = hashlib.sha256(restore_path.read_bytes()).hexdigest()
                if restored_sha != object_sha:
                    raise RecoveryRootError("restore drill output hash mismatch")
                drill_result[name] = {
                    "ok": True, "object_sha256": object_sha,
                    "manifest_sha256": manifest_sha, "restored_sha256": restored_sha,
                    "objects_root": str(layout["objects"]),
                    "manifests_root": str(layout["manifests"]),
                    "overwrite_policy": "absent_target_only",
                }
            finally:
                for item in (restore_path, manifest_path, object_path):
                    if item is not None and item.name.startswith((".xirang-recovery-canary-", ".xirang-restore-canary-")):
                        item.unlink(missing_ok=True)
    return {"ok": True, "registry": str(registry_path.resolve()), "checks": checks, "restore_drill": drill_result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("doctor", "require-registered"))
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args()
    registry = load_registry(args.registry)
    if args.action == "require-registered":
        if not args.path:
            raise SystemExit("require-registered needs --path")
        result = {"ok": True, "registration": require_registered(args.path, registry)}
    else:
        if not args.vault:
            raise SystemExit("doctor needs --vault")
        result = doctor(args.registry, args.vault, drill=args.drill)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecoveryRootError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
