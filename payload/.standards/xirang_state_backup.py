#!/usr/bin/env python3
"""Snapshot, restore, and projection drift checks for the Xi Rang StateStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xirang_state import StateStore
from xirang_recovery_roots import (
    RecoveryRootError,
    load_registry,
    recovery_layouts,
    require_registered,
    select_layout,
)
from xirang_state_migrate import (
    CutoverStateStore,
    CUTOVER_SQLITE,
    MigrationError,
    database_health,
    load_cutover_sentinel,
    metadata_get,
    migration_lock,
    projection_drift,
    refresh_events_projection,
    require_active,
    state_database,
)


SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_SIDECAR_POLICY = "forbidden"
RECOVERY_REGISTRY = Path(".xirang/contract/recovery-roots.yaml")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def _workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def _production_root(database: Path | None, root: Path | None = None) -> Path | None:
    """Return a governed workspace only when the database binding proves it."""
    candidates = [root.expanduser().resolve()] if root is not None else []
    module_root = Path(__file__).resolve().parents[1]
    if module_root not in candidates:
        candidates.append(module_root)
    if database is None:
        return candidates[0] if root is not None and (candidates[0] / RECOVERY_REGISTRY).is_file() else None
    resolved = database.expanduser().resolve()
    for candidate in candidates:
        registry = candidate / RECOVERY_REGISTRY
        runtime = Path.home() / ".xirang/workspaces" / _workspace_id(candidate) / "state/state.sqlite3"
        if registry.is_file() and resolved == runtime.resolve():
            return candidate
    return None


def _recovery_context(
    *, database: Path | None = None, root: Path | None = None, required_bytes: int = 0,
    for_write: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    workspace = _production_root(database, root)
    if workspace is None:
        return None
    registry_path = workspace / RECOVERY_REGISTRY
    registry = load_registry(registry_path)
    layout = select_layout(
        registry_path, workspace, required_bytes=required_bytes,
        initialize_fallback=for_write,
    )
    return workspace, registry, layout


def _registered_snapshot_manifest(
    snapshot: Path, context: tuple[Path, dict[str, Any], dict[str, Any]],
) -> Path:
    _, registry, layout = context
    snapshot = snapshot.expanduser().resolve()
    require_registered(snapshot, registry, kind="objects")
    for tier_layout in recovery_layouts(registry).values():
        objects = Path(tier_layout["objects"]).resolve()
        try:
            relative = snapshot.relative_to(objects)
        except ValueError:
            continue
        manifest = Path(tier_layout["manifests"]).resolve() / relative
        return manifest.with_suffix(manifest.suffix + ".manifest.json")
    raise MigrationError(f"快照不在任何登记 objects 根：{snapshot}; selected={layout['tier']}")


def _require_registered_kind(
    path: Path, context: tuple[Path, dict[str, Any], dict[str, Any]] | None, kind: str,
) -> None:
    if context is None:
        return
    try:
        require_registered(path, context[1], kind=kind)
    except RecoveryRootError as exc:
        raise MigrationError(str(exc)) from exc


def sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    return Path(str(path) + "-wal"), Path(str(path) + "-shm")


def require_no_sqlite_sidecars(path: Path) -> None:
    present = [str(item) for item in sqlite_sidecars(path) if item.exists()]
    if present:
        raise MigrationError(f"快照存在未纳管 SQLite sidecar：{present}")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file_and_parent(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def atomic_copy_bytes(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_raw)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def remove_database_family(database: Path) -> None:
    for item in (database, *sqlite_sidecars(database)):
        item.unlink(missing_ok=True)
    fsync_directory(database.parent)


def capture_forensic_database(
    database: Path, destination: Path, *, manifest_destination: Path | None = None,
) -> dict[str, Any]:
    if not database.is_file():
        raise MigrationError(f"损坏数据库取证源不存在：{database}")
    artifacts: list[dict[str, Any]] = []
    for source, suffix in (
        (database, ""),
        (Path(str(database) + "-wal"), "-wal"),
        (Path(str(database) + "-shm"), "-shm"),
    ):
        if not source.is_file():
            continue
        target = Path(str(destination) + suffix)
        atomic_copy_bytes(source, target)
        artifacts.append({
            "source": str(source),
            "artifact": str(target),
            "sha256": sha256_file(target),
            "size": target.stat().st_size,
        })
    manifest = {
        "schema_version": 1,
        "artifact_type": "forensic_database_bytes",
        "recoverable_snapshot": False,
        "database": str(database),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "artifacts": artifacts,
    }
    manifest_path = manifest_destination or destination.with_suffix(destination.suffix + ".forensic.json")
    atomic_json(manifest_path, manifest)
    manifest["manifest"] = str(manifest_path)
    return manifest


def restore_forensic_database(database: Path, forensic: dict[str, Any]) -> None:
    remove_database_family(database)
    for artifact in forensic.get("artifacts", []):
        source = Path(str(artifact["artifact"]))
        if sha256_file(source) != artifact.get("sha256"):
            raise MigrationError(f"取证工件 Hash 漂移：{source}")
        atomic_copy_bytes(source, Path(str(artifact["source"])))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_file_preimage(
    source: Path, *, object_root: Path, manifest_root: Path,
    recovery_id: str, logical_path: str,
) -> dict[str, Any]:
    """Store one file pre-image by content hash and emit an immutable manifest."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", recovery_id):
        raise MigrationError("recovery_id 含不安全字符")
    source = source.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise MigrationError(f"pre-image 必须是现存普通文件：{source}")
    digest = sha256_file(source)
    target = object_root.expanduser().resolve() / digest
    if target.exists() and (not target.is_file() or sha256_file(target) != digest):
        raise MigrationError(f"恢复对象 Hash 冲突：{target}")
    if not target.exists():
        atomic_copy_bytes(source, target)
    manifest = {
        "schema_version": 1,
        "artifact_type": "file_preimage",
        "recovery_id": recovery_id,
        "logical_path": logical_path,
        "source": str(source),
        "object": str(target),
        "sha256": digest,
        "size": source.stat().st_size,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }
    manifest_path = manifest_root.expanduser().resolve() / f"{recovery_id}.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        binding_keys = ("artifact_type", "recovery_id", "logical_path", "source", "object", "sha256", "size")
        if any(existing.get(key) != manifest.get(key) for key in binding_keys):
            raise MigrationError(f"recovery_id 已绑定不同 pre-image：{recovery_id}")
        manifest = existing
    else:
        atomic_json(manifest_path, manifest)
    return {**manifest, "manifest": str(manifest_path)}


def verify_file_preimage(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_type") != "file_preimage":
        raise MigrationError("不是 file_preimage manifest")
    object_path = Path(str(manifest.get("object") or "")).expanduser().resolve()
    if not object_path.is_file() or sha256_file(object_path) != manifest.get("sha256"):
        raise MigrationError("pre-image 对象缺失或 Hash 漂移")
    if object_path.name != manifest.get("sha256"):
        raise MigrationError("pre-image 对象不是内容寻址路径")
    return {**manifest, "manifest": str(manifest_path), "ok": True}


def restore_file_preimage(manifest_path: Path, destination: Path) -> dict[str, Any]:
    """Restore only into an absent path; never overwrite a newer worktree file."""
    verified = verify_file_preimage(manifest_path)
    destination = destination.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise MigrationError(f"恢复目标已经存在，拒绝覆盖：{destination}")
    atomic_copy_bytes(Path(verified["object"]), destination)
    if sha256_file(destination) != verified["sha256"]:
        raise MigrationError("恢复后 Hash 校验失败")
    return {"ok": True, "destination": str(destination), "sha256": verified["sha256"]}


def normalize_snapshot_database(path: Path) -> None:
    require_no_sqlite_sidecars(path)
    connection = sqlite3.connect(path)
    try:
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise MigrationError(f"快照无法切换为自包含 DELETE journal：{mode}")
        connection.commit()
        result = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if result != ["ok"]:
            raise MigrationError(f"快照 DELETE journal 转换后完整性检查失败：{result}")
    finally:
        connection.close()
    require_no_sqlite_sidecars(path)
    fsync_file_and_parent(path)


def inspect_snapshot_database(path: Path) -> dict[str, Any]:
    require_no_sqlite_sidecars(path)
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
            journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            schema = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            runtime = connection.execute(
                "SELECT value_json FROM runtime_meta WHERE key='backend_active'"
            ).fetchone()
            marker = connection.execute(
                "SELECT value_json FROM migration_metadata WHERE key='backend_active'"
            ).fetchone()
            migration = connection.execute(
                "SELECT value_json FROM migration_metadata WHERE key='migration_state'"
            ).fetchone()
            workspace_rows = connection.execute(
                "SELECT workspace_id FROM tasks UNION SELECT workspace_id FROM events"
            ).fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"快照只读检查失败：{path}: {exc}") from exc
    workspaces = sorted({str(row[0]) for row in workspace_rows if row[0]})
    if len(workspaces) > 1:
        raise MigrationError(f"快照包含多个 workspace_id：{workspaces}")
    return {
        "quick_check": quick,
        "integrity_check": integrity,
        "journal_mode": journal,
        "state_schema_version": int(schema or 0),
        "state_active": json.loads(runtime[0]) is True if runtime else False,
        "backend_active": json.loads(marker[0]) if marker else {},
        "migration_state": json.loads(migration[0]) if migration else {},
        "workspace_id": workspaces[0] if workspaces else None,
    }


def projection_snapshot(store: StateStore) -> list[dict[str, Any]]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT path, kind, sha256, rendered_at FROM projection_manifest ORDER BY path"
        ).fetchall()
    return [dict(row) for row in rows]


def snapshot_authority(store: StateStore, declared_database: Path | None = None) -> dict[str, Any]:
    """Authorize an active backend or the exact successfully completed shadow DB."""
    actual_database = store.path.resolve()
    declared_database = (declared_database or actual_database).resolve()
    health = database_health(actual_database)
    backend = metadata_get(store, "backend_active")
    if isinstance(backend, dict) and backend.get("active") is True:
        require_active(store, expected_database=declared_database)
        return {"mode": "active", "backend_active": backend, **health}

    migration_state = metadata_get(store, "migration_state")
    expected_database = str(declared_database)
    if not isinstance(backend, dict) or backend.get("active") is not False:
        raise MigrationError("pre-finalize 备份被拒绝: backend_active 不是 shadow 未激活状态")
    if backend.get("mode") != "shadow" or backend.get("database") != expected_database:
        raise MigrationError(
            "pre-finalize 备份被拒绝: backend_active 数据库路径漂移: "
            f"expected={expected_database}, actual={backend.get('database')!r}"
        )
    if not isinstance(migration_state, dict):
        raise MigrationError("pre-finalize 备份被拒绝: 缺少 migration_state")
    if migration_state.get("phase") != "shadow" or migration_state.get("status") != "succeeded":
        raise MigrationError(
            "pre-finalize 备份被拒绝: 最新 shadow 未成功: "
            f"phase={migration_state.get('phase')!r}, status={migration_state.get('status')!r}"
        )
    if migration_state.get("database") != expected_database:
        raise MigrationError(
            "pre-finalize 备份被拒绝: migration_state 数据库路径漂移: "
            f"expected={expected_database}, actual={migration_state.get('database')!r}"
        )
    return {
        "mode": "shadow",
        "backend_active": backend,
        "migration_state": migration_state,
        **health,
    }


def create_snapshot(
    store: StateStore, destination: Path, *, root: Path | None = None,
) -> dict[str, Any]:
    context = _recovery_context(
        database=store.path, root=root,
        required_bytes=store.path.stat().st_size if store.path.is_file() else 0,
        for_write=True,
    )
    _require_registered_kind(destination, context, "objects")
    authority = snapshot_authority(store)
    require_no_sqlite_sidecars(destination)
    destination = store.backup(destination)
    normalize_snapshot_database(destination)
    snapshot_health = inspect_snapshot_database(destination)
    migration_state = snapshot_health["migration_state"]
    workspace_root = migration_state.get("root") if isinstance(migration_state, dict) else None
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "sidecar_policy": SNAPSHOT_SIDECAR_POLICY,
        "journal_mode": "delete",
        "database": str(store.path.resolve()),
        "snapshot": str(destination),
        "snapshot_sha256": sha256_file(destination),
        "quick_check": snapshot_health["quick_check"],
        "integrity": snapshot_health["integrity_check"],
        "state_schema_version": snapshot_health["state_schema_version"],
        "workspace_root": workspace_root,
        "workspace_id": snapshot_health["workspace_id"],
        "backend_active": metadata_get(store, "backend_active"),
        "source_authority": authority,
        "projections": projection_snapshot(store),
    }
    manifest_path = _registered_snapshot_manifest(destination, context) if context else sidecar(destination)
    manifest["manifest"] = str(manifest_path)
    manifest["recovery_tier"] = context[2]["tier"] if context else "fixture_or_legacy"
    atomic_json(manifest_path, manifest)
    fsync_file_and_parent(destination)
    return manifest


def verify_snapshot(
    snapshot: Path,
    expected_database: Path | None = None,
    expected_workspace_id: str | None = None,
    *, root: Path | None = None,
) -> dict[str, Any]:
    snapshot = snapshot.expanduser().resolve()
    context = _recovery_context(database=expected_database, root=root)
    _require_registered_kind(snapshot, context, "objects")
    manifest_path = _registered_snapshot_manifest(snapshot, context) if context else sidecar(snapshot)
    _require_registered_kind(manifest_path, context, "manifests")
    if not snapshot.is_file() or not manifest_path.is_file():
        raise MigrationError("快照或 manifest 不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise MigrationError("快照 manifest schema 不受支持")
    if manifest.get("sidecar_policy") != SNAPSHOT_SIDECAR_POLICY:
        raise MigrationError("快照 manifest 未声明 sidecar forbidden")
    require_no_sqlite_sidecars(snapshot)
    current = sha256_file(snapshot)
    if current != manifest.get("snapshot_sha256"):
        raise MigrationError("快照 Hash 与 manifest 不一致")
    declared_database_raw = manifest.get("database")
    if not isinstance(declared_database_raw, str) or not declared_database_raw:
        raise MigrationError("快照 manifest 缺少源数据库路径")
    declared_database = Path(declared_database_raw).resolve()
    if expected_database is not None and declared_database != expected_database.resolve():
        raise MigrationError(
            "快照源数据库路径漂移: "
            f"expected={expected_database.resolve()}, actual={declared_database}"
        )
    inspected = inspect_snapshot_database(snapshot)
    if inspected["quick_check"] != ["ok"] or inspected["integrity_check"] != ["ok"]:
        raise MigrationError("快照完整性检查失败")
    if inspected["journal_mode"] != "delete":
        raise MigrationError("快照不是自包含 DELETE journal 工件")
    if inspected["state_schema_version"] != manifest.get("state_schema_version"):
        raise MigrationError("快照 schema 与 manifest 不一致")
    if inspected["workspace_id"] != manifest.get("workspace_id"):
        raise MigrationError("快照 workspace 与 manifest 不一致")
    if expected_workspace_id and inspected["workspace_id"] != expected_workspace_id:
        raise MigrationError(
            f"快照 workspace 漂移: expected={expected_workspace_id}, actual={inspected['workspace_id']}"
        )
    backend = inspected["backend_active"]
    marker_active = isinstance(backend, dict) and backend.get("active") is True
    if marker_active != inspected["state_active"]:
        raise MigrationError("快照 activation 双侧不一致")
    marker_database = backend.get("database") if isinstance(backend, dict) else None
    if marker_active and (
        not marker_database or Path(str(marker_database)).expanduser().resolve() != declared_database
    ):
        raise MigrationError("快照 backend marker 数据库路径漂移")
    return {
        "ok": True,
        "snapshot": str(snapshot),
        "sha256": current,
        "quick_check": inspected["quick_check"],
        "integrity": inspected["integrity_check"],
        "source_mode": "active" if marker_active else "shadow",
        "workspace_id": inspected["workspace_id"],
        "workspace_root": manifest.get("workspace_root"),
        "manifest": str(manifest_path),
        "recovery_tier": manifest.get("recovery_tier"),
    }


def restore_snapshot(store: StateStore, snapshot: Path, *, root: Path | None = None) -> dict[str, Any]:
    context = _recovery_context(database=store.path, root=root, for_write=True)
    if context is None and root is not None and (root / RECOVERY_REGISTRY).is_file():
        raise MigrationError("恢复目标无法绑定到当前工作区登记恢复根")
    verified = verify_snapshot(
        snapshot, expected_database=store.path, root=root
    )
    if verified["source_mode"] != "active":
        raise MigrationError("恢复源必须是双侧激活的 active snapshot，shadow snapshot 被拒绝")
    sentinel = load_cutover_sentinel(store.path)
    if (
        not isinstance(sentinel, dict)
        or sentinel.get("state") != CUTOVER_SQLITE
        or sentinel.get("legacy_import_disabled") is not True
    ):
        raise MigrationError("恢复目标缺少 SQLite 终态 cutover sentinel")
    expected_workspace = sentinel.get("workspace_id")
    verified = verify_snapshot(
        snapshot, expected_database=store.path, expected_workspace_id=expected_workspace, root=root
    )
    with migration_lock(store.path):
        exclusive_store = CutoverStateStore(store.path)
        verified = verify_snapshot(
            snapshot, expected_database=store.path, expected_workspace_id=expected_workspace, root=root
        )
        if verified["source_mode"] != "active":
            raise MigrationError("恢复源必须是双侧激活的 active snapshot，shadow snapshot 被拒绝")
        if context is None:
            rollback_root = store.path.parent / "rollback"
            rollback_manifest_root = rollback_root
        else:
            rollback_root = Path(context[2]["objects"]) / "pre-restore"
            rollback_manifest_root = Path(context[2]["manifests"]) / "pre-restore"
        rollback = rollback_root / (
            f"{store.path.stem}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        )
        _require_registered_kind(rollback, context, "objects")
        rollback_kind: str
        rollback_manifest: dict[str, Any] | None = None
        forensic: dict[str, Any] | None = None
        if not store.path.exists():
            rollback_kind = "missing_target"
        elif not store.path.is_file():
            raise MigrationError(f"恢复目标不是普通文件：{store.path}")
        else:
            try:
                require_active(exclusive_store, expected_database=exclusive_store.path)
                database_health(exclusive_store.path)
                rollback_kind = "recoverable_snapshot"
                rollback_manifest = create_snapshot(exclusive_store, rollback, root=root)
            except (MigrationError, sqlite3.Error, OSError, ValueError, TypeError):
                rollback_kind = "forensic_bytes"
                forensic_path = rollback.with_name(rollback.stem + ".forensic.sqlite3")
                forensic_manifest_path = rollback_manifest_root / (
                    forensic_path.name + ".forensic.json"
                )
                _require_registered_kind(forensic_path, context, "objects")
                _require_registered_kind(forensic_manifest_path, context, "manifests")
                forensic = capture_forensic_database(
                    store.path, forensic_path, manifest_destination=forensic_manifest_path,
                )
        try:
            exclusive_store._restore_from_backup_locked(snapshot)
            fsync_file_and_parent(exclusive_store.path)
            require_active(exclusive_store, expected_database=exclusive_store.path)
            database_health(exclusive_store.path)
            projection_result = None
            projection_root = root or (
                Path(verified["workspace_root"]).resolve() if verified.get("workspace_root") else None
            )
            if projection_root:
                projection_result = refresh_events_projection(projection_root, exclusive_store)
            drift = projection_drift(exclusive_store)
            if drift:
                raise MigrationError(f"恢复后投影漂移：{drift}")
            fsync_file_and_parent(exclusive_store.path)
        except Exception as exc:
            try:
                if rollback_kind == "recoverable_snapshot" and rollback_manifest is not None:
                    exclusive_store._restore_from_backup_locked(rollback)
                    fsync_file_and_parent(exclusive_store.path)
                    require_active(exclusive_store, expected_database=exclusive_store.path)
                    database_health(exclusive_store.path)
                elif rollback_kind == "missing_target":
                    remove_database_family(exclusive_store.path)
                elif forensic is not None:
                    restore_forensic_database(exclusive_store.path, forensic)
                else:
                    raise MigrationError("缺少可用的失败回滚工件")
            except Exception as rollback_exc:
                raise MigrationError(
                    f"恢复失败且自动回滚失败: restore={exc}; rollback={rollback_exc}"
                ) from rollback_exc
            raise MigrationError(f"恢复后校验失败，已自动回滚：{exc}") from exc
    return {
        **verified,
        "restored_to": str(store.path),
        "rollback_kind": rollback_kind,
        "rollback_snapshot": str(rollback) if rollback_manifest is not None else None,
        "rollback_snapshot_sha256": (
            rollback_manifest["snapshot_sha256"] if rollback_manifest is not None else None
        ),
        "forensic_rollback_artifact": forensic,
        "projection_refresh": projection_result,
        "projection_drift": drift,
        "ok": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤 SQLite 快照与恢复")
    parser.add_argument("action", choices=(
        "snapshot", "verify", "restore", "drift",
        "snapshot-file", "verify-file", "restore-file",
    ))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--object-root", type=Path)
    parser.add_argument("--manifest-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--recovery-id")
    parser.add_argument("--logical-path")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        registry_context = _recovery_context(
            root=root, for_write=args.action == "snapshot-file",
        )
        if args.action in {"snapshot-file", "verify-file", "restore-file"} and registry_context is None:
            raise MigrationError("file recovery CLI requires a workspace-bound recovery registry")
        if args.action == "snapshot-file":
            if not all((args.source, args.object_root, args.manifest_root, args.recovery_id, args.logical_path)):
                raise MigrationError("snapshot-file 参数不完整")
            _require_registered_kind(args.object_root, registry_context, "objects")
            _require_registered_kind(args.manifest_root, registry_context, "manifests")
            result = snapshot_file_preimage(
                args.source, object_root=args.object_root, manifest_root=args.manifest_root,
                recovery_id=args.recovery_id, logical_path=args.logical_path,
            )
            result["ok"] = True
        elif args.action == "verify-file":
            if not args.manifest:
                raise MigrationError("verify-file 需要 --manifest")
            _require_registered_kind(args.manifest, registry_context, "manifests")
            result = verify_file_preimage(args.manifest)
            _require_registered_kind(Path(result["object"]), registry_context, "objects")
        elif args.action == "restore-file":
            if not args.manifest or not args.destination:
                raise MigrationError("restore-file 需要 --manifest 与 --destination")
            _require_registered_kind(args.manifest, registry_context, "manifests")
            _require_registered_kind(args.destination, registry_context, "objects")
            verified_preimage = verify_file_preimage(args.manifest)
            _require_registered_kind(Path(verified_preimage["object"]), registry_context, "objects")
            result = restore_file_preimage(args.manifest, args.destination)
        else:
            database = state_database(root, explicit=args.database)
            store = StateStore(database)
            if args.action == "snapshot":
                if not args.output:
                    raise MigrationError("snapshot 需要 --output")
                result = create_snapshot(store, args.output.expanduser().resolve(), root=root)
                result["ok"] = True
            elif args.action == "verify":
                if not args.snapshot:
                    raise MigrationError("verify 需要 --snapshot")
                result = verify_snapshot(args.snapshot, expected_database=database, root=root)
            elif args.action == "restore":
                raise MigrationError(
                    "正式公共 CLI 禁止直接恢复 live StateStore；"
                    "只能由已授权维护/救援事务调用内部恢复原语"
                )
            else:
                result = {"ok": True, "database": str(database), "projection_drift": projection_drift(store)}
                result["ok"] = not result["projection_drift"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
