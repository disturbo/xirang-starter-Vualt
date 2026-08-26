#!/usr/bin/env python3
"""Stable read-only StateStore status CLI and Legacy-writer guard."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from xirang_state import StateStore
from xirang_state_migrate import (
    CUTOVER_FREEZE,
    CUTOVER_LEGACY,
    CUTOVER_SQLITE,
    MigrationError,
    NonAuthoritativeDatabase,
    activation_status,
    cutover_lock_path,
    discover_non_authoritative_databases,
    load_cutover_sentinel,
    state_database,
    workspace_state_binding,
)


EXIT_ACTIVE = 0
EXIT_INACTIVE = 1
EXIT_ERROR = 2


class LegacyStateWriteBlocked(RuntimeError):
    """Raised when a retired writer cannot safely use Legacy state."""


@dataclass(frozen=True)
class BackendProbe:
    active: bool | None
    database: Path
    reason: str
    cutover_state: str | None = None
    authoritative_database: Path | None = None
    workspace_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "command": "backend-active",
            "active": self.active,
            "database": str(self.database),
            "authoritative_database": str(
                self.authoritative_database or self.database
            ),
            "workspace_id": self.workspace_id,
            "reason": self.reason,
            "cutover_state": self.cutover_state,
            "read_only": True,
        }


_OPERATION_PROBE = ContextVar("xirang_cutover_operation_probe", default=None)


def default_root() -> Path:
    return Path(
        os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1])
    ).expanduser().resolve()


def resolve_database(root: Path, explicit: Path | str | None = None) -> Path:
    selected = explicit or os.environ.get("XIRANG_STATE_DB")
    return state_database(
        root.expanduser().resolve(),
        explicit=Path(selected).expanduser().resolve() if selected else None,
    )


def _binding_probe(
    root: Path,
    *,
    candidate: Path,
    reason: str,
    authoritative: Path | None = None,
) -> BackendProbe:
    root = root.expanduser().resolve()
    try:
        binding = workspace_state_binding(root)
    except MigrationError:
        binding = None
    return BackendProbe(
        None,
        candidate.expanduser().resolve(),
        reason,
        authoritative_database=(
            authoritative.expanduser().resolve()
            if authoritative is not None
            else binding.database if binding is not None else None
        ),
        workspace_id=binding.workspace_id if binding is not None else None,
    )


def _probe_backend_locked(root: Path, database: Path | str | None = None) -> BackendProbe:
    root = root.expanduser().resolve()
    try:
        db = resolve_database(root, database)
        binding = workspace_state_binding(root, database=db)
    except NonAuthoritativeDatabase as exc:
        return _binding_probe(
            root, candidate=exc.candidate, reason=exc.code,
            authoritative=exc.authoritative,
        )
    except MigrationError as exc:
        candidate = Path(database).expanduser().resolve() if database else root
        return _binding_probe(
            root,
            candidate=candidate,
            reason=f"authority_binding_invalid:{type(exc).__name__}",
        )
    try:
        sentinel = load_cutover_sentinel(db)
    except Exception as exc:
        return BackendProbe(None, db, f"sentinel_invalid:{type(exc).__name__}")
    if sentinel is None:
        return BackendProbe(None, db, "cutover_marker_missing")
    state = str(sentinel.get("state") or "")
    expected_root = Path(str(sentinel.get("workspace_root") or "")).expanduser().resolve()
    if expected_root != binding.workspace_root:
        return BackendProbe(
            None, db, "workspace_root_mismatch", state,
            binding.database, binding.workspace_id,
        )
    if str(sentinel.get("workspace_id") or "") != binding.workspace_id:
        return BackendProbe(
            None, db, "workspace_id_mismatch", state,
            binding.database, binding.workspace_id,
        )
    if state == CUTOVER_LEGACY:
        if sentinel.get("legacy_import_disabled") is True:
            return BackendProbe(None, db, "legacy_marker_contradiction", state)
        if db.is_file():
            try:
                status = activation_status(StateStore(db), expected_database=db)
            except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
                return BackendProbe(None, db, f"probe_failed:{type(exc).__name__}", state)
            if status["marker_active"] or status["state_active"]:
                return BackendProbe(None, db, "legacy_marker_but_database_active", state)
        return BackendProbe(False, db, "explicit_migration_legacy", state)
    if state == CUTOVER_FREEZE:
        return BackendProbe(None, db, "cutover_frozen", state)
    if state != CUTOVER_SQLITE:
        return BackendProbe(None, db, "cutover_state_unknown", state)
    if sentinel.get("legacy_import_disabled") is not True:
        return BackendProbe(None, db, "sqlite_marker_without_legacy_disable", state)
    if not db.is_file():
        return BackendProbe(None, db, "database_missing_after_cutover", state)
    try:
        status = activation_status(StateStore(db), expected_database=db)
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        return BackendProbe(None, db, f"probe_failed:{type(exc).__name__}", state)
    if not status["consistent"] or not status["active"]:
        return BackendProbe(None, db, "activation_not_strict", state)
    return BackendProbe(
        True, db, "active", state, binding.database, binding.workspace_id,
    )


def probe_backend(root: Path, database: Path | str | None = None) -> BackendProbe:
    """Read cutover state while sharing the migration lock, without creating files."""
    root = root.expanduser().resolve()
    try:
        db = resolve_database(root, database)
    except NonAuthoritativeDatabase as exc:
        return _binding_probe(
            root, candidate=exc.candidate, reason=exc.code,
            authoritative=exc.authoritative,
        )
    except MigrationError as exc:
        candidate = Path(database).expanduser().resolve() if database else root
        return _binding_probe(
            root,
            candidate=candidate,
            reason=f"authority_binding_invalid:{type(exc).__name__}",
        )
    held = _OPERATION_PROBE.get()
    if held is not None and held[0] == root.expanduser().resolve() and held[1] == db:
        return held[2]
    if os.environ.get("XIRANG_CUTOVER_GUARD_HELD") == "1":
        guarded = _probe_backend_locked(root, db)
        if _implicit_legacy_candidate(guarded, db):
            return BackendProbe(False, db, "implicit_legacy_locked", guarded.cutover_state)
        return guarded
    lock = cutover_lock_path(db)
    if not lock.exists():
        unlocked = _probe_backend_locked(root, db)
        if unlocked.reason == "cutover_marker_missing" and not db.exists():
            return unlocked
        return BackendProbe(None, db, "cutover_lock_missing", unlocked.cutover_state)
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return _probe_backend_locked(root, db)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def authority_doctor(root: Path, database: Path | str | None = None) -> dict[str, object]:
    """Diagnose StateStore authority without opening a non-authoritative database."""
    root = root.expanduser().resolve()
    try:
        binding = workspace_state_binding(root)
    except MigrationError as exc:
        return {
            "schema_version": 1,
            "command": "authority-doctor",
            "ok": False,
            "read_only": True,
            "workspace_root": str(root),
            "findings": [{
                "code": "authority_binding_invalid",
                "error": type(exc).__name__,
                "message": str(exc),
            }],
        }
    findings: list[dict[str, object]] = []
    if database is not None:
        candidate = Path(database).expanduser().resolve()
        if candidate != binding.database:
            findings.append({
                "code": NonAuthoritativeDatabase.code,
                "database": str(candidate),
                "authoritative_database": str(binding.database),
                "source": "doctor_argument",
            })
    for candidate in discover_non_authoritative_databases(root):
        findings.append({
            "code": NonAuthoritativeDatabase.code,
            "database": str(candidate),
            "authoritative_database": str(binding.database),
            "source": "vault_scan",
        })
    sentinel_state = None
    try:
        sentinel = load_cutover_sentinel(binding.database)
        sentinel_state = sentinel.get("state") if sentinel else None
        if sentinel is None:
            findings.append({
                "code": "cutover_marker_missing",
                "database": str(binding.database),
            })
        else:
            if Path(str(sentinel.get("workspace_root") or "")).expanduser().resolve() != binding.workspace_root:
                findings.append({
                    "code": "workspace_root_mismatch",
                    "database": str(binding.database),
                })
            if str(sentinel.get("workspace_id") or "") != binding.workspace_id:
                findings.append({
                    "code": "workspace_id_mismatch",
                    "database": str(binding.database),
                })
    except MigrationError as exc:
        findings.append({
            "code": "cutover_binding_invalid",
            "database": str(binding.database),
            "message": str(exc),
        })
    if not binding.cutover_lock.is_file():
        findings.append({
            "code": "cutover_lock_missing",
            "database": str(binding.database),
            "cutover_lock": str(binding.cutover_lock),
        })
    return {
        "schema_version": 1,
        "command": "authority-doctor",
        "ok": not findings,
        "read_only": True,
        "workspace_root": str(binding.workspace_root),
        "workspace_id": binding.workspace_id,
        "local_config": str(binding.local_config),
        "runtime_dir": str(binding.runtime_dir),
        "database": str(binding.database),
        "cutover_sentinel": str(binding.cutover_sentinel),
        "cutover_lock": str(binding.cutover_lock),
        "cutover_state": sentinel_state,
        "findings": findings,
    }


def sqlite_authority_artifacts_present(database: Path | str) -> bool:
    """Distinguish real SQLite state from the sole lock created for Legacy serialization."""
    database = Path(database).expanduser().resolve()
    sentinel = Path(str(database) + ".cutover.json")
    if database.exists() or sentinel.exists():
        return True
    state_dir = database.parent
    if not state_dir.exists():
        return False
    if state_dir.is_symlink() or not state_dir.is_dir():
        return True
    lock = cutover_lock_path(database)
    try:
        entries = list(state_dir.iterdir())
    except OSError:
        return True
    return not (
        len(entries) == 1 and entries[0] == lock
        and lock.is_file() and not lock.is_symlink()
    )


def _implicit_legacy_candidate(probe: BackendProbe, database: Path) -> bool:
    return (
        probe.reason == "cutover_marker_missing"
        and not sqlite_authority_artifacts_present(database)
    )


@contextmanager
def backend_operation_guard(
    root: Path, database: Path | str | None = None, *, component: str,
):
    """Hold a shared cutover lock only while a Legacy operation is running."""
    root = root.expanduser().resolve()
    db = resolve_database(root, database)
    held = _OPERATION_PROBE.get()
    if held is not None and held[0] == root and held[1] == db:
        yield held[2]
        return
    initial = probe_backend(root, db)
    implicit_legacy = _implicit_legacy_candidate(initial, db)
    if initial.active is True:
        yield initial
        return
    if initial.active is None and not implicit_legacy:
        raise LegacyStateWriteBlocked(
            f"{component} 无法确认 backend 状态，按 fail-closed 拒绝操作：{initial.reason}"
        )
    lock = cutover_lock_path(db)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    token = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        current = _probe_backend_locked(root, db)
        current_implicit_legacy = _implicit_legacy_candidate(current, db)
        if current.active is True:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            descriptor = -1
            yield current
            return
        if current.active is None and not current_implicit_legacy:
            raise LegacyStateWriteBlocked(
                f"{component} cutover 期间 backend 已变化，拒绝继续 Legacy 操作：{current.reason}"
            )
        if current_implicit_legacy:
            current = BackendProbe(False, db, "implicit_legacy_locked", CUTOVER_LEGACY)
        token = _OPERATION_PROBE.set((root, db, current))
        yield current
    finally:
        if token is not None:
            _OPERATION_PROBE.reset(token)
        if descriptor >= 0:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def cutover_guarded(component: str):
    """Decorate an operation whose first argument is its workspace root."""
    def decorate(function):
        @wraps(function)
        def guarded(root: Path, *args, **kwargs):
            with backend_operation_guard(Path(root), component=component):
                return function(root, *args, **kwargs)
        return guarded
    return decorate


def require_legacy_write_allowed(
    root: Path,
    database: Path | str | None = None,
    *,
    component: str = "legacy_writer",
) -> BackendProbe:
    probe = probe_backend(root, database)
    if probe.active is True:
        raise LegacyStateWriteBlocked(
            f"{component} 已退役：SQLite backend 已激活，拒绝写入 Legacy 状态"
        )
    if probe.active is None:
        raise LegacyStateWriteBlocked(
            f"{component} 无法确认 backend 状态，按 fail-closed 拒绝 Legacy 写入"
        )
    return probe


def run_legacy_guarded(
    root: Path,
    command: list[str],
    *,
    database: Path | str | None = None,
    component: str,
    advisory: bool,
) -> int:
    db = resolve_database(root, database)
    lock = cutover_lock_path(db)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        probe = probe_backend(root, db)
        legacy_available = probe.active is False or _implicit_legacy_candidate(probe, db)
        if not advisory and not legacy_available:
            legacy_label = {
                "pre-write-hook": "Legacy pre-write hook",
                "post-write-hook": "Legacy post-write hook",
            }.get(component, component)
            if probe.active is True:
                message = f"{legacy_label} 已退役；SQLite backend 已激活"
            else:
                message = f"无法确认 backend 状态；{component} fail-closed：{probe.reason}"
            print(f"[XIRANG-V3-BLOCK] {message}", file=sys.stderr)
            return EXIT_ERROR
        environment = os.environ.copy()
        environment["XIRANG_CUTOVER_GUARD_HELD"] = "1"
        return subprocess.run(command, env=environment, check=False).returncode
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤 StateStore 只读状态 CLI")
    subparsers = parser.add_subparsers(dest="action", required=True)
    active = subparsers.add_parser("backend-active", help="只读查询 SQLite backend 激活状态")
    active.add_argument("--root", type=Path, default=default_root())
    active.add_argument("--database", type=Path)
    active.add_argument("--quiet", action="store_true")
    doctor = subparsers.add_parser(
        "authority-doctor",
        help="只读核对 local-config、workspace、runtime、cutover 与陈旧数据库",
    )
    doctor.add_argument("--root", type=Path, default=default_root())
    doctor.add_argument("--database", type=Path)
    reconcile = subparsers.add_parser(
        "reconcile-existing-authority",
        help="从已消费的既有维护授权恢复 legacy_import task 的 proof/owner lease",
    )
    reconcile.add_argument("--database", type=Path, required=True)
    reconcile.add_argument("--root", type=Path, default=default_root())
    reconcile.add_argument("--task-id", required=True)
    reconcile.add_argument("--proposal-id", required=True)
    reconcile.add_argument("--session-id", required=True)
    reconcile.add_argument("--workspace-id", required=True)
    guarded = subparsers.add_parser("legacy-guard-exec", help="在共享 cutover 锁内执行 Legacy shell")
    guarded.add_argument("--root", type=Path, default=default_root())
    guarded.add_argument("--database", type=Path)
    guarded.add_argument("--component", required=True)
    guarded.add_argument("--advisory", action="store_true")
    guarded.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.action == "authority-doctor":
        result = authority_doctor(args.root, args.database)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_ACTIVE if result["ok"] is True else EXIT_ERROR

    if args.action == "reconcile-existing-authority":
        try:
            database = resolve_database(args.root, args.database)
            result = StateStore(database).reconcile_existing_legacy_task_authority(
                task_id=args.task_id,
                proposal_id=args.proposal_id,
                session_id=args.session_id,
                workspace_id=args.workspace_id,
            )
        except Exception as exc:
            print(json.dumps({"ok": False,
                              "error": getattr(exc, "code", type(exc).__name__),
                              "message": str(exc)},
                             ensure_ascii=False, sort_keys=True))
            return EXIT_ERROR
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        return EXIT_ACTIVE

    if args.action == "legacy-guard-exec":
        command = list(args.argv)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("legacy-guard-exec 缺少命令", file=sys.stderr)
            return EXIT_ERROR
        return run_legacy_guarded(
            args.root, command, database=args.database,
            component=args.component, advisory=args.advisory,
        )

    probe = probe_backend(args.root, args.database)
    if not args.quiet:
        print(json.dumps(probe.to_dict(), ensure_ascii=False, sort_keys=True))
    if probe.active is True:
        return EXIT_ACTIVE
    if probe.active is False:
        return EXIT_INACTIVE
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
