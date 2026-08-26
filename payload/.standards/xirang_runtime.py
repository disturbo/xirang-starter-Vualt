#!/usr/bin/env python3
"""Xirang V3 runtime coordinator backed by the unified StateStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from xirang_state import BLOCKED_STAGES, StateStore, refresh_events_projection
from xirang_task_projection import write_task_card_projection


STAGE_SEQUENCE = (
    "authorized",
    "preparing",
    "implementing",
    "validating",
    "discovery_review",
    "repairing",
    "revalidating",
    "confirmation_review",
    "committing",
    "submitted",
)
NEXT_STAGE = dict(zip(STAGE_SEQUENCE, STAGE_SEQUENCE[1:]))
FUSE_RESUME_STATES = {"closed", "reset", "healthy", "rearmed"}


class CoordinatorError(RuntimeError):
    """A requested runtime transition is not authorized by the state machine."""


def _now(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | str | None = None) -> str:
    return _now(value).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RuntimeCoordinator:
    """Persist and enforce the runtime stage machine for one task."""

    def __init__(self, store: StateStore | str | Path, task_id: str, session_id: str):
        self.store = store if isinstance(store, StateStore) else StateStore(store)
        self.store.initialize()
        self.task_id = task_id
        self.session_id = session_id

    def _task(self, *, capability: str = "read") -> dict[str, Any]:
        task = self.store.get_task(self.task_id)
        if task is None:
            raise CoordinatorError(f"unknown task: {self.task_id}")
        if self.store.resolve_task_access(
            session_id=self.session_id, task_id=self.task_id, capability=capability
        ) is None:
            raise CoordinatorError("session has neither task ownership nor a valid worker lease")
        return task

    def _active_stage(self) -> dict[str, Any]:
        self._task()
        with self.store.transaction() as conn:
            row = conn.execute(
                """SELECT stage_run_id, task_id, stage, review_round, status,
                          started_at, finished_at, details_json
                   FROM stage_runs
                   WHERE task_id = ? AND status = 'active'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (self.task_id,),
            ).fetchone()
        if row is None:
            raise CoordinatorError("task has no active runtime stage")
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json") or "{}")
        return result

    def status(self) -> dict[str, Any]:
        task = self._task()
        stage = self._active_stage()
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "lifecycle_status": task["lifecycle_status"],
            "runtime_status": task["runtime_status"],
            "stage": stage["stage"],
            "review_round": stage["review_round"],
            "stage_started_at": stage["started_at"],
            "details": stage["details"],
        }

    @staticmethod
    def _blocking_findings(details: Mapping[str, Any]) -> int:
        value = details.get("blocking_findings", 0)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return len(value)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise CoordinatorError("blocking_findings must be an integer or a list") from exc

    def advance(
        self,
        to_stage: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        self._task(capability="runtime_mutate")
        current = self._active_stage()
        stage = current["stage"]
        payload = dict(details or {})
        if stage in BLOCKED_STAGES:
            raise CoordinatorError("blocked stages must be resumed, not advanced")
        if stage == "submitted":
            raise CoordinatorError("submitted is terminal")

        expected = NEXT_STAGE.get(stage)
        if stage == "confirmation_review" and to_stage == "repairing":
            expected = "repairing"
            if self._blocking_findings(payload) <= 0:
                raise CoordinatorError("confirmation_review may return to repairing only with blocking findings")
        target = to_stage or expected
        if target != expected:
            raise CoordinatorError(f"illegal stage transition: {stage} -> {target}; expected {expected}")
        if stage == "confirmation_review" and target == "committing" and self._blocking_findings(payload) != 0:
            raise CoordinatorError("blocking findings must be zero before committing")
        if target == "submitted" and self.store.get_latest_delivery(self.task_id) is None:
            raise CoordinatorError("a registered delivery is required before submitted")

        review_round = int(current["review_round"])
        if target == "discovery_review" and review_round == 0:
            review_round = 1
        elif stage == "confirmation_review" and target == "repairing":
            review_round += 1
        result = self.store.transition_stage(
            task_id=self.task_id,
            to_stage=target,
            review_round=review_round,
            details=payload,
            at=at,
        )
        return {"task_id": self.task_id, **result}

    def block(
        self,
        blocked_stage: str,
        *,
        details: Mapping[str, Any] | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        self._task(capability="runtime_mutate")
        current = self._active_stage()
        if current["stage"] in BLOCKED_STAGES:
            raise CoordinatorError("task is already blocked")
        if current["stage"] == "submitted":
            raise CoordinatorError("submitted is terminal")
        if blocked_stage not in BLOCKED_STAGES:
            raise CoordinatorError(f"unknown blocked stage: {blocked_stage}")

        payload = dict(details or {})
        payload.update({"resume_stage": current["stage"], "blocked_at": _iso(at)})
        if blocked_stage == "blocked_budget":
            try:
                window = int(payload.get("window_seconds", 0))
            except (TypeError, ValueError) as exc:
                raise CoordinatorError("blocked_budget requires a positive window_seconds") from exc
            if window <= 0:
                raise CoordinatorError("blocked_budget requires a positive window_seconds")
            payload["window_seconds"] = window
        elif blocked_stage == "blocked_external_dependency":
            if not str(payload.get("probe_name", "")).strip():
                raise CoordinatorError("blocked_external_dependency requires probe_name")
        elif blocked_stage == "blocked_nonconvergent":
            if not str(payload.get("strategy_digest", "")).strip():
                raise CoordinatorError("blocked_nonconvergent requires the exhausted strategy_digest")
        elif blocked_stage == "awaiting_material_user_choice":
            if not str(payload.get("choice_key", "")).strip():
                raise CoordinatorError("awaiting_material_user_choice requires choice_key")

        result = self.store.transition_stage(
            task_id=self.task_id,
            to_stage=blocked_stage,
            review_round=int(current["review_round"]),
            details=payload,
            at=at,
        )
        return {"task_id": self.task_id, **result}

    def _resume_atomically(
        self,
        *,
        current: Mapping[str, Any],
        reason: str,
        at: datetime | str | None,
        user_event_id: str | None = None,
        metadata_update: Mapping[str, Any] | None = None,
        resume_details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _iso(at)
        target = str(current["details"].get("resume_stage") or "")
        if target not in STAGE_SEQUENCE or target == "submitted":
            raise CoordinatorError("blocked stage does not contain a valid resume_stage")
        task = self._task()
        stage_run_id = "SR-" + uuid.uuid4().hex
        event_id = "EV-" + uuid.uuid4().hex
        details = {"resumed_from": current["stage"], "resume_reason": reason, **dict(resume_details or {})}

        with self.store.transaction(immediate=True) as conn:
            active = conn.execute(
                "SELECT stage_run_id, stage FROM stage_runs WHERE task_id = ? AND status = 'active'",
                (self.task_id,),
            ).fetchone()
            if active is None or active["stage_run_id"] != current["stage_run_id"]:
                raise CoordinatorError("runtime stage changed concurrently")

            if user_event_id:
                event = conn.execute("SELECT * FROM user_events WHERE event_id = ?", (user_event_id,)).fetchone()
                if event is None:
                    raise CoordinatorError("unknown user event")
                if event["workspace_id"] != task["workspace_id"]:
                    raise CoordinatorError("user event belongs to another workspace")
                if event["consumed_at"] is not None or _now(event["expires_at"]) <= _now(now):
                    raise CoordinatorError("user event is consumed or expired")
                if _now(event["first_observed_at"]) <= _now(current["started_at"]):
                    raise CoordinatorError("material choice requires a user event newer than the block")
                bindings = json.loads(event["bindings_json"] or "{}")
                if bindings.get("task_id") not in (None, self.task_id):
                    raise CoordinatorError("user event is bound to another task")
                changed = conn.execute(
                    """UPDATE user_events SET consumed_at = ?, consumed_by = ?
                       WHERE event_id = ? AND consumed_at IS NULL""",
                    (now, f"runtime-resume:{self.task_id}", user_event_id),
                ).rowcount
                if changed != 1:
                    raise CoordinatorError("user event was consumed concurrently")

            metadata = dict(task.get("metadata") or {})
            metadata.update(dict(metadata_update or {}))
            conn.execute(
                "UPDATE stage_runs SET status = 'completed', finished_at = ? WHERE stage_run_id = ?",
                (now, current["stage_run_id"]),
            )
            conn.execute(
                """INSERT INTO stage_runs
                   (stage_run_id, task_id, stage, review_round, status, started_at, details_json)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (stage_run_id, self.task_id, target, int(current["review_round"]), now, _json(details)),
            )
            conn.execute(
                """UPDATE tasks SET lifecycle_status = 'in_progress', runtime_status = ?,
                          metadata_json = ?, updated_at = ?
                   WHERE task_id = ?""",
                (target, _json(metadata), now, self.task_id),
            )
            event_payload = {
                "from_stage": current["stage"],
                "to_stage": target,
                "review_round": int(current["review_round"]),
                "reason": reason,
                "user_event_id": user_event_id,
            }
            conn.execute(
                """INSERT INTO events
                   (event_id, event_type, occurred_at, workspace_id, session_id, task_id, payload_json)
                   VALUES (?, 'runtime_resumed', ?, ?, ?, ?, ?)""",
                (event_id, now, task["workspace_id"], self.session_id, self.task_id, _json(event_payload)),
            )
            conn.execute(
                """INSERT INTO outbox
                   (dedupe_key, event_type, aggregate_type, aggregate_id, payload_json, available_at)
                   VALUES (?, 'runtime_resumed', 'task', ?, ?, ?)""",
                (event_id, self.task_id, _json(event_payload), now),
            )
        return self.status()

    def resume(
        self,
        *,
        probe_name: str | None = None,
        fuse_record_id: str | None = None,
        review_artifact_id: str | None = None,
        user_event_id: str | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        self._task(capability="runtime_mutate")
        current = self._active_stage()
        stage = current["stage"]
        details = current["details"]
        now = _now(at)
        if stage not in BLOCKED_STAGES:
            raise CoordinatorError("task is not blocked")

        if stage == "blocked_budget":
            elapsed = now >= _now(details["blocked_at"]) + timedelta(seconds=int(details["window_seconds"]))
            if not elapsed:
                artifact = self._review_artifact(fuse_record_id, "budget_fuse_reset")
                maintenance_task_id = str((artifact or {}).get("maintenance_task_id") or "")
                maintenance_task = self.store.get_task(maintenance_task_id) if maintenance_task_id else None
                if (artifact is None or artifact.get("fuse_state") not in FUSE_RESUME_STATES
                        or maintenance_task is None
                        or maintenance_task.get("task_kind") != "control_plane_maintenance"
                        or artifact.get("maintenance_envelope_id") != maintenance_task.get("envelope_id")
                        or not self.store.task_execution_authorized(maintenance_task_id)):
                    raise CoordinatorError("budget resume requires a bound maintenance fuse record")
            reason = "budget_window_elapsed" if elapsed else f"fuse_record:{fuse_record_id}"
            return self._resume_atomically(current=current, reason=reason, at=now)

        if stage == "blocked_external_dependency":
            selected_probe = probe_name or str(details.get("probe_name") or "")
            registered_probe = PROBE_REGISTRY.get(selected_probe)
            if registered_probe is None:
                raise CoordinatorError("external dependency resume requires a registered probe")
            try:
                healthy = bool(registered_probe(self.store))
            except Exception as exc:
                raise CoordinatorError(f"health probe failed: {exc}") from exc
            if not healthy:
                raise CoordinatorError("external dependency health probe is not healthy")
            return self._resume_atomically(current=current, reason="health_probe_passed", at=now)

        if stage == "blocked_nonconvergent":
            task = self._task()
            old_digest = str(details.get("strategy_digest") or "")
            artifact = self._review_artifact(review_artifact_id, "independent_review_completed")
            new_digest = str((artifact or {}).get("strategy_digest") or "").strip()
            artifact_session = str((artifact or {}).get("_artifact_session_id") or "")
            if (not new_digest or new_digest == old_digest
                    or not artifact_session or artifact_session == task["session_id"]):
                raise CoordinatorError("resume requires an independent review artifact with a new strategy")
            if (task.get("metadata") or {}).get("nonconvergent_resume_used"):
                raise CoordinatorError("the task has already consumed its one nonconvergent strategy resume")
            return self._resume_atomically(
                current=current,
                reason="new_strategy",
                at=now,
                metadata_update={
                    "nonconvergent_resume_used": True,
                    "nonconvergent_strategy_digest": new_digest,
                },
                resume_details={"strategy_digest": new_digest},
            )

        if stage == "awaiting_material_user_choice":
            if not user_event_id:
                raise CoordinatorError("material choice resume requires a new user_event_id")
            return self._resume_atomically(
                current=current,
                reason="material_user_choice",
                at=now,
                user_event_id=user_event_id,
            )

        if stage == "suspended_lease_expired":
            if not self.store.find_valid_leases(self.session_id, task_id=self.task_id, at=now):
                raise CoordinatorError("worker lease is still expired")
            return self._resume_atomically(current=current, reason="valid_lease_restored", at=now)

        raise CoordinatorError(f"unsupported blocked stage: {stage}")

    def _review_artifact(self, event_id: str | None, event_type: str) -> dict[str, Any] | None:
        if not event_id:
            return None
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT event_type, task_id, session_id, payload_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None or row["event_type"] != event_type or row["task_id"] != self.task_id:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        payload["_artifact_session_id"] = row["session_id"]
        return payload

    def issue_lease(
        self,
        *,
        worker_session_id: str,
        allowed_write_roots: Sequence[str],
        role: str = "worker",
        expires_at: datetime | str | None = None,
        duration_seconds: int = 3600,
        read_only: bool = False,
        at: datetime | str | None = None,
    ) -> str:
        task = self._task(capability="lease_admin")
        if task is None:
            raise CoordinatorError(f"unknown task: {self.task_id}")
        if task["session_id"] != self.session_id:
            raise CoordinatorError("only the envelope owner may issue a worker lease")
        issued = _now(at)
        expiry = _now(expires_at) if expires_at is not None else issued + timedelta(seconds=duration_seconds)
        if expiry <= issued:
            raise CoordinatorError("lease expiry must be after issuance")
        return self.store.create_lease(
            task_id=self.task_id,
            source_session_id=self.session_id,
            worker_session_id=worker_session_id,
            role=role,
            allowed_write_roots=list(allowed_write_roots),
            issued_at=issued,
            expires_at=expiry,
            read_only=read_only,
            enforcement_verified=False,
        )

    def list_leases(self, *, worker_session_id: str | None = None, at: datetime | str | None = None) -> list[dict[str, Any]]:
        worker = worker_session_id or self.session_id
        return self.store.find_valid_leases(worker, task_id=self.task_id, at=at)

    def revoke_lease(self, lease_id: str, *, at: datetime | str | None = None) -> bool:
        task = self._task(capability="lease_admin")
        if task is None or task["session_id"] != self.session_id:
            raise CoordinatorError("only the envelope owner may revoke a worker lease")
        with self.store.transaction(immediate=True) as conn:
            changed = conn.execute(
                """UPDATE leases SET status = 'revoked'
                   WHERE lease_id = ? AND task_id = ? AND status = 'active'""",
                (lease_id, self.task_id),
            ).rowcount
        return changed == 1

    def lease(self, action: str, **kwargs: Any) -> Any:
        if action == "create":
            return self.issue_lease(**kwargs)
        if action == "list":
            return self.list_leases(**kwargs)
        if action == "revoke":
            return self.revoke_lease(**kwargs)
        raise CoordinatorError(f"unknown lease action: {action}")


def _state_store_probe(store: StateStore) -> bool:
    try:
        with store.connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and row[0] == "ok")
    except Exception:
        return False


PROBE_REGISTRY: dict[str, Callable[[StateStore], bool]] = {
    "state_store": _state_store_probe,
    "database": _state_store_probe,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--workspace-root")
    parser.add_argument("--events-output")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    advance = commands.add_parser("advance")
    advance.add_argument("--to")
    advance.add_argument("--details-json", default="{}")
    block = commands.add_parser("block")
    block.add_argument("stage", choices=sorted(BLOCKED_STAGES))
    block.add_argument("--details-json", default="{}")
    resume = commands.add_parser("resume")
    resume.add_argument("--probe", choices=sorted(PROBE_REGISTRY))
    resume.add_argument("--fuse-record-id")
    resume.add_argument("--review-artifact-id")
    resume.add_argument("--user-event-id")
    lease = commands.add_parser("lease")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)
    create = lease_sub.add_parser("create")
    create.add_argument("--worker-session-id", required=True)
    create.add_argument("--root", action="append", required=True)
    create.add_argument("--role", default="worker")
    create.add_argument("--duration-seconds", type=int, default=3600)
    create.add_argument("--read-only", action="store_true")
    listing = lease_sub.add_parser("list")
    listing.add_argument("--worker-session-id")
    revoke = lease_sub.add_parser("revoke")
    revoke.add_argument("lease_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime = RuntimeCoordinator(args.db, args.task_id, args.session_id)
    try:
        if args.command == "status":
            result = runtime.status()
        elif args.command == "advance":
            result = runtime.advance(args.to, details=json.loads(args.details_json))
        elif args.command == "block":
            result = runtime.block(args.stage, details=json.loads(args.details_json))
        elif args.command == "resume":
            result = runtime.resume(
                probe_name=args.probe,
                fuse_record_id=args.fuse_record_id,
                review_artifact_id=args.review_artifact_id,
                user_event_id=args.user_event_id,
            )
        elif args.lease_action == "create":
            result = {"lease_id": runtime.issue_lease(
                worker_session_id=args.worker_session_id,
                allowed_write_roots=args.root,
                role=args.role,
                duration_seconds=args.duration_seconds,
                read_only=args.read_only,
            )}
        elif args.lease_action == "list":
            result = runtime.list_leases(worker_session_id=args.worker_session_id)
        else:
            result = {"revoked": runtime.revoke_lease(args.lease_id)}
        mutating = args.command in {"advance", "block", "resume"} or (
            args.command == "lease" and args.lease_action in {"create", "revoke"}
        )
        if mutating:
            if not args.workspace_root:
                raise CoordinatorError("mutating runtime command requires --workspace-root")
            output = (
                Path(args.events_output).expanduser().resolve()
                if args.events_output
                else Path(args.db).expanduser().resolve().parent.parent / "events/events.jsonl"
            )
            refresh_events_projection(
                runtime.store,
                workspace_root=Path(args.workspace_root),
                output=output,
            )
            try:
                projection = write_task_card_projection(
                    runtime.store,
                    workspace_root=Path(args.workspace_root),
                    task_id=args.task_id,
                )
                if isinstance(result, dict):
                    result["task_projection"] = projection
            except Exception as exc:
                runtime.store.set_task_metadata(args.task_id, {"projection_degraded": True})
                updated = runtime.store.get_task(args.task_id) or {}
                runtime.store.enqueue_outbox(
                    dedupe_key=f"runtime-projection:{args.task_id}:{updated.get('updated_at')}",
                    event_type="projection_degraded",
                    aggregate_type="task",
                    aggregate_id=args.task_id,
                    payload={"authority_committed": True, "error": str(exc)},
                )
                if isinstance(result, dict):
                    result["projection_degraded"] = True
                    result["projection_error"] = str(exc)
    except Exception as exc:
        print(_json({"ok": False, "error": str(exc)}))
        return 2
    print(_json({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
