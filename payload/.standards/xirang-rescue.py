#!/usr/bin/env python3
"""Fail-closed Xi Rang StateStore rescue with snapshot and append-only audit.

This tool never edits Vault knowledge files and never restores a snapshot by
itself.  Mutating actions require an active authorized maintenance task.
"""

from __future__ import annotations

import sys

# The production rescue bundle is deliberately self-contained.  Avoid creating
# unmanaged __pycache__ files in ~/.xirang/bin while importing its local bundle.
sys.dont_write_bytecode = True

import argparse
import fcntl
import json
import os
import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xirang_state import (
    OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH,
    OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON,
    SCHEMA_VERSION,
    StateStore,
    canonical_grants,
    canonical_operations,
    canonical_scopes,
    scope_covers,
)
from xirang_state_backup import verify_snapshot
from xirang_recovery_roots import load_registry, require_registered


TERMINAL = {"submitted", "completed", "canceled", "archived", "legacy_unreviewed", "invalid_envelope"}
VAULT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_METADATA = Path(__file__).resolve().with_name("xirang-rescue-install.json")


def resolve_recovery_registry() -> Path:
    """Resolve only the workspace registry pinned by source layout or installer."""
    source_registry = VAULT_ROOT / ".xirang/contract/recovery-roots.yaml"
    if source_registry.is_file():
        return source_registry.resolve()
    if not INSTALL_METADATA.is_file():
        return source_registry.resolve()
    metadata = json.loads(INSTALL_METADATA.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise RuntimeError("installed rescue metadata schema is invalid")
    workspace = Path(str(metadata.get("workspace_root") or "")).expanduser().resolve()
    registry = Path(str(metadata.get("registry_path") or "")).expanduser().resolve()
    if registry != (workspace / ".xirang/contract/recovery-roots.yaml").resolve():
        raise RuntimeError("installed rescue registry is not the workspace contract path")
    if not registry.is_file():
        raise RuntimeError("installed rescue registry is missing")
    expected_hash = str(metadata.get("registry_sha256") or "")
    actual_hash = hashlib.sha256(registry.read_bytes()).hexdigest()
    if not expected_hash or actual_hash != expected_hash:
        raise RuntimeError("installed rescue registry hash drifted; redeploy is required")
    expected_workspace = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    if metadata.get("workspace_id") != expected_workspace:
        raise RuntimeError("installed rescue workspace binding is invalid")
    return registry


RECOVERY_REGISTRY = resolve_recovery_registry()


def registered_workspace_root(
    registry_path: Path = RECOVERY_REGISTRY,
) -> tuple[Path, str]:
    """Return the workspace root/id proven by the pinned recovery registry."""
    registry_path = registry_path.expanduser().resolve()
    workspace = registry_path.parents[2]
    if registry_path != (workspace / ".xirang/contract/recovery-roots.yaml").resolve():
        raise RuntimeError("recovery registry is not the canonical workspace contract path")
    registry = load_registry(registry_path)
    expected_workspace_id = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    if str(registry.get("workspace_id") or "") != expected_workspace_id:
        raise RuntimeError("recovery registry workspace binding is invalid")
    if INSTALL_METADATA.is_file():
        metadata = json.loads(INSTALL_METADATA.read_text(encoding="utf-8"))
        if (
            metadata.get("schema_version") != 1
            or metadata.get("workspace_id") != expected_workspace_id
            or Path(str(metadata.get("workspace_root") or "")).expanduser().resolve() != workspace
            or Path(str(metadata.get("registry_path") or "")).expanduser().resolve() != registry_path
        ):
            raise RuntimeError("installed rescue workspace binding is invalid")
    return workspace, expected_workspace_id


def require_authoritative_database(
    database: Path, *, registry_path: Path = RECOVERY_REGISTRY,
) -> Path:
    """Reject mutating rescue calls against anything except this workspace authority DB."""
    _workspace, workspace_id = registered_workspace_root(registry_path)
    expected = (
        Path.home() / ".xirang/workspaces" / workspace_id / "state/state.sqlite3"
    ).resolve()
    resolved = database.expanduser().resolve()
    if resolved != expected:
        raise RuntimeError(
            f"mutating rescue database is not the installed workspace authority: {resolved}"
        )
    return resolved


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def append_audit(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_recovery_paths(
    snapshot: Path, audit: Path, *, registry_path: Path = RECOVERY_REGISTRY,
) -> dict[str, str]:
    """Reject ad-hoc recovery artifacts before opening the authority database."""
    registry = load_registry(registry_path)
    snapshot_registration = require_registered(snapshot, registry, kind="objects")
    audit_registration = require_registered(audit, registry, kind="audit")
    if audit.suffix != ".jsonl":
        raise RuntimeError("rescue audit must be a registered .jsonl path")
    return {"snapshot": snapshot_registration, "audit": audit_registration}


def inspect(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    workspace_id: str | None = None,
) -> dict:
    connection.row_factory = sqlite3.Row
    if workspace_id is None:
        candidates = connection.execute(
            "SELECT DISTINCT workspace_id FROM tasks WHERE session_id=?",
            (session_id,),
        ).fetchall()
        if len(candidates) == 1:
            workspace_id = str(candidates[0]["workspace_id"])
    if workspace_id:
        active_query = """SELECT task_id,lifecycle_status,runtime_status,review_status,proposal_id,session_id
             FROM tasks WHERE workspace_id=?
               AND lifecycle_status IN ('authorized','in_progress','blocked')
               AND runtime_status NOT IN ('canceled','submitted','completed') ORDER BY created_at"""
        active_params = (workspace_id,)
    else:
        active_query = """SELECT task_id,lifecycle_status,runtime_status,review_status,proposal_id,session_id
             FROM tasks WHERE session_id=?
               AND lifecycle_status IN ('authorized','in_progress','blocked')
               AND runtime_status NOT IN ('canceled','submitted','completed') ORDER BY created_at"""
        active_params = (session_id,)
    active = [dict(row) for row in connection.execute(
        active_query,
        active_params,
    )]
    terminal_leases = [dict(row) for row in connection.execute(
        """SELECT l.lease_id,l.task_id,l.status,t.lifecycle_status,t.runtime_status
           FROM leases l JOIN tasks t ON t.task_id=l.task_id
           WHERE l.status='active' AND (
             t.lifecycle_status IN ('submitted','completed','canceled','archived','legacy_unreviewed','invalid_envelope')
             OR t.runtime_status IN ('submitted','completed','canceled','archived','invalid_envelope'))
           ORDER BY l.issued_at"""
    )]
    return {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "inspection_scope": "workspace" if workspace_id else "session",
        "active_tasks": active,
        "terminal_active_leases": terminal_leases,
    }


def require_maintenance_task(
    connection: sqlite3.Connection, task_id: str, session_id: str,
    *, required_operations: set[str] | None = None,
    operation_targets: list[tuple[str, str | Path]] | None = None,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM tasks WHERE task_id=? AND session_id=?
           AND task_kind='control_plane_maintenance'
           AND lifecycle_status IN ('authorized','in_progress','blocked')
           AND runtime_status NOT IN ('canceled','submitted','completed')""",
        (task_id, session_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("repair-invalid-envelope requires an active maintenance task")
    if not StateStore._execution_authority_in_connection(connection, row):
        raise RuntimeError("maintenance task has no valid authorization envelope")
    operations = set(canonical_operations(
        json.loads(row["allowed_operations_json"] or "[]"), task_kind=row["task_kind"],
    ))
    missing = set(required_operations or ()) - operations
    if missing:
        raise RuntimeError(f"maintenance task lacks rescue operations: {sorted(missing)}")
    roots = json.loads(row["allowed_write_roots_json"] or "[]")
    grants = json.loads(row["grants_json"] or "[]") or canonical_grants(
        roots, task_kind=row["task_kind"], operations=sorted(operations),
    )
    for operation, target in operation_targets or []:
        raw_target = str(target)
        candidate = Path(raw_target).expanduser()
        normalized = str(candidate.resolve()) if candidate.is_absolute() else canonical_scopes([raw_target])[0]
        if not any(
            scope_covers(str(grant.get("path") or ""), normalized)
            and operation in (grant.get("operations") or [])
            for grant in grants
        ):
            raise RuntimeError(f"maintenance task lacks path operation grant: {normalized} + {operation}")
    return row


def repair(database: Path, snapshot: Path, audit: Path, session_id: str,
           maintenance_task_id: str, keep_task: str | None, cancel_tasks: list[str],
           *, registry_path: Path = RECOVERY_REGISTRY) -> dict:
    registrations = validate_recovery_paths(snapshot, audit, registry_path=registry_path)
    workspace_root, _workspace_id = registered_workspace_root(registry_path)
    verified = verify_snapshot(
        snapshot,
        expected_database=database,
        root=workspace_root,
    )
    lock_path = Path(str(database) + ".cutover.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            maintainer = require_maintenance_task(
                connection, maintenance_task_id, session_id,
                required_operations={"lease_revoke", "audit_append", "verify"},
                operation_targets=[("audit_append", audit), ("verify", snapshot)],
            )
            if verified.get("workspace_id") not in (None, maintainer["workspace_id"]):
                raise RuntimeError("snapshot workspace does not match maintenance envelope")
            if keep_task != maintenance_task_id:
                raise RuntimeError("state rescue must keep its active maintenance task")
            before = inspect(connection, session_id, workspace_id=maintainer["workspace_id"])
            active_ids = {row["task_id"] for row in before["active_tasks"]}
            if keep_task and keep_task not in active_ids:
                raise RuntimeError("keep task is not active")
            if set(cancel_tasks) - active_ids:
                raise RuntimeError("cancel target is not active")
            if keep_task and keep_task in cancel_tasks:
                raise RuntimeError("keep task cannot also be canceled")
            if active_ids - set(cancel_tasks) != ({keep_task} if keep_task else set()):
                raise RuntimeError("plan does not account for every active task")
            at = stamp()
            for task_id in cancel_tasks:
                connection.execute(
                    "UPDATE tasks SET lifecycle_status='canceled',runtime_status='canceled',updated_at=? WHERE task_id=?",
                    (at, task_id),
                )
                connection.execute(
                    "UPDATE leases SET status='revoked' WHERE task_id=? AND status='active'", (task_id,),
                )
            connection.execute(
                """UPDATE leases SET status=CASE
                     WHEN task_id IN (SELECT task_id FROM tasks WHERE lifecycle_status='canceled'
                                      OR runtime_status='canceled') THEN 'revoked' ELSE 'completed' END
                   WHERE status='active' AND task_id IN (
                     SELECT task_id FROM tasks WHERE lifecycle_status IN
                       ('submitted','completed','canceled','archived','legacy_unreviewed')
                       OR runtime_status IN ('submitted','completed','canceled','archived'))"""
            )
            after = inspect(connection, session_id, workspace_id=maintainer["workspace_id"])
            if len(after["active_tasks"]) > 1 or after["terminal_active_leases"]:
                raise RuntimeError("rescue postcondition failed")
            record = {"schema_version": 2, "recorded_at": stamp(), "operation": "state_rescue",
                      "transaction_status": "commit_requested", "database": str(database),
                      "snapshot": str(snapshot), "snapshot_sha256": verified["sha256"],
                      "maintenance_task_id": maintenance_task_id,
                      "recovery_registrations": registrations,
                      "before": before, "after": after}
            append_audit(audit, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def repair_invalid_envelope(
    database: Path, snapshot: Path, audit: Path, session_id: str,
    maintenance_task_id: str, old_task_id: str, corrected_roots: list[str],
    *, failpoint: str | None = None, registry_path: Path = RECOVERY_REGISTRY,
) -> dict:
    """Atomically invalidate a provably malformed scope and create its corrected successor."""
    registrations = validate_recovery_paths(snapshot, audit, registry_path=registry_path)
    workspace_root, _workspace_id = registered_workspace_root(registry_path)
    verified = verify_snapshot(
        snapshot, expected_database=database, root=workspace_root,
    )
    roots = canonical_scopes(corrected_roots)
    if not roots:
        raise RuntimeError("corrected scope is empty")
    lock_path = Path(str(database) + ".cutover.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if int(version or 0) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"rescue requires schema {SCHEMA_VERSION}; migrate in a separate audited transaction"
                )
            maintainer = require_maintenance_task(
                connection, maintenance_task_id, session_id,
                required_operations={"envelope_repair", "lease_revoke", "audit_append", "verify"},
                operation_targets=[("audit_append", audit), ("verify", snapshot)],
            )
            if verified.get("workspace_id") not in (None, maintainer["workspace_id"]):
                raise RuntimeError("snapshot workspace does not match maintenance envelope")
            old = connection.execute("SELECT * FROM tasks WHERE task_id=?", (old_task_id,)).fetchone()
            if old is None:
                raise RuntimeError("old task does not exist")
            if old["lifecycle_status"] not in {"authorized", "in_progress", "blocked"}:
                raise RuntimeError("old task is not active")
            old_roots = json.loads(old["allowed_write_roots_json"] or "[]")
            if len(old_roots) != 1 or not any(mark in old_roots[0] for mark in (";", "；", "\n", "\r", ",", "，")):
                raise RuntimeError("old task is not a machine-provable serialized scope error")
            raw_parts = re.split(r"[;；,，\n\r]", old_roots[0])
            if not raw_parts or any(not part.strip() for part in raw_parts):
                raise RuntimeError("serialized scope contains empty or ambiguous path components")
            parsed_roots = canonical_scopes([part.strip() for part in raw_parts])
            if roots != parsed_roots:
                raise RuntimeError(
                    f"corrected scope must equal deterministic serialized path set: {parsed_roots}"
                )
            chain = connection.execute(
                """SELECT p.*, d.objective_id, d.workspace_id AS disclosure_workspace_id,
                          u.event_id AS source_event_id, u.workspace_id AS event_workspace_id,
                          u.consumed_at AS event_consumed_at
                   FROM maintenance_proposals p
                   JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                   JOIN user_events u ON u.event_id=p.authorized_by_event_id
                   WHERE p.proposal_id=? AND p.disclosure_id=?""",
                (old["proposal_id"], old["disclosure_id"]),
            ).fetchone()
            if chain is None or not chain["event_consumed_at"] or chain["status"] != "consumed":
                raise RuntimeError("original authorization chain is missing or unconsumed")
            original_payload = json.loads(chain["payload_json"] or "{}")
            original_roots = list(
                original_payload.get("allowed_write_roots") or original_payload.get("scopes") or []
            )
            if original_roots != old_roots:
                raise RuntimeError("old task scope no longer matches its immutable proposal")
            if not (
                chain["objective_id"] == old["objective_id"]
                and chain["workspace_id"] == old["workspace_id"] == chain["disclosure_workspace_id"]
                and chain["event_workspace_id"] == old["workspace_id"] == maintainer["workspace_id"]
            ):
                raise RuntimeError("original authorization chain binding mismatch")
            maintenance_roots = json.loads(maintainer["allowed_write_roots_json"] or "[]")
            for root in roots:
                if not any(scope_covers(parent, root) for parent in maintenance_roots):
                    raise RuntimeError(f"corrected scope expands beyond maintenance authorization: {root}")

            old_operations = json.loads(old["allowed_operations_json"] or "[]")
            if not old_operations:
                raise RuntimeError("old operation authority is not machine-provable")
            operations = canonical_operations(old_operations, task_kind=old["task_kind"])
            grants = canonical_grants(roots, task_kind=old["task_kind"], operations=operations)
            require_maintenance_task(
                connection, maintenance_task_id, session_id,
                required_operations={"envelope_repair"},
                operation_targets=[("envelope_repair", root) for root in roots],
            )
            repair_id, disclosure_id, proposal_id = new_id("ER"), new_id("D"), new_id("M")
            new_task_id, envelope_id, lease_id, stage_run_id = (
                new_id("T"), new_id("ENV"), new_id("L"), new_id("SR")
            )
            now = stamp()
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="microseconds")
            excluded = json.loads(old["excluded_actions_json"] or "[]")
            machine = {
                "objective_record_id": old["objective_id"], "task_kind": old["task_kind"],
                "allowed_write_roots": roots, "external_write_roots": [],
                "allowed_operations": operations, "grants": grants,
                "excluded_actions": excluded, "irreversible_effects": [],
                "external_effects": [], "acceptance_owner": "user",
            }
            disclosure_payload = {
                "objective_record_id": old["objective_id"], "task_kind": old["task_kind"],
                "allowed_write_roots": roots, "external_write_roots": [],
                "excluded_actions": excluded, "machine": machine,
                "repair_record_id": repair_id,
            }
            disclosure_digest = digest(machine)
            proposal_payload = {
                "objective_record_id": old["objective_id"], "task_kind": old["task_kind"],
                "title": json.loads(old["metadata_json"] or "{}").get("title"),
                "allowed_write_roots": roots, "scopes": roots,
                "allowed_operations": operations, "grants": grants,
                "external_write_roots": [], "external_roots": [],
                "excluded_actions": excluded, "excludes": excluded,
                "repair_record_id": repair_id,
            }
            maintainer_proposal = connection.execute(
                "SELECT authorized_by_event_id FROM maintenance_proposals WHERE proposal_id=?",
                (maintainer["proposal_id"],),
            ).fetchone()
            if maintainer_proposal is None or not maintainer_proposal["authorized_by_event_id"]:
                raise RuntimeError("maintenance authorization event is missing")
            current_authorization_event = maintainer_proposal["authorized_by_event_id"]

            connection.execute(
                """INSERT INTO disclosures(
                       disclosure_id,objective_id,workspace_id,session_id,task_kind,
                       disclosure_digest,payload_json,displayed_at,expires_at,
                       actor_verified,disclosure_verified,sequence_verified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (disclosure_id, old["objective_id"], old["workspace_id"], old["session_id"],
                 old["task_kind"], disclosure_digest, json.dumps(disclosure_payload, ensure_ascii=False, sort_keys=True),
                 now, expires, old["actor_verified"], old["disclosure_verified"], old["sequence_verified"]),
            )
            connection.execute(
                """INSERT INTO maintenance_proposals(
                       proposal_id,disclosure_id,workspace_id,session_id,platform,scope_digest,
                       payload_json,additional_intents_json,status,created_at,expires_at,
                       authorized_by_event_id,authorized_at,consumed_at,
                       actor_verified,disclosure_verified,sequence_verified,enforcement_verified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, disclosure_id, old["workspace_id"], old["session_id"], old["platform"],
                 digest({"roots": roots, "operations": operations, "grants": grants}),
                 json.dumps(proposal_payload, ensure_ascii=False, sort_keys=True),
                 chain["additional_intents_json"], "consumed", now, expires,
                 current_authorization_event, now, now, old["actor_verified"], old["disclosure_verified"],
                 old["sequence_verified"], old["enforcement_verified"]),
            )
            envelope_digest = digest({
                "task_id": new_task_id, "disclosure_digest": disclosure_digest,
                "authorization_event_id": current_authorization_event, "authorization_version": 1,
            })
            metadata = json.loads(old["metadata_json"] or "{}")
            metadata.update({
                "repair_record_id": repair_id,
                "repaired_from_task_id": old_task_id,
                "proposal_id": proposal_id,
            })
            old_card = str(metadata.get("card_path") or "")
            if old_card:
                metadata["card_path"] = str(Path(old_card).with_name(f"{new_task_id}.md"))
            connection.execute(
                """INSERT INTO tasks(
                       task_id,envelope_id,workspace_id,session_id,platform,task_kind,
                       objective_id,disclosure_id,proposal_id,envelope_digest,
                       lifecycle_status,runtime_status,review_status,allowed_write_roots_json,
                       allowed_operations_json,grants_json,excluded_actions_json,metadata_json,
                       actor_verified,disclosure_verified,sequence_verified,enforcement_verified,
                       created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,'in_progress','authorized','draft',?,?,?,?,?,?,?,?,?,?,?)""",
                (new_task_id, envelope_id, old["workspace_id"], old["session_id"], old["platform"], old["task_kind"],
                 old["objective_id"], disclosure_id, proposal_id, envelope_digest,
                 json.dumps(roots, ensure_ascii=False), json.dumps(operations, ensure_ascii=False),
                 json.dumps(grants, ensure_ascii=False, sort_keys=True), old["excluded_actions_json"],
                 json.dumps(metadata, ensure_ascii=False, sort_keys=True), old["actor_verified"],
                 old["disclosure_verified"], old["sequence_verified"], old["enforcement_verified"], now, now),
            )
            connection.execute(
                """INSERT INTO leases(
                       lease_id,task_id,source_session_id,worker_session_id,role,
                       allowed_write_roots_json,allowed_operations_json,grants_json,
                       read_only,status,issued_at,expires_at,enforcement_verified
                   ) VALUES (?,?,?,?,?,?,?,?,0,'active',?,?,?)""",
                (lease_id, new_task_id, old["session_id"], old["session_id"], "owner",
                 json.dumps(roots, ensure_ascii=False), json.dumps(operations, ensure_ascii=False),
                 json.dumps(grants, ensure_ascii=False, sort_keys=True), now, expires, old["enforcement_verified"]),
            )
            connection.execute(
                """INSERT INTO stage_runs(stage_run_id,task_id,stage,review_round,status,started_at,details_json)
                   VALUES (?,?,'authorized',0,'active',?,?)""",
                (stage_run_id, new_task_id, now, json.dumps({"repaired_from_task_id": old_task_id})),
            )
            connection.execute(
                """INSERT INTO envelope_repairs(
                       repair_id,workspace_id,old_task_id,new_task_id,maintenance_task_id,
                       source_user_event_id,old_roots_json,corrected_roots_json,
                       corrected_grants_json,reason,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (repair_id, old["workspace_id"], old_task_id, new_task_id, maintenance_task_id,
                 chain["source_event_id"], json.dumps(old_roots, ensure_ascii=False),
                 json.dumps(roots, ensure_ascii=False), json.dumps(grants, ensure_ascii=False, sort_keys=True),
                 "machine_provable_serialized_scope_repaired_under_current_maintenance_authorization", now),
            )
            connection.execute(
                "UPDATE stage_runs SET status='completed',finished_at=?,details_json=? WHERE task_id=? AND status='active'",
                (now, json.dumps({"invalidated_by_repair_id": repair_id}), old_task_id),
            )
            connection.execute("UPDATE leases SET status='revoked' WHERE task_id=? AND status='active'", (old_task_id,))
            connection.execute(
                "UPDATE tasks SET lifecycle_status='invalid_envelope',runtime_status='invalid_envelope',updated_at=? WHERE task_id=?",
                (now, old_task_id),
            )
            if failpoint == "after_old_invalidated":
                raise RuntimeError("injected crash after old task invalidated")

            old_active = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (old_task_id,)
            ).fetchone()[0]
            new_active = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (new_task_id,)
            ).fetchone()[0]
            if old_active != 0 or new_active != 1:
                raise RuntimeError("envelope repair lease postcondition failed")
            record = {
                "schema_version": 1, "recorded_at": stamp(),
                "operation": "repair_invalid_envelope", "transaction_status": "commit_requested",
                "database": str(database), "snapshot": str(snapshot),
                "snapshot_sha256": verified["sha256"], "maintenance_task_id": maintenance_task_id,
                "repair_id": repair_id, "old_task_id": old_task_id, "new_task_id": new_task_id,
                "source_user_event_id": chain["source_event_id"], "old_roots": old_roots,
                "corrected_roots": roots, "old_active_leases_after": old_active,
                "new_active_leases_after": new_active,
                "recovery_registrations": registrations,
            }
            append_audit(audit, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def repair_omitted_agent_registry_dependency(
    database: Path, snapshot: Path, audit: Path, session_id: str,
    maintenance_task_id: str, old_task_id: str,
    *, failpoint: str | None = None, registry_path: Path = RECOVERY_REGISTRY,
) -> dict:
    """Repair the one canonical Agent registry omitted from an all-Agent envelope.

    This is deliberately not a general scope-expansion primitive or user-event
    replay.  It accepts no
    caller-selected path, requires the immutable objective to name all-Agent
    management/canary, preserves every existing operation and grant, and adds
    only update/control_plane_patch for the canonical registry file.
    """
    if maintenance_task_id != old_task_id:
        raise RuntimeError("omitted registry repair must be a self-repair of the malformed envelope")
    registrations = validate_recovery_paths(snapshot, audit, registry_path=registry_path)
    workspace_root, _workspace_id = registered_workspace_root(registry_path)
    verified = verify_snapshot(
        snapshot, expected_database=database, root=workspace_root,
    )
    lock_path = Path(str(database) + ".cutover.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if int(version or 0) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"rescue requires schema {SCHEMA_VERSION}; migrate in a separate audited transaction"
                )
            old = require_maintenance_task(
                connection, maintenance_task_id, session_id,
                required_operations={"envelope_repair", "lease_revoke", "audit_append", "verify"},
                operation_targets=[("audit_append", audit), ("verify", snapshot)],
            )
            if verified.get("workspace_id") not in (None, old["workspace_id"]):
                raise RuntimeError("snapshot workspace does not match maintenance envelope")
            if old["lifecycle_status"] not in {"authorized", "in_progress", "blocked"}:
                raise RuntimeError("old task is not active")
            if old["task_kind"] != "control_plane_maintenance":
                raise RuntimeError("omitted registry repair only applies to control-plane maintenance")

            chain = connection.execute(
                """SELECT p.*, d.objective_id, d.workspace_id AS disclosure_workspace_id,
                          d.task_kind AS disclosure_task_kind,
                          d.disclosure_digest, d.payload_json AS disclosure_payload_json,
                          u.event_id AS source_event_id, u.workspace_id AS event_workspace_id,
                          u.consumed_at AS event_consumed_at, o.original_text
                   FROM maintenance_proposals p
                   JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                   JOIN user_events u ON u.event_id=p.authorized_by_event_id
                   JOIN objectives o ON o.objective_id=d.objective_id
                   WHERE p.proposal_id=? AND p.disclosure_id=?""",
                (old["proposal_id"], old["disclosure_id"]),
            ).fetchone()
            if chain is None or not chain["event_consumed_at"] or chain["status"] != "consumed":
                raise RuntimeError("original authorization chain is missing or unconsumed")
            if not any(
                marker in str(chain["original_text"] or "")
                for marker in ("全Agent", "全 Agent", "所有Agent", "所有 Agent")
            ):
                raise RuntimeError("immutable objective does not prove all-Agent management intent")
            if not (
                chain["objective_id"] == old["objective_id"]
                and chain["workspace_id"] == old["workspace_id"] == chain["disclosure_workspace_id"]
                and chain["event_workspace_id"] == old["workspace_id"]
                and chain["disclosure_task_kind"] == old["task_kind"]
            ):
                raise RuntimeError("original authorization chain binding mismatch")

            old_proposal = json.loads(chain["payload_json"] or "{}")
            old_disclosure = json.loads(chain["disclosure_payload_json"] or "{}")
            old_machine = old_disclosure.get("machine") or {}
            if digest(old_machine) != chain["disclosure_digest"]:
                raise RuntimeError("original disclosure digest mismatch")
            local_roots = canonical_scopes(
                old_proposal.get("allowed_write_roots") or old_proposal.get("scopes") or []
            )
            external_targets = old_machine.get("external_write_roots") or []
            external_roots = canonical_scopes([
                str(item.get("path") or "") if isinstance(item, dict) else str(item)
                for item in external_targets
            ])
            old_roots = canonical_scopes(local_roots + external_roots)
            if old_roots != json.loads(old["allowed_write_roots_json"] or "[]"):
                raise RuntimeError("old task roots no longer match its immutable proposal")
            if OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH in old_roots:
                raise RuntimeError("canonical Agent registry is already present")
            if not ({".standards", ".xirang/contract"} <= set(local_roots)):
                raise RuntimeError("all-Agent control-plane surfaces are not present in the old envelope")

            operations = canonical_operations(
                json.loads(old["allowed_operations_json"] or "[]"),
                task_kind=old["task_kind"],
            )
            if not {"control_plane_patch", "update", "envelope_repair"} <= set(operations):
                raise RuntimeError("old operation authority cannot support the deterministic repair")
            old_grants = canonical_grants(
                old_roots, task_kind=old["task_kind"], operations=operations,
                grants=json.loads(old["grants_json"] or "[]"),
            )
            corrected_local_roots = canonical_scopes(
                local_roots + [OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH]
            )
            corrected_roots = canonical_scopes(corrected_local_roots + external_roots)
            corrected_grants = canonical_grants(
                corrected_roots, task_kind=old["task_kind"], operations=operations,
                grants=old_grants + [{
                    "path": OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH,
                    "operations": ["control_plane_patch", "update"],
                }],
            )

            repair_id, disclosure_id, proposal_id = new_id("ER"), new_id("D"), new_id("M")
            new_task_id, envelope_id, lease_id, stage_run_id = (
                new_id("T"), new_id("ENV"), new_id("L"), new_id("SR")
            )
            now = stamp()
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="microseconds")
            excluded = json.loads(old["excluded_actions_json"] or "[]")

            machine = dict(old_machine)
            machine.update({
                "allowed_write_roots": corrected_local_roots,
                "allowed_operations": operations,
                "grants": corrected_grants,
            })
            disclosure_payload = dict(old_disclosure)
            disclosure_payload.update({
                "allowed_write_roots": corrected_local_roots,
                "allowed_operations": operations,
                "grants": corrected_grants,
                "machine": machine,
                "repair_record_id": repair_id,
            })
            disclosure_digest = digest(machine)
            proposal_payload = dict(old_proposal)
            proposal_payload.update({
                "allowed_write_roots": corrected_local_roots,
                "scopes": corrected_local_roots,
                "allowed_operations": operations,
                "grants": corrected_grants,
                "repair_record_id": repair_id,
            })
            source_event_id = chain["source_event_id"]

            connection.execute(
                """INSERT INTO disclosures(
                       disclosure_id,objective_id,workspace_id,session_id,task_kind,
                       disclosure_digest,payload_json,displayed_at,expires_at,
                       actor_verified,disclosure_verified,sequence_verified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (disclosure_id, old["objective_id"], old["workspace_id"], old["session_id"],
                 old["task_kind"], disclosure_digest,
                 json.dumps(disclosure_payload, ensure_ascii=False, sort_keys=True),
                 now, expires, old["actor_verified"], old["disclosure_verified"], old["sequence_verified"]),
            )
            connection.execute(
                """INSERT INTO maintenance_proposals(
                       proposal_id,disclosure_id,workspace_id,session_id,platform,scope_digest,
                       payload_json,additional_intents_json,status,created_at,expires_at,
                       authorized_by_event_id,authorized_at,consumed_at,
                       actor_verified,disclosure_verified,sequence_verified,enforcement_verified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, disclosure_id, old["workspace_id"], old["session_id"], old["platform"],
                 digest({"roots": corrected_roots, "operations": operations, "grants": corrected_grants}),
                 json.dumps(proposal_payload, ensure_ascii=False, sort_keys=True),
                 chain["additional_intents_json"], "consumed", now, expires,
                 source_event_id, now, now, old["actor_verified"], old["disclosure_verified"],
                 old["sequence_verified"], old["enforcement_verified"]),
            )
            envelope_digest = digest({
                "task_id": new_task_id, "disclosure_digest": disclosure_digest,
                "authorization_event_id": source_event_id, "authorization_version": 1,
            })
            metadata = json.loads(old["metadata_json"] or "{}")
            metadata.update({
                "repair_record_id": repair_id,
                "repaired_from_task_id": old_task_id,
                "proposal_id": proposal_id,
            })
            old_card = str(metadata.get("card_path") or "")
            if old_card:
                metadata["card_path"] = str(Path(old_card).with_name(f"{new_task_id}.md"))
            connection.execute(
                """INSERT INTO tasks(
                       task_id,envelope_id,workspace_id,session_id,platform,task_kind,
                       objective_id,disclosure_id,proposal_id,envelope_digest,
                       lifecycle_status,runtime_status,review_status,allowed_write_roots_json,
                       allowed_operations_json,grants_json,excluded_actions_json,metadata_json,
                       actor_verified,disclosure_verified,sequence_verified,enforcement_verified,
                       created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_task_id, envelope_id, old["workspace_id"], old["session_id"], old["platform"],
                 old["task_kind"], old["objective_id"], disclosure_id, proposal_id, envelope_digest,
                 old["lifecycle_status"], old["runtime_status"], old["review_status"],
                 json.dumps(corrected_roots, ensure_ascii=False),
                 json.dumps(operations, ensure_ascii=False),
                 json.dumps(corrected_grants, ensure_ascii=False, sort_keys=True),
                 old["excluded_actions_json"], json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                 old["actor_verified"], old["disclosure_verified"], old["sequence_verified"],
                 old["enforcement_verified"], now, now),
            )
            connection.execute(
                """INSERT INTO leases(
                       lease_id,task_id,source_session_id,worker_session_id,role,
                       allowed_write_roots_json,allowed_operations_json,grants_json,
                       read_only,status,issued_at,expires_at,enforcement_verified
                   ) VALUES (?,?,?,?,?,?,?,?,0,'active',?,?,?)""",
                (lease_id, new_task_id, old["session_id"], old["session_id"], "owner",
                 json.dumps(corrected_roots, ensure_ascii=False), json.dumps(operations, ensure_ascii=False),
                 json.dumps(corrected_grants, ensure_ascii=False, sort_keys=True), now, expires,
                 old["enforcement_verified"]),
            )
            connection.execute(
                """INSERT INTO stage_runs(stage_run_id,task_id,stage,review_round,status,started_at,details_json)
                   VALUES (?,?,?,?, 'active',?,?)""",
                (stage_run_id, new_task_id, old["runtime_status"], 0, now,
                 json.dumps({"repaired_from_task_id": old_task_id}, sort_keys=True)),
            )
            connection.execute(
                """INSERT INTO envelope_repairs(
                       repair_id,workspace_id,old_task_id,new_task_id,maintenance_task_id,
                       source_user_event_id,old_roots_json,corrected_roots_json,
                       corrected_grants_json,reason,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (repair_id, old["workspace_id"], old_task_id, new_task_id, old_task_id,
                 source_event_id, json.dumps(old_roots, ensure_ascii=False),
                 json.dumps(corrected_roots, ensure_ascii=False),
                 json.dumps(corrected_grants, ensure_ascii=False, sort_keys=True),
                 OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON, now),
            )
            connection.execute(
                """UPDATE stage_runs SET status='completed',finished_at=?,details_json=?
                   WHERE task_id=? AND status='active'""",
                (now, json.dumps({"invalidated_by_repair_id": repair_id}), old_task_id),
            )
            connection.execute(
                "UPDATE leases SET status='revoked' WHERE task_id=? AND status='active'",
                (old_task_id,),
            )
            connection.execute(
                """UPDATE tasks SET lifecycle_status='invalid_envelope',
                          runtime_status='invalid_envelope',updated_at=? WHERE task_id=?""",
                (now, old_task_id),
            )
            if failpoint == "after_old_invalidated":
                raise RuntimeError("injected crash after old task invalidated")

            new_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (new_task_id,)
            ).fetchone()
            old_active = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (old_task_id,)
            ).fetchone()[0]
            new_active = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (new_task_id,)
            ).fetchone()[0]
            if old_active != 0 or new_active != 1 or not StateStore._execution_authority_in_connection(connection, new_row):
                raise RuntimeError("omitted registry repair authority/lease postcondition failed")
            record = {
                "schema_version": 1, "recorded_at": stamp(),
                "operation": "repair_omitted_agent_registry_dependency",
                "transaction_status": "commit_requested", "database": str(database),
                "snapshot": str(snapshot), "snapshot_sha256": verified["sha256"],
                "maintenance_task_id": old_task_id, "repair_id": repair_id,
                "old_task_id": old_task_id, "new_task_id": new_task_id,
                "source_user_event_id": source_event_id, "old_roots": old_roots,
                "corrected_roots": corrected_roots,
                "added_dependency": OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH,
                "old_active_leases_after": old_active, "new_active_leases_after": new_active,
                "recovery_registrations": registrations,
            }
            append_audit(audit, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def cleanup_legacy_orphans(
    database: Path, snapshot: Path, audit: Path, session_id: str,
    maintenance_task_id: str, task_ids: list[str],
    *, registry_path: Path = RECOVERY_REGISTRY,
) -> dict:
    """Terminalize only provably powerless legacy imports and revoke their leases."""
    registrations = validate_recovery_paths(snapshot, audit, registry_path=registry_path)
    workspace_root, _workspace_id = registered_workspace_root(registry_path)
    verified = verify_snapshot(
        snapshot, expected_database=database, root=workspace_root,
    )
    targets = sorted(set(task_ids))
    if not targets or len(targets) != len(task_ids):
        raise RuntimeError("legacy orphan cleanup requires a non-empty unique target list")
    lock_path = Path(str(database) + ".cutover.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            maintainer = require_maintenance_task(
                connection, maintenance_task_id, session_id,
                required_operations={"control_plane_patch", "lease_revoke", "audit_append", "verify"},
                operation_targets=[
                    ("control_plane_patch", database),
                    ("lease_revoke", database),
                    ("audit_append", audit),
                    ("verify", snapshot),
                ],
            )
            if verified.get("workspace_id") not in (None, maintainer["workspace_id"]):
                raise RuntimeError("snapshot workspace does not match maintenance envelope")
            placeholders = ",".join("?" for _ in targets)
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE task_id IN ({placeholders}) ORDER BY task_id",
                targets,
            ).fetchall()
            if [row["task_id"] for row in rows] != targets:
                raise RuntimeError("legacy orphan cleanup target does not exist")
            before: list[dict] = []
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                leases = connection.execute(
                    "SELECT lease_id,status,expires_at FROM leases WHERE task_id=? ORDER BY lease_id",
                    (row["task_id"],),
                ).fetchall()
                if not (
                    metadata.get("legacy_import") is True
                    and row["workspace_id"] == maintainer["workspace_id"]
                    and row["lifecycle_status"] in {"authorized", "in_progress", "blocked"}
                    and json.loads(row["allowed_write_roots_json"] or "[]") == []
                    and json.loads(row["allowed_operations_json"] or "[]") == []
                    and json.loads(row["grants_json"] or "[]") == []
                    and any(lease["status"] == "active" for lease in leases)
                    and not StateStore._execution_authority_in_connection(connection, row)
                ):
                    raise RuntimeError(f"target is not a provably powerless legacy orphan: {row['task_id']}")
                before.append({
                    "task_id": row["task_id"],
                    "lifecycle_status": row["lifecycle_status"],
                    "runtime_status": row["runtime_status"],
                    "review_status": row["review_status"],
                    "active_leases": [lease["lease_id"] for lease in leases if lease["status"] == "active"],
                })
            at = stamp()
            for row in rows:
                task_id = row["task_id"]
                connection.execute(
                    """UPDATE stage_runs SET status='completed',finished_at=?,details_json=?
                       WHERE task_id=? AND status='active'""",
                    (at, json.dumps({"terminalized_as": "legacy_unreviewed"}), task_id),
                )
                connection.execute(
                    "UPDATE leases SET status='revoked' WHERE task_id=? AND status='active'",
                    (task_id,),
                )
                connection.execute(
                    """UPDATE tasks SET lifecycle_status='legacy_unreviewed',
                              runtime_status='legacy_unreviewed',review_status='legacy_unreviewed',updated_at=?
                       WHERE task_id=?""",
                    (at, task_id),
                )
            remaining = connection.execute(
                f"""SELECT COUNT(*) FROM leases WHERE status='active'
                     AND task_id IN ({placeholders})""",
                targets,
            ).fetchone()[0]
            if remaining:
                raise RuntimeError("legacy orphan cleanup left active leases")
            record = {
                "schema_version": 1,
                "recorded_at": stamp(),
                "operation": "cleanup_legacy_orphans",
                "transaction_status": "commit_requested",
                "database": str(database),
                "snapshot": str(snapshot),
                "snapshot_sha256": verified["sha256"],
                "maintenance_task_id": maintenance_task_id,
                "recovery_registrations": registrations,
                "before": before,
                "after": [
                    {"task_id": task_id, "status": "legacy_unreviewed", "active_leases": 0}
                    for task_id in targets
                ],
            }
            append_audit(audit, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "inspect", "repair", "repair-invalid-envelope",
            "repair-omitted-agent-registry", "cleanup-legacy-orphans",
        ),
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--keep-task")
    parser.add_argument("--cancel-task", action="append", default=[])
    parser.add_argument("--maintenance-task-id")
    parser.add_argument("--old-task-id")
    parser.add_argument("--legacy-task-id", action="append", default=[])
    parser.add_argument("--corrected-scope", action="append", default=[])
    parser.add_argument("--failpoint", choices=("after_old_invalidated",))
    args = parser.parse_args()
    if args.action == "inspect":
        database_uri = f"file:{args.database.expanduser().resolve().as_posix()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            result = inspect(connection, args.session_id)
    elif args.action == "repair":
        args.database = require_authoritative_database(args.database)
        if not args.snapshot or not args.audit or not args.maintenance_task_id:
            raise SystemExit("repair requires --snapshot, --audit and --maintenance-task-id")
        result = repair(args.database, args.snapshot, args.audit, args.session_id,
                        args.maintenance_task_id, args.keep_task, args.cancel_task)
    elif args.action == "repair-invalid-envelope":
        args.database = require_authoritative_database(args.database)
        if not args.snapshot or not args.audit or not args.maintenance_task_id or not args.old_task_id:
            raise SystemExit(
                "repair-invalid-envelope requires --snapshot, --audit, --maintenance-task-id and --old-task-id"
            )
        result = repair_invalid_envelope(
            args.database, args.snapshot, args.audit, args.session_id,
            args.maintenance_task_id, args.old_task_id, args.corrected_scope,
            failpoint=args.failpoint,
        )
    elif args.action == "repair-omitted-agent-registry":
        args.database = require_authoritative_database(args.database)
        if not args.snapshot or not args.audit or not args.maintenance_task_id or not args.old_task_id:
            raise SystemExit(
                "repair-omitted-agent-registry requires --snapshot, --audit, "
                "--maintenance-task-id and --old-task-id"
            )
        result = repair_omitted_agent_registry_dependency(
            args.database, args.snapshot, args.audit, args.session_id,
            args.maintenance_task_id, args.old_task_id,
            failpoint=args.failpoint,
        )
    else:
        args.database = require_authoritative_database(args.database)
        if not args.snapshot or not args.audit or not args.maintenance_task_id:
            raise SystemExit(
                "cleanup-legacy-orphans requires --snapshot, --audit, --maintenance-task-id "
                "and --legacy-task-id"
            )
        result = cleanup_legacy_orphans(
            args.database, args.snapshot, args.audit, args.session_id,
            args.maintenance_task_id, args.legacy_task_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
