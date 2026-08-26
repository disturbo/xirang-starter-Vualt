#!/usr/bin/env python3
"""Fail-closed Legacy to SQLite shadow migration for Xi Rang V3."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from xirang_state import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    StateConflict,
    StateStore,
    canonical_scopes,
    refresh_events_projection as refresh_state_events_projection,
    scope_covers,
)


MIGRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_sources (
    source_path TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_task_cards (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    source_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    card_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_manifest (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    rendered_at TEXT NOT NULL
);
"""

LIFECYCLE_MAP = {
    "draft": "draft",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "submitted": "submitted",
    "reviewing": "submitted",
    "changes_requested": "in_progress",
    "done": "completed",
    "completed": "completed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "historical": "legacy_unreviewed",
    "legacy_unreviewed": "legacy_unreviewed",
    "superseded": "superseded",
}
REVIEW_STATES = {
    "draft", "submitted", "reviewing", "accepted", "changes_requested",
    "canceled", "superseded", "legacy_unreviewed", "not_required",
}
RUNTIME_MAP = {
    "draft": "authorized",
    "in_progress": "implementing",
    "blocked": "blocked_external_dependency",
    "submitted": "submitted",
    "reviewing": "submitted",
    "changes_requested": "repairing",
    "done": "submitted",
    "completed": "submitted",
    "canceled": "canceled",
    "cancelled": "canceled",
    "historical": "archived",
    "legacy_unreviewed": "archived",
    "superseded": "archived",
}
HISTORICAL_TERMINAL_STATES = {"historical", "legacy_unreviewed", "superseded"}
HISTORICAL_TIMESTAMP_STATES = HISTORICAL_TERMINAL_STATES | {"canceled", "cancelled"}
HISTORICAL_REVIEW_MAP = {
    "historical": "legacy_unreviewed",
    "legacy_unreviewed": "legacy_unreviewed",
    "superseded": "superseded",
}
PROPOSAL_STATES = {"pending", "authorized", "consumed", "expired", "canceled"}
FOCUS_STATES = {"active", "consumed", "superseded", "expired"}
ZERO_TIME = "1970-01-01T00:00:00+00:00"
CUTOVER_SENTINEL_SCHEMA = 1
CUTOVER_LEGACY = "migration_legacy"
CUTOVER_FREEZE = "cutover_frozen"
CUTOVER_SQLITE = "sqlite"
CUTOVER_STATES = {CUTOVER_LEGACY, CUTOVER_FREEZE, CUTOVER_SQLITE}
_PROCESS_EXCLUSIVE_LOCKS: set[Path] = set()


class MigrationError(RuntimeError):
    pass


class NonAuthoritativeDatabase(MigrationError):
    """A caller selected a database outside the workspace authority binding."""

    code = "non_authoritative_database"

    def __init__(self, candidate: Path, authoritative: Path, *, source: str) -> None:
        self.candidate = Path(candidate).expanduser().resolve()
        self.authoritative = Path(authoritative).expanduser().resolve()
        self.source = source
        super().__init__(
            f"{self.code}: source={source}, candidate={self.candidate}, "
            f"authoritative={self.authoritative}"
        )


@dataclass(frozen=True)
class WorkspaceStateBinding:
    workspace_root: Path
    workspace_id: str
    runtime_dir: Path
    database: Path
    cutover_sentinel: Path
    cutover_lock: Path
    local_config: Path
    config_bound: bool


class CutoverStateStore(StateStore):
    """StateStore variant that reuses a process-held exclusive cutover fence."""

    def initialize(self) -> None:
        if cutover_lock_path(self.path) not in _PROCESS_EXCLUSIVE_LOCKS:
            super().initialize()
            return
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_version"
                ).fetchone()
                version = row["version"] if row else None
                if version is not None and int(version) > SCHEMA_VERSION:
                    raise StateConflict(
                        f"不支持的 schema_version: database={version}, runtime={SCHEMA_VERSION}"
                    )
                migrations = {
                    "user_events": {"bindings_json": "TEXT NOT NULL DEFAULT '{}'"},
                    "maintenance_proposals": {
                        "platform": "TEXT NOT NULL DEFAULT 'unknown'",
                        "additional_intents_json": "TEXT NOT NULL DEFAULT '[]'",
                    },
                    "tasks": {"metadata_json": "TEXT NOT NULL DEFAULT '{}'"},
                }
                for table, columns in migrations.items():
                    existing = {
                        item["name"]
                        for item in connection.execute(f"PRAGMA table_info({table})")
                    }
                    for column, declaration in columns.items():
                        if column not in existing:
                            connection.execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                            )
                if version is None or int(version) < SCHEMA_VERSION:
                    connection.execute(
                        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, now_iso()),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def _local_config(root: Path) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    path = root / ".xirang/local-config.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise MigrationError(f"local-config 损坏：{path}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"local-config 必须是对象：{path}")
    runtime = value.get("runtime_dir")
    if not runtime:
        raise MigrationError(f"local-config 缺少 runtime_dir：{path}")
    try:
        schema_version = int(value.get("schema_version") or 1)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"local-config schema_version 非法：{path}") from exc
    runtime_path = Path(str(runtime)).expanduser()
    if not runtime_path.is_absolute():
        raise MigrationError(f"local-config runtime_dir 必须是绝对路径：{path}")
    if schema_version >= 2:
        expected_root = str(root)
        expected_workspace = workspace_id(root)
        if value.get("workspace_root") != expected_root:
            raise MigrationError(
                "local-config workspace_root 漂移："
                f"expected={expected_root}, actual={value.get('workspace_root')!r}"
            )
        if value.get("workspace_id") != expected_workspace:
            raise MigrationError(
                "local-config workspace_id 漂移："
                f"expected={expected_workspace}, actual={value.get('workspace_id')!r}"
            )
    return value


def runtime_dir(root: Path, explicit: Path | None = None) -> Path:
    root = root.expanduser().resolve()
    config = _local_config(root)
    if config is not None:
        authoritative = Path(str(config["runtime_dir"])).expanduser().resolve()
        candidates = (
            ("runtime_argument", explicit),
            ("XIRANG_RUNTIME_DIR", os.environ.get("XIRANG_RUNTIME_DIR")),
        )
        for source, raw in candidates:
            if raw is None:
                continue
            candidate = Path(raw).expanduser().resolve()
            if candidate != authoritative:
                raise NonAuthoritativeDatabase(
                    candidate / "state/state.sqlite3",
                    authoritative / "state/state.sqlite3",
                    source=source,
                )
        return authoritative
    if explicit is not None:
        return explicit.expanduser().resolve()
    if value := os.environ.get("XIRANG_RUNTIME_DIR"):
        return Path(value).expanduser().resolve()
    return Path.home() / ".xirang/workspaces" / workspace_id(root)


def state_database(root: Path, runtime: Path | None = None, explicit: Path | None = None) -> Path:
    root = root.expanduser().resolve()
    selected_runtime = runtime_dir(root, runtime)
    authoritative = selected_runtime / "state/state.sqlite3"
    config = _local_config(root)
    candidates = (
        ("database_argument", explicit),
        ("XIRANG_STATE_DB", os.environ.get("XIRANG_STATE_DB")),
    )
    for source, raw in candidates:
        if raw is None:
            continue
        candidate = Path(raw).expanduser().resolve()
        if config is not None and candidate != authoritative:
            raise NonAuthoritativeDatabase(candidate, authoritative, source=source)
        return candidate
    return authoritative


def workspace_state_binding(
    root: Path,
    *,
    runtime: Path | None = None,
    database: Path | None = None,
) -> WorkspaceStateBinding:
    """Resolve the one StateStore binding declared by local-config and cutover paths.

    Unconfigured roots retain the migration bootstrap behaviour, while every
    configured production workspace fails closed on runtime/database overrides.
    """
    root = root.expanduser().resolve()
    config = _local_config(root)
    selected_runtime = runtime_dir(root, runtime)
    selected_database = state_database(root, selected_runtime, database)
    return WorkspaceStateBinding(
        workspace_root=root,
        workspace_id=workspace_id(root),
        runtime_dir=selected_runtime,
        database=selected_database,
        cutover_sentinel=cutover_sentinel_path(selected_database),
        cutover_lock=cutover_lock_path(selected_database),
        local_config=root / ".xirang/local-config.json",
        config_bound=config is not None,
    )


def discover_non_authoritative_databases(root: Path) -> list[Path]:
    """Find Vault-local StateStore files without opening or modifying them."""
    root = root.expanduser().resolve()
    binding = workspace_state_binding(root)
    control_root = root / ".xirang"
    if not control_root.is_dir():
        return []
    findings: list[Path] = []
    for candidate in control_root.rglob("state.sqlite3"):
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve()
            if resolved != binding.database:
                findings.append(resolved)
    return sorted(set(findings))


def cutover_sentinel_path(database: Path) -> Path:
    return Path(str(Path(database).expanduser().resolve()) + ".cutover.json")


def cutover_lock_path(database: Path) -> Path:
    return Path(str(Path(database).expanduser().resolve()) + ".cutover.lock")


def load_cutover_sentinel(database: Path) -> dict[str, Any] | None:
    path = cutover_sentinel_path(database)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise MigrationError(f"cutover sentinel 损坏：{path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CUTOVER_SENTINEL_SCHEMA:
        raise MigrationError(f"cutover sentinel schema 异常：{path}")
    state = payload.get("state")
    if state not in CUTOVER_STATES:
        raise MigrationError(f"cutover sentinel state 异常：{state!r}")
    recorded = payload.get("database")
    if not recorded or Path(str(recorded)).expanduser().resolve() != Path(database).expanduser().resolve():
        raise MigrationError(
            "cutover sentinel 数据库路径漂移："
            f"expected={Path(database).expanduser().resolve()}, actual={recorded!r}"
        )
    return payload


def publish_cutover_sentinel(
    database: Path,
    *,
    state: str,
    root: Path,
    legacy_import_disabled: bool,
    activated_at: str | None = None,
    lock_held: bool = False,
) -> dict[str, Any]:
    if state not in CUTOVER_STATES:
        raise MigrationError(f"非法 cutover state：{state}")
    database = Path(database).expanduser().resolve()
    root = Path(root).expanduser().resolve()
    lock = cutover_lock_path(database)
    if lock_held:
        if lock not in _PROCESS_EXCLUSIVE_LOCKS:
            raise MigrationError(f"cutover sentinel 声明复用但当前进程未持排他锁：{lock}")
        return _publish_cutover_sentinel_locked(
            database,
            state=state,
            root=root,
            legacy_import_disabled=legacy_import_disabled,
            activated_at=activated_at,
        )
    with migration_lock(database):
        return _publish_cutover_sentinel_locked(
            database,
            state=state,
            root=root,
            legacy_import_disabled=legacy_import_disabled,
            activated_at=activated_at,
        )


def _publish_cutover_sentinel_locked(
    database: Path,
    *,
    state: str,
    root: Path,
    legacy_import_disabled: bool,
    activated_at: str | None,
) -> dict[str, Any]:
    path = cutover_sentinel_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_cutover_sentinel(database)
    if existing is not None:
        if (
            existing.get("workspace_root") != str(root)
            or existing.get("workspace_id") != workspace_id(root)
        ):
            raise MigrationError("cutover sentinel workspace 漂移")
        ranks = {CUTOVER_LEGACY: 0, CUTOVER_FREEZE: 1, CUTOVER_SQLITE: 2}
        current_state = str(existing["state"])
        if ranks[state] < ranks[current_state]:
            raise MigrationError(
                f"cutover sentinel 禁止降级：current={current_state}, requested={state}"
            )
        if state == current_state:
            if existing.get("legacy_import_disabled") is not bool(legacy_import_disabled):
                raise MigrationError("cutover sentinel 同态发布的 legacy_import_disabled 冲突")
            if activated_at and existing.get("activated_at") not in {None, activated_at}:
                raise MigrationError("cutover sentinel 同态发布的 activated_at 冲突")
            return existing
    payload = {
        "schema_version": CUTOVER_SENTINEL_SCHEMA,
        "state": state,
        "database": str(database),
        "workspace_root": str(root),
        "workspace_id": workspace_id(root),
        "legacy_import_disabled": bool(legacy_import_disabled),
        "active": state == CUTOVER_SQLITE,
        "updated_at": now_iso(),
    }
    if activated_at:
        payload["activated_at"] = activated_at
    descriptor, temporary_raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json_text(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *values: object) -> str:
    raw = "\0".join(str(value) for value in values)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


_TIME_DEFAULT_UNSET = object()
_NULL_TIME_VALUES = {"", "null", "none", "~"}


def time_is_missing(value: object) -> bool:
    return value is None or str(value).strip().lower() in _NULL_TIME_VALUES


def normalize_time(value: object, *, default: object = _TIME_DEFAULT_UNSET) -> str:
    if time_is_missing(value):
        if default is not _TIME_DEFAULT_UNSET:
            return str(default)
        raise MigrationError(f"时间字段缺失：{value}")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"时间字段异常：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def authoritative_time(
    value: object,
    *,
    object_type: str,
    object_id: str,
    field: str,
    default: object = _TIME_DEFAULT_UNSET,
) -> str:
    try:
        return normalize_time(value, default=default)
    except MigrationError as exc:
        raise MigrationError(
            "时间字段错误："
            f"object_type={object_type} object_id={object_id} field={field} value={value!r}"
        ) from exc


def deterministic_legacy_time(
    record: dict[str, Any],
    target_field: str,
    primary_fields: tuple[str, ...],
    fallback_fields: tuple[str, ...],
    source_path: Path,
    inference: dict[str, str],
    object_type: str,
    object_id: str,
) -> str:
    primary_errors: list[str] = []
    for field in primary_fields:
        value = record.get(field)
        if time_is_missing(value):
            continue
        try:
            return normalize_time(value)
        except MigrationError:
            primary_errors.append(f"{field}={value}")
    for field in fallback_fields:
        value = record.get(field)
        if time_is_missing(value):
            continue
        try:
            resolved = normalize_time(value)
        except MigrationError:
            continue
        inference[target_field] = f"record.{field}"
        return resolved
    try:
        repository = subprocess.run(
            ["git", "-C", str(source_path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if repository.returncode == 0 and repository.stdout.strip():
            repo = Path(repository.stdout.strip()).resolve()
            relative = source_path.resolve().relative_to(repo)
            committed = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%cI", "--", str(relative)],
                capture_output=True, text=True, check=False,
            )
            if committed.returncode == 0 and committed.stdout.strip():
                inference[target_field] = "git_last_commit_time"
                return normalize_time(committed.stdout.strip())
    except (OSError, ValueError):
        pass
    inference[target_field] = "unknown_diagnostic"
    return ZERO_TIME


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_migration_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(MIGRATION_SCHEMA)


def metadata_get(store: StateStore, key: str, default: object = None) -> object:
    with store.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_metadata'"
        ).fetchone()
        if not exists:
            return default
        row = connection.execute("SELECT value_json FROM migration_metadata WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def metadata_set(store: StateStore, key: str, value: object) -> None:
    with store.transaction(immediate=True) as connection:
        ensure_migration_schema(connection)
        connection.execute(
            """INSERT INTO migration_metadata(key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, json_text(value), now_iso()),
        )


def activation_status(
    store: StateStore,
    *,
    expected_database: Path | None = None,
) -> dict[str, Any]:
    """Read both activation records without treating either one as sufficient."""
    database = Path(expected_database or store.path).expanduser().resolve()
    marker = metadata_get(store, "backend_active")
    marker_object = marker if isinstance(marker, dict) else {}
    marker_active = marker_object.get("active") is True
    state_active = store.is_backend_active()
    errors: list[str] = []

    if marker_active != state_active:
        errors.append(
            "activation sides disagree: "
            f"marker_active={str(marker_active).lower()}, "
            f"state_active={str(state_active).lower()}"
        )
    if marker_active:
        marker_database = marker_object.get("database")
        if not marker_database:
            errors.append("active marker is missing database")
        elif Path(str(marker_database)).expanduser().resolve() != database:
            errors.append(
                "active marker database mismatch: "
                f"expected={database}, actual={marker_database}"
            )

    return {
        "active": marker_active and state_active and not errors,
        "consistent": not errors,
        "marker_active": marker_active,
        "state_active": state_active,
        "database": str(database),
        "marker": marker_object,
        "errors": errors,
    }


def require_active(
    store: StateStore,
    *,
    expected_database: Path | None = None,
) -> dict[str, Any]:
    status = activation_status(store, expected_database=expected_database)
    if status["errors"]:
        raise MigrationError(
            "SQLite backend activation drift: "
            + json.dumps(status, ensure_ascii=False, sort_keys=True)
        )
    if status["active"] is not True:
        raise MigrationError("SQLite backend 未激活；禁止回退读取 Legacy 文件")
    return status["marker"]


def database_health(database: Path) -> dict[str, list[str]]:
    """Run SQLite health checks without creating or repairing the database."""
    database = Path(database).resolve()
    if not database.is_file():
        raise MigrationError(f"SQLite 数据库不存在: {database}")
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            quick_check = [
                str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            integrity_check = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite 数据库完整性检查失败: {database}: {exc}") from exc
    if quick_check != ["ok"] or integrity_check != ["ok"]:
        raise MigrationError(
            "SQLite 数据库完整性检查失败: "
            f"{database}: quick_check={quick_check!r}, integrity_check={integrity_check!r}"
        )
    return {"quick_check": quick_check, "integrity_check": integrity_check}


def record_projection(store: StateStore, path: Path, kind: str) -> None:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"投影不存在：{path}")
    with store.transaction(immediate=True) as connection:
        ensure_migration_schema(connection)
        connection.execute(
            """INSERT INTO projection_manifest(path, kind, sha256, rendered_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, sha256=excluded.sha256,
                 rendered_at=excluded.rendered_at""",
            (str(path), kind, sha256_file(path), now_iso()),
        )


def task_projection_semantics(store: StateStore) -> dict[str, str]:
    state = metadata_get(store, "migration_state", {})
    root_raw = state.get("root") if isinstance(state, dict) else None
    root = Path(str(root_raw)).expanduser().resolve() if root_raw else None
    semantics: dict[str, str] = {}
    with store.connect() as connection:
        legacy_rows = connection.execute(
            "SELECT source_path, card_json FROM legacy_task_cards ORDER BY source_path"
        ).fetchall()
        task_rows = connection.execute(
            "SELECT task_id, metadata_json FROM tasks ORDER BY task_id"
        ).fetchall()
    for row in legacy_rows:
        if root is None:
            continue
        card = json.loads(row["card_json"] or "{}")
        path = (root / row["source_path"]).resolve()
        historical = bool(card.get("diagnostic_history") or card.get("historical_terminal"))
        semantics[str(path)] = "legacy_archive" if historical else "task_card"
    for row in task_rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        card_path = metadata.get("card_path")
        if not card_path:
            continue
        path = Path(str(card_path)).expanduser()
        if not path.is_absolute():
            if root is None:
                continue
            path = root / path
        historical = bool(
            metadata.get("diagnostic_history") or metadata.get("historical_terminal")
        )
        expected = "legacy_archive" if historical else "task_card"
        resolved = str(path.resolve())
        previous = semantics.get(resolved)
        if previous is not None and previous != expected:
            raise MigrationError(
                f"任务投影语义冲突：path={resolved}, expected={previous}/{expected}"
            )
        semantics[resolved] = expected
    return semantics


def reconcile_task_projections(store: StateStore) -> list[str]:
    paths: list[str] = []
    for raw_path, kind in sorted(task_projection_semantics(store).items()):
        path = Path(raw_path)
        if not path.is_file():
            raise MigrationError(f"任务投影不存在：{path}")
        record_projection(store, path, kind)
        paths.append(str(path))
    return paths


def refresh_events_projection(root: Path, store: StateStore) -> dict[str, Any]:
    """Cutover-aware wrapper around the StateStore public projection primitive."""
    root = Path(root).expanduser().resolve()
    active = require_active(store)
    configured_database = active.get("database") if isinstance(active, dict) else None
    if configured_database and Path(str(configured_database)).expanduser().resolve() != store.path.resolve():
        raise MigrationError(
            "事件投影刷新被拒绝: backend_active 数据库路径与 StateStore 不一致"
        )
    configured_runtime = runtime_dir(root)
    configured_database_path = state_database(root, configured_runtime)
    if configured_database_path == store.path.resolve():
        projection_runtime = configured_runtime
    else:
        projection_runtime = (
            store.path.parent.parent if store.path.parent.name == "state" else store.path.parent
        )
    return refresh_state_events_projection(
        store,
        workspace_root=root,
        output=projection_runtime / "events/events.jsonl",
    )


def projection_drift(store: StateStore) -> list[dict[str, str]]:
    require_active(store)
    semantics = task_projection_semantics(store)
    with store.connect() as connection:
        rows = connection.execute("SELECT path, kind, sha256 FROM projection_manifest ORDER BY path").fetchall()
    drift: list[dict[str, str]] = []
    manifested: set[str] = set()
    for row in rows:
        path = Path(row["path"]).expanduser().resolve()
        raw_path = str(path)
        manifested.add(raw_path)
        current = sha256_file(path) if path.is_file() else ""
        item: dict[str, str] = {"path": raw_path, "kind": row["kind"]}
        if current != row["sha256"]:
            item.update({
                "reason": "hash_drift", "expected": row["sha256"],
                "current": current or "missing",
            })
        expected_kind = semantics.get(raw_path)
        if row["kind"] in {"task_card", "legacy_archive"} and expected_kind is None:
            item.update({"reason": "task_projection_unbound", "expected_kind": "none"})
        elif expected_kind is not None and row["kind"] != expected_kind:
            item.update({
                "reason": "task_projection_kind_drift",
                "expected_kind": expected_kind,
                "current_kind": row["kind"],
            })
        if len(item) > 2:
            drift.append(item)
    for raw_path, expected_kind in sorted(semantics.items()):
        if raw_path not in manifested:
            drift.append({
                "path": raw_path, "kind": "missing",
                "reason": "task_projection_manifest_missing",
                "expected_kind": expected_kind,
            })
    return drift


@contextmanager
def cutover_lock(database: Path, *, exclusive: bool) -> Iterator[Path]:
    lock = cutover_lock_path(database)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and lock in _PROCESS_EXCLUSIVE_LOCKS:
        raise MigrationError(f"迁移锁已被当前进程占用：{lock}")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        if exclusive:
            _PROCESS_EXCLUSIVE_LOCKS.add(lock)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()} {now_iso()}\n".encode())
        os.fsync(descriptor)
        yield lock
    finally:
        if exclusive:
            _PROCESS_EXCLUSIVE_LOCKS.discard(lock)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def migration_lock(database: Path) -> Iterator[Path]:
    return cutover_lock(database, exclusive=True)


def legacy_shared_lock(database: Path) -> Iterator[Path]:
    return cutover_lock(database, exclusive=False)


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end >= 0 else ""


def scalar_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in frontmatter(text).splitlines():
        if match := re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line):
            result[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return result


def list_field(text: str, name: str) -> list[str]:
    result: list[str] = []
    active = False
    for line in frontmatter(text).splitlines():
        if re.match(rf"^{re.escape(name)}:\s*$", line):
            active = True
            continue
        if active and re.match(r"^\s+-\s+", line):
            result.append(re.sub(r"^\s+-\s+", "", line).strip().strip('"').strip("'"))
            continue
        if active and line and not line.startswith(" "):
            break
    return result


def parse_json_field(value: str | None, default: object) -> object:
    if value in (None, "", "null"):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"JSON 字段异常：{value}") from exc


def discover_task_cards(
    root: Path, authoritative_task_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    authoritative_task_ids = authoritative_task_ids or set()
    paths: list[Path] = []
    for base in (root / ".xirang/tasks", root / "02-项目管理/任务卡"):
        if base.exists():
            paths.extend(path for path in base.glob("**/T-*.md") if not path.name.startswith("T-YYYY"))
    cards: list[dict[str, Any]] = []
    seen: dict[str, Path] = {}
    for path in sorted(set(paths)):
        text = path.read_text(encoding="utf-8")
        fields = scalar_fields(text)
        task_id = fields.get("task_id") or path.stem
        if task_id in seen:
            raise MigrationError(f"重复 task_id：{task_id}（{seen[task_id]} / {path}）")
        seen[task_id] = path
        status = fields.get("status", "draft")
        original_review_status = fields.get("review_status", "draft")
        diagnostic_history = (
            status in HISTORICAL_TERMINAL_STATES
            or (
                status in {"canceled", "cancelled", "done", "completed"}
                and task_id not in authoritative_task_ids
            )
        )
        historical_terminal = diagnostic_history
        review = (
            HISTORICAL_REVIEW_MAP[status]
            if status in HISTORICAL_REVIEW_MAP
            else "legacy_unreviewed" if diagnostic_history else original_review_status
        )
        if status not in LIFECYCLE_MAP:
            raise MigrationError(f"{task_id} 状态映射异常：status={status}")
        if review not in REVIEW_STATES:
            raise MigrationError(f"{task_id} 状态映射异常：review_status={review}")
        if review == "accepted" and LIFECYCLE_MAP[status] in {"draft", "in_progress", "blocked"}:
            raise MigrationError(f"{task_id} 状态组合异常：活动任务不能 accepted")
        timestamp_source: dict[str, str] = {}
        if diagnostic_history:
            created_at = deterministic_legacy_time(
                fields, "created_at", ("created_at",), ("updated_at", "submitted_at"),
                path, timestamp_source, "task_card", task_id,
            )
            updated_at = deterministic_legacy_time(
                fields, "updated_at", ("updated_at",), ("submitted_at", "created_at"),
                path, timestamp_source, "task_card", task_id,
            )
            submitted_at = deterministic_legacy_time(
                fields, "submitted_at", ("submitted_at",), ("updated_at", "created_at"),
                path, timestamp_source, "task_card", task_id,
            )
        else:
            created_at = authoritative_time(
                fields.get("created_at"), object_type="task_card", object_id=task_id, field="created_at"
            )
            updated_at = authoritative_time(
                fields.get("updated_at"), object_type="task_card", object_id=task_id, field="updated_at"
            )
            submitted_value = fields.get("submitted_at")
            if review in {"submitted", "reviewing", "accepted", "changes_requested"}:
                submitted_at = authoritative_time(
                    submitted_value,
                    object_type="task_card",
                    object_id=task_id,
                    field="submitted_at",
                )
            elif time_is_missing(submitted_value):
                submitted_at = updated_at
            else:
                submitted_at = authoritative_time(
                    submitted_value,
                    object_type="task_card",
                    object_id=task_id,
                    field="submitted_at",
                )
        roots = canonical_scopes(list_field(text, "allowed_write_roots"))
        external_roots: list[str] = []
        for external_root in list_field(text, "external_write_roots"):
            normalized_external_root = Path(external_root).expanduser()
            if not normalized_external_root.is_absolute():
                raise MigrationError(
                    "external_write_roots 必须是绝对路径："
                    f"task_id={task_id} root={external_root}"
                )
            external_roots.append(
                normalized_external_root.as_posix().rstrip("/") or "/"
            )
        external_roots = sorted(set(external_roots))
        excluded = list_field(text, "excluded_scope")
        changed = parse_json_field(fields.get("changed_paths_json"), [])
        receipts = parse_json_field(fields.get("write_receipt_ids_json"), [])
        if not isinstance(changed, list) or not isinstance(receipts, list):
            raise MigrationError(f"{task_id} 交付字段必须是 JSON 数组")
        maintenance = str(fields.get("maintenance") or "").strip().lower() == "true"
        raw_proposal_id = fields.get("maintenance_authorization_receipt") or fields.get("proposal_id")
        proposal_id = str(raw_proposal_id or "").strip()
        if len(proposal_id) >= 2 and proposal_id[0] == proposal_id[-1] and proposal_id[0] in {'"', "'"}:
            proposal_id = proposal_id[1:-1]
        if proposal_id.lower() in {"", "null", "none", "~"}:
            proposal_id = ""
        if proposal_id and not maintenance:
            raise MigrationError(
                "普通任务不得携带维护提案："
                f"task_id={task_id} proposal_id={proposal_id}"
            )
        relative = path.relative_to(root).as_posix()
        cards.append({
            "task_id": task_id,
            "title": fields.get("title") or task_id,
            "source_path": relative,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "session_id": fields.get("session_id") or f"legacy:{task_id}",
            "platform": fields.get("platform") or "legacy",
            "task_kind": "control_plane_maintenance" if maintenance else "standard",
            "maintenance": maintenance,
            "proposal_id": proposal_id or None,
            "lifecycle_status": "legacy_unreviewed" if diagnostic_history else LIFECYCLE_MAP[status],
            "runtime_status": "archived" if diagnostic_history else RUNTIME_MAP[status],
            "review_status": review,
            "allowed_write_roots": roots,
            "external_write_roots": external_roots,
            "excluded_actions": excluded,
            "created_at": created_at,
            "updated_at": updated_at,
            "submitted_at": submitted_at,
            "changed_paths": changed,
            "receipt_ids": receipts,
            "continuation_policy": fields.get("continuation_policy"),
            "original_status": status,
            "original_review_status": original_review_status,
            "historical_terminal": historical_terminal,
            "diagnostic_history": diagnostic_history,
            "timestamp_inferred": bool(timestamp_source),
            "timestamp_source": timestamp_source,
            "raw_fields": fields,
        })
    return cards


def load_jsonl(path: Path, namespace: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"{path}:{number} JSONL 损坏") from exc
        if not isinstance(row, dict):
            raise MigrationError(f"{path}:{number} 事件必须是对象")
        item = dict(row)
        item["_source_path"] = str(path)
        item["_source_line"] = number
        item["_namespace"] = namespace
        rows.append(item)
    return rows


def load_json_objects(paths: list[Path], kind: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: dict[str, Path] = {}
    for path in sorted(set(paths)):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"{kind} 文件损坏：{path}") from exc
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if not isinstance(row, dict):
                raise MigrationError(f"{kind} 必须是 JSON 对象：{path}")
            key = str(row.get("proposal_id") or row.get("decision_receipt_id") or stable_id("OBJ", path, len(result)))
            if key in seen:
                raise MigrationError(f"{kind} 标识重复：{key}")
            seen[key] = path
            item = dict(row)
            item["_source_path"] = str(path)
            result.append(item)
    return result


def event_data(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    result = dict(payload) if isinstance(payload, dict) else {}
    result.update({key: value for key, value in row.items() if not key.startswith("_") and key != "payload"})
    return result


def event_name(row: dict[str, Any]) -> str:
    return str(row.get("event") or row.get("type") or row.get("event_type") or "unknown")


def event_identifier(row: dict[str, Any]) -> str:
    return str(row.get("event_id") or stable_id(
        "ELEG", row.get("_namespace"), row.get("_source_path"), row.get("_source_line"), json_text(row)
    ))


def normalized_user_event_unique_key(
    data: dict[str, Any],
) -> tuple[str, str | None]:
    platform = str(data.get("platform") or "legacy").strip() or "legacy"
    for field in ("host_message_id", "turn_id"):
        raw_value = data.get(field)
        if raw_value is None:
            continue
        normalized = str(raw_value).strip()
        if normalized:
            return platform, normalized
    return platform, None


def user_event_conflict_error(
    *,
    platform: str,
    host_message_id: str,
    event_ids: list[str],
    session_ids: list[str],
    prompt_sha256: list[str],
) -> MigrationError:
    return MigrationError(
        "user_event_unique_key_conflict: "
        + json_text({
            "object_type": "user_event_unique_key",
            "platform": platform,
            "host_message_id": host_message_id,
            "event_ids": event_ids,
            "session_ids": session_ids,
            "prompt_sha256": prompt_sha256,
        })
    )


def merge_duplicate_user_event_registrations(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    registrations: dict[
        tuple[str, str], list[tuple[int, dict[str, Any], str, str, str, str]]
    ] = {}
    user_event_names = {"user_prompt", "user_event", "user_event_recorded"}
    for index, row in enumerate(rows):
        data = event_data(row)
        name = event_name(row)
        if name not in user_event_names:
            continue
        platform, host_message_id = normalized_user_event_unique_key(data)
        if host_message_id is None:
            continue
        session_id = str(data.get("session_id") or "legacy")
        event_id = str(
            data.get("user_event_id")
            or data.get("source_event_id")
            or data.get("event_id")
            or event_identifier(row)
        )
        prompt_sha256 = str(
            data.get("prompt_sha256")
            or hashlib.sha256(
                str(data.get("prompt") or data.get("text") or "").encode()
            ).hexdigest()
        )
        observed_at = authoritative_time(
            data.get("ts") or data.get("first_observed_at"),
            object_type="user_event",
            object_id=event_id,
            field="first_observed_at",
        )
        registrations.setdefault((platform, host_message_id), []).append(
            (index, row, event_id, session_id, prompt_sha256, observed_at)
        )

    replacements: dict[int, dict[str, Any]] = {}
    for (platform, host_message_id), group in registrations.items():
        if len(group) < 2:
            continue
        sessions = {item[3] for item in group}
        prompt_hashes = {item[4] for item in group}
        event_ids = [item[2] for item in group]
        if len(sessions) != 1 or len(prompt_hashes) != 1:
            raise user_event_conflict_error(
                platform=platform,
                host_message_id=host_message_id,
                event_ids=event_ids,
                session_ids=sorted(sessions),
                prompt_sha256=sorted(prompt_hashes),
            )
        canonical = min(
            group,
            key=lambda item: (datetime.fromisoformat(item[5]), item[0]),
        )
        canonical_event_id = canonical[2]
        for index, row, event_id, _session_id, _prompt_sha256, _observed_at in group:
            if index == canonical[0]:
                continue
            audit_id = "AUDUP-" + hashlib.sha256(
                f"{platform}\0{host_message_id}\0{canonical_event_id}\0{event_id}\0{index}".encode()
            ).hexdigest()[:24]
            audit_row = dict(row)
            audit_data = dict(event_data(row))
            original_bindings = {
                field: audit_data[field]
                for field in ("task_id", "delivery_id", "focus_id", "proposal_id")
                if audit_data.get(field) is not None
            }
            audit_data.update({
                "event_id": audit_id,
                "event": "user_event_duplicate_diagnostic",
                "diagnostic_only": True,
                "duplicate_of_user_event_id": canonical_event_id,
                "duplicate_original_event_id": event_id,
                "duplicate_original_bindings": original_bindings,
                "duplicate_reason": "identical_platform_host_session_prompt",
                "task_id": None,
            })
            for discriminator in ("event", "type", "event_type", "name"):
                audit_data[discriminator] = "user_event_duplicate_diagnostic"
                if discriminator in audit_row:
                    audit_row[discriminator] = "user_event_duplicate_diagnostic"
            if isinstance(row.get("payload"), dict):
                audit_row["payload"] = audit_data
                audit_row["event_id"] = audit_id
            else:
                audit_row.update(audit_data)
            replacements[index] = audit_row
    return [replacements.get(index, row) for index, row in enumerate(rows)]


def v9_authoritative_task_ids(
    v9_events: list[dict[str, Any]], decision_files: list[dict[str, Any]]
) -> set[str]:
    v9_events[:] = merge_duplicate_user_event_registrations(v9_events)
    result: set[str] = set()
    for row in v9_events + decision_files:
        data = event_data(row)
        if data.get("diagnostic_only"):
            continue
        task_id = str(data.get("task_id") or "")
        if task_id:
            result.add(task_id)
    return result


def discover_legacy(root: Path, runtime: Path) -> dict[str, Any]:
    v9_path = runtime / "events/events.jsonl"
    v8_path = root / "02-项目管理/智能体状态/智能体事件.jsonl"
    proposal_paths: list[Path] = []
    for pattern in ("maintenance/proposals/*.json", "maintenance-proposals/*.json", "proposals/*.json"):
        proposal_paths.extend(runtime.glob(pattern))
    decision_paths: list[Path] = []
    for pattern in ("decisions/*.json", "decision-receipts/*.json"):
        decision_paths.extend(runtime.glob(pattern))
    v9_events = load_jsonl(v9_path, "v9_legacy")
    decision_files = load_json_objects(decision_paths, "decision receipt")
    authoritative_tasks = v9_authoritative_task_ids(v9_events, decision_files)
    return {
        "cards": discover_task_cards(root, authoritative_tasks),
        "v9_events": v9_events,
        "v8_events": load_jsonl(v8_path, "v8_legacy"),
        "proposals": load_json_objects(proposal_paths, "maintenance proposal"),
        "decision_files": decision_files,
        "source_paths": [
            (path, kind) for path, kind in (
                (v9_path, "v9_events"), (v8_path, "v8_legacy_events")
            ) if path.is_file()
        ] + [(path, "maintenance_proposal") for path in proposal_paths]
          + [(path, "decision_receipt") for path in decision_paths],
    }


def decode_git_quoted_path(path: object) -> str | None:
    value = str(path or "")
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return None
    body = value[1:-1]
    output = bytearray()
    saw_octal = False
    escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            output.extend(character.encode("utf-8"))
            index += 1
            continue
        if index + 3 < len(body) and all(
            digit in "01234567" for digit in body[index + 1:index + 4]
        ):
            output.append(int(body[index + 1:index + 4], 8))
            saw_octal = True
            index += 4
            continue
        if index + 1 >= len(body):
            return None
        escaped = body[index + 1]
        if escaped in {'"', "\\"}:
            output.extend(escaped.encode("utf-8"))
        elif escaped in escapes:
            output.append(escapes[escaped])
        else:
            return None
        index += 2
    if not saw_octal:
        return None
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        return None


def receipt_graph_error(kind: str, **details: object) -> MigrationError:
    return MigrationError(
        "receipt_graph_error: "
        + json_text({"object_type": "write_receipt_graph", "kind": kind, **details})
    )


def validate_receipt_graph(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    successor_by_predecessor: dict[str, str] = {}
    for source_index, row in enumerate(rows):
        if event_name(row) != "file_write":
            continue
        data = event_data(row)
        receipt_id = str(data.get("receipt_id") or "")
        if not receipt_id:
            raise MigrationError("V9 file_write 缺少 receipt_id")
        immutable = {
            "task_id": data.get("task_id"),
            "file": data.get("file") or data.get("path"),
            "sha256": data.get("sha256"),
            "predecessor": data.get("supersedes_receipt") or data.get("replaces_receipt"),
        }
        if receipt_id in receipts and receipts[receipt_id]["_immutable"] != immutable:
            raise MigrationError(f"重复 receipt_id 内容冲突：{receipt_id}")
        item = dict(data)
        item["_immutable"] = immutable
        item["_event_id"] = event_identifier(row)
        item["_source_index"] = source_index
        item["_quoted_path_decoded"] = decode_git_quoted_path(immutable["file"])
        receipts[receipt_id] = item
        predecessor = immutable["predecessor"]
        if predecessor:
            if predecessor in successor_by_predecessor and successor_by_predecessor[predecessor] != receipt_id:
                raise receipt_graph_error(
                    "fork",
                    predecessor_receipt_id=str(predecessor),
                    successor_receipt_ids=[
                        successor_by_predecessor[predecessor], receipt_id
                    ],
                )
            successor_by_predecessor[str(predecessor)] = receipt_id
    for receipt_id, row in receipts.items():
        predecessor = row["_immutable"]["predecessor"]
        if predecessor and predecessor not in receipts:
            raise receipt_graph_error(
                "missing_predecessor",
                receipt_id=receipt_id,
                task_id=row.get("task_id"),
                path=row.get("file") or row.get("path"),
                predecessor_receipt_id=str(predecessor),
            )

    for receipt_id, row in receipts.items():
        decoded_path = row["_quoted_path_decoded"]
        if decoded_path is None or receipt_id in successor_by_predecessor:
            continue

        def compatible_historical_repair(candidate: dict[str, Any]) -> bool:
            if candidate.get("task_id") != row.get("task_id"):
                return False
            if str(candidate.get("session_id") or "") != str(row.get("session_id") or ""):
                return False
            if candidate["_source_index"] <= row["_source_index"]:
                return False
            if candidate["_quoted_path_decoded"] is not None:
                return False
            if str(candidate.get("file") or candidate.get("path") or "") != decoded_path:
                return False
            if str(candidate.get("tool_name") or "") != "git-evidence-repair":
                return False
            reason = str(candidate.get("reason") or "")
            if reason not in {"", "path_canonicalization", "git_path_canonicalization"}:
                return False
            predecessor_sha = str(row.get("sha256") or "")
            candidate_sha = str(candidate.get("sha256") or "")
            if predecessor_sha and candidate_sha and predecessor_sha != candidate_sha:
                return False
            for key in ("summary", "change_summary", "content_digest"):
                left, right = row.get(key), candidate.get(key)
                if left not in (None, "") and right not in (None, "") and left != right:
                    return False
            predecessor_operation = str(row.get("operation") or "")
            candidate_operation = str(candidate.get("operation") or "")
            return (
                predecessor_operation == candidate_operation
                or (not predecessor_sha and candidate_operation in {"create", "edit", "update", "delete"})
            )

        candidates = [
            candidate_id
            for candidate_id, candidate in receipts.items()
            if candidate_id != receipt_id
            and compatible_historical_repair(candidate)
        ]
        if len(candidates) != 1:
            raise receipt_graph_error(
                "quoted_predecessor_without_unique_canonical_tail",
                receipt_id=receipt_id,
                task_id=row.get("task_id"),
                path=row.get("file") or row.get("path"),
                decoded_path=decoded_path,
                candidate_tail_receipt_ids=candidates,
            )
        successor = candidates[0]
        existing_predecessor = receipts[successor]["_immutable"]["predecessor"]
        if existing_predecessor and str(existing_predecessor) != receipt_id:
            raise receipt_graph_error(
                "successor_has_multiple_predecessors",
                receipt_id=successor,
                task_id=row.get("task_id"),
                path=decoded_path,
                predecessor_receipt_ids=[str(existing_predecessor), receipt_id],
            )
        receipts[successor]["_immutable"]["predecessor"] = receipt_id
        receipts[successor]["_compatibility_link"] = {
            "kind": "legacy_git_path_canonicalization",
            "predecessor_receipt_id": receipt_id,
            "same_session": True,
            "digest_compatible": True,
        }
        successor_by_predecessor[receipt_id] = successor

    for receipt_id in receipts:
        visited: set[str] = set()
        current: str | None = receipt_id
        while current:
            if current in visited:
                raise receipt_graph_error("cycle", receipt_id=receipt_id)
            visited.add(current)
            predecessor = receipts[current]["_immutable"]["predecessor"]
            current = str(predecessor) if predecessor else None
    for receipt_id, row in receipts.items():
        visited: set[str] = set()
        tail_id = receipt_id
        while tail_id in successor_by_predecessor:
            if tail_id in visited:
                raise receipt_graph_error("cycle", receipt_id=receipt_id)
            visited.add(tail_id)
            tail_id = successor_by_predecessor[tail_id]
        row["_graph_successor"] = successor_by_predecessor.get(receipt_id)
        row["_graph_tail_id"] = tail_id
        row["_graph_role"] = (
            "effective_tail" if receipt_id == tail_id else "superseded_predecessor"
        )
    return receipts


def _upsert_source(connection: sqlite3.Connection, path: Path, kind: str, count: int) -> None:
    connection.execute(
        """INSERT INTO migration_sources(source_path, source_kind, sha256, row_count, imported_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_path) DO UPDATE SET source_kind=excluded.source_kind,
             sha256=excluded.sha256, row_count=excluded.row_count, imported_at=excluded.imported_at""",
        (str(path.resolve()), kind, sha256_file(path), count, now_iso()),
    )


def _import_tasks(connection: sqlite3.Connection, workspace: str, cards: list[dict[str, Any]]) -> None:
    stamp = now_iso()
    for card in cards:
        task_id = card["task_id"]
        envelope_id = stable_id("ENVLEG", workspace, task_id)
        envelope_digest = hashlib.sha256(json_text({
            "task_id": task_id, "roots": card["allowed_write_roots"],
            "external_roots": card["external_write_roots"],
            "source_sha256": card["source_sha256"],
        }).encode()).hexdigest()
        connection.execute(
            """INSERT INTO tasks(
                 task_id, envelope_id, workspace_id, session_id, platform, task_kind,
                 envelope_digest, lifecycle_status, runtime_status, review_status,
                 allowed_write_roots_json, excluded_actions_json, metadata_json,
                 actor_verified, disclosure_verified, sequence_verified, enforcement_verified,
                 created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 session_id=excluded.session_id, platform=excluded.platform, task_kind=excluded.task_kind,
                 lifecycle_status=excluded.lifecycle_status, runtime_status=excluded.runtime_status,
                 review_status=excluded.review_status,
                 allowed_write_roots_json=excluded.allowed_write_roots_json,
                 excluded_actions_json=excluded.excluded_actions_json,
                 metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
            (
                task_id, envelope_id, workspace, card["session_id"], card["platform"], card["task_kind"],
                envelope_digest, card["lifecycle_status"], card["runtime_status"], card["review_status"],
                json_text(card["allowed_write_roots"]), json_text(card["excluded_actions"]),
                json_text({
                "legacy_import": True,
                "original_status": card["original_status"],
                "original_review_status": card["original_review_status"],
                "diagnostic_history": card["diagnostic_history"],
                "external_write_roots": card["external_write_roots"],
                "historical_terminal": card["historical_terminal"],
                "timestamp_inferred": card["timestamp_inferred"],
                "timestamp_source": card["timestamp_source"],
                "maintenance": card["maintenance"],
                "proposal_id": card["proposal_id"],
                }),
                card["created_at"], card["updated_at"],
            ),
        )
        connection.execute(
            """INSERT INTO legacy_task_cards(task_id, source_path, title, source_sha256, card_json, imported_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET source_path=excluded.source_path, title=excluded.title,
                 source_sha256=excluded.source_sha256, card_json=excluded.card_json,
                 imported_at=excluded.imported_at""",
            (task_id, card["source_path"], card["title"], card["source_sha256"], json_text(card), stamp),
        )
        stage_id = stable_id("SRLEG", task_id)
        connection.execute("DELETE FROM stage_runs WHERE stage_run_id = ?", (stage_id,))
        connection.execute(
            """INSERT INTO stage_runs(stage_run_id, task_id, stage, review_round, status, started_at, details_json)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (
                stage_id, task_id, card["runtime_status"],
                "completed" if card["historical_terminal"] else "active",
                card["updated_at"],
                json_text({
                    "legacy_import": True,
                    "original_status": card["original_status"],
                    "original_review_status": card["original_review_status"],
                    "diagnostic_history": card["diagnostic_history"],
                    "external_write_roots": card["external_write_roots"],
                    "historical_terminal": card["historical_terminal"],
                    "timestamp_inferred": card["timestamp_inferred"],
                    "timestamp_source": card["timestamp_source"],
                }),
            ),
        )
        if card["lifecycle_status"] in {"in_progress", "blocked"}:
            lease_id = stable_id("LLEG", task_id, card["session_id"])
            connection.execute(
                """INSERT INTO leases(
                     lease_id, task_id, source_session_id, worker_session_id, role,
                     allowed_write_roots_json, read_only, status, issued_at, expires_at,
                     enforcement_verified
                   ) VALUES (?, ?, ?, ?, 'owner', ?, 0, 'active', ?, ?, 0)
                   ON CONFLICT(lease_id) DO UPDATE SET allowed_write_roots_json=excluded.allowed_write_roots_json,
                     status='active', expires_at=excluded.expires_at""",
                (
                    lease_id, task_id, card["session_id"], card["session_id"],
                    json_text(card["allowed_write_roots"]), card["created_at"],
                    "9999-12-31T23:59:59.999999+00:00",
                ),
            )
        if not card["historical_terminal"] and card["review_status"] in {
            "submitted", "reviewing", "accepted", "changes_requested", "superseded"
        }:
            delivery_id = stable_id("DELLEG", task_id, card["submitted_at"])
            manifest = [{"path": path, "legacy": True} for path in card["changed_paths"]]
            delivery_status = "accepted" if card["review_status"] == "accepted" else (
                "changes_requested" if card["review_status"] == "changes_requested" else "submitted"
            )
            connection.execute(
                """INSERT INTO deliveries(
                     delivery_id, task_id, manifest_json, validation_summary, status, submitted_at
                   ) VALUES (?, ?, ?, 'legacy_import', ?, ?)
                   ON CONFLICT(delivery_id) DO UPDATE SET manifest_json=excluded.manifest_json,
                     status=excluded.status""",
                (delivery_id, task_id, json_text(manifest), delivery_status, card["submitted_at"]),
            )


def _import_events(
    connection: sqlite3.Connection,
    workspace: str,
    rows: list[dict[str, Any]],
    namespace: str,
    diagnostic_task_ids: set[str] | None = None,
) -> dict[int, str]:
    event_ids: dict[int, str] = {}
    diagnostic_task_ids = diagnostic_task_ids or set()
    for index, row in enumerate(rows):
        data = event_data(row)
        event_id = event_identifier(row)
        name = event_name(row)
        event_type = f"v8_legacy.{name}" if namespace == "v8_legacy" else name
        task_id = str(data.get("task_id") or "")
        diagnostic_only = (
            namespace == "v8_legacy"
            or task_id in diagnostic_task_ids
            or bool(data.get("diagnostic_only"))
        )
        timestamp_source: dict[str, str] = {}
        if diagnostic_only:
            occurred = deterministic_legacy_time(
                data,
                "occurred_at",
                ("ts", "occurred_at"),
                ("updated_at", "created_at", "submitted_at", "decided_at", "presented_at"),
                Path(str(row.get("_source_path") or "")),
                timestamp_source,
                "v8_event" if namespace == "v8_legacy" else "v9_event",
                event_id,
            )
        else:
            occurred_value = data.get("ts") if not time_is_missing(data.get("ts")) else data.get("occurred_at")
            occurred = authoritative_time(
                occurred_value,
                object_type="v9_event",
                object_id=event_id,
                field="occurred_at",
            )
        payload = dict(data)
        payload.update({
            "namespace": namespace,
            "diagnostic_only": diagnostic_only,
            "strong_receipt": False if diagnostic_only else name == "file_write",
            "legacy_source": row.get("_source_path"),
            "legacy_line": row.get("_source_line"),
            "timestamp_inferred": bool(timestamp_source),
            "timestamp_source": timestamp_source,
        })
        existing = connection.execute("SELECT event_type, payload_json FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            existing_type = existing["event_type"]
            existing_payload = json.loads(existing["payload_json"])
            if (
                existing_type == "legacy_write_receipt_diagnostic"
                and existing_payload.get("original_event_type") == event_type
            ):
                existing_type = event_type
                existing_payload["diagnostic_only"] = payload["diagnostic_only"]
                existing_payload["strong_receipt"] = payload["strong_receipt"]
            for derived_field in (
                "receipt_graph_role",
                "effective_tail_receipt_id",
                "predecessor_receipt_id",
                "successor_receipt_id",
                "receipt_link_inference",
                "evidence_verified",
                "diagnostic_reason",
                "original_event_type",
            ):
                existing_payload.pop(derived_field, None)
            if existing_type != event_type or existing_payload != payload:
                raise MigrationError(f"重复 event_id 内容冲突：{event_id}")
        connection.execute(
            """INSERT OR IGNORE INTO events(
                 event_id, event_type, occurred_at, workspace_id, session_id, task_id, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, event_type, occurred, workspace,
                data.get("session_id"), data.get("task_id"), json_text(payload),
            ),
        )
        event_ids[index] = event_id
        if namespace == "v9_legacy" and name in {"user_prompt", "user_event", "user_event_recorded"}:
            user_event_id = str(data.get("user_event_id") or data.get("source_event_id") or data.get("event_id") or event_id)
            observed = authoritative_time(
                data.get("ts") or data.get("first_observed_at"),
                object_type="user_event",
                object_id=user_event_id,
                field="first_observed_at",
            )
            expires = authoritative_time(
                data.get("expires_at"),
                object_type="user_event",
                object_id=user_event_id,
                field="expires_at",
                default=(datetime.fromisoformat(observed) + timedelta(minutes=10)).isoformat(
                    timespec="microseconds"
                ),
            )
            prompt_sha = str(data.get("prompt_sha256") or hashlib.sha256(
                str(data.get("prompt") or data.get("text") or "").encode()
            ).hexdigest())
            session_id = str(data.get("session_id") or "legacy")
            platform, host_message_id = normalized_user_event_unique_key(data)
            existing = None
            if host_message_id is not None:
                existing = connection.execute(
                    """SELECT event_id, session_id, prompt_sha256
                       FROM user_events
                       WHERE platform=? AND host_message_id=?""",
                    (platform, host_message_id),
                ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] != session_id
                    or existing["prompt_sha256"] != prompt_sha
                ):
                    raise user_event_conflict_error(
                        platform=platform,
                        host_message_id=host_message_id,
                        event_ids=[existing["event_id"], user_event_id],
                        session_ids=sorted({existing["session_id"], session_id}),
                        prompt_sha256=sorted({existing["prompt_sha256"], prompt_sha}),
                    )
                if existing["event_id"] != user_event_id:
                    diagnostic_payload = dict(payload)
                    diagnostic_payload.update({
                        "diagnostic_only": True,
                        "duplicate_of_user_event_id": existing["event_id"],
                        "duplicate_original_event_id": user_event_id,
                        "duplicate_reason": "identical_platform_host_session_prompt",
                    })
                    connection.execute(
                        """UPDATE events
                           SET event_type='user_event_duplicate_diagnostic',
                               task_id=NULL, payload_json=?
                           WHERE event_id=?""",
                        (json_text(diagnostic_payload), event_id),
                    )
                continue
            try:
                connection.execute(
                    """INSERT INTO user_events(
                         event_id, workspace_id, session_id, platform, host_message_id,
                         prompt_sha256, first_observed_at, expires_at, consumed_at,
                         consumed_by, actor_verified
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_id) DO NOTHING""",
                    (
                        user_event_id, workspace, session_id, platform, host_message_id,
                        prompt_sha, observed, expires, data.get("consumed_at"), data.get("consumed_by"),
                        1 if data.get("actor_verified") is True else 0,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conflicting = connection.execute(
                    """SELECT event_id, session_id, prompt_sha256
                       FROM user_events
                       WHERE platform=? AND host_message_id=?""",
                    (platform, host_message_id),
                ).fetchone()
                raise user_event_conflict_error(
                    platform=platform,
                    host_message_id=host_message_id or "<null>",
                    event_ids=[
                        *([conflicting["event_id"]] if conflicting else []),
                        user_event_id,
                    ],
                    session_ids=sorted({
                        *([conflicting["session_id"]] if conflicting else []),
                        session_id,
                    }),
                    prompt_sha256=sorted({
                        *([conflicting["prompt_sha256"]] if conflicting else []),
                        prompt_sha,
                    }),
                ) from exc
    return event_ids


def _import_receipts(
    connection: sqlite3.Connection, receipts: dict[str, dict[str, Any]]
) -> None:
    tasks = {
        row["task_id"]: row
        for row in connection.execute(
            """SELECT task_id, lifecycle_status, runtime_status, review_status,
                      allowed_write_roots_json, metadata_json
               FROM tasks"""
        ).fetchall()
    }
    task_metadata = {
        task_id: json.loads(task["metadata_json"] or "{}")
        for task_id, task in tasks.items()
    }
    historical_tasks = {
        task_id for task_id, task in tasks.items()
        if task_metadata[task_id].get("historical_terminal") is True
    }
    dispositions: dict[str, str] = {}
    diagnostic_reasons: dict[str, str] = {}
    diagnostic_affects_task_evidence: set[str] = set()

    def completed_historical_delivery(task_id: str) -> bool:
        task = tasks[task_id]
        metadata = task_metadata[task_id]
        return (
            metadata.get("original_status") in {"done", "completed"}
            and (
                metadata.get("original_review_status") == "accepted"
                or task["review_status"] == "accepted"
            )
        )

    def scope_violation(receipt_id: str, task_id: str, path: str) -> MigrationError:
        task = tasks[task_id]
        metadata = task_metadata[task_id]
        return MigrationError(
            "receipt_scope_violation: "
            + json_text({
                "object_type": "write_receipt",
                "receipt_id": receipt_id,
                "task_id": task_id,
                "path": path,
                "lifecycle_status": task["lifecycle_status"],
                "runtime_status": task["runtime_status"],
                "review_status": task["review_status"],
                "allowed_write_roots": json.loads(task["allowed_write_roots_json"]),
                "external_write_roots": metadata.get("external_write_roots", []),
            })
        )

    def path_is_authorized(task_id: str, path: str) -> bool:
        task = tasks[task_id]
        metadata = task_metadata[task_id]
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            normalized_path = candidate.as_posix()
            roots = metadata.get("external_write_roots", [])
        else:
            normalized_path = path
            roots = json.loads(task["allowed_write_roots_json"])
        return bool(roots) and any(
            scope_covers(root, normalized_path) for root in roots
        )

    def qualification_error(
        kind: str, receipt_id: str, task_id: str, path: str
    ) -> MigrationError:
        return MigrationError(
            "receipt_qualification_error: "
            + json_text({
                "object_type": "write_receipt",
                "kind": kind,
                "receipt_id": receipt_id,
                "task_id": task_id,
                "path": path,
            })
        )

    for receipt_id, row in receipts.items():
        task_id = str(row.get("task_id") or "")
        if task_id not in tasks:
            raise MigrationError(f"收据引用未知任务：{receipt_id} -> {task_id}")
        if task_id in historical_tasks:
            dispositions[receipt_id] = "diagnostic"
            diagnostic_reasons[receipt_id] = "historical_terminal_task"
            continue
        if row.get("_graph_role") != "effective_tail":
            if row.get("_quoted_path_decoded") is not None:
                dispositions[receipt_id] = "diagnostic"
                diagnostic_reasons[receipt_id] = "superseded_quoted_path_predecessor"
            else:
                dispositions[receipt_id] = "superseded"
            continue
        path = str(row.get("file") or row.get("path") or "")
        if not path:
            raise MigrationError(f"收据缺少路径：{receipt_id}")
        sha256 = row.get("sha256")
        if sha256 is not None and (
            len(str(sha256)) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in str(sha256))
        ):
            raise qualification_error("invalid_sha256", receipt_id, task_id, path)
        if row.get("exists", True) and sha256 is None:
            raise qualification_error("missing_sha256_for_existing_path", receipt_id, task_id, path)
        if path_is_authorized(task_id, path):
            dispositions[receipt_id] = "strong"
        elif completed_historical_delivery(task_id):
            dispositions[receipt_id] = "diagnostic"
            diagnostic_reasons[receipt_id] = "external_path_not_explicitly_authorized"
            diagnostic_affects_task_evidence.add(receipt_id)
        else:
            raise scope_violation(receipt_id, task_id, path)

    for receipt_id, row in receipts.items():
        event = connection.execute(
            "SELECT payload_json FROM events WHERE event_id=?",
            (row["_event_id"],),
        ).fetchone()
        if event is None:
            continue
        payload = json.loads(event["payload_json"] or "{}")
        payload.update({
            "receipt_graph_role": row.get("_graph_role"),
            "effective_tail_receipt_id": row.get("_graph_tail_id"),
            "predecessor_receipt_id": row["_immutable"]["predecessor"],
            "successor_receipt_id": row.get("_graph_successor"),
            "receipt_link_inference": row.get("_compatibility_link"),
        })
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json_text(payload), row["_event_id"]),
        )

    for receipt_id, row in receipts.items():
        if dispositions.get(receipt_id) != "diagnostic":
            continue
        task_id = str(row.get("task_id") or "")
        if task_id in historical_tasks:
            continue
        path = str(row.get("file") or row.get("path") or "")
        event = connection.execute(
            "SELECT event_type, payload_json FROM events WHERE event_id=?",
            (row["_event_id"],),
        ).fetchone()
        if event is not None:
            payload = json.loads(event["payload_json"] or "{}")
            payload.update({
                "diagnostic_only": True,
                "evidence_verified": False,
                "diagnostic_reason": diagnostic_reasons[receipt_id],
                "original_event_type": (
                    payload.get("original_event_type") or event["event_type"]
                ),
            })
            connection.execute(
                """UPDATE events
                   SET event_type='legacy_write_receipt_diagnostic', payload_json=?
                   WHERE event_id=?""",
                (json_text(payload), row["_event_id"]),
            )
        if receipt_id in diagnostic_affects_task_evidence:
            metadata = task_metadata[task_id]
            metadata["evidence_verified"] = False
            diagnostic_receipts = metadata.setdefault("diagnostic_receipts", [])
            diagnostic_record = {
                "receipt_id": receipt_id,
                "path": path,
                "reason": diagnostic_reasons[receipt_id],
            }
            if diagnostic_record not in diagnostic_receipts:
                diagnostic_receipts.append(diagnostic_record)
            connection.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id=?",
                (json_text(metadata), task_id),
            )

    for receipt_id, row in receipts.items():
        if dispositions.get(receipt_id) not in {"strong", "superseded"}:
            continue
        task_id = str(row.get("task_id") or "")
        path = str(row.get("file") or row.get("path") or "")
        if Path(path).expanduser().is_absolute():
            path = Path(path).expanduser().as_posix()
        predecessor = row["_immutable"]["predecessor"]
        database_predecessor = (
            str(predecessor)
            if predecessor
            and dispositions.get(str(predecessor)) in {"strong", "superseded"}
            else None
        )
        existing = connection.execute("SELECT * FROM write_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
        immutable = (
            task_id, path, row.get("sha256"),
            database_predecessor,
        )
        if existing:
            current = (
                existing["task_id"], existing["path"], existing["sha256"],
                existing["predecessor_receipt_id"],
            )
            if current != immutable:
                raise MigrationError(f"数据库收据内容冲突：{receipt_id}")
            continue
        connection.execute(
            """INSERT INTO write_receipts(
                 receipt_id, task_id, event_id, session_id, path, operation, sha256,
                 exists_after, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'effective', ?)""",
            (
                receipt_id, task_id, row["_event_id"], str(row.get("session_id") or "legacy"),
                path, str(row.get("operation") or "edit"), row.get("sha256"),
                1 if row.get("exists", True) else 0,
                authoritative_time(
                    row.get("ts"),
                    object_type="write_receipt",
                    object_id=receipt_id,
                    field="created_at",
                ),
            ),
        )
    for receipt_id, row in receipts.items():
        if dispositions.get(receipt_id) not in {"strong", "superseded"}:
            continue
        predecessor = row["_immutable"]["predecessor"]
        if predecessor and dispositions.get(str(predecessor)) in {"strong", "superseded"}:
            connection.execute(
                "UPDATE write_receipts SET predecessor_receipt_id=? WHERE receipt_id=?",
                (str(predecessor), receipt_id),
            )
            updated = connection.execute(
                """UPDATE write_receipts SET status='superseded', superseded_by_receipt_id=?
                   WHERE receipt_id=? AND (superseded_by_receipt_id IS NULL OR superseded_by_receipt_id=?)""",
                (receipt_id, str(predecessor), receipt_id),
            )
            if updated.rowcount != 1:
                raise MigrationError(f"数据库收据分叉：{predecessor}")


def _import_proposals(
    connection: sqlite3.Connection, workspace: str, proposals: list[dict[str, Any]]
) -> None:
    for row in proposals:
        proposal_id = str(row.get("proposal_id") or "")
        if not proposal_id:
            raise MigrationError("maintenance proposal 缺少 proposal_id")
        status = str(row.get("status") or "pending")
        if status not in PROPOSAL_STATES:
            raise MigrationError(f"维护提案状态异常：{proposal_id} -> {status}")
        session = str(row.get("session_id") or "legacy")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else dict(row)
        roots = payload.get("allowed_write_roots") or payload.get("scopes") or []
        objective_id = stable_id("OBJLEG", proposal_id)
        disclosure_id = stable_id("DLEG", proposal_id)
        created = authoritative_time(
            row.get("created_at") or row.get("ts"),
            object_type="maintenance_proposal",
            object_id=proposal_id,
            field="created_at",
        )
        expires = authoritative_time(
            row.get("expires_at"),
            object_type="maintenance_proposal",
            object_id=proposal_id,
            field="expires_at",
            default=(datetime.fromisoformat(created) + timedelta(minutes=30)).isoformat(
                timespec="microseconds"
            ),
        )
        connection.execute(
            """INSERT OR IGNORE INTO objectives(
                 objective_id, workspace_id, original_text, text_sha256,
                 created_from_conversation, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                objective_id, workspace, str(row.get("title") or proposal_id),
                hashlib.sha256(str(row.get("title") or proposal_id).encode()).hexdigest(),
                session, created,
            ),
        )
        digest_payload = {
            "proposal_id": proposal_id, "roots": canonical_scopes([str(value) for value in roots]),
            "source": row.get("_source_path"),
        }
        disclosure_digest = hashlib.sha256(json_text(digest_payload).encode()).hexdigest()
        connection.execute(
            """INSERT OR IGNORE INTO disclosures(
                 disclosure_id, objective_id, workspace_id, session_id, task_kind,
                 disclosure_digest, payload_json, displayed_at, expires_at,
                 actor_verified, disclosure_verified, sequence_verified
               ) VALUES (?, ?, ?, ?, 'control_plane_maintenance', ?, ?, ?, ?, 0, 0, 0)""",
            (disclosure_id, objective_id, workspace, session, disclosure_digest, json_text(payload), created, expires),
        )
        authorized_event = row.get("authorized_by_event_id")
        if authorized_event and not connection.execute(
            "SELECT 1 FROM user_events WHERE event_id=?", (authorized_event,)
        ).fetchone():
            authorized_event = None
        connection.execute(
            """INSERT INTO maintenance_proposals(
                 proposal_id, disclosure_id, workspace_id, session_id, scope_digest,
                 payload_json, status, created_at, expires_at, authorized_by_event_id,
                 authorized_at, consumed_at, actor_verified, disclosure_verified,
                 sequence_verified, enforcement_verified
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
               ON CONFLICT(proposal_id) DO UPDATE SET status=excluded.status,
                 payload_json=excluded.payload_json""",
            (
                proposal_id, disclosure_id, workspace, session,
                str(row.get("scope_digest") or disclosure_digest), json_text(payload), status,
                created, expires, authorized_event, row.get("authorized_at"), row.get("consumed_at"),
            ),
        )


def _bind_maintenance_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    proposal_id: str,
    card_path: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    task_row = connection.execute(
        "SELECT * FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if task_row is None:
        raise MigrationError(f"maintenance task 不存在：task_id={task_id}")
    proposal_row = connection.execute(
        """SELECT p.*, d.objective_id
           FROM maintenance_proposals p
           JOIN disclosures d ON d.disclosure_id=p.disclosure_id
           WHERE p.proposal_id=?""",
        (proposal_id,),
    ).fetchone()
    if proposal_row is None:
        raise MigrationError(
            "maintenance task 引用未知提案："
            f"task_id={task_id} proposal_id={proposal_id}"
        )

    task = dict(task_row)
    proposal = dict(proposal_row)
    if proposal["status"] != "consumed":
        raise MigrationError(
            "maintenance task 只能绑定 consumed 提案："
            f"task_id={task_id} proposal_id={proposal_id} status={proposal['status']}"
        )
    for field in ("workspace_id", "session_id"):
        if task[field] != proposal[field]:
            raise MigrationError(
                "maintenance task 与提案关系冲突："
                f"task_id={task_id} proposal_id={proposal_id} field={field} "
                f"task={task[field]} proposal={proposal[field]}"
            )

    try:
        metadata = json.loads(task["metadata_json"] or "{}")
        payload = json.loads(proposal["payload_json"] or "{}")
        task_roots = json.loads(task["allowed_write_roots_json"] or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "maintenance task/proposal JSON 损坏："
            f"task_id={task_id} proposal_id={proposal_id}"
        ) from exc
    if not isinstance(metadata, dict) or not isinstance(payload, dict) or not isinstance(task_roots, list):
        raise MigrationError(
            "maintenance task/proposal JSON 类型异常："
            f"task_id={task_id} proposal_id={proposal_id}"
        )

    proposal_platform = str(payload.get("platform") or "")
    if proposal_platform and proposal_platform != task["platform"]:
        raise MigrationError(
            "maintenance task 与提案平台冲突："
            f"task_id={task_id} proposal_id={proposal_id} "
            f"task={task['platform']} proposal={proposal_platform}"
        )
    proposal_roots = canonical_scopes([
        str(value)
        for value in (payload.get("allowed_write_roots") or payload.get("scopes") or [])
    ])
    task_roots = canonical_scopes([str(value) for value in task_roots])
    if not proposal_roots or any(
        not any(scope_covers(root, candidate) for root in proposal_roots)
        for candidate in task_roots
    ):
        raise MigrationError(
            "maintenance task 超出提案 Vault 范围："
            f"task_id={task_id} proposal_id={proposal_id}"
        )
    task_external = canonical_scopes([
        str(value) for value in (metadata.get("external_write_roots") or [])
    ])
    proposal_external = canonical_scopes([
        str(value)
        for value in (payload.get("external_write_roots") or payload.get("external_roots") or [])
    ])
    if any(
        not any(scope_covers(root, candidate) for root in proposal_external)
        for candidate in task_external
    ):
        raise MigrationError(
            "maintenance task 超出提案外部范围："
            f"task_id={task_id} proposal_id={proposal_id}"
        )

    metadata.update({"maintenance": True, "proposal_id": proposal_id})
    if card_path:
        metadata["card_path"] = card_path
    connection.execute(
        """UPDATE tasks
           SET task_kind='control_plane_maintenance', proposal_id=?,
               disclosure_id=?, objective_id=?, metadata_json=?,
               updated_at=COALESCE(?, updated_at)
           WHERE task_id=?""",
        (
            proposal_id,
            proposal["disclosure_id"],
            proposal["objective_id"],
            json_text(metadata),
            updated_at,
            task_id,
        ),
    )
    return {
        "task_id": task_id,
        "proposal_id": proposal_id,
        "disclosure_id": proposal["disclosure_id"],
        "objective_id": proposal["objective_id"],
        "maintenance": True,
    }


def _link_imported_maintenance_tasks(
    connection: sqlite3.Connection,
    cards: list[dict[str, Any]],
) -> None:
    for card in cards:
        proposal_id = card.get("proposal_id")
        if not card.get("maintenance"):
            if proposal_id:
                raise MigrationError(
                    "普通任务不得绑定维护提案："
                    f"task_id={card['task_id']} proposal_id={proposal_id}"
                )
            continue
        if not proposal_id:
            if card.get("historical_terminal"):
                continue
            raise MigrationError(
                "活动维护任务缺少提案："
                f"task_id={card['task_id']} field=maintenance_authorization_receipt"
            )
        _bind_maintenance_task(
            connection,
            task_id=card["task_id"],
            proposal_id=str(proposal_id),
        )


def _latest_delivery(connection: sqlite3.Connection, task_id: str) -> str | None:
    row = connection.execute(
        "SELECT delivery_id FROM deliveries WHERE task_id=? ORDER BY submitted_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    return str(row[0]) if row else None


def _historical_task(connection: sqlite3.Connection, task_id: str) -> bool:
    row = connection.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"任务 metadata 损坏：{task_id}") from exc
    return metadata.get("historical_terminal") is True


def _import_focus_and_decisions(
    connection: sqlite3.Connection, v9_rows: list[dict[str, Any]], decision_files: list[dict[str, Any]]
) -> None:
    focus_rows = [row for row in v9_rows if event_name(row) in {"presented_focus", "review_focus_presented"}]
    for row in focus_rows:
        data = event_data(row)
        task_id = str(data.get("task_id") or "")
        if task_id and _historical_task(connection, task_id):
            continue
        delivery_id = str(data.get("delivery_id") or _latest_delivery(connection, task_id) or "")
        focus_id = str(data.get("focus_id") or data.get("review_focus_id") or "")
        if not task_id or not delivery_id or not focus_id:
            raise MigrationError("review focus 缺少 task/delivery/focus 标识")
        delivery = connection.execute(
            "SELECT submitted_at FROM deliveries WHERE delivery_id=? AND task_id=?", (delivery_id, task_id)
        ).fetchone()
        if not delivery:
            raise MigrationError(f"review focus 引用未知交付：{focus_id}")
        presented = authoritative_time(
            data.get("presented_at") or data.get("ts"),
            object_type="review_focus",
            object_id=focus_id,
            field="presented_at",
        )
        expires = authoritative_time(
            data.get("expires_at"),
            object_type="review_focus",
            object_id=focus_id,
            field="expires_at",
            default=(datetime.fromisoformat(presented) + timedelta(days=1)).isoformat(
                timespec="microseconds"
            ),
        )
        status = str(data.get("status") or "active")
        if status not in FOCUS_STATES:
            raise MigrationError(f"review focus 状态异常：{focus_id} -> {status}")
        connection.execute(
            """INSERT INTO review_focus(
                 focus_id, task_id, delivery_id, conversation_id, submitted_at,
                 presented_at, expires_at, consumed_at, status, superseded_at,
                 superseded_by_focus_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(focus_id) DO UPDATE SET status=excluded.status,
                 consumed_at=excluded.consumed_at, superseded_at=excluded.superseded_at,
                 superseded_by_focus_id=excluded.superseded_by_focus_id""",
            (
                focus_id, task_id, delivery_id, str(data.get("conversation_id") or data.get("session_id") or "legacy"),
                delivery["submitted_at"], presented, expires, data.get("consumed_at"), status,
                data.get("superseded_at"), data.get("superseded_by_focus_id"),
            ),
        )
    decision_rows = [
        row for row in v9_rows
        if event_name(row) in {"review_decision_applied", "decision_receipt", "task_decision"}
    ] + decision_files
    for row in decision_rows:
        data = event_data(row)
        decision = str(data.get("decision") or data.get("action") or "")
        decision = {"accepted": "accept", "changes_requested": "request_changes"}.get(decision, decision)
        if decision not in {"accept", "request_changes"}:
            raise MigrationError(f"decision receipt 决策异常：{decision}")
        receipt_id = str(data.get("decision_receipt_id") or data.get("receipt_id") or "")
        user_event = str(data.get("user_event_id") or data.get("source_event_id") or "")
        focus_id = str(data.get("focus_id") or data.get("review_focus_id") or "")
        task_id = str(data.get("task_id") or "")
        if task_id and _historical_task(connection, task_id):
            continue
        delivery_id = str(data.get("delivery_id") or _latest_delivery(connection, task_id) or "")
        if not all((receipt_id, user_event, focus_id, task_id, delivery_id)):
            raise MigrationError("decision receipt 缺少绑定字段")
        dependencies = (
            connection.execute("SELECT 1 FROM user_events WHERE event_id=?", (user_event,)).fetchone(),
            connection.execute("SELECT 1 FROM review_focus WHERE focus_id=?", (focus_id,)).fetchone(),
            connection.execute("SELECT 1 FROM deliveries WHERE delivery_id=? AND task_id=?", (delivery_id, task_id)).fetchone(),
        )
        if not all(dependencies):
            raise MigrationError(f"decision receipt 依赖缺失：{receipt_id}")
        connection.execute(
            """INSERT INTO decision_receipts(
                 decision_receipt_id, user_event_id, focus_id, task_id,
                 delivery_id, decision, reason, decided_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_receipt_id) DO NOTHING""",
            (
                receipt_id, user_event, focus_id, task_id, delivery_id, decision,
                data.get("reason"),
                authoritative_time(
                    data.get("decided_at") or data.get("ts"),
                    object_type="decision_receipt",
                    object_id=receipt_id,
                    field="decided_at",
                ),
            ),
        )


def _contains_report_once_no_prompt(value: object) -> bool:
    if isinstance(value, str):
        return value == "report_once_no_prompt"
    if isinstance(value, list):
        return any(_contains_report_once_no_prompt(item) for item in value)
    if isinstance(value, dict):
        return any(
            key in {"review_prompt_policy", "interaction_preference", "preference"}
            and item == "report_once_no_prompt"
            for key, item in value.items()
        )
    return False


def _backfill_interaction_preference(connection: sqlite3.Connection, workspace: str) -> int:
    if connection.execute(
        "SELECT 1 FROM preferences WHERE workspace_id=? AND user_scope='workspace_user'",
        (workspace,),
    ).fetchone():
        return 0
    candidates: list[tuple[str, str | None]] = []
    for row in connection.execute(
        """SELECT additional_intents_json, authorized_by_event_id,
                  COALESCE(authorized_at, created_at) AS source_time
           FROM maintenance_proposals"""
    ).fetchall():
        try:
            intents = json.loads(row["additional_intents_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if _contains_report_once_no_prompt(intents):
            candidates.append((row["source_time"], row["authorized_by_event_id"]))
    for row in connection.execute(
        """SELECT l.card_json, t.updated_at, p.authorized_by_event_id
           FROM legacy_task_cards l JOIN tasks t ON t.task_id=l.task_id
           LEFT JOIN maintenance_proposals p ON p.proposal_id=t.proposal_id"""
    ).fetchall():
        try:
            card = json.loads(row["card_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if card.get("continuation_policy") in {
            "continue_until_terminal_condition", "report_once_no_prompt"
        }:
            candidates.append((row["updated_at"], row["authorized_by_event_id"]))
    if not candidates:
        return 0
    source_time, source_event = max(candidates, key=lambda item: item[0])
    if source_event and connection.execute(
        "SELECT 1 FROM user_events WHERE event_id=?", (source_event,)
    ).fetchone() is None:
        source_event = None
    connection.execute(
        """INSERT INTO preferences(
             preference_id, workspace_id, user_scope, intermediate_confirmation_policy,
             review_prompt_policy, source_user_event_id, updated_at
           ) VALUES (?, ?, 'workspace_user', 'never_within_envelope',
                     'report_once_no_prompt', ?, ?)
           ON CONFLICT(workspace_id, user_scope) DO NOTHING""",
        (stable_id("PREFLEG", workspace, "workspace_user"), workspace, source_event, source_time),
    )
    return 1


def import_legacy(
    store: StateStore, root: Path, runtime: Path, legacy: dict[str, Any]
) -> dict[str, int]:
    historical_task_ids = {
        card["task_id"] for card in legacy["cards"] if card["historical_terminal"]
    }
    receipt_rows = [
        row for row in legacy["v9_events"]
        if str(event_data(row).get("task_id") or "") not in historical_task_ids
    ]
    receipts = validate_receipt_graph(receipt_rows)
    workspace = workspace_id(root)
    with store.transaction(immediate=True) as connection:
        ensure_migration_schema(connection)
        _import_tasks(connection, workspace, legacy["cards"])
        _import_events(
            connection, workspace, legacy["v9_events"], "v9_legacy",
            diagnostic_task_ids=historical_task_ids,
        )
        _import_events(connection, workspace, legacy["v8_events"], "v8_legacy")
        _import_receipts(connection, receipts)
        _import_proposals(connection, workspace, legacy["proposals"])
        _link_imported_maintenance_tasks(connection, legacy["cards"])
        _import_focus_and_decisions(connection, legacy["v9_events"], legacy["decision_files"])
        preference_count = _backfill_interaction_preference(connection, workspace)
        for card in legacy["cards"]:
            path = root / card["source_path"]
            _upsert_source(connection, path, "task_card", 1)
        for path, kind in legacy["source_paths"]:
            count = sum(1 for row in legacy["v9_events"] + legacy["v8_events"] if row.get("_source_path") == str(path))
            if kind in {"maintenance_proposal", "decision_receipt"}:
                count = 1
            _upsert_source(connection, path, kind, count)
        connection.execute(
            """INSERT INTO migration_metadata(key, value_json, updated_at) VALUES ('last_import', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (json_text({"completed_at": now_iso(), "runtime": str(runtime)}), now_iso()),
        )
    return {
        "tasks": len(legacy["cards"]),
        "v9_events": len(legacy["v9_events"]),
        "v8_diagnostic_events": len(legacy["v8_events"]),
        "write_receipts": len(receipts),
        "maintenance_proposals": len(legacy["proposals"]),
        "preferences_backfilled": preference_count,
    }


def _run_import_migration(
    root: Path,
    *,
    runtime: Path | None = None,
    database: Path | None = None,
    activate: bool = False,
    lock_held: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    runtime = runtime_dir(root, runtime)
    database = state_database(root, runtime, database)
    store = CutoverStateStore(database)
    lock_context = nullcontext(database) if lock_held else migration_lock(database)
    with lock_context:
        store.initialize()
        with store.transaction(immediate=True) as connection:
            ensure_migration_schema(connection)
        if metadata_get(store, "backend_active", {}).get("active") is True:
            raise MigrationError("backend 已激活，禁止再次从 Legacy 导入")
        migration_phase = "finalize" if activate else "shadow"
        metadata_set(store, "migration_state", {
            "schema_version": 1,
            "phase": migration_phase,
            "status": "running",
            "database": str(database.resolve()),
            "root": str(root.resolve()),
            "runtime": str(runtime.resolve()),
            "started_at": now_iso(),
        })
        legacy = discover_legacy(root, runtime)
        counts = import_legacy(store, root, runtime, legacy)
        metadata_set(store, "backend_active", {
            "active": False, "mode": "shadow", "database": str(database), "updated_at": now_iso(),
        })
        health = database_health(database)
        metadata_set(store, "migration_state", {
            "schema_version": 1,
            "phase": migration_phase,
            "status": "succeeded",
            "database": str(database.resolve()),
            "root": str(root.resolve()),
            "runtime": str(runtime.resolve()),
            "completed_at": now_iso(),
            "quick_check": health["quick_check"],
            "integrity_check": health["integrity_check"],
            "counts": counts,
        })
        database_health(database)
        projection_paths: list[str] = []
        if activate:
            events_projection = runtime / "events/events.jsonl"
            store.export_events_jsonl(events_projection)
            record_projection(store, events_projection, "events_jsonl")
            projection_paths.append(str(events_projection))
            for card in legacy["cards"]:
                path = root / card["source_path"]
                kind = (
                    "legacy_archive"
                    if card["diagnostic_history"] or card["historical_terminal"]
                    else "task_card"
                )
                record_projection(store, path, kind)
                projection_paths.append(str(path))
            metadata_set(store, "backend_active", {
                "active": True, "mode": "sqlite", "database": str(database),
                "activated_at": now_iso(), "legacy_import_disabled": True,
            })
        return {
            "ok": True,
            "mode": "finalize" if activate else "shadow",
            "database": str(database),
            "backend_active": activate,
            "counts": counts,
            "projections": projection_paths,
        }


def _successful_import_state(store: StateStore, database: Path) -> dict[str, Any] | None:
    state = metadata_get(store, "migration_state")
    if not isinstance(state, dict):
        return None
    if state.get("status") != "succeeded" or state.get("phase") not in {"shadow", "finalize"}:
        return None
    recorded_database = state.get("database")
    if not recorded_database:
        raise MigrationError("成功迁移记录缺少 database，拒绝激活")
    if Path(str(recorded_database)).expanduser().resolve() != database.resolve():
        raise MigrationError(
            "成功迁移记录数据库路径漂移: "
            f"expected={database.resolve()}, actual={recorded_database}"
        )
    return state


def _publish_activation_marker(
    store: StateStore,
    *,
    database: Path,
    activated_at: str,
) -> dict[str, Any]:
    marker = {
        "active": True,
        "mode": "sqlite",
        "database": str(database.resolve()),
        "activated_at": activated_at,
        "legacy_import_disabled": True,
    }
    metadata_set(store, "backend_active", marker)
    return marker


def _finalize_existing_import(
    root: Path,
    *,
    runtime: Path,
    database: Path,
    legacy_reimported: bool,
    lock_held: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    runtime = runtime.expanduser().resolve()
    database = database.expanduser().resolve()
    store = CutoverStateStore(database)

    lock_context = nullcontext(database) if lock_held else migration_lock(database)
    with lock_context:
        store.initialize()
        with store.transaction(immediate=True) as connection:
            ensure_migration_schema(connection)
        state = _successful_import_state(store, database)
        if state is None:
            raise MigrationError("没有成功的 shadow/finalize 记录，拒绝无依据激活")

        with store.transaction(immediate=True) as connection:
            preference_count = _backfill_interaction_preference(
                connection, workspace_id(root)
            )

        before = activation_status(store, expected_database=database)
        marker = before["marker"]
        marker_database = marker.get("database") if isinstance(marker, dict) else None
        if marker_database and Path(str(marker_database)).expanduser().resolve() != database:
            raise MigrationError(
                "backend marker 数据库路径漂移: "
                f"expected={database}, actual={marker_database}"
            )

        health = database_health(database)
        activated_at = str(
            marker.get("activated_at")
            or state.get("completed_at")
            or now_iso()
        )

        # StateStore is authoritative and is activated first. If a later write
        # fails, readers see DB=true/marker=false and fail closed until retry.
        store.activate_backend(at=activated_at)
        if store.is_backend_active() is not True:
            raise MigrationError("StateStore activation write did not become visible")

        events_projection = refresh_state_events_projection(
            store,
            workspace_root=root,
            output=runtime / "events/events.jsonl",
        )
        projection_paths = [str(events_projection["path"])]
        projection_paths.extend(reconcile_task_projections(store))

        finalized_state = dict(state)
        finalized_counts = dict(finalized_state.get("counts") or {})
        finalized_counts["preferences"] = max(
            int(finalized_counts.get("preferences") or 0), preference_count
        )
        finalized_state.update({
            "phase": "finalize",
            "status": "succeeded",
            "database": str(database),
            "root": str(root),
            "runtime": str(runtime),
            "completed_at": (
                state.get("completed_at") if before["active"] is True else now_iso()
            ),
            "quick_check": health["quick_check"],
            "integrity_check": health["integrity_check"],
            "legacy_import_disabled": True,
            "counts": finalized_counts,
        })
        metadata_set(store, "migration_state", finalized_state)

        # Publish the control marker last and atomically. New code can therefore
        # never create marker=true/StateStore=false.
        marker = _publish_activation_marker(
            store,
            database=database,
            activated_at=activated_at,
        )
        require_active(store, expected_database=database)
        sentinel = publish_cutover_sentinel(
            database,
            state=CUTOVER_SQLITE,
            root=root,
            legacy_import_disabled=True,
            activated_at=activated_at,
            lock_held=True,
        )
        return {
            "ok": True,
            "mode": "finalize",
            "database": str(database),
            "backend_active": True,
            "activation_marker": marker,
            "cutover_sentinel": sentinel,
            "counts": finalized_state.get("counts", {}),
            "projections": projection_paths,
            "legacy_reimported": legacy_reimported,
            "reconciled": before["marker_active"] != before["state_active"],
            "idempotent": before["active"] is True,
        }


def run_migration(
    root: Path,
    *,
    runtime: Path | None = None,
    database: Path | None = None,
    activate: bool = False,
    reconcile_only: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    runtime_path = runtime_dir(root, runtime)
    database_path = state_database(root, runtime_path, database)
    if not activate:
        sentinel = load_cutover_sentinel(database_path)
        if reconcile_only:
            raise MigrationError("reconcile_only requires activate=True")
        if sentinel is None:
            publish_cutover_sentinel(
                database_path, state=CUTOVER_LEGACY, root=root,
                legacy_import_disabled=False,
            )
        elif sentinel.get("state") != CUTOVER_LEGACY:
            raise MigrationError(
                f"cutover state={sentinel.get('state')!r}，拒绝重新进入 Legacy shadow"
            )
        return _run_import_migration(
            root,
            runtime=runtime_path,
            database=database_path,
            activate=False,
        )

    with migration_lock(database_path):
        sentinel = load_cutover_sentinel(database_path)
        if sentinel and sentinel.get("state") == CUTOVER_SQLITE and not database_path.is_file():
            raise MigrationError("cutover sentinel 已宣告 SQLite，但数据库缺失；只能走恢复链")
        store = CutoverStateStore(database_path)
        store.initialize()
        with store.transaction(immediate=True) as connection:
            ensure_migration_schema(connection)
        terminal_sqlite = bool(sentinel and sentinel.get("state") == CUTOVER_SQLITE)
        if not terminal_sqlite:
            sentinel = publish_cutover_sentinel(
                database_path, state=CUTOVER_FREEZE, root=root,
                legacy_import_disabled=True,
                lock_held=True,
            )
        ready = _successful_import_state(store, database_path)
        before = activation_status(store, expected_database=database_path)
        marker = before["marker"]
        legacy_reimported = False
        if ready is None:
            if terminal_sqlite:
                raise MigrationError("SQLite 终态缺少成功迁移记录，拒绝重导 Legacy")
            if reconcile_only:
                raise MigrationError("没有成功 shadow，reconcile 禁止读取或重导 Legacy")
            if before["marker_active"] or before["state_active"]:
                raise MigrationError("激活状态存在但成功迁移记录缺失，拒绝重导 Legacy")
            if marker.get("legacy_import_disabled") is True and not terminal_sqlite:
                raise MigrationError("legacy_import_disabled=true，拒绝重导 Legacy")
            _run_import_migration(
                root, runtime=runtime_path, database=database_path,
                activate=False, lock_held=True,
            )
            legacy_reimported = True
        elif (
            ready.get("phase") == "shadow"
            and not reconcile_only
            and not before["marker_active"]
            and not before["state_active"]
            and marker.get("legacy_import_disabled") is not True
        ):
            _run_import_migration(
                root, runtime=runtime_path, database=database_path,
                activate=False, lock_held=True,
            )
            legacy_reimported = True
        return _finalize_existing_import(
            root, runtime=runtime_path, database=database_path,
            legacy_reimported=legacy_reimported, lock_held=True,
        )


def reconcile_activation(
    root: Path,
    *,
    runtime: Path | None = None,
    database: Path | None = None,
) -> dict[str, Any]:
    return run_migration(
        root,
        runtime=runtime,
        database=database,
        activate=True,
        reconcile_only=True,
    )


def reconcile_maintenance_task(
    root: Path,
    *,
    task_id: str,
    proposal_id: str,
    runtime: Path | None = None,
    database: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    runtime_path = runtime_dir(root, runtime)
    database_path = state_database(root, runtime_path, database)
    store = CutoverStateStore(database_path)
    with migration_lock(database_path):
        require_active(store, expected_database=database_path)
        database_health(database_path)
        with store.transaction(immediate=True) as connection:
            ensure_migration_schema(connection)
            before = connection.execute(
                "SELECT proposal_id, task_kind, metadata_json FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if before is None:
                raise MigrationError(f"maintenance task 不存在：task_id={task_id}")
            card_row = connection.execute(
                "SELECT source_path FROM legacy_task_cards WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if card_row is None:
                raise MigrationError(f"maintenance task 缺少确定性任务卡来源：task_id={task_id}")
            card_path = (root / str(card_row["source_path"])).resolve()
            try:
                card_path.relative_to(root)
            except ValueError as exc:
                raise MigrationError(f"maintenance task 任务卡路径越界：task_id={task_id}") from exc
            if not card_path.is_file():
                raise MigrationError(f"maintenance task 任务卡不存在：task_id={task_id}")
            relation = _bind_maintenance_task(
                connection,
                task_id=task_id,
                proposal_id=proposal_id,
                card_path=str(card_path),
                updated_at=now_iso(),
            )
            audit = {
                "action": "reconcile_maintenance_task",
                "task_id": task_id,
                "proposal_id": proposal_id,
                "before": dict(before),
                "after": relation,
                "reconciled_at": now_iso(),
                "legacy_reimported": False,
                "created_task": False,
            }
            connection.execute(
                """INSERT INTO migration_metadata(key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (f"maintenance_task_reconcile:{task_id}", json_text(audit), now_iso()),
            )
    # The projection primitive owns the cutover lock. Calling it while holding
    # migration_lock opens the same lock twice and self-deadlocks on macOS.
    projection = refresh_state_events_projection(
        store,
        workspace_root=root,
        output=runtime_path / "events/events.jsonl",
    )
    return {
        "ok": True,
        **relation,
        "database": str(database_path),
        "legacy_reimported": False,
        "created_task": False,
        "events_projection": projection["path"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤 Legacy 到 SQLite 迁移")
    parser.add_argument(
        "action",
        choices=("shadow", "finalize", "reconcile", "reconcile-maintenance-task", "status"),
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--proposal-id")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    database = state_database(root, args.runtime, args.database)
    try:
        if args.action == "status":
            sentinel = load_cutover_sentinel(database)
            if not database.is_file():
                raise MigrationError(
                    f"SQLite 数据库缺失；cutover={sentinel.get('state') if sentinel else 'missing'}"
                )
            store = StateStore(database)
            activation = activation_status(store, expected_database=database)
            result = {
                "ok": activation["consistent"], "database": str(database),
                "backend_active": activation["marker"],
                "activation": activation,
                "last_import": metadata_get(store, "last_import"),
                "cutover_sentinel": sentinel,
            }
        elif args.action == "reconcile-maintenance-task":
            if not args.task_id or not args.proposal_id:
                raise MigrationError("reconcile-maintenance-task 需要 --task-id 与 --proposal-id")
            result = reconcile_maintenance_task(
                root,
                task_id=args.task_id,
                proposal_id=args.proposal_id,
                runtime=args.runtime,
                database=args.database,
            )
        else:
            result = run_migration(
                root, runtime=args.runtime, database=args.database,
                activate=args.action in {"finalize", "reconcile"},
                reconcile_only=args.action == "reconcile",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is True else 1
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": getattr(exc, "code", type(exc).__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
