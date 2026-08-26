#!/usr/bin/env python3
"""SQLite state backend for Xi Rang runtime governance.

SQLite is the authoritative runtime store. Markdown, JSON, and JSONL files are
projections or diagnostic exports and are never imported by this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import os
import secrets
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from xirang_recovery_roots import RecoveryRootError, load_registry, require_registered


SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MAINTENANCE_INTENTS = {
    "continue_execution",
    "adversarial_review",
    "no_intermediate_confirmation",
    "report_once_no_prompt",
}
DISCLOSURE_MACHINE_FIELDS = {
    "allowed_write_roots", "external_write_roots", "excluded_actions",
    "irreversible_effects", "external_effects", "acceptance_owner",
    "task_kind", "objective_record_id", "allowed_operations", "grants",
}
TASK_METADATA_FIELDS = {
    "title",
    "card_path",
    "delivery_mode",
    "maintenance",
    "proposal_id",
    "external_write_roots",
    "submitted_at",
    "verification_summary",
    "submission_summary",
    "delivery_id",
    "interaction_preference_snapshot",
    "review_prompt_consumed_at",
    "projection_degraded",
    "repair_record_id",
    "repaired_from_task_id",
    "execution_budget",
    "irreversible_effects",
    "external_effects",
    "acceptance_owner",
}

EXECUTION_BUDGET_FIELDS = {
    "max_agents",
    "max_nested_depth",
    "max_review_rounds",
    "max_wall_minutes",
    "max_external_cost_usd",
    "nonconvergent_after_consecutive_rounds_without_new_evidence",
}
EXECUTION_BUDGET_INTEGER_LIMITS = {
    "max_agents": 64,
    "max_nested_depth": 16,
    "max_review_rounds": 100,
    "max_wall_minutes": 43_200,
    "nonconvergent_after_consecutive_rounds_without_new_evidence": 100,
}
MAX_EXTERNAL_COST_USD = 1_000_000
DELIVERY_MODES = {"chat", "files", "files_no_git"}

ORDINARY_OPERATIONS = {"add", "update", "delete", "move"}
MAINTENANCE_OPERATIONS = ORDINARY_OPERATIONS | {
    "control_plane_patch", "snapshot", "verify", "restore_drill",
    "projection_rebuild", "lease_revoke", "envelope_repair", "audit_append",
}
OPERATION_ALIASES = {
    "create": "add", "write": "update", "edit": "update", "notebookedit": "update",
    "remove": "delete", "rename": "move",
}
SCHEMA_V3_EMPTY_GRANTS_UPGRADE_REASON = (
    "schema_v3_empty_operation_grants_upgraded_"
    "from_same_disclosed_roots_without_scope_expansion"
)
OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON = (
    "omitted_canonical_agent_registry_dependency_repaired_"
    "from_immutable_all_agent_objective"
)
OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH = ".xirang/adapters/registry.json"


class StateError(RuntimeError):
    """Base error for state-store failures."""


class StateNotFound(StateError):
    """A referenced state object does not exist."""


class StateConflict(StateError):
    """An immutable object or state transition conflicts with stored state."""


class ExpiredUserEvent(StateError):
    """A user event is outside its original, non-renewable TTL."""


class ScopeViolation(StateError):
    """A worker lease requests scope outside its parent task."""


class ClosingConnection(sqlite3.Connection):
    """sqlite context manager that also closes, rather than only committing."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_workspace_sequence
    ON events(workspace_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_session_sequence
    ON events(session_id, sequence);

CREATE TABLE IF NOT EXISTS user_events (
    event_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    host_message_id TEXT,
    prompt_sha256 TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT,
    bindings_json TEXT NOT NULL DEFAULT '{}',
    actor_verified INTEGER NOT NULL DEFAULT 0 CHECK(actor_verified IN (0, 1)),
    UNIQUE(platform, host_message_id)
);
CREATE INDEX IF NOT EXISTS idx_user_events_session
    ON user_events(session_id, first_observed_at);

CREATE TABLE IF NOT EXISTS objectives (
    objective_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    original_text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    created_from_conversation TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disclosures (
    disclosure_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    disclosure_digest TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    displayed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    actor_verified INTEGER NOT NULL DEFAULT 0 CHECK(actor_verified IN (0, 1)),
    disclosure_verified INTEGER NOT NULL DEFAULT 0 CHECK(disclosure_verified IN (0, 1)),
    sequence_verified INTEGER NOT NULL DEFAULT 0 CHECK(sequence_verified IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_disclosures_session
    ON disclosures(session_id, displayed_at);

CREATE TABLE IF NOT EXISTS maintenance_proposals (
    proposal_id TEXT PRIMARY KEY,
    disclosure_id TEXT NOT NULL REFERENCES disclosures(disclosure_id),
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    scope_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    additional_intents_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'authorized', 'consumed', 'expired', 'canceled')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    authorized_by_event_id TEXT REFERENCES user_events(event_id),
    authorized_at TEXT,
    consumed_at TEXT,
    actor_verified INTEGER NOT NULL DEFAULT 0 CHECK(actor_verified IN (0, 1)),
    disclosure_verified INTEGER NOT NULL DEFAULT 0 CHECK(disclosure_verified IN (0, 1)),
    sequence_verified INTEGER NOT NULL DEFAULT 0 CHECK(sequence_verified IN (0, 1)),
    enforcement_verified INTEGER NOT NULL DEFAULT 0 CHECK(enforcement_verified IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_maintenance_candidates
    ON maintenance_proposals(session_id, status, expires_at);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    objective_id TEXT REFERENCES objectives(objective_id),
    disclosure_id TEXT REFERENCES disclosures(disclosure_id),
    proposal_id TEXT REFERENCES maintenance_proposals(proposal_id),
    envelope_digest TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    runtime_status TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'draft',
    allowed_write_roots_json TEXT NOT NULL,
    allowed_operations_json TEXT NOT NULL DEFAULT '[]',
    grants_json TEXT NOT NULL DEFAULT '[]',
    excluded_actions_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    actor_verified INTEGER NOT NULL DEFAULT 0 CHECK(actor_verified IN (0, 1)),
    disclosure_verified INTEGER NOT NULL DEFAULT 0 CHECK(disclosure_verified IN (0, 1)),
    sequence_verified INTEGER NOT NULL DEFAULT 0 CHECK(sequence_verified IN (0, 1)),
    enforcement_verified INTEGER NOT NULL DEFAULT 0 CHECK(enforcement_verified IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session_state
    ON tasks(session_id, lifecycle_status, runtime_status);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    source_session_id TEXT NOT NULL,
    worker_session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    allowed_write_roots_json TEXT NOT NULL,
    allowed_operations_json TEXT NOT NULL DEFAULT '[]',
    grants_json TEXT NOT NULL DEFAULT '[]',
    read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'expired', 'revoked', 'completed')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    enforcement_verified INTEGER NOT NULL DEFAULT 0 CHECK(enforcement_verified IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_leases_worker_state
    ON leases(worker_session_id, status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_worker_task_lease
    ON leases(task_id, worker_session_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS stage_runs (
    stage_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    review_round INTEGER NOT NULL DEFAULT 0 CHECK(review_round >= 0),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_task
    ON stage_runs(task_id, started_at);

CREATE TABLE IF NOT EXISTS write_receipts (
    receipt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    lease_id TEXT REFERENCES leases(lease_id),
    event_id TEXT REFERENCES events(event_id),
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    operation TEXT NOT NULL,
    sha256 TEXT,
    exists_after INTEGER NOT NULL DEFAULT 1 CHECK(exists_after IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'effective'
        CHECK(status IN ('effective', 'superseded')),
    predecessor_receipt_id TEXT UNIQUE REFERENCES write_receipts(receipt_id),
    superseded_by_receipt_id TEXT UNIQUE REFERENCES write_receipts(receipt_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_receipts_task
    ON write_receipts(task_id, created_at);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    implementation_commit TEXT,
    implementation_tree TEXT,
    tag_object TEXT,
    manifest_json TEXT NOT NULL,
    validation_summary TEXT,
    adversarial_review_summary TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
    submitted_at TEXT NOT NULL,
    UNIQUE(task_id, submitted_at)
);

CREATE TABLE IF NOT EXISTS review_focus (
    focus_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    presented_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'consumed', 'superseded', 'expired')),
    superseded_at TEXT,
    superseded_by_focus_id TEXT REFERENCES review_focus(focus_id)
);
CREATE INDEX IF NOT EXISTS idx_review_focus_conversation
    ON review_focus(conversation_id, presented_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_delivery_focus
    ON review_focus(delivery_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS decision_receipts (
    decision_receipt_id TEXT PRIMARY KEY,
    user_event_id TEXT NOT NULL REFERENCES user_events(event_id),
    focus_id TEXT NOT NULL REFERENCES review_focus(focus_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
    decision TEXT NOT NULL CHECK(decision IN ('accept', 'request_changes')),
    reason TEXT,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_scope TEXT NOT NULL,
    intermediate_confirmation_policy TEXT NOT NULL,
    review_prompt_policy TEXT NOT NULL,
    source_user_event_id TEXT REFERENCES user_events(event_id),
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, user_scope)
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    claimed_at TEXT,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox(delivered_at, available_at, outbox_id);

CREATE TABLE IF NOT EXISTS envelope_repairs (
    repair_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    old_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    new_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    maintenance_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    source_user_event_id TEXT REFERENCES user_events(event_id),
    old_roots_json TEXT NOT NULL,
    corrected_roots_json TEXT NOT NULL,
    corrected_grants_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_handoffs (
    handoff_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    envelope_id TEXT NOT NULL,
    delivery_id TEXT REFERENCES deliveries(delivery_id),
    state_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','stale')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_handoffs_active
    ON session_handoffs(workspace_id, session_id, status, created_at);

CREATE TABLE IF NOT EXISTS handoff_consumptions (
    consumption_id TEXT PRIMARY KEY,
    handoff_id TEXT NOT NULL REFERENCES session_handoffs(handoff_id) ON DELETE CASCADE,
    consumer_session_id TEXT NOT NULL,
    consumer_agent_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    verified_task_updated_at TEXT NOT NULL,
    UNIQUE(handoff_id, consumer_session_id, consumer_agent_id)
);
"""


BLOCKED_STAGES = {
    "blocked_budget",
    "blocked_nonconvergent",
    "blocked_external_dependency",
    "awaiting_material_user_choice",
    "suspended_lease_expired",
}

STAGE_TRANSITIONS = {
    "authorized": {"preparing"},
    "preparing": {"implementing"},
    "implementing": {"validating"},
    "validating": {"discovery_review"},
    "discovery_review": {"repairing", "committing"},
    "repairing": {"revalidating"},
    "revalidating": {"confirmation_review"},
    "confirmation_review": {"repairing", "committing"},
    "committing": {"submitted"},
    "submitted": set(),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime | str | None = None) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_delivery_mode(value: Any) -> str:
    mode = str(value or "files").strip()
    if mode not in DELIVERY_MODES:
        raise StateConflict(f"未知 delivery_mode：{mode}")
    return mode


def _git_commit_is_excluded(values: Sequence[Any]) -> bool:
    return any(
        "git" in str(value).casefold()
        and ("提交" in str(value) or "commit" in str(value).casefold())
        for value in values
    )


def _bound_delivery_workspace_root(store: "StateStore", task: Mapping[str, Any]) -> Path:
    sentinel_path = Path(str(store.path) + ".cutover.json")
    try:
        sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise StateConflict("文件交付缺少可验证的 SQLite workspace 绑定") from exc
    root = Path(str(sentinel.get("workspace_root") or "")).expanduser().resolve()
    root, bound_workspace_id = _validate_projection_workspace_binding(store, root)
    if task["workspace_id"] != bound_workspace_id:
        raise StateConflict("文件交付任务与 cutover workspace 绑定不一致")
    return root


def _read_file_preimage_binding(
    manifest_path: Path, *, logical_path: str, workspace_root: Path,
) -> dict[str, Any]:
    resolved = manifest_path.expanduser().resolve()
    registry_path = workspace_root / ".xirang/contract/recovery-roots.yaml"
    try:
        registry = load_registry(registry_path)
        require_registered(resolved, registry, kind="manifests")
    except (OSError, RecoveryRootError) as exc:
        raise StateConflict(f"无 Git 交付 pre-image 不在登记恢复根：{logical_path}") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise StateConflict(f"无 Git 交付缺少普通文件 pre-image manifest：{logical_path}")
    try:
        raw = resolved.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateConflict(f"无 Git 交付 pre-image manifest 不可读：{logical_path}") from exc
    object_path = Path(str(manifest.get("object") or "")).expanduser().resolve()
    try:
        require_registered(object_path, registry, kind="objects")
    except RecoveryRootError as exc:
        raise StateConflict(f"无 Git 交付 pre-image 对象不在登记恢复根：{logical_path}") from exc
    expected_sha = str(manifest.get("sha256") or "")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("artifact_type") != "file_preimage"
        or manifest.get("logical_path") != logical_path
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or not object_path.is_file()
        or object_path.is_symlink()
        or object_path.name != expected_sha
        or _file_sha256(object_path) != expected_sha
    ):
        raise StateConflict(f"无 Git 交付 pre-image 绑定无效：{logical_path}")
    return {
        **manifest,
        "manifest": str(resolved),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _files_no_git_manifest_digest(manifest: Sequence[Mapping[str, Any]]) -> str:
    core = [
        {key: value for key, value in item.items() if key != "no_git_manifest_sha256"}
        for item in manifest
    ]
    return _digest(core)


def _verify_files_no_git_manifest(
    store: "StateStore", *, task: Mapping[str, Any], manifest: Sequence[Mapping[str, Any]],
) -> None:
    if not manifest or any(not isinstance(item, Mapping) for item in manifest):
        raise StateConflict("无 Git 文件交付必须包含非空逐文件 manifest")
    root = _bound_delivery_workspace_root(store, task)
    expected_manifest_sha = _files_no_git_manifest_digest(manifest)
    seen: set[str] = set()
    for item in manifest:
        path_value = str(item.get("path") or "")
        if (
            not path_value
            or path_value in seen
            or Path(path_value).is_absolute()
            or canonical_scope(path_value) != path_value
            or path_value == "."
        ):
            raise StateConflict("无 Git 文件交付路径必须是唯一的 workspace 相对精确路径")
        seen.add(path_value)
        if (
            item.get("delivery_mode") != "no_git"
            or item.get("git_effect") is not False
            or item.get("evidence_only") is not False
            or item.get("git_recovery_available") is not False
            or item.get("no_git_manifest_sha256") != expected_manifest_sha
        ):
            raise StateConflict(f"无 Git 文件交付不得冒充 Git/evidence-only 能力：{path_value}")
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path_value],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if tracked.returncode != 0:
            raise StateConflict(f"files_no_git 只允许已跟踪文件：{path_value}")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", path_value], cwd=root, check=False,
        )
        unstaged = subprocess.run(
            ["git", "diff", "--quiet", "--", path_value], cwd=root, check=False,
        )
        if staged.returncode != 0 or unstaged.returncode != 1:
            raise StateConflict(f"files_no_git 只允许未暂存的实际工作树变更：{path_value}")
        exists_after = item.get("exists_after")
        target = root / path_value
        if not isinstance(exists_after, bool):
            raise StateConflict(f"无 Git 文件交付缺少存在性断言：{path_value}")
        if exists_after:
            if not target.is_file() or target.is_symlink() or _file_sha256(target) != item.get("sha256"):
                raise StateConflict(f"无 Git 文件交付当前文件哈希漂移：{path_value}")
        elif target.exists() or target.is_symlink() or item.get("sha256") is not None:
            raise StateConflict(f"无 Git 文件交付删除项状态漂移：{path_value}")
        recovery_path = Path(str(item.get("recovery_manifest") or ""))
        recovery = _read_file_preimage_binding(
            recovery_path, logical_path=path_value, workspace_root=root,
        )
        if (
            recovery["sha256"] != item.get("preimage_sha256")
            or recovery["manifest_sha256"] != item.get("recovery_manifest_sha256")
        ):
            raise StateConflict(f"无 Git 文件交付 pre-image 与不可变 manifest 不一致：{path_value}")


def _verify_controlled_delivery_tag(
    store: "StateStore",
    *,
    task: Mapping[str, Any],
    delivery_id: str,
    manifest: Sequence[Mapping[str, Any]],
    implementation_commit: str,
    implementation_tree: str,
    tag_object: str,
) -> None:
    """Verify the immutable Git proof before authoritative delivery registration."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", delivery_id):
        raise StateConflict("交付标识包含不安全字符")
    object_pattern = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
    for label, value in (
        ("implementation commit", implementation_commit),
        ("implementation tree", implementation_tree),
        ("annotated tag object", tag_object),
    ):
        if not re.fullmatch(object_pattern, value):
            raise StateConflict(f"文件交付缺少有效的 {label}")

    root = _bound_delivery_workspace_root(store, task)

    def git_text(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            raise StateConflict("文件交付无法调用 Git 验证不可变身份") from exc
        if result.returncode != 0:
            raise StateConflict(f"文件交付 Git 证明无效：{' '.join(args)}")
        return result.stdout

    repository_root = Path(git_text("rev-parse", "--show-toplevel").strip()).resolve()
    if repository_root != root:
        raise StateConflict("文件交付 Git 根与 cutover workspace 不一致")
    if git_text("cat-file", "-t", tag_object).strip() != "tag":
        raise StateConflict("文件交付 tag_object 不是 annotated tag")
    if git_text("rev-parse", f"{tag_object}^{{commit}}").strip() != implementation_commit:
        raise StateConflict("文件交付 annotated tag 未绑定 implementation commit")
    if git_text("rev-parse", f"{implementation_commit}^{{tree}}").strip() != implementation_tree:
        raise StateConflict("文件交付 implementation tree 与 commit 不一致")
    tag_ref = git_text(
        "rev-parse", "--verify", f"refs/tags/xirang/submitted/{delivery_id}^{{tag}}",
    ).strip()
    if tag_ref != tag_object:
        raise StateConflict("文件交付 tag ref 与登记的 annotated tag object 不一致")

    raw_tag = git_text("cat-file", "-p", tag_object)
    _headers, separator, body = raw_tag.partition("\n\n")
    if not separator:
        raise StateConflict("文件交付 annotated tag 缺少不可变 manifest payload")
    body = body.rstrip("\n")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StateConflict("文件交付 annotated tag payload 不是有效 JSON") from exc
    expected_payload = {
        "kind": "xirang_controlled_delivery_manifest",
        "schema_version": 1,
        "delivery_id": delivery_id,
        "task_id": task["task_id"],
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "manifest": list(manifest),
    }
    if not isinstance(payload, dict) or _json(payload) != body or payload != expected_payload:
        raise StateConflict("文件交付 annotated tag payload 与权威登记内容不一致")

    seen: set[str] = set()
    for item in manifest:
        path_value = str(item.get("path") or "")
        if not path_value or path_value in seen:
            raise StateConflict("文件交付 manifest 包含空路径或重复路径")
        seen.add(path_value)
        mode = item.get("delivery_mode")
        git_effect = item.get("git_effect")
        evidence_only = item.get("evidence_only")
        if (
            mode not in {"git", "evidence_only"}
            or not isinstance(git_effect, bool)
            or not isinstance(evidence_only, bool)
            or evidence_only != (mode == "evidence_only")
            or (evidence_only and git_effect)
        ):
            raise StateConflict(f"文件交付 manifest 路径模式无效：{path_value}")
        target = Path(path_value) if Path(path_value).is_absolute() else root / path_value
        exists_after = item.get("exists_after")
        if not isinstance(exists_after, bool):
            raise StateConflict(f"文件交付 manifest 缺少存在性断言：{path_value}")
        if exists_after:
            if not target.is_file() or target.is_symlink():
                raise StateConflict(f"文件交付当前文件缺失或类型无效：{path_value}")
            if _file_sha256(target) != item.get("sha256"):
                raise StateConflict(f"文件交付当前文件哈希漂移：{path_value}")
        elif target.exists() or target.is_symlink() or item.get("sha256") is not None:
            raise StateConflict(f"文件交付删除项当前状态漂移：{path_value}")

        if not git_effect:
            if mode == "git" and exists_after:
                raise StateConflict(f"现存 Git 交付路径不能排除 commit effect：{path_value}")
            if not exists_after:
                recovery_path = Path(str(item.get("recovery_manifest") or "")).expanduser().resolve()
                expected_preimage_sha = str(item.get("preimage_sha256") or "")
                if not recovery_path.is_file() or recovery_path.is_symlink():
                    raise StateConflict(f"非 Git 删除缺少可恢复 pre-image：{path_value}")
                try:
                    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise StateConflict(f"非 Git 删除 pre-image manifest 不可读：{path_value}") from exc
                recovery_object = Path(str(recovery.get("object") or "")).expanduser().resolve()
                if (
                    recovery.get("artifact_type") != "file_preimage"
                    or recovery.get("logical_path") != path_value
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_preimage_sha)
                    or recovery.get("sha256") != expected_preimage_sha
                    or not recovery_object.is_file() or recovery_object.is_symlink()
                    or recovery_object.name != expected_preimage_sha
                    or _file_sha256(recovery_object) != expected_preimage_sha
                ):
                    raise StateConflict(f"非 Git 删除 pre-image 绑定无效：{path_value}")
            continue
        if Path(path_value).is_absolute():
            raise StateConflict("Git effect 不能绑定 workspace 外的绝对路径")
        raw_entry = subprocess.run(
            ["git", "ls-tree", "-z", implementation_commit, "--", path_value],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if raw_entry.returncode != 0:
            raise StateConflict(f"文件交付无法验证 commit 路径：{path_value}")
        rows = raw_entry.stdout.rstrip(b"\0").split(b"\0") if raw_entry.stdout else []
        if not exists_after:
            if rows:
                raise StateConflict(f"文件交付 commit 未落实删除：{path_value}")
            continue
        if len(rows) != 1 or b"\t" not in rows[0]:
            raise StateConflict(f"文件交付 commit 缺少唯一 blob：{path_value}")
        metadata, stored_path = rows[0].split(b"\t", 1)
        parts = metadata.split()
        if (
            len(parts) != 3 or parts[1] != b"blob"
            or stored_path.decode("utf-8") != path_value
        ):
            raise StateConflict(f"文件交付 commit 路径不是精确 blob：{path_value}")
        blob = subprocess.run(
            ["git", "cat-file", "blob", parts[2].decode("ascii")], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != item.get("sha256"):
            raise StateConflict(f"文件交付 commit blob 与 manifest 不一致：{path_value}")


def _bool(value: bool) -> int:
    return 1 if value else 0


def canonical_scope(value: str) -> str:
    """Return a stable scope path without resolving filesystem symlinks."""
    if not isinstance(value, str) or not value.strip():
        raise ScopeViolation("写入范围不能为空")
    raw = value.strip().replace("\\", "/")
    if any(delimiter in raw for delimiter in (";", "；", "\n", "\r", ",", "，")):
        raise ScopeViolation(f"写入范围必须逐路径传递，不能包含拼接分隔符：{value}")
    raw_parts = [part for part in raw.split("/") if part]
    if ".." in raw_parts:
        raise ScopeViolation(f"写入范围不能包含父目录跳转：{value}")
    absolute = raw.startswith("/")
    normalized = os.path.normpath(raw).replace("\\", "/")
    if normalized in ("", "."):
        return "/" if absolute else "."
    parts = [part for part in normalized.split("/") if part]
    result = "/".join(parts)
    return f"/{result}" if absolute else result


def canonical_scopes(values: Sequence[str]) -> list[str]:
    return sorted({canonical_scope(value) for value in values})


def canonical_operation(value: str) -> str:
    operation = OPERATION_ALIASES.get(str(value).strip().lower(), str(value).strip().lower())
    if operation not in MAINTENANCE_OPERATIONS:
        raise ScopeViolation(f"不受支持的操作类型：{value}")
    return operation


def canonical_operations(values: Sequence[str], *, task_kind: str) -> list[str]:
    default = MAINTENANCE_OPERATIONS if canonical_task_kind(task_kind) == "control_plane_maintenance" else ORDINARY_OPERATIONS
    operations = {canonical_operation(value) for value in values} if values else set(default)
    if canonical_task_kind(task_kind) != "control_plane_maintenance" and not operations <= ORDINARY_OPERATIONS:
        raise ScopeViolation("普通任务不能获得控制面操作权限")
    return sorted(operations)


def canonical_grants(
    roots: Sequence[str], *, task_kind: str, operations: Sequence[str] | None = None,
    grants: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    allowed_roots = canonical_scopes(roots)
    default_operations = canonical_operations(list(operations or []), task_kind=task_kind)
    if not grants:
        return [{"path": path, "operations": default_operations} for path in allowed_roots]
    normalized: dict[str, set[str]] = {}
    for grant in grants:
        if not isinstance(grant, Mapping):
            raise ScopeViolation("授权项必须是 path + operations 对象")
        path = canonical_scope(str(grant.get("path") or ""))
        if path not in allowed_roots:
            raise ScopeViolation(f"授权项路径不在逐路径范围清单中：{path}")
        item_operations = canonical_operations(
            [str(item) for item in (grant.get("operations") or [])], task_kind=task_kind,
        )
        normalized.setdefault(path, set()).update(item_operations)
    if set(normalized) != set(allowed_roots):
        raise ScopeViolation("每个写入路径都必须具有显式操作授权")
    return [{"path": path, "operations": sorted(normalized[path])} for path in sorted(normalized)]


def canonical_external_targets(values: Sequence[Any]) -> list[dict[str, str]]:
    """Freeze external authority as an exact path and filesystem kind."""
    targets: dict[str, str] = {}
    for value in values:
        if isinstance(value, Mapping):
            raw = str(value.get("path") or "")
            declared_kind = str(value.get("kind") or "")
        else:
            raw, declared_kind = str(value), ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ScopeViolation(f"外部目标必须是绝对路径：{raw}")
        resolved = path.resolve()
        if not resolved.exists():
            raise ScopeViolation(f"外部目标必须在授权时存在：{raw}")
        actual_kind = "file" if resolved.is_file() else "dir" if resolved.is_dir() else ""
        if actual_kind not in {"file", "dir"}:
            raise ScopeViolation(f"外部目标必须是普通文件或目录：{raw}")
        if declared_kind and declared_kind != actual_kind:
            raise ScopeViolation(f"外部目标类型与声明不一致：{raw}")
        existing = targets.get(str(resolved))
        if existing and existing != actual_kind:
            raise ScopeViolation(f"同一外部目标出现冲突类型：{raw}")
        targets[str(resolved)] = actual_kind
    return [{"path": path, "kind": targets[path]} for path in sorted(targets)]


def scope_covers(parent: str, child: str) -> bool:
    parent_value = canonical_scope(parent)
    child_value = canonical_scope(child)
    if parent_value == child_value:
        return True
    if parent_value == "/":
        return child_value.startswith("/")
    if parent_value == ".":
        return not child_value.startswith("/")
    return child_value.startswith(parent_value.rstrip("/") + "/")


def disclosure_machine_payload(
    payload: Mapping[str, Any], *, objective_id: str, task_kind: str
) -> dict[str, Any]:
    """Canonical machine-only disclosure fields; display text never affects authority."""
    external = payload.get("external_write_roots") or payload.get("external_roots") or []
    roots = payload.get("allowed_write_roots") or payload.get("scopes") or []
    excluded = payload.get("excluded_actions") or payload.get("excludes") or []
    task_kind = canonical_task_kind(task_kind)
    delivery_mode = canonical_delivery_mode(payload.get("delivery_mode", "files"))
    if delivery_mode == "files_no_git" and not _git_commit_is_excluded(list(excluded)):
        raise ScopeViolation("files_no_git 必须在不可变展示包络中明确排除 Git 提交")
    external_targets = canonical_external_targets(list(external))
    if external_targets and task_kind != "control_plane_maintenance":
        raise ScopeViolation("普通任务不得声明 external_write_roots")
    all_roots = canonical_scopes(list(roots) + [item["path"] for item in external_targets])
    operations = canonical_operations(
        [str(value) for value in (payload.get("allowed_operations") or [])], task_kind=task_kind,
    )
    grants = canonical_grants(
        all_roots,
        task_kind=task_kind,
        operations=operations,
        grants=payload.get("grants") if isinstance(payload.get("grants"), Sequence) else None,
    )
    return {
        "objective_record_id": objective_id,
        "task_kind": task_kind,
        "delivery_mode": delivery_mode,
        "allowed_write_roots": canonical_scopes(list(roots)),
        "external_write_roots": external_targets,
        "allowed_operations": operations,
        "grants": grants,
        "excluded_actions": sorted({str(value) for value in excluded}),
        "irreversible_effects": sorted({str(value) for value in payload.get("irreversible_effects", [])}),
        "external_effects": sorted({str(value) for value in payload.get("external_effects", [])}),
        "acceptance_owner": payload.get("acceptance_owner") or "user",
    }


def canonical_task_kind(value: str) -> str:
    normalized = str(value).strip()
    return "control_plane_maintenance" if normalized in {"maintenance", "control_plane_maintenance"} else normalized


def canonical_execution_budget(value: Mapping[str, Any]) -> dict[str, int | float]:
    """Validate a bounded, fully explicit task execution budget."""
    if not isinstance(value, Mapping):
        raise StateConflict("execution_budget 必须是对象")
    missing = EXECUTION_BUDGET_FIELDS - set(value)
    extra = set(value) - EXECUTION_BUDGET_FIELDS
    if missing or extra:
        raise StateConflict(
            "execution_budget 字段必须完整且不能扩展："
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    result: dict[str, int | float] = {}
    for field, upper_bound in EXECUTION_BUDGET_INTEGER_LIMITS.items():
        raw = value[field]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise StateConflict(f"execution_budget.{field} 必须是整数")
        if raw < 0 or raw > upper_bound:
            raise StateConflict(
                f"execution_budget.{field} 必须在 0..{upper_bound} 之间"
            )
        result[field] = raw
    raw_cost = value["max_external_cost_usd"]
    if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)):
        raise StateConflict("execution_budget.max_external_cost_usd 必须是数字")
    cost = float(raw_cost)
    if not math.isfinite(cost) or cost < 0 or cost > MAX_EXTERNAL_COST_USD:
        raise StateConflict(
            f"execution_budget.max_external_cost_usd 必须是 0..{MAX_EXTERNAL_COST_USD} 的有限数字"
        )
    result["max_external_cost_usd"] = raw_cost
    without_evidence = result["nonconvergent_after_consecutive_rounds_without_new_evidence"]
    if without_evidence > result["max_review_rounds"]:
        raise StateConflict(
            "execution_budget.nonconvergent_after_consecutive_rounds_without_new_evidence "
            "不能大于 max_review_rounds"
        )
    return {field: result[field] for field in sorted(EXECUTION_BUDGET_FIELDS)}


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json") and isinstance(result[key], str):
            result[key[:-5]] = json.loads(result[key])
    return result


def _task_view(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    task = _row(row) if isinstance(row, sqlite3.Row) else dict(row)
    if task is None:
        raise StateNotFound("任务不存在")
    metadata = task.get("metadata")
    if metadata is None and isinstance(task.get("metadata_json"), str):
        metadata = json.loads(str(task["metadata_json"]) or "{}")
    metadata = metadata or {}
    if not isinstance(metadata, Mapping):
        raise StateConflict(f"任务元数据不是对象：{task.get('task_id') or '<unknown>'}")
    metadata = dict(metadata)
    metadata["delivery_mode"] = canonical_delivery_mode(metadata.get("delivery_mode"))
    if "execution_budget" in metadata:
        metadata["execution_budget"] = canonical_execution_budget(metadata["execution_budget"])
    task.update({key: metadata.get(key) for key in TASK_METADATA_FIELDS})
    task["metadata"] = metadata
    task["external_write_targets"] = list(metadata.get("external_write_targets") or [])
    return task


def render_task_card_projection(task: Mapping[str, Any]) -> str:
    """Render the only valid SQLite-active Markdown task-card projection."""
    if not task.get("task_id") or not task.get("session_id"):
        raise StateConflict("任务投影缺少 task_id/session_id")
    lines = [
        "---",
        f"task_id: {json.dumps(task['task_id'])}",
        f"title: {json.dumps(task.get('title') or task['task_id'], ensure_ascii=False)}",
        f"session_id: {json.dumps(task['session_id'])}",
        f"task_kind: {task.get('task_kind') or 'ordinary'}",
        f"platform: {json.dumps(task['platform'])}",
        f"delivery_mode: {task.get('delivery_mode') or 'files'}",
        f"maintenance: {'true' if task.get('maintenance') else 'false'}",
        "maintenance_authorization_receipt: "
        + (json.dumps(task.get("proposal_id")) if task.get("proposal_id") else "null"),
        "repaired_from_task_id: "
        + (json.dumps(task.get("repaired_from_task_id")) if task.get("repaired_from_task_id") else "null"),
        "repair_record_id: "
        + (json.dumps(task.get("repair_record_id")) if task.get("repair_record_id") else "null"),
        f"status: {task['lifecycle_status']}",
        f"runtime_status: {task['runtime_status']}",
        f"review_status: {task['review_status']}",
        f"created_at: {json.dumps(task['created_at'])}",
        f"updated_at: {json.dumps(task['updated_at'])}",
        "submitted_at: "
        + (json.dumps(task.get("submitted_at")) if task.get("submitted_at") else "null"),
        "execution_budget: "
        + json.dumps(task.get("execution_budget"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "interaction_preference_snapshot: "
        + json.dumps(
            task.get("interaction_preference_snapshot"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ),
        "irreversible_effects: "
        + json.dumps(task.get("irreversible_effects") or [], ensure_ascii=False),
        "external_effects: "
        + json.dumps(task.get("external_effects") or [], ensure_ascii=False),
        f"acceptance_owner: {json.dumps(task.get('acceptance_owner') or 'user', ensure_ascii=False)}",
        f"execution_owner_session_id: {json.dumps(task.get('execution_owner_session_id') or task['session_id'])}",
        "active_worker_leases: "
        + json.dumps(
            task.get("active_worker_leases") or [], ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ),
        "latest_handoff_id: "
        + (json.dumps(task.get("latest_handoff_id")) if task.get("latest_handoff_id") else "null"),
        "latest_delivery_id: "
        + (json.dumps(task.get("latest_delivery_id")) if task.get("latest_delivery_id") else "null"),
        "user_notes_path: "
        + (json.dumps(task.get("user_notes_path"), ensure_ascii=False) if task.get("user_notes_path") else "null"),
        "allowed_operations:",
        *[f"  - {value}" for value in (task.get("allowed_operations") or [])],
        "allowed_write_roots:",
        *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in task["allowed_write_roots"]],
        "external_write_roots:",
        *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in (task.get("external_write_roots") or [])],
        "excluded_scope:",
        *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in task["excluded_actions"]],
        f"verification_summary: {json.dumps(task.get('verification_summary'), ensure_ascii=False)}",
        f"submission_summary: {json.dumps(task.get('submission_summary'), ensure_ascii=False)}",
        "---", "", f"# {task.get('title') or task['task_id']}", "",
        "本卡由 SQLite 权威状态确定性投影；不得作为授权或验收真源。逐路径 `grants` 保存在权威数据库，本卡只展示规范化路径清单与操作类型并集。",
        "用户备注写入 `user_notes_path` 指向的同名 `.notes.md` 文件；该文件不由投影器创建、覆盖或解释为运行状态。",
        "",
    ]
    return "\n".join(lines)


def task_projection_view(
    store: "StateStore", task: Mapping[str, Any], card_path: str | Path,
) -> dict[str, Any]:
    """Attach verified read-only summaries used by the human task-card projection."""
    enriched = dict(task)
    path = Path(card_path).expanduser().resolve()
    with store.connect(readonly=True) as connection:
        leases = connection.execute(
            """SELECT lease_id,worker_session_id,role,read_only,expires_at
               FROM leases WHERE task_id=? AND status='active'
               ORDER BY lease_id""",
            (task["task_id"],),
        ).fetchall()
        handoff = connection.execute(
            """SELECT handoff_id FROM session_handoffs
               WHERE task_id=? AND status='active'
               ORDER BY created_at DESC LIMIT 1""",
            (task["task_id"],),
        ).fetchone()
        delivery = connection.execute(
            """SELECT delivery_id FROM deliveries
               WHERE task_id=? ORDER BY submitted_at DESC LIMIT 1""",
            (task["task_id"],),
        ).fetchone()
    enriched.update({
        "execution_owner_session_id": task["session_id"],
        "active_worker_leases": [
            {
                "lease_id": row["lease_id"],
                "worker_session_id": row["worker_session_id"],
                "role": row["role"],
                "read_only": bool(row["read_only"]),
                "expires_at": row["expires_at"],
            }
            for row in leases
        ],
        "latest_handoff_id": handoff["handoff_id"] if handoff else None,
        "latest_delivery_id": delivery["delivery_id"] if delivery else None,
        "user_notes_path": str(path.with_name(f"{path.stem}.notes.md")),
    })
    return enriched


def _validate_projection_workspace_binding(
    store: "StateStore", workspace_root: str | Path, *, allow_cutover_freeze: bool = False,
) -> tuple[Path, str]:
    root = Path(workspace_root).expanduser().resolve()
    sentinel_path = Path(str(store.path) + ".cutover.json")
    try:
        sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise StateConflict("投影缺少可验证的 SQLite cutover workspace 绑定") from exc
    bound_root = Path(str(sentinel.get("workspace_root") or "")).expanduser().resolve()
    bound_database = Path(str(sentinel.get("database") or "")).expanduser().resolve()
    bound_workspace_id = str(sentinel.get("workspace_id") or "")
    canonical_workspace_id = hashlib.sha256(str(bound_root).encode()).hexdigest()[:12]
    allowed_states = {"sqlite", "cutover_frozen"} if allow_cutover_freeze else {"sqlite"}
    state = sentinel.get("state")
    state_flags_valid = (
        state == "sqlite" and sentinel.get("active") is True
    ) or (
        allow_cutover_freeze and state == "cutover_frozen" and sentinel.get("active") is False
    )
    if (
        sentinel.get("schema_version") != 1
        or state not in allowed_states
        or not state_flags_valid
        or sentinel.get("legacy_import_disabled") is not True
        or bound_database != store.path
        or root != bound_root
        or bound_workspace_id != canonical_workspace_id
    ):
        raise StateConflict("投影 workspace_root 与不可变 cutover 绑定不一致")
    return root, bound_workspace_id


class StateStore:
    """Transactional SQLite state store with no file-to-database import path."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = int(busy_timeout_ms)

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            target = self.path.expanduser().resolve().as_uri() + "?mode=ro"
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.path)
        connection = sqlite3.connect(
            target,
            timeout=max(self.busy_timeout_ms / 1000, 0.001),
            isolation_level=None,
            check_same_thread=False,
            factory=ClosingConnection,
            uri=readonly,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        lock_path = Path(str(self.path) + ".cutover.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as cutover_lock:
            fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_SH)
            try:
                with self.connect() as connection:
                    connection.executescript(SCHEMA_SQL)
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS task_projection_manifest (
                            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                            projection_kind TEXT NOT NULL DEFAULT 'task_card',
                            path TEXT,
                            authority_updated_at TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN ('pending','projected','degraded')),
                            sha256 TEXT,
                            projected_at TEXT
                        );
                        CREATE TRIGGER IF NOT EXISTS trg_task_projection_insert
                        AFTER INSERT ON tasks BEGIN
                            INSERT INTO task_projection_manifest(
                                task_id, projection_kind, path, authority_updated_at, status
                            ) VALUES (
                                NEW.task_id, 'task_card', json_extract(NEW.metadata_json, '$.card_path'),
                                NEW.updated_at, 'pending'
                            ) ON CONFLICT(task_id) DO UPDATE SET
                                projection_kind='task_card', path=excluded.path,
                                authority_updated_at=excluded.authority_updated_at,
                                status='pending', sha256=NULL, projected_at=NULL;
                        END;
                        CREATE TRIGGER IF NOT EXISTS trg_task_projection_update
                        AFTER UPDATE ON tasks BEGIN
                            INSERT INTO task_projection_manifest(
                                task_id, projection_kind, path, authority_updated_at, status
                            ) VALUES (
                                NEW.task_id, 'task_card', json_extract(NEW.metadata_json, '$.card_path'),
                                NEW.updated_at, 'pending'
                            ) ON CONFLICT(task_id) DO UPDATE SET
                                projection_kind='task_card', path=excluded.path,
                                authority_updated_at=excluded.authority_updated_at,
                                status='pending', sha256=NULL, projected_at=NULL;
                        END;
                        DROP TRIGGER IF EXISTS trg_task_terminal_revoke_leases;
                        CREATE TRIGGER trg_task_terminal_revoke_leases
                        AFTER UPDATE OF lifecycle_status, runtime_status ON tasks
                        WHEN NEW.lifecycle_status IN ('submitted','completed','canceled','archived','legacy_unreviewed','invalid_envelope')
                          OR NEW.runtime_status IN ('submitted','completed','canceled','archived','invalid_envelope')
                        BEGIN
                            UPDATE leases SET status=CASE
                                WHEN NEW.lifecycle_status IN ('canceled','invalid_envelope')
                                  OR NEW.runtime_status IN ('canceled','invalid_envelope')
                                THEN 'revoked' ELSE 'completed' END
                            WHERE task_id=NEW.task_id AND status='active';
                        END;
                    """)
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
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
                            "tasks": {
                                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                                "allowed_operations_json": "TEXT NOT NULL DEFAULT '[]'",
                                "grants_json": "TEXT NOT NULL DEFAULT '[]'",
                            },
                            "leases": {
                                "allowed_operations_json": "TEXT NOT NULL DEFAULT '[]'",
                                "grants_json": "TEXT NOT NULL DEFAULT '[]'",
                            },
                        }
                        for table, columns in migrations.items():
                            existing = {item["name"] for item in connection.execute(f"PRAGMA table_info({table})")}
                            for column, declaration in columns.items():
                                if column not in existing:
                                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                        if version is None or int(version) < SCHEMA_VERSION:
                            connection.execute(
                                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                                (SCHEMA_VERSION, _timestamp()),
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
            finally:
                fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        lock_path = Path(str(self.path) + ".cutover.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as cutover_lock:
            fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_SH)
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
                fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_UN)

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM runtime_meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set_meta(self, key: str, value: Any, *, at: datetime | str | None = None) -> Any:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO runtime_meta(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (key, _json(value), _timestamp(at)),
            )
        return value

    def is_backend_active(self) -> bool:
        if not self.path.is_file():
            return False
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_meta WHERE key='backend_active'"
            ).fetchone()
        return bool(row and json.loads(row["value_json"]) is True)

    def activate_backend(self, *, at: datetime | str | None = None) -> bool:
        self.set_meta("backend_active", True, at=at)
        return True

    def _append_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
        workspace_id: str,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        event_id = event_id or _new_id("E")
        connection.execute(
            """INSERT INTO events(
                   event_id, event_type, occurred_at, workspace_id, session_id, task_id, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, _timestamp(occurred_at), workspace_id, session_id, task_id, _json(payload)),
        )
        return event_id

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
        workspace_id: str,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        with self.transaction(immediate=True) as connection:
            return self._append_event(
                connection,
                event_type,
                payload or {},
                event_id=event_id,
                occurred_at=occurred_at,
                workspace_id=workspace_id,
                session_id=session_id,
                task_id=task_id,
            )


    def enqueue_outbox(
        self,
        *,
        dedupe_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        available_at: datetime | str | None = None,
    ) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id,
                       payload_json, available_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (dedupe_key, event_type, aggregate_type, aggregate_id,
                 _json(dict(payload)), _timestamp(available_at)),
            )
            return cursor.rowcount == 1

    def record_user_event(
        self,
        *,
        event_id: str,
        workspace_id: str,
        session_id: str,
        platform: str,
        prompt_sha256: str,
        observed_at: datetime | str,
        ttl_seconds: int,
        host_message_id: str | None = None,
        bindings: dict[str, Any] | None = None,
        actor_verified: bool = False,
    ) -> dict[str, Any]:
        """Insert once; retries never refresh first_observed_at or expires_at."""
        first = _as_utc(observed_at)
        expires = first + timedelta(seconds=int(ttl_seconds))
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """INSERT INTO user_events(
                       event_id, workspace_id, session_id, platform, host_message_id,
                       prompt_sha256, first_observed_at, expires_at, bindings_json, actor_verified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_id) DO NOTHING""",
                (
                    event_id,
                    workspace_id,
                    session_id,
                    platform,
                    host_message_id,
                    prompt_sha256,
                    _timestamp(first),
                    _timestamp(expires),
                    _json(bindings or {}),
                    _bool(actor_verified),
                ),
            )
            row = connection.execute("SELECT * FROM user_events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                raise StateError("用户事件写入失败")
            immutable = {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "platform": platform,
                "host_message_id": host_message_id,
                "prompt_sha256": prompt_sha256,
                "bindings_json": _json(bindings or {}),
            }
            if any(row[key] != value for key, value in immutable.items()):
                raise StateConflict("同一 event_id 的不可变来源字段不一致")
            if cursor.rowcount == 1:
                self._append_event(
                    connection,
                    "user_event_recorded",
                    {
                        "user_event_id": event_id,
                        "first_observed_at": row["first_observed_at"],
                        "expires_at": row["expires_at"],
                        "actor_verified": bool(row["actor_verified"]),
                    },
                    workspace_id=workspace_id,
                    session_id=session_id,
                    occurred_at=first,
                )
            return _row(row) or {}

    def get_user_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect(readonly=True) as connection:
            return _row(connection.execute("SELECT * FROM user_events WHERE event_id = ?", (event_id,)).fetchone())

    def resolve_live_user_event(
        self,
        *,
        workspace_id: str,
        session_id: str,
        platform: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Resolve exactly one live event without accepting replay text or actor fields."""
        current = _as_utc(_timestamp(at))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM user_events
                   WHERE workspace_id = ? AND session_id = ? AND platform = ?
                     AND consumed_at IS NULL""",
                (workspace_id, session_id, platform),
            ).fetchall()
        candidates = [row for row in rows if _as_utc(row["expires_at"]) >= current]
        if not candidates:
            raise StateNotFound("当前会话没有有效且未消费的用户事件")
        if len(candidates) != 1:
            raise StateConflict("当前会话存在多个有效用户事件，拒绝静默关联")
        return _row(candidates[0]) or {}

    def freeze_user_event_bindings(self, event_id: str, additions: Mapping[str, Any]) -> dict[str, Any]:
        """Fill previously unbound semantic targets once; retries must be identical."""
        reserved = {"interaction_preference_intent", "legacy_authority_reconciliation",
                    "review_target_reference"}
        if reserved & set(additions):
            raise StateConflict("结构化授权/偏好/验收意图只能通过受约束冻结入口写入")
        if "interaction_preference_intent" in additions:
            raise StateConflict("交互偏好意图只能通过受约束冻结入口写入")
        try:
            canonical_additions = {
                str(key): json.loads(_json(value)) for key, value in additions.items()
            }
        except (TypeError, ValueError) as exc:
            raise StateConflict("用户事件冻结绑定必须是可规范化 JSON 值") from exc
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM user_events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                raise StateNotFound(f"用户事件不存在：{event_id}")
            if row["consumed_at"] is not None:
                raise StateConflict("已消费用户事件不能补充绑定")
            current = json.loads(row["bindings_json"])
            explicit_refinement = (
                canonical_additions.get("explicit_target") is True
                and current.get("explicit_target") is not True
            ) or (
                canonical_additions.get("explicit_proposal_reference") is True
                and current.get("explicit_proposal_reference") is not True
            )
            for key, value in canonical_additions.items():
                refinable = key in {"task_id", "delivery_id", "focus_id", "maintenance_proposal_id", "disclosure_id"}
                present = current.get(key)
                unbound = key not in current or present is None or present == ""
                identical = not unbound and _json(present) == _json(value)
                if not unbound and not identical and not (explicit_refinement and refinable):
                    raise StateConflict(f"用户事件冻结的 {key} 不允许改变")
                current[key] = value
            connection.execute(
                "UPDATE user_events SET bindings_json = ? WHERE event_id = ? AND consumed_at IS NULL",
                (_json(current), event_id),
            )
        return self.get_user_event(event_id) or {}

    def freeze_legacy_authority_reconciliation(
        self, *, event_id: str, task_id: str, at: datetime | str | None = None
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        current = _as_utc(stamp)
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            metadata = json.loads(task["metadata_json"] or "{}")
            if metadata.get("legacy_import") is not True or canonical_task_kind(task["task_kind"]) != "control_plane_maintenance":
                raise StateConflict("只有活动的切换导入维护任务可以进行权限对账")
            if task["lifecycle_status"] != "in_progress":
                raise StateConflict("只有活动任务可以进行 legacy 权限对账")
            event = connection.execute("SELECT * FROM user_events WHERE event_id=?", (event_id,)).fetchone()
            if event is None:
                raise StateNotFound(f"用户事件不存在：{event_id}")
            if event["workspace_id"] != task["workspace_id"]:
                raise StateConflict("legacy 对账事件与任务不属于同一 workspace")
            core = {
                "task_id": task_id, "envelope_id": task["envelope_id"],
                "envelope_digest": task["envelope_digest"], "user_event_id": event_id,
            }
            proof_digest = _digest(core)
            frozen = {**core, "proof_digest": proof_digest}
            receipt_id = f"EV-LARF-{proof_digest[:24]}"
            bindings = json.loads(event["bindings_json"] or "{}")
            existing = bindings.get("legacy_authority_reconciliation")
            if existing is not None:
                if existing != frozen:
                    raise StateConflict("legacy 权限对账意图不可改变")
                receipt = connection.execute("SELECT event_id FROM events WHERE event_id=?", (receipt_id,)).fetchone()
                if receipt is None:
                    raise StateConflict("legacy 权限对账缺少冻结审计收据")
                return frozen
            if event["consumed_at"] is not None or _as_utc(event["expires_at"]) <= current:
                raise StateConflict("legacy 权限对账必须使用有效未消费用户事件")
            for key, expected in (("task_id", task_id), ("envelope_id", task["envelope_id"])):
                if bindings.get(key) not in {None, "", expected}:
                    raise StateConflict(f"用户事件 {key} 与 legacy 对账目标冲突")
                bindings[key] = expected
            bindings["legacy_authority_reconciliation"] = frozen
            updated = connection.execute(
                """UPDATE user_events SET bindings_json=?
                   WHERE event_id=? AND consumed_at IS NULL AND bindings_json=?""",
                (_json(bindings), event_id, event["bindings_json"]),
            )
            if updated.rowcount != 1:
                raise StateConflict("legacy 权限对账冻结发生并发冲突")
            self._append_event(
                connection, "legacy_authority_reconciliation_frozen",
                {"user_event_id": event_id, "proof_digest": proof_digest,
                 "envelope_id": task["envelope_id"], "actor_verified": bool(event["actor_verified"])},
                event_id=receipt_id, occurred_at=stamp, workspace_id=task["workspace_id"],
                session_id=event["session_id"], task_id=task_id,
            )
            return frozen

    def reconcile_legacy_execution_authority(
        self, *, task_id: str, user_event_id: str, at: datetime | str | None = None
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            event = connection.execute("SELECT * FROM user_events WHERE event_id=?", (user_event_id,)).fetchone()
            if task is None or event is None:
                raise StateNotFound("legacy 对账任务或用户事件不存在")
            bindings = json.loads(event["bindings_json"] or "{}")
            frozen = bindings.get("legacy_authority_reconciliation")
            core = {
                "task_id": task_id, "envelope_id": task["envelope_id"],
                "envelope_digest": task["envelope_digest"], "user_event_id": user_event_id,
            }
            proof_digest = _digest(core)
            if frozen != {**core, "proof_digest": proof_digest}:
                raise StateConflict("legacy 权限对账没有匹配的冻结结构化意图")
            freeze_receipt = connection.execute(
                "SELECT event_id FROM events WHERE event_id=?", (f"EV-LARF-{proof_digest[:24]}",)
            ).fetchone()
            if freeze_receipt is None:
                raise StateConflict("legacy 权限对账缺少冻结收据")
            proof_event_id = f"EV-LAR-{proof_digest[:24]}"
            existing = connection.execute("SELECT event_id FROM events WHERE event_id=?", (proof_event_id,)).fetchone()
            expected_consumer = f"legacy_authority_reconciliation:{task_id}"
            if existing is not None:
                if event["consumed_by"] != expected_consumer:
                    raise StateConflict("legacy 权限对账收据与事件消费状态不一致")
                return {"proof_event_id": proof_event_id, "proof_digest": proof_digest, "idempotent": True}
            if event["consumed_at"] is not None or _as_utc(event["expires_at"]) <= _as_utc(stamp):
                raise StateConflict("legacy 权限对账事件已消费或过期")
            updated = connection.execute(
                """UPDATE user_events SET consumed_at=?, consumed_by=?
                   WHERE event_id=? AND consumed_at IS NULL""",
                (stamp, expected_consumer, user_event_id),
            )
            if updated.rowcount != 1:
                raise StateConflict("legacy 权限对账消费发生并发冲突")
            self._append_event(
                connection, "legacy_execution_authority_reconciled",
                {"user_event_id": user_event_id, "proof_digest": proof_digest,
                 "envelope_id": task["envelope_id"], "actor_verified": bool(event["actor_verified"])},
                event_id=proof_event_id, occurred_at=stamp, workspace_id=task["workspace_id"],
                session_id=event["session_id"], task_id=task_id,
            )
            return {"proof_event_id": proof_event_id, "proof_digest": proof_digest, "idempotent": False}

    def reconcile_existing_legacy_task_authority(
        self, *, task_id: str, proposal_id: str, session_id: str,
        workspace_id: str, at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Recover one imported task from its already-consumed authorization chain."""
        stamp = _timestamp(at)
        expires_at = _timestamp(_as_utc(stamp) + timedelta(hours=24))
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT value_json FROM runtime_meta WHERE key='backend_active'"
            ).fetchone()
            if active is None or json.loads(active["value_json"]) is not True:
                raise StateConflict("existing-authority reconciliation requires activated SQLite")
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            chain = connection.execute(
                """SELECT p.*, d.objective_id AS disclosure_objective_id,
                          d.workspace_id AS disclosure_workspace_id,
                          d.task_kind AS disclosure_task_kind,
                          d.disclosure_digest, d.payload_json AS disclosure_payload_json,
                          o.workspace_id AS objective_workspace_id
                   FROM maintenance_proposals p
                   JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                   JOIN objectives o ON o.objective_id=d.objective_id
                   WHERE p.proposal_id=?""",
                (proposal_id,),
            ).fetchone()
            if task is None or chain is None:
                raise StateNotFound("legacy task or its authorization chain does not exist")
            metadata = json.loads(task["metadata_json"] or "{}")
            if metadata.get("legacy_import") is not True:
                raise StateConflict("only an existing legacy_import task may be reconciled")
            if not (
                task["workspace_id"] == workspace_id == chain["workspace_id"]
                == chain["disclosure_workspace_id"] == chain["objective_workspace_id"]
                and task["session_id"] == session_id
                and task["proposal_id"] == proposal_id
                and task["disclosure_id"] == chain["disclosure_id"]
                and task["objective_id"] == chain["disclosure_objective_id"]
                and task["envelope_id"]
                and canonical_task_kind(task["task_kind"]) == "control_plane_maintenance"
                and canonical_task_kind(chain["disclosure_task_kind"])
                    == "control_plane_maintenance"
                and chain["status"] == "consumed" and chain["consumed_at"]
            ):
                raise StateConflict("task/proposal/disclosure/objective/source-event chain conflicts")
            proposal_payload = json.loads(chain["payload_json"] or "{}")
            legacy_shape = not chain["authorized_by_event_id"]
            if legacy_shape:
                source_handle = str(proposal_payload.get("source_event_id") or "")
                source_sha = str(proposal_payload.get("raw_prompt_sha256") or "")
                if not (source_handle and source_sha and proposal_payload.get("action") == "authorize_maintenance"):
                    raise StateConflict("legacy proposal lacks its frozen source event reference")
                source_rows = connection.execute(
                    """SELECT * FROM user_events
                       WHERE workspace_id=? AND session_id=? AND host_message_id=?
                             AND prompt_sha256=?""",
                    (workspace_id, session_id, source_handle, source_sha),
                ).fetchall()
                if len(source_rows) != 1:
                    raise StateConflict("legacy source event reference is missing or ambiguous")
                source_event = source_rows[0]
                source_event_id = source_event["event_id"]
                receipts = connection.execute(
                    """SELECT event_type, payload_json FROM events
                       WHERE workspace_id=? AND session_id=? AND event_type IN
                         ('user_prompt','maintenance_continuation_requested','semantic_event_consumed')""",
                    (workspace_id, session_id),
                ).fetchall()
                prompt_receipt = continuation_receipt = consumed_receipt = False
                for receipt in receipts:
                    payload = json.loads(receipt["payload_json"] or "{}")
                    if receipt["event_type"] == "user_prompt":
                        prompt_receipt |= (
                            payload.get("turn_id") == source_handle
                            and payload.get("maintenance_proposal_id") == proposal_id
                            and payload.get("prompt_sha256") == source_sha
                        )
                    elif receipt["event_type"] == "maintenance_continuation_requested":
                        continuation_receipt |= (
                            payload.get("source_event_id") == source_handle
                            and payload.get("maintenance_proposal_id") == proposal_id
                        )
                    else:
                        consumed_receipt |= (
                            payload.get("source_event_id") == source_handle
                            and payload.get("action") == "authorize_maintenance"
                            and payload.get("target") == "current_maintenance_proposal"
                        )
                if not (prompt_receipt and continuation_receipt and consumed_receipt):
                    raise StateConflict("legacy authorization receipt chain is incomplete")
            else:
                source_event = connection.execute(
                    "SELECT * FROM user_events WHERE event_id=?", (chain["authorized_by_event_id"],)
                ).fetchone()
                if source_event is None or source_event["workspace_id"] != workspace_id or not source_event["consumed_at"]:
                    raise StateConflict("authorized source user event is missing or unconsumed")
                source_event_id = source_event["event_id"]
                bindings = json.loads(source_event["bindings_json"] or "{}")
                if bindings.get("maintenance_proposal_id") != proposal_id:
                    raise StateConflict("source user event is not frozen to the proposal")
                if bindings.get("disclosure_id") not in (None, "", task["disclosure_id"]):
                    raise StateConflict("source user event disclosure binding conflicts")
                receipts = connection.execute(
                    """SELECT event_type, payload_json FROM events
                       WHERE workspace_id=? AND event_type IN
                             ('user_event_recorded', 'maintenance_authorized')""",
                    (workspace_id,),
                ).fetchall()
                recorded = authorized = False
                for receipt in receipts:
                    payload = json.loads(receipt["payload_json"] or "{}")
                    recorded |= receipt["event_type"] == "user_event_recorded" and payload.get("user_event_id") == source_event_id
                    authorized |= receipt["event_type"] == "maintenance_authorized" and payload.get("user_event_id") == source_event_id and payload.get("proposal_id") == proposal_id
                if not recorded or not authorized:
                    raise StateConflict("source user event or maintenance authorization receipt is missing")
            disclosure_payload = json.loads(chain["disclosure_payload_json"] or "{}")
            if legacy_shape:
                roots = canonical_scopes([str(value) for value in proposal_payload.get("scopes") or []])
                external = canonical_scopes([str(value) for value in proposal_payload.get("external_roots") or []])
                excluded = sorted(set(proposal_payload.get("excludes") or []))
                expected_disclosure = _digest({
                    "proposal_id": proposal_id, "roots": roots,
                    "source": proposal_payload.get("_source_path"),
                })
                if disclosure_payload != proposal_payload or chain["disclosure_digest"] != expected_disclosure or chain["scope_digest"] != expected_disclosure:
                    raise StateConflict("legacy proposal/disclosure scope digest conflicts")
                card = connection.execute(
                    "SELECT source_sha256, card_json FROM legacy_task_cards WHERE task_id=?", (task_id,)
                ).fetchone()
                if card is None:
                    raise StateConflict("legacy task card authority snapshot is missing")
                card_payload = json.loads(card["card_json"] or "{}")
                if canonical_scopes(card_payload.get("allowed_write_roots") or []) != roots or canonical_scopes(card_payload.get("external_write_roots") or []) != external:
                    raise StateConflict("legacy task card range conflicts with proposal")
                if canonical_scopes(metadata.get("external_write_roots") or []) != external:
                    raise StateConflict("legacy task external roots conflict")
                envelope_matches = bool(
                    str(task["envelope_id"] or "").startswith("ENVLEG-")
                    and re.fullmatch(r"[0-9a-f]{64}", str(task["envelope_digest"] or ""))
                )
            else:
                machine = disclosure_payload.get("machine") or {}
                proposal_machine = disclosure_machine_payload(
                    proposal_payload, objective_id=task["objective_id"], task_kind=task["task_kind"],
                )
                expected_proposal_digest = _digest({
                    "disclosure_id": task["disclosure_id"],
                    "disclosure_digest": chain["disclosure_digest"], "machine": machine,
                })
                if machine != proposal_machine or _digest(machine) != chain["disclosure_digest"] or expected_proposal_digest != chain["scope_digest"]:
                    raise StateConflict("proposal/disclosure machine scope digest conflicts")
                external = [str(item.get("path") or "") for item in machine.get("external_write_roots") or []]
                roots = canonical_scopes(list(proposal_payload.get("allowed_write_roots") or []) + external)
                excluded = sorted(set(machine.get("excluded_actions") or []))
                expected_envelope = _digest({
                    "task_id": task_id, "disclosure_digest": chain["disclosure_digest"],
                    "authorization_event_id": source_event_id, "authorization_version": 1,
                })
                envelope_matches = task["envelope_digest"] == expected_envelope
            if not (
                roots == json.loads(task["allowed_write_roots_json"] or "[]")
                and excluded == json.loads(task["excluded_actions_json"] or "[]")
                and envelope_matches
            ):
                raise StateConflict("task machine range or envelope digest conflicts")
            base = {
                "task_id": task_id, "proposal_id": proposal_id,
                "disclosure_id": task["disclosure_id"], "objective_id": task["objective_id"],
                "envelope_id": task["envelope_id"], "envelope_digest": task["envelope_digest"],
                "source_user_event_id": source_event_id, "workspace_id": workspace_id,
                "owner_session_id": session_id, "allowed_write_roots": roots,
                "external_write_roots": canonical_scopes(external),
                "excluded_actions": excluded,
                "actor_verified": False, "enforcement_verified": False,
                "reconciled_from_existing_authorization": True,
            }
            reusable = []
            for row in connection.execute(
                """SELECT * FROM leases
                   WHERE task_id=? AND source_session_id=? AND worker_session_id=?
                         AND role='owner' AND read_only=0 AND status='active'
                         AND enforcement_verified=0 AND expires_at>?""",
                (task_id, session_id, session_id, stamp),
            ).fetchall():
                if json.loads(row["allowed_write_roots_json"] or "[]") == roots:
                    reusable.append(row["lease_id"])
            if len(reusable) > 1:
                raise StateConflict("multiple matching owner leases make reconciliation ambiguous")
            lease_id = reusable[0] if reusable else f"L-RECON-{_digest(base)[:16]}"
            core = {**base, "owner_lease_id": lease_id}
            proof_digest = _digest(core)
            proof_event_id = f"EV-LAER-{proof_digest[:24]}"
            existing = connection.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (proof_event_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"] or "{}") != {**core, "proof_digest": proof_digest}:
                    raise StateConflict("existing reconciliation proof conflicts")
                lease = connection.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
                if lease is None or _as_utc(lease["expires_at"]) <= _as_utc(stamp):
                    raise StateConflict("reconciled owner lease is missing or expired")
                return {"proof_event_id": proof_event_id, "proof_digest": proof_digest,
                        "owner_lease_id": lease_id, "idempotent": True}
            if not reusable:
                connection.execute(
                    """INSERT INTO leases(
                           lease_id, task_id, source_session_id, worker_session_id, role,
                           allowed_write_roots_json, read_only, status, issued_at, expires_at,
                           enforcement_verified)
                       VALUES (?, ?, ?, ?, 'owner', ?, 0, 'active', ?, ?, 0)""",
                    (lease_id, task_id, session_id, session_id, _json(roots), stamp, expires_at),
                )
            self._append_event(
                connection, "legacy_execution_authority_reconciled",
                {**core, "proof_digest": proof_digest}, event_id=proof_event_id,
                occurred_at=stamp, workspace_id=workspace_id,
                session_id=session_id, task_id=task_id,
            )
            metadata.update({
                "execution_authority_reconciled": True,
                "execution_reconciliation_proof_id": proof_event_id,
                "reconciled_owner_lease_id": lease_id,
                "reconciled_from_existing_authorization": True,
            })
            connection.execute(
                "UPDATE tasks SET metadata_json=?, updated_at=? WHERE task_id=?",
                (_json(metadata), stamp, task_id),
            )
            return {"proof_event_id": proof_event_id, "proof_digest": proof_digest,
                    "owner_lease_id": lease_id, "idempotent": False}

    def freeze_review_target_reference(
        self, *, event_id: str, task_id: str, delivery_id: str,
        target_reference: str, user_prompt_text: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        normalized_text = " ".join(user_prompt_text.casefold().split())
        normalized_reference = " ".join(target_reference.casefold().split())
        if not normalized_reference or normalized_reference not in normalized_text:
            raise StateConflict("target_reference 不能证明来自用户原文")
        with self.transaction(immediate=True) as connection:
            event = connection.execute("SELECT * FROM user_events WHERE event_id=?", (event_id,)).fetchone()
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            delivery = connection.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if event is None or task is None or delivery is None or delivery["task_id"] != task_id:
                raise StateNotFound("验收引用事件、任务或交付不存在")
            if event["workspace_id"] != task["workspace_id"]:
                raise StateConflict("验收引用与任务不属于同一 workspace")
            if hashlib.sha256(user_prompt_text.encode()).hexdigest() != event["prompt_sha256"]:
                raise StateConflict("target_reference 原文与冻结用户事件摘要不一致")
            if event["consumed_at"] is not None or _as_utc(event["expires_at"]) <= _as_utc(stamp):
                raise StateConflict("验收引用必须绑定有效未消费用户事件")
            core = {
                "task_id": task_id, "delivery_id": delivery_id,
                "target_reference": normalized_reference,
                "source_prompt_sha256": event["prompt_sha256"],
            }
            reference_digest = _digest(core)
            proof = {**core, "reference_digest": reference_digest}
            bindings = json.loads(event["bindings_json"] or "{}")
            existing = bindings.get("review_target_reference")
            if existing is not None:
                if existing != proof:
                    raise StateConflict("验收目标引用不可改变")
                return self.get_user_event(event_id) or {}
            for key, expected in (("task_id", task_id), ("delivery_id", delivery_id)):
                if bindings.get(key) not in {None, "", expected}:
                    raise StateConflict(f"验收引用冻结的 {key} 冲突")
                bindings[key] = expected
            bindings["target_reference"] = target_reference
            bindings["review_target_reference"] = proof
            updated = connection.execute(
                """UPDATE user_events SET bindings_json=?
                   WHERE event_id=? AND consumed_at IS NULL AND bindings_json=?""",
                (_json(bindings), event_id, event["bindings_json"]),
            )
            if updated.rowcount != 1:
                raise StateConflict("验收目标引用冻结发生并发冲突")
            self._append_event(
                connection, "review_target_reference_frozen",
                {"user_event_id": event_id, "reference_digest": reference_digest,
                 "task_id": task_id, "delivery_id": delivery_id},
                event_id=f"EV-RTR-{reference_digest[:24]}", occurred_at=stamp,
                workspace_id=task["workspace_id"], session_id=event["session_id"], task_id=task_id,
            )
        return self.get_user_event(event_id) or {}

    def freeze_interaction_preference_intent(
        self,
        *,
        event_id: str,
        task_id: str,
        intent_code: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Freeze a registry-backed preference intent while its user event is live."""
        supported_code = "never_report_once_no_prompt"
        if intent_code != supported_code:
            raise ScopeViolation("不支持的交互偏好结构化意图")
        stamp = _timestamp(at)
        current = _as_utc(stamp)
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            metadata = json.loads(task["metadata_json"] or "{}")
            if not self._execution_authority_in_connection(connection, task):
                raise StateConflict("交互偏好意图必须绑定真实授权执行包络")
            if metadata.get("legacy_import") and not (
                task["task_kind"] == "control_plane_maintenance"
                and task["objective_id"]
                and task["disclosure_id"]
                and task["proposal_id"]
                and task["envelope_id"]
            ):
                raise StateConflict("切换导入任务缺少完整维护包络，不能绑定交互偏好")
            if task["lifecycle_status"] != "in_progress":
                raise StateConflict("交互偏好意图只能绑定当前执行包络")

            event = connection.execute(
                "SELECT * FROM user_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise StateNotFound(f"用户事件不存在：{event_id}")
            if event["workspace_id"] != task["workspace_id"]:
                raise ScopeViolation("用户事件与任务不属于同一工作区")

            bindings = json.loads(event["bindings_json"] or "{}")
            existing = bindings.get("interaction_preference_intent")
            core = {
                "intent_code": supported_code,
                "intermediate_confirmation_policy": "never",
                "review_prompt_policy": "report_once_no_prompt",
                "task_id": task_id,
                "envelope_id": task["envelope_id"],
                "source_user_event_id": event_id,
            }
            intent_digest = _digest(core)
            frozen = {**core, "intent_digest": intent_digest}
            receipt_id = f"EV-PIF-{intent_digest[:24]}"
            if existing is not None:
                if existing != frozen:
                    raise StateConflict("用户事件中的交互偏好意图不可改变")
                receipt = connection.execute(
                    "SELECT * FROM events WHERE event_id = ?", (receipt_id,)
                ).fetchone()
                if receipt is None or receipt["event_type"] != "interaction_preference_intent_frozen":
                    raise StateConflict("冻结意图缺少数据库审计收据")
                return frozen

            if event["consumed_at"] is not None:
                raise StateConflict("已消费用户事件不能补冻交互偏好意图")
            if _as_utc(event["expires_at"]) < current:
                raise StateConflict("过期用户事件不能补冻交互偏好意图")
            for key, expected in (
                ("task_id", task_id),
                ("envelope_id", task["envelope_id"]),
            ):
                present = bindings.get(key)
                if present not in (None, "", expected):
                    raise StateConflict(f"用户事件冻结的 {key} 与当前执行包络冲突")
                bindings[key] = expected
            bindings["interaction_preference_intent"] = frozen
            cursor = connection.execute(
                """UPDATE user_events SET bindings_json = ?
                   WHERE event_id = ? AND consumed_at IS NULL AND bindings_json = ?""",
                (_json(bindings), event_id, event["bindings_json"]),
            )
            if cursor.rowcount != 1:
                raise StateConflict("用户事件交互偏好意图冻结发生并发冲突")
            self._append_event(
                connection,
                "interaction_preference_intent_frozen",
                {
                    "source_user_event_id": event_id,
                    "intent_digest": intent_digest,
                    "intent_code": supported_code,
                    "envelope_id": task["envelope_id"],
                    "actor_verified": bool(event["actor_verified"]),
                },
                event_id=receipt_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=event["session_id"],
                task_id=task_id,
            )
            return frozen

    def reconcile_interaction_preference_history(
        self,
        *,
        task_id: str,
        user_event_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Persist a frozen preference intent without accepting replayed user text."""
        stamp = _timestamp(at)
        supported_code = "never_report_once_no_prompt"
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            metadata = json.loads(task["metadata_json"] or "{}")
            if not self._execution_authority_in_connection(connection, task):
                raise StateConflict("历史偏好对账必须绑定真实授权执行包络")
            if metadata.get("legacy_import") and not (
                task["task_kind"] == "control_plane_maintenance"
                and task["objective_id"]
                and task["disclosure_id"]
                and task["proposal_id"]
                and task["envelope_id"]
            ):
                raise StateConflict("切换导入任务缺少完整维护包络，不能执行偏好对账")
            if task["lifecycle_status"] != "in_progress":
                raise StateConflict("历史偏好对账只能绑定当前执行包络")

            event = connection.execute(
                "SELECT * FROM user_events WHERE event_id = ?", (user_event_id,)
            ).fetchone()
            if event is None:
                raise StateNotFound(f"用户事件不存在：{user_event_id}")
            if event["workspace_id"] != task["workspace_id"]:
                raise ScopeViolation("用户事件与任务不属于同一工作区")
            bindings = json.loads(event["bindings_json"] or "{}")
            if bindings.get("task_id") != task_id or bindings.get("envelope_id") != task["envelope_id"]:
                raise StateConflict("用户事件未冻结绑定当前任务与执行包络")
            intent = bindings.get("interaction_preference_intent")
            if not isinstance(intent, dict):
                raise StateConflict("用户事件缺少冻结的结构化交互偏好意图")
            core = {
                "intent_code": supported_code,
                "intermediate_confirmation_policy": "never",
                "review_prompt_policy": "report_once_no_prompt",
                "task_id": task_id,
                "envelope_id": task["envelope_id"],
                "source_user_event_id": user_event_id,
            }
            intent_digest = _digest(core)
            if intent != {**core, "intent_digest": intent_digest}:
                raise StateConflict("结构化交互偏好意图内容或摘要不一致")
            freeze_receipt_id = f"EV-PIF-{intent_digest[:24]}"
            freeze_receipt = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (freeze_receipt_id,)
            ).fetchone()
            if freeze_receipt is None or freeze_receipt["event_type"] != "interaction_preference_intent_frozen":
                raise StateConflict("结构化交互偏好意图缺少冻结审计收据")
            freeze_payload = json.loads(freeze_receipt["payload_json"] or "{}")
            if (
                freeze_receipt["workspace_id"] != task["workspace_id"]
                or freeze_receipt["task_id"] != task_id
                or freeze_payload.get("source_user_event_id") != user_event_id
                or freeze_payload.get("intent_digest") != intent_digest
                or freeze_payload.get("envelope_id") != task["envelope_id"]
            ):
                raise StateConflict("冻结审计收据与当前执行包络不一致")

            reconciliation_digest = _digest(
                {
                    "task_id": task_id,
                    "envelope_id": task["envelope_id"],
                    "source_user_event_id": user_event_id,
                    "intent_digest": intent_digest,
                }
            )
            reconciliation_event_id = f"EV-PREFREC-{reconciliation_digest[:24]}"
            existing_receipt = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (reconciliation_event_id,)
            ).fetchone()
            preference = connection.execute(
                "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = 'default'",
                (task["workspace_id"],),
            ).fetchone()
            expected_preference = (
                preference is not None
                and preference["source_user_event_id"] == user_event_id
                and preference["intermediate_confirmation_policy"] == "never"
                and preference["review_prompt_policy"] == "report_once_no_prompt"
            )
            if existing_receipt is not None:
                if existing_receipt["event_type"] != "interaction_preference_history_reconciled" or not expected_preference:
                    raise StateConflict("历史偏好对账收据与当前偏好状态不一致")
                return {**_row(preference), "idempotent": True, "reconciliation_event_id": reconciliation_event_id}
            if preference is not None and not expected_preference:
                raise StateConflict("历史用户事件不得覆盖已有或更新的交互偏好")

            expected_consumer = f"reconcile_interaction_preference:{task_id}"
            if event["consumed_at"] is None:
                cursor = connection.execute(
                    """UPDATE user_events SET consumed_at = ?, consumed_by = ?
                       WHERE event_id = ? AND consumed_at IS NULL""",
                    (stamp, expected_consumer, user_event_id),
                )
                if cursor.rowcount != 1:
                    raise StateConflict("历史偏好对账消费用户事件发生并发冲突")
            elif event["consumed_by"] != expected_consumer:
                proposal = connection.execute(
                    """SELECT * FROM maintenance_proposals
                       WHERE proposal_id = ? AND authorized_by_event_id = ?""",
                    (task["proposal_id"], user_event_id),
                ).fetchone()
                additional = json.loads(proposal["additional_intents_json"] or "[]") if proposal else []
                if supported_code not in additional:
                    raise StateConflict("已消费用户事件未将交互偏好声明为授权附加意图")

            preference_id = _digest(
                {"workspace_id": task["workspace_id"], "user_scope": "default"}
            )[:24]
            connection.execute(
                """INSERT INTO preferences(
                       preference_id, workspace_id, user_scope,
                       intermediate_confirmation_policy, review_prompt_policy,
                       source_user_event_id, updated_at
                   ) VALUES (?, ?, 'default', 'never', 'report_once_no_prompt', ?, ?)
                   ON CONFLICT(workspace_id, user_scope) DO UPDATE SET
                       intermediate_confirmation_policy = excluded.intermediate_confirmation_policy,
                       review_prompt_policy = excluded.review_prompt_policy,
                       source_user_event_id = excluded.source_user_event_id,
                       updated_at = excluded.updated_at""",
                (preference_id, task["workspace_id"], user_event_id, stamp),
            )
            self._append_event(
                connection,
                "interaction_preference_history_reconciled",
                {
                    "source_user_event_id": user_event_id,
                    "intent_digest": intent_digest,
                    "envelope_id": task["envelope_id"],
                    "reconciliation_digest": reconciliation_digest,
                    "actor_verified": bool(event["actor_verified"]),
                },
                event_id=reconciliation_event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=event["session_id"],
                task_id=task_id,
            )
            preference = connection.execute(
                "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = 'default'",
                (task["workspace_id"],),
            ).fetchone()
            return {**_row(preference), "idempotent": False, "reconciliation_event_id": reconciliation_event_id}

    def reconcile_interaction_preference_from_authorization(
        self,
        *,
        task_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Repair a dropped preference from the task's frozen authorization event.

        This repairs derived state rather than replaying user text: both preference
        intents must already exist in the immutable bindings of the event that
        authorized the task's consumed proposal.
        """
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            if task["lifecycle_status"] != "in_progress":
                raise StateConflict("交互偏好派生修复只能绑定当前执行任务")
            if not self._execution_authority_in_connection(connection, task):
                raise StateConflict("交互偏好派生修复必须绑定真实授权执行包络")
            proposal = connection.execute(
                "SELECT * FROM maintenance_proposals WHERE proposal_id = ?",
                (task["proposal_id"],),
            ).fetchone()
            if proposal is None or proposal["status"] != "consumed":
                raise StateConflict("当前任务缺少已消费的授权提案")
            source_event_id = str(proposal["authorized_by_event_id"] or "")
            event = connection.execute(
                "SELECT * FROM user_events WHERE event_id = ?", (source_event_id,)
            ).fetchone()
            if event is None or event["workspace_id"] != task["workspace_id"]:
                raise StateConflict("任务授权用户事件不存在或不属于当前工作区")
            bindings = json.loads(event["bindings_json"] or "{}")
            requested = {
                str(value) for value in (bindings.get("additional_intents") or [])
            }
            required = {"no_intermediate_confirmation", "report_once_no_prompt"}
            if not required.issubset(requested):
                raise StateConflict("授权用户事件没有同时声明两项交互偏好")
            proposal_intents = {
                str(value) for value in json.loads(proposal["additional_intents_json"] or "[]")
            }
            if not required.issubset(proposal_intents):
                raise StateConflict("已消费提案没有不可变保存两项交互偏好")
            authorization_receipts = connection.execute(
                """SELECT sequence,payload_json FROM events
                   WHERE event_type='maintenance_authorized'
                     AND workspace_id=? ORDER BY sequence""",
                (task["workspace_id"],),
            ).fetchall()
            proved_receipts: list[sqlite3.Row] = []
            for row in authorization_receipts:
                payload = json.loads(row["payload_json"] or "{}")
                receipt_intents = {
                    str(value) for value in (payload.get("additional_intents") or [])
                }
                if (
                    payload.get("user_event_id") == source_event_id
                    and payload.get("proposal_id") == task["proposal_id"]
                    and required.issubset(receipt_intents)
                ):
                    proved_receipts.append(row)
            if len(proved_receipts) != 1:
                raise StateConflict("维护授权审计收据没有不可变证明两项交互偏好")
            authorization_sequence = int(proved_receipts[0]["sequence"])

            proof = {
                "task_id": task_id,
                "envelope_id": task["envelope_id"],
                "proposal_id": task["proposal_id"],
                "source_user_event_id": source_event_id,
                "intermediate_confirmation_policy": "never",
                "review_prompt_policy": "report_once_no_prompt",
            }
            proof_digest = _digest(proof)
            receipt_id = f"EV-PREFDER-{proof_digest[:24]}"
            existing_receipt = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (receipt_id,)
            ).fetchone()
            preference = connection.execute(
                "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = 'default'",
                (task["workspace_id"],),
            ).fetchone()
            expected_preference = (
                preference is not None
                and preference["source_user_event_id"] == source_event_id
                and preference["intermediate_confirmation_policy"] == "never"
                and preference["review_prompt_policy"] == "report_once_no_prompt"
            )
            if existing_receipt is not None:
                if existing_receipt["event_type"] != "interaction_preference_derived_from_authorization":
                    raise StateConflict("交互偏好派生修复收据类型冲突")
                if not expected_preference:
                    raise StateConflict("交互偏好派生修复收据与权威偏好不一致")
                return {**_row(preference), "idempotent": True, "receipt_id": receipt_id}
            if preference is not None and not expected_preference:
                raise StateConflict("不得用历史授权覆盖其他用户事件建立的交互偏好")
            later_preference_rows = connection.execute(
                """SELECT event_type,sequence,payload_json FROM events
                   WHERE workspace_id=? AND sequence>?
                     AND event_type IN (
                       'interaction_preference_set',
                       'interaction_preference_cleared',
                       'interaction_preference_history_reconciled',
                       'interaction_preference_derived_from_authorization',
                       'interaction_preference_derivation_reverted'
                     )
                   ORDER BY sequence""",
                (task["workspace_id"], authorization_sequence),
            ).fetchall()
            for later in later_preference_rows:
                payload = json.loads(later["payload_json"] or "{}")
                if later["event_type"] in {"interaction_preference_set", "interaction_preference_cleared"}:
                    if payload.get("user_scope") != "default":
                        continue
                raise StateConflict("较新的用户偏好决定已存在，禁止旧授权复活交互偏好")

            preference_id = _digest(
                {"workspace_id": task["workspace_id"], "user_scope": "default"}
            )[:24]
            connection.execute(
                """INSERT INTO preferences(
                       preference_id, workspace_id, user_scope,
                       intermediate_confirmation_policy, review_prompt_policy,
                       source_user_event_id, updated_at
                   ) VALUES (?, ?, 'default', 'never', 'report_once_no_prompt', ?, ?)
                   ON CONFLICT(workspace_id, user_scope) DO UPDATE SET
                       intermediate_confirmation_policy=excluded.intermediate_confirmation_policy,
                       review_prompt_policy=excluded.review_prompt_policy,
                       source_user_event_id=excluded.source_user_event_id,
                       updated_at=excluded.updated_at""",
                (preference_id, task["workspace_id"], source_event_id, stamp),
            )
            metadata = json.loads(task["metadata_json"] or "{}")
            metadata["interaction_preference_snapshot"] = {
                "intermediate_confirmation_policy": "never",
                "review_prompt_policy": "report_once_no_prompt",
            }
            connection.execute(
                "UPDATE tasks SET metadata_json=?, updated_at=? WHERE task_id=?",
                (_json(metadata), stamp, task_id),
            )
            self._append_event(
                connection,
                "interaction_preference_derived_from_authorization",
                {**proof, "proof_digest": proof_digest,
                 "actor_verified": bool(event["actor_verified"])},
                event_id=receipt_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=task["session_id"],
                task_id=task_id,
            )
            return {
                **_row(connection.execute(
                    "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = 'default'",
                    (task["workspace_id"],),
                ).fetchone()),
                "idempotent": False,
                "receipt_id": receipt_id,
            }

    def revert_unproven_interaction_preference_derivation(
        self,
        *,
        task_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Revert a derived preference whose authorization receipt lacks proof."""
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or not self._execution_authority_in_connection(connection, task):
                raise StateConflict("偏好纠错必须绑定真实授权执行包络")
            proposal = connection.execute(
                "SELECT * FROM maintenance_proposals WHERE proposal_id=?", (task["proposal_id"],)
            ).fetchone()
            if proposal is None or not proposal["authorized_by_event_id"]:
                raise StateConflict("偏好纠错缺少任务授权提案")
            source_event_id = str(proposal["authorized_by_event_id"])
            proof = {
                "task_id": task_id,
                "envelope_id": task["envelope_id"],
                "proposal_id": task["proposal_id"],
                "source_user_event_id": source_event_id,
                "intermediate_confirmation_policy": "never",
                "review_prompt_policy": "report_once_no_prompt",
            }
            derived_id = f"EV-PREFDER-{_digest(proof)[:24]}"
            derived = connection.execute(
                "SELECT * FROM events WHERE event_id=? AND event_type='interaction_preference_derived_from_authorization'",
                (derived_id,),
            ).fetchone()
            if derived is None:
                raise StateConflict("找不到待纠正的偏好派生收据")
            required = {"no_intermediate_confirmation", "report_once_no_prompt"}
            proposal_intents = {
                str(value) for value in json.loads(proposal["additional_intents_json"] or "[]")
            }
            receipt_proved = False
            for row in connection.execute(
                "SELECT payload_json FROM events WHERE event_type='maintenance_authorized' AND workspace_id=?",
                (task["workspace_id"],),
            ):
                payload = json.loads(row["payload_json"] or "{}")
                if (
                    payload.get("user_event_id") == source_event_id
                    and payload.get("proposal_id") == task["proposal_id"]
                    and required.issubset({str(value) for value in (payload.get("additional_intents") or [])})
                ):
                    receipt_proved = True
                    break
            if required.issubset(proposal_intents) and receipt_proved:
                raise StateConflict("偏好已有完整不可变授权证据，不得撤回")
            revert_digest = _digest({"derived_event_id": derived_id, "task_id": task_id})
            revert_id = f"EV-PREFREV-{revert_digest[:24]}"
            existing = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (revert_id,)
            ).fetchone()
            preference = connection.execute(
                "SELECT * FROM preferences WHERE workspace_id=? AND user_scope='default'",
                (task["workspace_id"],),
            ).fetchone()
            if existing is not None:
                if preference is not None and preference["source_user_event_id"] == source_event_id:
                    raise StateConflict("偏好撤回收据与当前偏好状态不一致")
                return {"idempotent": True, "receipt_id": revert_id}
            if preference is None or preference["source_user_event_id"] != source_event_id:
                raise StateConflict("当前偏好不是待纠正的派生状态")
            connection.execute(
                "DELETE FROM preferences WHERE workspace_id=? AND user_scope='default'",
                (task["workspace_id"],),
            )
            metadata = json.loads(task["metadata_json"] or "{}")
            metadata["interaction_preference_snapshot"] = {
                "intermediate_confirmation_policy": "default",
                "review_prompt_policy": "default",
            }
            connection.execute(
                "UPDATE tasks SET metadata_json=?,updated_at=? WHERE task_id=?",
                (_json(metadata), stamp, task_id),
            )
            self._append_event(
                connection,
                "interaction_preference_derivation_reverted",
                {
                    "derived_event_id": derived_id,
                    "source_user_event_id": source_event_id,
                    "reason": "authorization_receipt_did_not_freeze_preference_intents",
                },
                event_id=revert_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=task["session_id"],
                task_id=task_id,
            )
            return {"idempotent": False, "receipt_id": revert_id}

    def create_objective(
        self,
        *,
        workspace_id: str,
        original_text: str,
        conversation_id: str,
        objective_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> str:
        objective_id = objective_id or _new_id("OBJ")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO objectives(
                       objective_id, workspace_id, original_text, text_sha256,
                       created_from_conversation, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    objective_id,
                    workspace_id,
                    original_text,
                    hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
                    conversation_id,
                    _timestamp(created_at),
                ),
            )
        return objective_id

    def create_disclosure(
        self,
        *,
        objective_id: str,
        workspace_id: str,
        session_id: str,
        task_kind: str,
        payload: dict[str, Any],
        displayed_at: datetime | str,
        ttl_seconds: int = 1_800,
        disclosure_id: str | None = None,
        actor_verified: bool = False,
        disclosure_verified: bool = False,
        sequence_verified: bool = False,
    ) -> str:
        disclosure_id = disclosure_id or _new_id("D")
        task_kind = canonical_task_kind(task_kind)
        displayed = _as_utc(displayed_at)
        machine = disclosure_machine_payload(payload, objective_id=objective_id, task_kind=task_kind)
        digest = _digest(machine)
        stored_payload = dict(payload)
        stored_payload["machine"] = machine
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO disclosures(
                       disclosure_id, objective_id, workspace_id, session_id, task_kind,
                       disclosure_digest, payload_json, displayed_at, expires_at,
                       actor_verified, disclosure_verified, sequence_verified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    disclosure_id,
                    objective_id,
                    workspace_id,
                    session_id,
                    task_kind,
                    digest,
                    _json(stored_payload),
                    _timestamp(displayed),
                    _timestamp(displayed + timedelta(seconds=int(ttl_seconds))),
                    _bool(actor_verified),
                    _bool(disclosure_verified),
                    _bool(sequence_verified),
                ),
            )
        return disclosure_id

    def create_maintenance_proposal(
        self,
        *,
        disclosure_id: str,
        workspace_id: str,
        session_id: str,
        scope_digest: str,
        payload: dict[str, Any],
        created_at: datetime | str,
        platform: str = "unknown",
        ttl_seconds: int = 1_800,
        proposal_id: str | None = None,
        actor_verified: bool = False,
        disclosure_verified: bool = False,
        sequence_verified: bool = False,
        enforcement_verified: bool = False,
    ) -> str:
        proposal_id = proposal_id or _new_id("M")
        created = _as_utc(created_at)
        with self.transaction(immediate=True) as connection:
            disclosure = connection.execute(
                "SELECT * FROM disclosures WHERE disclosure_id = ?", (disclosure_id,)
            ).fetchone()
            if disclosure is None:
                raise StateNotFound(f"范围展示不存在：{disclosure_id}")
            if disclosure["workspace_id"] != workspace_id or disclosure["session_id"] != session_id:
                raise StateConflict("提案必须引用创建会话中的不可变范围展示")
            if created < _as_utc(disclosure["displayed_at"]):
                raise StateConflict("维护提案 created_at 不能早于范围展示 displayed_at")
            disclosure_payload = json.loads(disclosure["payload_json"])
            machine = disclosure_payload.get("machine") or disclosure_machine_payload(
                disclosure_payload, objective_id=disclosure["objective_id"], task_kind=disclosure["task_kind"]
            )
            proposed_machine = disclosure_machine_payload(
                payload, objective_id=disclosure["objective_id"], task_kind=disclosure["task_kind"]
            )
            if proposed_machine != machine:
                raise ScopeViolation("维护提案范围与不可变展示不一致")
            scope_digest = _digest({
                "disclosure_id": disclosure_id,
                "disclosure_digest": disclosure["disclosure_digest"],
                "machine": machine,
            })
            connection.execute(
                """INSERT INTO maintenance_proposals(
                       proposal_id, disclosure_id, workspace_id, session_id, platform, scope_digest,
                       payload_json, additional_intents_json, created_at, expires_at, actor_verified,
                       disclosure_verified, sequence_verified, enforcement_verified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id,
                    disclosure_id,
                    workspace_id,
                    session_id,
                    platform,
                    scope_digest,
                    _json(payload),
                    _timestamp(created),
                    _timestamp(created + timedelta(seconds=int(ttl_seconds))),
                    _bool(actor_verified),
                    _bool(disclosure_verified),
                    _bool(sequence_verified),
                    _bool(enforcement_verified),
                ),
            )
        return proposal_id

    def consume_user_event_and_maintenance_proposal(
        self,
        *,
        event_id: str,
        proposal_id: str,
        consumer_id: str,
        additional_intents: Sequence[str] = (),
        now: datetime | str | None = None,
    ) -> bool:
        """Compatibility wrapper: authorize, but do not consume, the proposal."""
        return self.authorize_maintenance_from_user_event(
            event_id=event_id,
            proposal_id=proposal_id,
            consumer_id=consumer_id,
            additional_intents=additional_intents,
            now=now,
        )

    def authorize_maintenance_from_user_event(
        self,
        *,
        event_id: str,
        proposal_id: str,
        consumer_id: str,
        additional_intents: Sequence[str] = (),
        user_scope: str = "default",
        now: datetime | str | None = None,
    ) -> bool:
        """Atomically consume a user event and authorize one pending proposal."""
        current = _as_utc(now)
        current_text = _timestamp(current)
        intents = sorted(set(additional_intents))
        unknown = set(intents) - MAINTENANCE_INTENTS
        if unknown:
            raise StateConflict(f"不支持的维护附加意图：{sorted(unknown)}")
        with self.transaction(immediate=True) as connection:
            event = connection.execute("SELECT * FROM user_events WHERE event_id = ?", (event_id,)).fetchone()
            if event is None:
                raise StateNotFound(f"用户事件不存在：{event_id}")
            proposal = connection.execute(
                "SELECT * FROM maintenance_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise StateNotFound(f"维护提案不存在：{proposal_id}")
            if event["consumed_at"] is not None or proposal["status"] != "pending":
                return False
            if _as_utc(event["expires_at"]) <= current:
                raise ExpiredUserEvent(f"用户事件已过期：{event_id}")
            if _as_utc(proposal["expires_at"]) <= current:
                raise StateConflict(f"维护提案已过期：{proposal_id}")
            if event["workspace_id"] != proposal["workspace_id"]:
                raise StateConflict("用户事件与维护提案不属于同一 workspace")
            disclosure = connection.execute(
                "SELECT * FROM disclosures WHERE disclosure_id=?", (proposal["disclosure_id"],)
            ).fetchone()
            if disclosure is None:
                raise StateConflict("维护提案引用的 disclosure 不存在")
            if (
                _as_utc(event["first_observed_at"]) < _as_utc(disclosure["displayed_at"])
                or _as_utc(event["first_observed_at"]) < _as_utc(proposal["created_at"])
            ):
                raise StateConflict("授权用户事件早于 disclosure/proposal，拒绝消费旧事件")
            bindings = json.loads(event["bindings_json"])
            if bindings.get("maintenance_proposal_id") != proposal_id:
                raise StateConflict("授权事件必须冻结绑定目标 proposal")
            if bindings.get("disclosure_id") != proposal["disclosure_id"]:
                raise StateConflict("授权事件必须冻结绑定目标 disclosure")
            cross_agent = event["session_id"] != proposal["session_id"] or event["platform"] != proposal["platform"]
            if cross_agent and not (
                bindings.get("maintenance_proposal_id") == proposal_id
                and bindings.get("explicit_proposal_reference") is True
            ):
                raise StateConflict("跨 Agent 授权必须显式绑定不可变维护提案")

            event_update = connection.execute(
                """UPDATE user_events
                   SET consumed_at = ?, consumed_by = ?
                   WHERE event_id = ? AND consumed_at IS NULL AND expires_at > ?""",
                (current_text, consumer_id, event_id, current_text),
            )
            if event_update.rowcount != 1:
                return False
            proposal_update = connection.execute(
                """UPDATE maintenance_proposals
                   SET status = 'authorized', authorized_by_event_id = ?, authorized_at = ?,
                       additional_intents_json = ?
                   WHERE proposal_id = ? AND status = 'pending' AND consumed_at IS NULL""",
                (event_id, current_text, _json(intents), proposal_id),
            )
            if proposal_update.rowcount != 1:
                raise StateConflict("维护提案并发消费冲突")

            payload = {
                "proposal_id": proposal_id,
                "user_event_id": event_id,
                "consumer_id": consumer_id,
                "actor_verified": bool(proposal["actor_verified"]),
                "disclosure_verified": bool(proposal["disclosure_verified"]),
                "sequence_verified": bool(proposal["sequence_verified"]),
                "enforcement_verified": bool(proposal["enforcement_verified"]),
                "additional_intents": intents,
            }
            self._append_event(
                connection,
                "maintenance_authorized",
                payload,
                workspace_id=proposal["workspace_id"],
                session_id=proposal["session_id"],
                occurred_at=current,
            )
            connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id,
                       payload_json, available_at
                   ) VALUES (?, ?, 'maintenance_proposal', ?, ?, ?)""",
                (
                    f"maintenance-authorized:{proposal_id}",
                    "maintenance_authorized",
                    proposal_id,
                    _json(payload),
                    current_text,
                ),
            )
            return True

    def create_task_from_authorized_proposal(
        self,
        *,
        proposal_id: str,
        task_id: str,
        envelope_digest: str,
        platform: str,
        envelope_id: str | None = None,
        owner_lease_expires_at: datetime | str | None = None,
        execution_session_id: str | None = None,
        expected_task_kind: str | None = None,
        expected_allowed_write_roots: Sequence[str] | None = None,
        replace_blocked_task_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, str]:
        """Consume an authorized proposal and atomically create its runtime objects."""
        current = _as_utc(created_at)
        stamp = _timestamp(current)
        envelope_id = envelope_id or _new_id("ENV")
        lease_id = _new_id("L")
        stage_run_id = _new_id("SR")
        lease_expires = _as_utc(owner_lease_expires_at) if owner_lease_expires_at else current + timedelta(hours=24)
        if lease_expires <= current:
            raise StateConflict("owner lease 必须在任务创建后过期")

        with self.transaction(immediate=True) as connection:
            proposal = connection.execute(
                """SELECT p.*, d.objective_id, d.task_kind, d.disclosure_digest,
                          d.payload_json AS disclosure_payload_json
                   FROM maintenance_proposals p
                   JOIN disclosures d ON d.disclosure_id = p.disclosure_id
                   WHERE p.proposal_id = ?""",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise StateNotFound(f"维护提案不存在：{proposal_id}")
            if proposal["status"] != "authorized" or proposal["consumed_at"] is not None:
                raise StateConflict("只有未消费的 authorized 提案可以创建任务")
            if not proposal["authorized_by_event_id"]:
                raise StateConflict("任务创建必须绑定已消费的用户授权事件")
            authorization_event = connection.execute(
                "SELECT session_id FROM user_events WHERE event_id=?",
                (proposal["authorized_by_event_id"],),
            ).fetchone()
            if authorization_event is None:
                raise StateConflict("任务授权事件不存在")
            execution_session = execution_session_id or proposal["session_id"]
            if execution_session not in {proposal["session_id"], authorization_event["session_id"]}:
                raise StateConflict("执行会话必须是提案 owner 或实际授权事件所在会话")
            payload = json.loads(proposal["payload_json"])
            disclosure_payload = json.loads(proposal["disclosure_payload_json"])
            machine = disclosure_payload.get("machine") or disclosure_machine_payload(
                disclosure_payload, objective_id=proposal["objective_id"], task_kind=proposal["task_kind"]
            )
            proposal_task_kind = canonical_task_kind(proposal["task_kind"])
            external_targets = list(machine.get("external_write_roots") or [])
            if external_targets and proposal_task_kind != "control_plane_maintenance":
                raise ScopeViolation("普通任务不得从授权提案继承 external_write_roots")
            roots = canonical_scopes(
                list(payload.get("allowed_write_roots") or payload.get("scopes") or [])
                + [str(item.get("path") or "") for item in external_targets]
            )
            if not roots:
                raise ScopeViolation("维护提案缺少 allowed_write_roots")
            if expected_task_kind is not None and canonical_task_kind(expected_task_kind) != proposal_task_kind:
                raise ScopeViolation("启动 task_kind 与用户授权提案不一致")
            if expected_allowed_write_roots is not None and canonical_scopes(expected_allowed_write_roots) != roots:
                raise ScopeViolation("启动写入范围与用户授权提案不一致")
            operations = canonical_operations(
                [str(value) for value in (machine.get("allowed_operations") or payload.get("allowed_operations") or [])],
                task_kind=proposal_task_kind,
            )
            grants = canonical_grants(
                roots,
                task_kind=proposal_task_kind,
                operations=operations,
                grants=machine.get("grants") if isinstance(machine.get("grants"), list) else None,
            )
            excluded = sorted(set(payload.get("excluded_actions") or payload.get("excludes") or []))
            envelope_digest = _digest({
                "task_id": task_id,
                "disclosure_digest": proposal["disclosure_digest"],
                "authorization_event_id": proposal["authorized_by_event_id"],
                "authorization_version": 1,
            })
            preference = connection.execute(
                """SELECT intermediate_confirmation_policy, review_prompt_policy
                   FROM preferences WHERE workspace_id = ? AND user_scope = 'default'""",
                (proposal["workspace_id"],),
            ).fetchone()
            preference_snapshot = dict(preference) if preference else {
                "intermediate_confirmation_policy": "default",
                "review_prompt_policy": "default",
            }

            if replace_blocked_task_id:
                predecessor = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (replace_blocked_task_id,)
                ).fetchone()
                if predecessor is None:
                    raise StateNotFound(f"被接管任务不存在：{replace_blocked_task_id}")
                if predecessor["session_id"] != proposal["session_id"]:
                    raise StateConflict("原任务与接管提案不属于同一 owner session")
                if predecessor["workspace_id"] != proposal["workspace_id"]:
                    raise StateConflict("原任务与接管提案不属于同一 workspace")
                if predecessor["lifecycle_status"] != "blocked" or predecessor["runtime_status"] not in {
                    "blocked_budget", "blocked_nonconvergent", "blocked_external_dependency",
                    "awaiting_material_user_choice", "suspended_lease_expired",
                }:
                    raise StateConflict("只有真实 blocked 主任务可以原子接管")
                competing = connection.execute(
                    """SELECT task_id FROM tasks WHERE session_id=? AND task_id<>?
                       AND lifecycle_status IN ('authorized','in_progress','blocked')
                       AND runtime_status NOT IN ('canceled','submitted','completed')""",
                    (proposal["session_id"], replace_blocked_task_id),
                ).fetchall()
                if competing:
                    raise StateConflict("接管前存在其他活动主任务")
                predecessor_metadata = json.loads(predecessor["metadata_json"] or "{}")
                predecessor_metadata["takeover"] = {
                    "replacement_task_id": task_id,
                    "replacement_proposal_id": proposal_id,
                    "replaced_at": stamp,
                    "reason": "authorized_atomic_takeover",
                }
                connection.execute(
                    """UPDATE tasks SET lifecycle_status='canceled', runtime_status='canceled',
                           metadata_json=?, updated_at=? WHERE task_id=?""",
                    (_json(predecessor_metadata), stamp, replace_blocked_task_id),
                )
                connection.execute(
                    "UPDATE leases SET status='revoked' WHERE task_id=? AND status='active'",
                    (replace_blocked_task_id,),
                )
                connection.execute(
                    "UPDATE stage_runs SET status='completed',finished_at=? WHERE task_id=? AND status='active'",
                    (stamp, replace_blocked_task_id),
                )

            connection.execute(
                """INSERT INTO tasks(
                       task_id, envelope_id, workspace_id, session_id, platform, task_kind,
                       objective_id, disclosure_id, proposal_id, envelope_digest,
                       lifecycle_status, runtime_status, review_status,
                       allowed_write_roots_json, allowed_operations_json, grants_json,
                       excluded_actions_json, metadata_json,
                       actor_verified, disclosure_verified, sequence_verified,
                       enforcement_verified, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', 'authorized',
                             'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    envelope_id,
                    proposal["workspace_id"],
                    proposal["session_id"],
                    platform,
                    proposal_task_kind,
                    proposal["objective_id"],
                    proposal["disclosure_id"],
                    proposal_id,
                    envelope_digest,
                    _json(roots),
                    _json(operations),
                    _json(grants),
                    _json(excluded),
                    _json({
                        key: value for key, value in {
                            "title": payload.get("title"),
                            "delivery_mode": canonical_delivery_mode(
                                machine.get("delivery_mode", "files")
                            ),
                            "maintenance": True,
                            "proposal_id": proposal_id,
                            "external_write_roots": [item["path"] for item in external_targets],
                            "external_write_targets": external_targets,
                            "irreversible_effects": list(machine.get("irreversible_effects") or []),
                            "external_effects": list(machine.get("external_effects") or []),
                            "acceptance_owner": machine.get("acceptance_owner") or "user",
                            "interaction_preference_snapshot": preference_snapshot,
                        }.items() if value is not None
                    }),
                    proposal["actor_verified"],
                    proposal["disclosure_verified"],
                    proposal["sequence_verified"],
                    proposal["enforcement_verified"],
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """INSERT INTO leases(
                       lease_id, task_id, source_session_id, worker_session_id, role,
                       allowed_write_roots_json, allowed_operations_json, grants_json,
                       read_only, issued_at, expires_at,
                       enforcement_verified
                   ) VALUES (?, ?, ?, ?, 'owner', ?, ?, ?, 0, ?, ?, ?)""",
                (
                    lease_id,
                    task_id,
                    proposal["session_id"],
                    proposal["session_id"],
                    _json(roots),
                    _json(operations),
                    _json(grants),
                    stamp,
                    _timestamp(lease_expires),
                    proposal["enforcement_verified"],
                ),
            )
            worker_lease_id = None
            if execution_session != proposal["session_id"]:
                worker_lease_id = _new_id("L")
                connection.execute(
                    """INSERT INTO leases(
                           lease_id, task_id, source_session_id, worker_session_id, role,
                           allowed_write_roots_json, allowed_operations_json, grants_json,
                           read_only, issued_at, expires_at,
                           enforcement_verified
                       ) VALUES (?, ?, ?, ?, 'authorization_worker', ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        worker_lease_id,
                        task_id,
                        proposal["session_id"],
                        execution_session,
                        _json(roots),
                        _json(operations),
                        _json(grants),
                        stamp,
                        _timestamp(lease_expires),
                        proposal["enforcement_verified"],
                    ),
                )
            connection.execute(
                """INSERT INTO stage_runs(
                       stage_run_id, task_id, stage, review_round, status, started_at
                   ) VALUES (?, ?, 'authorized', 0, 'active', ?)""",
                (stage_run_id, task_id, stamp),
            )
            updated = connection.execute(
                """UPDATE maintenance_proposals
                   SET status = 'consumed', consumed_at = ?
                   WHERE proposal_id = ? AND status = 'authorized' AND consumed_at IS NULL""",
                (stamp, proposal_id),
            )
            if updated.rowcount != 1:
                raise StateConflict("维护提案消费冲突")
            result = {
                "task_id": task_id,
                "envelope_id": envelope_id,
                "owner_lease_id": lease_id,
                "stage_run_id": stage_run_id,
            }
            if replace_blocked_task_id:
                result["replaced_blocked_task_id"] = replace_blocked_task_id
            if worker_lease_id:
                result["execution_worker_lease_id"] = worker_lease_id
            self._append_event(
                connection,
                "task_created_from_maintenance",
                result,
                workspace_id=proposal["workspace_id"],
                session_id=proposal["session_id"],
                task_id=task_id,
                occurred_at=current,
            )
            connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id,
                       payload_json, available_at
                   ) VALUES (?, 'task_created_from_maintenance', 'task', ?, ?, ?)""",
                (f"task-created:{task_id}", task_id, _json(result), stamp),
            )
            return result

    def create_task(
        self,
        *,
        task_id: str,
        envelope_id: str,
        workspace_id: str,
        session_id: str,
        platform: str,
        task_kind: str,
        envelope_digest: str,
        allowed_write_roots: Sequence[str],
        excluded_actions: Sequence[str] = (),
        lifecycle_status: str = "in_progress",
        runtime_status: str = "active",
        review_status: str = "draft",
        objective_id: str | None = None,
        disclosure_id: str | None = None,
        proposal_id: str | None = None,
        actor_verified: bool = False,
        disclosure_verified: bool = False,
        sequence_verified: bool = False,
        enforcement_verified: bool = False,
        created_at: datetime | str | None = None,
        legacy_import: bool = False,
    ) -> str:
        if not legacy_import:
            raise StateConflict("V3 任务只能由已授权提案创建；create_task 仅供未激活库的 legacy import")
        roots = canonical_scopes(allowed_write_roots)
        if not roots:
            raise ScopeViolation("任务至少需要一个写入范围")
        stamp = _timestamp(created_at)
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT value_json FROM runtime_meta WHERE key='backend_active'"
            ).fetchone()
            if active is not None and json.loads(active["value_json"]) is True:
                raise StateConflict("已激活 V3 后端禁止导入无用户授权链的任务")
            connection.execute(
                """INSERT INTO tasks(
                       task_id, envelope_id, workspace_id, session_id, platform, task_kind,
                       objective_id, disclosure_id, proposal_id, envelope_digest,
                       lifecycle_status, runtime_status, review_status, allowed_write_roots_json,
                       excluded_actions_json, metadata_json, actor_verified, disclosure_verified,
                       sequence_verified, enforcement_verified, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    envelope_id,
                    workspace_id,
                    session_id,
                    platform,
                    canonical_task_kind(task_kind),
                    objective_id,
                    disclosure_id,
                    proposal_id,
                    envelope_digest,
                    lifecycle_status,
                    runtime_status,
                    review_status,
                    _json(roots),
                    _json(sorted(set(excluded_actions))),
                    _json({"legacy_import": True}),
                    _bool(actor_verified),
                    _bool(disclosure_verified),
                    _bool(sequence_verified),
                    _bool(enforcement_verified),
                    stamp,
                    stamp,
                ),
            )
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect(readonly=True) as connection:
            raw = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _task_view(raw) if raw is not None else None

    def set_task_metadata(self, task_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        unknown = set(metadata) - TASK_METADATA_FIELDS
        if unknown:
            raise StateConflict(f"不支持的任务元数据字段：{sorted(unknown)}")
        normalized = dict(metadata)
        if "delivery_mode" in normalized:
            normalized["delivery_mode"] = canonical_delivery_mode(normalized["delivery_mode"])
        if "execution_budget" in normalized:
            normalized["execution_budget"] = canonical_execution_budget(normalized["execution_budget"])
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT metadata_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            merged = json.loads(row["metadata_json"])
            if not isinstance(merged, dict):
                raise StateConflict(f"任务元数据不是对象：{task_id}")
            if "delivery_mode" in normalized and "delivery_mode" in merged:
                current_mode = canonical_delivery_mode(merged["delivery_mode"])
                if normalized["delivery_mode"] != current_mode:
                    raise StateConflict(
                        "delivery_mode 已绑定执行包络；只能由正式原子修复事务改变"
                    )
            merged.update(normalized)
            if "execution_budget" in merged:
                merged["execution_budget"] = canonical_execution_budget(merged["execution_budget"])
            connection.execute(
                "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE task_id = ?",
                (_json(merged), _timestamp(), task_id),
            )
        return self.get_task(task_id) or {}

    def list_tasks(
        self,
        session_id: str | None = None,
        lifecycle_statuses: Sequence[str] | None = None,
        review_statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        for column, values in (("lifecycle_status", lifecycle_statuses), ("review_status", review_statuses)):
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(values)
        query = "SELECT task_id FROM tasks" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at"
        with self.connect() as connection:
            ids = [row["task_id"] for row in connection.execute(query, params)]
        return [self.get_task(task_id) or {} for task_id in ids]

    def list_pending_maintenance_proposals(
        self,
        session_id: str | None,
        platform: str | None,
        at: datetime | str | None = None,
        *,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stamp = _timestamp(at)
        clauses = ["status = 'pending'", "expires_at > ?"]
        params: list[Any] = [stamp]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(
                "SELECT * FROM maintenance_proposals WHERE " + " AND ".join(clauses) + " ORDER BY created_at",
                params,
            )]

    def find_active_tasks(self, session_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM tasks
                   WHERE session_id = ?
                     AND lifecycle_status IN ('authorized', 'in_progress', 'blocked')
                     AND runtime_status NOT IN ('canceled', 'submitted', 'completed')
                   ORDER BY created_at""",
                (session_id,),
            ).fetchall()
            return [_row(row) or {} for row in rows]

    def create_lease(
        self,
        *,
        task_id: str,
        source_session_id: str,
        worker_session_id: str,
        role: str,
        allowed_write_roots: Sequence[str],
        issued_at: datetime | str,
        expires_at: datetime | str,
        allowed_operations: Sequence[str] | None = None,
        read_only: bool = False,
        enforcement_verified: bool = False,
        lease_id: str | None = None,
    ) -> str:
        lease_id = lease_id or _new_id("L")
        requested = canonical_scopes(allowed_write_roots)
        task = self.get_task(task_id)
        if task is None:
            raise StateNotFound(f"任务不存在：{task_id}")
        if not self.task_execution_authorized(task_id):
            raise StateConflict("无有效执行包络的任务不能签发 worker lease")
        if source_session_id != task["session_id"]:
            raise StateConflict("只有执行包络 owner session 可以签发 worker lease")
        task_roots = task["allowed_write_roots"]
        if read_only and not requested:
            requested = []
        elif not requested:
            raise ScopeViolation("可写租约至少需要一个写入范围")
        for child in requested:
            if not any(scope_covers(parent, child) for parent in task_roots):
                raise ScopeViolation(f"租约范围超出任务范围：{child}")
        task_operations = task.get("allowed_operations") or canonical_operations([], task_kind=task["task_kind"])
        requested_operations = canonical_operations(
            list(allowed_operations or task_operations), task_kind=task["task_kind"],
        )
        if not set(requested_operations) <= set(task_operations):
            raise ScopeViolation("租约操作类型超出任务包络")
        task_grants = task.get("grants") or canonical_grants(
            task_roots, task_kind=task["task_kind"], operations=task_operations,
        )
        requested_grants: list[dict[str, Any]] = []
        for child in requested:
            permitted = {
                operation
                for grant in task_grants
                if scope_covers(str(grant.get("path") or ""), child)
                for operation in grant.get("operations") or []
            }
            expanded = set(requested_operations) - permitted
            if expanded:
                raise ScopeViolation(
                    f"租约逐路径操作超出任务包络：{child} + {sorted(expanded)}"
                )
            requested_grants.append({
                "path": child,
                "operations": sorted(set(requested_operations) & permitted),
            })
        issued = _as_utc(issued_at)
        expires = _as_utc(expires_at)
        if expires <= issued:
            raise StateConflict("租约过期时间必须晚于签发时间")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO leases(
                       lease_id, task_id, source_session_id, worker_session_id, role,
                       allowed_write_roots_json, allowed_operations_json, grants_json,
                       read_only, issued_at, expires_at,
                       enforcement_verified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lease_id,
                    task_id,
                    source_session_id,
                    worker_session_id,
                    role,
                    _json(requested),
                    _json(requested_operations),
                    _json(requested_grants),
                    _bool(read_only),
                    _timestamp(issued),
                    _timestamp(expires),
                    _bool(enforcement_verified),
                ),
            )
        return lease_id

    def find_valid_leases(
        self,
        worker_session_id: str,
        *,
        task_id: str | None = None,
        at: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        current = _timestamp(at)
        query = """SELECT l.* FROM leases l
                   JOIN tasks t ON t.task_id = l.task_id
                   WHERE l.worker_session_id = ? AND l.status = 'active'
                     AND l.issued_at <= ? AND l.expires_at > ?
                     AND t.lifecycle_status IN ('authorized', 'in_progress', 'blocked')
                     AND t.runtime_status NOT IN ('canceled', 'submitted', 'completed')"""
        params: list[Any] = [worker_session_id, current, current]
        if task_id is not None:
            query += " AND l.task_id = ?"
            params.append(task_id)
        query += " ORDER BY l.issued_at"
        with self.connect() as connection:
            return [_row(row) or {} for row in connection.execute(query, params).fetchall()]

    def resolve_task_access(
        self,
        *,
        session_id: str,
        task_id: str,
        at: datetime | str | None = None,
        capability: str = "read",
    ) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if capability in {"file_write", "runtime_mutate", "lease_admin", "delivery"}:
            if (
                task["lifecycle_status"] not in {"authorized", "in_progress", "blocked"}
                or task["runtime_status"] in {"canceled", "submitted", "completed"}
            ):
                return None
            if not self.task_execution_authorized(task_id):
                return None
        if task["session_id"] == session_id:
            return {
                "kind": "owner", "task": task,
                "allowed_write_roots": task["allowed_write_roots"],
                "allowed_operations": task.get("allowed_operations") or canonical_operations([], task_kind=task["task_kind"]),
                "grants": task.get("grants") or canonical_grants(task["allowed_write_roots"], task_kind=task["task_kind"]),
            }
        leases = self.find_valid_leases(session_id, task_id=task_id, at=at)
        if not leases:
            self.expire_worker_lease_and_suspend(session_id=session_id, task_id=task_id, at=at)
            return None
        lease = leases[0]
        if capability in {"file_write", "runtime_mutate", "lease_admin", "delivery"} and lease.get("read_only"):
            return None
        if capability == "lease_admin":
            return None
        return {
            "kind": "lease",
            "task": task,
            "lease": lease,
            "allowed_write_roots": lease["allowed_write_roots"],
            "allowed_operations": lease.get("allowed_operations") or task.get("allowed_operations") or canonical_operations([], task_kind=task["task_kind"]),
            "grants": lease.get("grants") or canonical_grants(
                lease["allowed_write_roots"], task_kind=task["task_kind"],
                operations=lease.get("allowed_operations") or task.get("allowed_operations") or [],
            ),
        }

    def expire_worker_lease_and_suspend(
        self, *, session_id: str, task_id: str, at: datetime | str | None = None
    ) -> bool:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            expired = connection.execute(
                """SELECT lease_id FROM leases
                   WHERE task_id = ? AND worker_session_id = ? AND status = 'active'
                     AND expires_at <= ?""",
                (task_id, session_id, stamp),
            ).fetchall()
            if not expired:
                return False
            connection.execute(
                """UPDATE leases SET status = 'expired'
                   WHERE task_id = ? AND worker_session_id = ? AND status = 'active'
                     AND expires_at <= ?""",
                (task_id, session_id, stamp),
            )
            active = connection.execute(
                """SELECT * FROM stage_runs WHERE task_id = ? AND status = 'active'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (target_task_id,),
            ).fetchone()
            if active is not None and active["stage"] not in {"submitted", "canceled", "suspended_lease_expired"}:
                details = {"resume_stage": active["stage"], "expired_worker_session_id": session_id}
                connection.execute(
                    "UPDATE stage_runs SET status = 'completed', finished_at = ? WHERE stage_run_id = ?",
                    (stamp, active["stage_run_id"]),
                )
                connection.execute(
                    """INSERT INTO stage_runs(
                           stage_run_id, task_id, stage, review_round, status, started_at, details_json
                       ) VALUES (?, ?, 'suspended_lease_expired', ?, 'active', ?, ?)""",
                    (_new_id("SR"), task_id, active["review_round"], stamp, _json(details)),
                )
                connection.execute(
                    "UPDATE tasks SET lifecycle_status='blocked', runtime_status='suspended_lease_expired', updated_at=? WHERE task_id=?",
                    (stamp, task_id),
                )
            return True

    @staticmethod
    def _existing_authorization_reconciliation_in_connection(
        connection: sqlite3.Connection, task: sqlite3.Row
    ) -> bool:
        metadata = json.loads(task["metadata_json"] or "{}")
        proof_id = str(metadata.get("execution_reconciliation_proof_id") or "")
        lease_id = str(metadata.get("reconciled_owner_lease_id") or "")
        if not (
            metadata.get("execution_authority_reconciled") is True
            and metadata.get("reconciled_from_existing_authorization") is True
            and proof_id and lease_id
        ):
            return False
        proof = connection.execute(
            """SELECT payload_json FROM events
               WHERE event_id=? AND event_type='legacy_execution_authority_reconciled'
                     AND task_id=?""",
            (proof_id, task["task_id"]),
        ).fetchone()
        lease = connection.execute(
            """SELECT * FROM leases
               WHERE lease_id=? AND task_id=? AND source_session_id=?
                     AND worker_session_id=? AND role='owner' AND read_only=0
                     AND status='active'""",
            (lease_id, task["task_id"], task["session_id"], task["session_id"]),
        ).fetchone()
        if proof is None or lease is None:
            return False
        payload = json.loads(proof["payload_json"] or "{}")
        roots = json.loads(task["allowed_write_roots_json"] or "[]")
        external_roots = canonical_scopes(metadata.get("external_write_roots") or [])
        excluded_actions = sorted(set(json.loads(task["excluded_actions_json"] or "[]")))
        core = {
            "task_id": task["task_id"],
            "proposal_id": task["proposal_id"],
            "disclosure_id": task["disclosure_id"],
            "objective_id": task["objective_id"],
            "envelope_id": task["envelope_id"],
            "envelope_digest": task["envelope_digest"],
            "source_user_event_id": payload.get("source_user_event_id"),
            "workspace_id": task["workspace_id"],
            "owner_session_id": task["session_id"],
            "owner_lease_id": lease_id,
            "allowed_write_roots": roots,
            "external_write_roots": external_roots,
            "excluded_actions": excluded_actions,
            "actor_verified": False,
            "enforcement_verified": False,
            "reconciled_from_existing_authorization": True,
        }
        return bool(
            payload == {**core, "proof_digest": _digest(core)}
            and json.loads(lease["allowed_write_roots_json"] or "[]") == roots
            and _as_utc(lease["expires_at"]) >= datetime.now(timezone.utc)
        )

    @staticmethod
    def _execution_authority_in_connection(
        connection: sqlite3.Connection, task: sqlite3.Row
    ) -> bool:
        metadata = json.loads(task["metadata_json"] or "{}")
        repair_record_id = str(metadata.get("repair_record_id") or "")
        if repair_record_id:
            repair = connection.execute(
                "SELECT * FROM envelope_repairs WHERE repair_id=? AND new_task_id=?",
                (repair_record_id, task["task_id"]),
            ).fetchone()
            if repair is None or repair["workspace_id"] != task["workspace_id"]:
                return False
            predecessor = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (repair["old_task_id"],)
            ).fetchone()
            maintainer = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (repair["maintenance_task_id"],)
            ).fetchone()
            if predecessor is None or maintainer is None:
                return False
            predecessor_chain = connection.execute(
                """SELECT p.status AS proposal_status, p.consumed_at,
                          p.authorized_by_event_id, p.payload_json,
                          d.payload_json AS disclosure_payload_json,
                          d.objective_id, d.workspace_id AS disclosure_workspace_id,
                          d.task_kind AS disclosure_task_kind, d.disclosure_digest,
                          u.workspace_id AS event_workspace_id, u.consumed_at AS event_consumed_at
                   FROM maintenance_proposals p
                   JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                   JOIN user_events u ON u.event_id=p.authorized_by_event_id
                   WHERE p.proposal_id=? AND p.disclosure_id=?""",
                (predecessor["proposal_id"], predecessor["disclosure_id"]),
            ).fetchone()
            if predecessor_chain is None:
                return False
            predecessor_payload = json.loads(predecessor_chain["payload_json"] or "{}")
            predecessor_disclosure = json.loads(
                predecessor_chain["disclosure_payload_json"] or "{}"
            )
            predecessor_machine = predecessor_disclosure.get("machine") or {}
            raw_predecessor_roots = list(
                predecessor_payload.get("allowed_write_roots")
                or predecessor_payload.get("scopes") or []
            ) + [
                str(item.get("path") or "")
                for item in (predecessor_machine.get("external_write_roots") or [])
                if isinstance(item, Mapping)
            ]
            stored_predecessor_roots = json.loads(
                predecessor["allowed_write_roots_json"] or "[]"
            )
            try:
                canonical_predecessor_roots = canonical_scopes(raw_predecessor_roots)
            except ScopeViolation:
                # A serialized single-scope envelope is intentionally invalid.  Keep
                # its exact original value available to the generic rescue branch;
                # only the dedicated schema-upgrade branch may require canonical
                # legacy roots.
                canonical_predecessor_roots = None
            predecessor_disclosure_digest_valid = bool(
                predecessor_machine
                and _digest(predecessor_machine) == predecessor_chain["disclosure_digest"]
                and str(predecessor_machine.get("objective_record_id") or "")
                    == predecessor["objective_id"]
                and canonical_task_kind(
                    str(predecessor_machine.get("task_kind") or "ordinary")
                ) == canonical_task_kind(predecessor["task_kind"])
                and canonical_task_kind(predecessor_chain["disclosure_task_kind"])
                    == canonical_task_kind(predecessor["task_kind"])
            )
            expected_predecessor_envelope_digest = _digest({
                "task_id": predecessor["task_id"],
                "disclosure_digest": predecessor_chain["disclosure_digest"],
                "authorization_event_id": predecessor_chain["authorized_by_event_id"],
                "authorization_version": 1,
            })
            predecessor_chain_valid = bool(
                predecessor_chain["proposal_status"] == "consumed"
                and predecessor_chain["consumed_at"]
                and predecessor_chain["event_consumed_at"]
                and predecessor_chain["objective_id"] == predecessor["objective_id"]
                and predecessor_chain["disclosure_workspace_id"] == predecessor["workspace_id"]
                and predecessor_chain["event_workspace_id"] == predecessor["workspace_id"]
                and (
                    raw_predecessor_roots == stored_predecessor_roots
                    or canonical_predecessor_roots == stored_predecessor_roots
                )
                and predecessor_disclosure_digest_valid
                and predecessor["envelope_digest"] == expected_predecessor_envelope_digest
            )
            corrected_roots = json.loads(repair["corrected_roots_json"] or "[]")
            corrected_grants = json.loads(repair["corrected_grants_json"] or "[]")
            if repair["reason"] == OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON:
                current_chain = connection.execute(
                    """SELECT p.*, d.objective_id AS disclosure_objective_id,
                              d.workspace_id AS disclosure_workspace_id,
                              d.task_kind AS disclosure_task_kind,
                              d.disclosure_digest,
                              d.payload_json AS disclosure_payload_json,
                              u.workspace_id AS event_workspace_id,
                              u.consumed_at AS event_consumed_at
                       FROM maintenance_proposals p
                       JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                       JOIN user_events u ON u.event_id=p.authorized_by_event_id
                       WHERE p.proposal_id=? AND p.disclosure_id=?""",
                    (task["proposal_id"], task["disclosure_id"]),
                ).fetchone()
                objective = connection.execute(
                    "SELECT original_text FROM objectives WHERE objective_id=?",
                    (predecessor["objective_id"],),
                ).fetchone()
                if current_chain is None or objective is None:
                    return False
                objective_text = str(objective["original_text"] or "")
                if not any(marker in objective_text for marker in ("全Agent", "全 Agent", "所有Agent", "所有 Agent")):
                    return False
                current_payload = json.loads(current_chain["payload_json"] or "{}")
                current_disclosure = json.loads(
                    current_chain["disclosure_payload_json"] or "{}"
                )
                machine = current_disclosure.get("machine") or {}
                proposal_external_roots = [
                    str(item.get("path") or "") if isinstance(item, Mapping) else str(item)
                    for item in (current_payload.get("external_write_roots") or [])
                ]
                machine_external_roots = [
                    str(item.get("path") or "") if isinstance(item, Mapping) else str(item)
                    for item in (machine.get("external_write_roots") or [])
                ]
                proposal_roots = canonical_scopes(
                    list(current_payload.get("allowed_write_roots") or [])
                    + proposal_external_roots
                )
                disclosure_roots = canonical_scopes(
                    list(machine.get("allowed_write_roots") or [])
                    + machine_external_roots
                )
                old_roots = json.loads(repair["old_roots_json"] or "[]")
                expected_roots = canonical_scopes(
                    old_roots + [OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH]
                )
                old_operations = canonical_operations(
                    json.loads(predecessor["allowed_operations_json"] or "[]"),
                    task_kind=predecessor["task_kind"],
                )
                old_grants = json.loads(predecessor["grants_json"] or "[]")
                expected_grants = canonical_grants(
                    expected_roots,
                    task_kind=predecessor["task_kind"],
                    operations=old_operations,
                    grants=old_grants + [{
                        "path": OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH,
                        "operations": ["control_plane_patch", "update"],
                    }],
                )
                current_operations = canonical_operations(
                    [str(value) for value in (machine.get("allowed_operations") or [])],
                    task_kind=task["task_kind"],
                )
                proposal_operations = canonical_operations(
                    [str(value) for value in (current_payload.get("allowed_operations") or [])],
                    task_kind=task["task_kind"],
                )
                current_grants = canonical_grants(
                    disclosure_roots,
                    task_kind=task["task_kind"],
                    operations=current_operations,
                    grants=machine.get("grants") if isinstance(machine.get("grants"), list) else None,
                )
                proposal_grants = current_payload.get("grants") or []
                current_excluded = sorted(set(machine.get("excluded_actions") or []))
                proposal_excluded = sorted(set(
                    current_payload.get("excluded_actions")
                    or current_payload.get("excludes") or []
                ))
                expected_current_envelope_digest = _digest({
                    "task_id": task["task_id"],
                    "disclosure_digest": current_chain["disclosure_digest"],
                    "authorization_event_id": current_chain["authorized_by_event_id"],
                    "authorization_version": 1,
                })
                return bool(
                    predecessor_chain_valid
                    and repair["maintenance_task_id"] == predecessor["task_id"]
                    and metadata.get("repaired_from_task_id") == predecessor["task_id"]
                    and predecessor["lifecycle_status"] == "invalid_envelope"
                    and predecessor["runtime_status"] == "invalid_envelope"
                    and canonical_task_kind(predecessor["task_kind"])
                        == canonical_task_kind(task["task_kind"])
                        == "control_plane_maintenance"
                    and OMITTED_AGENT_REGISTRY_DEPENDENCY_PATH not in old_roots
                    and corrected_roots == expected_roots == proposal_roots == disclosure_roots
                    and json.loads(task["allowed_write_roots_json"] or "[]") == expected_roots
                    and old_operations == current_operations == proposal_operations
                        == json.loads(task["allowed_operations_json"] or "[]")
                    and current_grants == proposal_grants == corrected_grants
                        == expected_grants == json.loads(task["grants_json"] or "[]")
                    and task["objective_id"] == predecessor["objective_id"]
                        == current_chain["disclosure_objective_id"]
                        == str(machine.get("objective_record_id") or "")
                    and canonical_task_kind(current_chain["disclosure_task_kind"])
                        == canonical_task_kind(str(machine.get("task_kind") or "ordinary"))
                        == "control_plane_maintenance"
                    and _digest(machine) == current_chain["disclosure_digest"]
                    and task["envelope_digest"] == expected_current_envelope_digest
                    and current_chain["status"] == "consumed"
                    and current_chain["consumed_at"]
                    and current_chain["event_consumed_at"]
                    and current_chain["workspace_id"] == task["workspace_id"]
                        == current_chain["disclosure_workspace_id"]
                        == current_chain["event_workspace_id"]
                    and repair["source_user_event_id"]
                        == predecessor_chain["authorized_by_event_id"]
                        == current_chain["authorized_by_event_id"]
                    and json.loads(task["excluded_actions_json"] or "[]")
                        == json.loads(predecessor["excluded_actions_json"] or "[]")
                        == current_excluded == proposal_excluded
                )
            if repair["reason"] == SCHEMA_V3_EMPTY_GRANTS_UPGRADE_REASON:
                current_chain = connection.execute(
                    """SELECT p.*, d.objective_id AS disclosure_objective_id,
                              d.workspace_id AS disclosure_workspace_id,
                              d.task_kind AS disclosure_task_kind,
                              d.disclosure_digest,
                              d.payload_json AS disclosure_payload_json,
                              u.workspace_id AS event_workspace_id,
                              u.consumed_at AS event_consumed_at
                       FROM maintenance_proposals p
                       JOIN disclosures d ON d.disclosure_id=p.disclosure_id
                       JOIN user_events u ON u.event_id=p.authorized_by_event_id
                       WHERE p.proposal_id=? AND p.disclosure_id=?""",
                    (task["proposal_id"], task["disclosure_id"]),
                ).fetchone()
                if current_chain is None:
                    return False
                current_payload = json.loads(current_chain["payload_json"] or "{}")
                current_disclosure = json.loads(
                    current_chain["disclosure_payload_json"] or "{}"
                )
                machine = current_disclosure.get("machine") or {}
                proposal_external_roots = [
                    str(item.get("path") or "") if isinstance(item, Mapping) else str(item)
                    for item in (current_payload.get("external_write_roots") or [])
                ]
                proposal_roots = canonical_scopes(
                    list(current_payload.get("allowed_write_roots") or [])
                    + proposal_external_roots
                )
                disclosure_roots = canonical_scopes(
                    list(machine.get("allowed_write_roots") or [])
                    + [
                        str(item.get("path") or "")
                        for item in (machine.get("external_write_roots") or [])
                        if isinstance(item, Mapping)
                    ]
                )
                explicit_current_authority = bool(
                    isinstance(machine.get("allowed_operations"), list)
                    and isinstance(machine.get("grants"), list)
                    and isinstance(current_payload.get("allowed_operations"), list)
                    and isinstance(current_payload.get("grants"), list)
                )
                current_operations = canonical_operations(
                    [str(value) for value in (machine.get("allowed_operations") or [])],
                    task_kind=task["task_kind"],
                )
                proposal_operations = canonical_operations(
                    [str(value) for value in (current_payload.get("allowed_operations") or [])],
                    task_kind=task["task_kind"],
                )
                current_grants = canonical_grants(
                    disclosure_roots, task_kind=task["task_kind"],
                    operations=current_operations,
                    grants=machine.get("grants") if isinstance(machine.get("grants"), list) else None,
                )
                proposal_grants = current_payload.get("grants") or []
                old_roots = json.loads(repair["old_roots_json"] or "[]")
                current_excluded = sorted(set(machine.get("excluded_actions") or []))
                proposal_excluded = sorted(set(
                    current_payload.get("excluded_actions")
                    or current_payload.get("excludes") or []
                ))
                expected_current_envelope_digest = _digest({
                    "task_id": task["task_id"],
                    "disclosure_digest": current_chain["disclosure_digest"],
                    "authorization_event_id": current_chain["authorized_by_event_id"],
                    "authorization_version": 1,
                })
                return bool(
                    predecessor_chain_valid
                    and repair["maintenance_task_id"] == predecessor["task_id"]
                    and metadata.get("repaired_from_task_id") == predecessor["task_id"]
                    and predecessor["lifecycle_status"] == "invalid_envelope"
                    and predecessor["runtime_status"] == "invalid_envelope"
                    and canonical_task_kind(predecessor["task_kind"])
                        == canonical_task_kind(task["task_kind"])
                        == "control_plane_maintenance"
                    and json.loads(predecessor["allowed_operations_json"] or "[]") == []
                    and json.loads(predecessor["grants_json"] or "[]") == []
                    and not predecessor_payload.get("allowed_operations")
                    and not predecessor_payload.get("grants")
                    and not predecessor_machine.get("allowed_operations")
                    and not predecessor_machine.get("grants")
                    and canonical_predecessor_roots is not None
                    and old_roots == corrected_roots == proposal_roots == disclosure_roots
                    and canonical_predecessor_roots == old_roots
                    and json.loads(task["allowed_write_roots_json"] or "[]") == corrected_roots
                    and explicit_current_authority
                    and current_operations == proposal_operations
                        == json.loads(task["allowed_operations_json"] or "[]")
                    and current_grants == proposal_grants == corrected_grants
                        == json.loads(task["grants_json"] or "[]")
                    and task["objective_id"] == predecessor["objective_id"]
                        == current_chain["disclosure_objective_id"]
                        == str(machine.get("objective_record_id") or "")
                    and canonical_task_kind(current_chain["disclosure_task_kind"])
                        == canonical_task_kind(str(machine.get("task_kind") or "ordinary"))
                        == canonical_task_kind(task["task_kind"])
                        == "control_plane_maintenance"
                    and _digest(machine) == current_chain["disclosure_digest"]
                    and task["envelope_digest"] == expected_current_envelope_digest
                    and current_chain["status"] == "consumed"
                    and current_chain["consumed_at"]
                    and current_chain["event_consumed_at"]
                    and current_chain["workspace_id"] == task["workspace_id"]
                        == current_chain["disclosure_workspace_id"]
                        == current_chain["event_workspace_id"]
                    and repair["source_user_event_id"]
                        == predecessor_chain["authorized_by_event_id"]
                        == current_chain["authorized_by_event_id"]
                    and json.loads(task["excluded_actions_json"] or "[]")
                        == json.loads(predecessor["excluded_actions_json"] or "[]")
                        == current_excluded == proposal_excluded
                )
            effective_maintainer = maintainer
            maintainer_is_active = bool(
                maintainer["lifecycle_status"] in {"authorized", "in_progress", "blocked"}
                and maintainer["runtime_status"]
                    not in {"invalid_envelope", "canceled", "submitted", "completed"}
            )
            if not maintainer_is_active:
                # A completed repair must not lose authority merely because the
                # independent maintainer was itself upgraded from the legacy empty
                # operation/grant schema.  Only that exact, scope-preserving upgrade
                # may replace the recorded maintainer; arbitrary repair successors
                # cannot become authority by indirection.
                successor_rows = connection.execute(
                    """SELECT t.* FROM envelope_repairs er
                         JOIN tasks t ON t.task_id=er.new_task_id
                         WHERE er.old_task_id=? AND er.reason=?
                           AND t.task_id<>?
                           AND t.lifecycle_status IN ('authorized','in_progress','blocked')
                           AND t.runtime_status NOT IN
                               ('invalid_envelope','canceled','submitted','completed')""",
                    (
                        maintainer["task_id"], SCHEMA_V3_EMPTY_GRANTS_UPGRADE_REASON,
                        task["task_id"],
                    ),
                ).fetchall()
                if len(successor_rows) != 1:
                    return False
                effective_maintainer = successor_rows[0]
            return bool(
                predecessor["lifecycle_status"] == "invalid_envelope"
                and maintainer["task_id"] != predecessor["task_id"]
                and effective_maintainer["task_id"]
                    not in {predecessor["task_id"], task["task_id"]}
                and effective_maintainer["lifecycle_status"]
                    in {"authorized", "in_progress", "blocked"}
                and effective_maintainer["runtime_status"]
                    not in {"invalid_envelope", "canceled", "submitted", "completed"}
                and canonical_task_kind(maintainer["task_kind"])
                    == canonical_task_kind(effective_maintainer["task_kind"])
                    == "control_plane_maintenance"
                and task["objective_id"] == predecessor["objective_id"]
                and json.loads(task["allowed_write_roots_json"] or "[]") == corrected_roots
                and json.loads(task["grants_json"] or "[]") == corrected_grants
                and predecessor_chain_valid
                and repair["source_user_event_id"]
                    == predecessor_chain["authorized_by_event_id"]
                and StateStore._execution_authority_in_connection(
                    connection, effective_maintainer
                )
            )
        if metadata.get("legacy_import") is True:
            if StateStore._existing_authorization_reconciliation_in_connection(connection, task):
                return True
            proof = connection.execute(
                """SELECT e.*, u.consumed_by, u.bindings_json
                   FROM events e
                   JOIN user_events u ON u.event_id=json_extract(e.payload_json, '$.user_event_id')
                   WHERE e.event_type='legacy_execution_authority_reconciled'
                     AND e.task_id=? ORDER BY e.sequence DESC LIMIT 1""",
                (task["task_id"],),
            ).fetchone()
            if proof is None or proof["workspace_id"] != task["workspace_id"]:
                return False
            payload = json.loads(proof["payload_json"] or "{}")
            bindings = json.loads(proof["bindings_json"] or "{}")
            core = {
                "task_id": task["task_id"], "envelope_id": task["envelope_id"],
                "envelope_digest": task["envelope_digest"],
                "user_event_id": payload.get("user_event_id"),
            }
            expected = _digest(core)
            return bool(
                canonical_task_kind(task["task_kind"]) == "control_plane_maintenance"
                and proof["consumed_by"] == f"legacy_authority_reconciliation:{task['task_id']}"
                and payload.get("proof_digest") == expected
                and bindings.get("task_id") == task["task_id"]
                and bindings.get("envelope_id") == task["envelope_id"]
                and (bindings.get("legacy_authority_reconciliation") or {}).get("proof_digest") == expected
            )
        if not all((task["objective_id"], task["disclosure_id"], task["proposal_id"])):
            return False
        chain = connection.execute(
            """SELECT p.*, d.objective_id AS disclosure_objective_id,
                      d.workspace_id AS disclosure_workspace_id,
                      d.task_kind AS disclosure_task_kind,
                      d.disclosure_digest, d.payload_json AS disclosure_payload_json,
                      u.workspace_id AS event_workspace_id,
                      u.consumed_at AS event_consumed_at
               FROM maintenance_proposals p
               JOIN disclosures d ON d.disclosure_id=p.disclosure_id
               JOIN user_events u ON u.event_id=p.authorized_by_event_id
               WHERE p.proposal_id=?""",
            (task["proposal_id"],),
        ).fetchone()
        if chain is None:
            return False
        if not (
            chain["status"] == "consumed" and chain["consumed_at"]
            and chain["authorized_by_event_id"] and chain["event_consumed_at"]
            and chain["workspace_id"] == task["workspace_id"] == chain["disclosure_workspace_id"]
            and chain["event_workspace_id"] == task["workspace_id"]
            and chain["disclosure_id"] == task["disclosure_id"]
            and chain["disclosure_objective_id"] == task["objective_id"]
            and canonical_task_kind(chain["disclosure_task_kind"])
                == canonical_task_kind(task["task_kind"])
        ):
            return False
        payload = json.loads(chain["payload_json"])
        disclosure_payload = json.loads(chain["disclosure_payload_json"])
        machine = disclosure_payload.get("machine") or {}
        external_targets = list(machine.get("external_write_roots") or [])
        roots = canonical_scopes(
            list(payload.get("allowed_write_roots") or payload.get("scopes") or [])
            + [str(item.get("path") or "") for item in external_targets]
        )
        if roots != json.loads(task["allowed_write_roots_json"]):
            return False
        if "allowed_operations" in machine or "grants" in machine:
            operations = canonical_operations(
                [str(value) for value in (machine.get("allowed_operations") or [])],
                task_kind=task["task_kind"],
            )
            grants = canonical_grants(
                roots, task_kind=task["task_kind"], operations=operations,
                grants=machine.get("grants") if isinstance(machine.get("grants"), list) else None,
            )
            proposal_operations = canonical_operations(
                [str(value) for value in (payload.get("allowed_operations") or [])],
                task_kind=task["task_kind"],
            )
            if not (
                operations == proposal_operations == json.loads(task["allowed_operations_json"] or "[]")
                and grants == json.loads(task["grants_json"] or "[]")
            ):
                return False
        expected_digest = _digest({
            "task_id": task["task_id"],
            "disclosure_digest": chain["disclosure_digest"],
            "authorization_event_id": chain["authorized_by_event_id"],
            "authorization_version": 1,
        })
        return task["envelope_digest"] == expected_digest

    def task_execution_authorized(self, task_id: str) -> bool:
        with self.connect() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return bool(task is not None and self._execution_authority_in_connection(connection, task))

    @staticmethod
    def _handoff_state_version_in_connection(
        connection: sqlite3.Connection, task: sqlite3.Row
    ) -> tuple[str, str | None]:
        delivery = connection.execute(
            "SELECT delivery_id FROM deliveries WHERE task_id=? ORDER BY submitted_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        delivery_id = delivery["delivery_id"] if delivery is not None else None
        latest_receipt = connection.execute(
            "SELECT receipt_id,sha256,created_at FROM write_receipts WHERE task_id=? AND status='effective' ORDER BY created_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        leases = connection.execute(
            """SELECT lease_id,source_session_id,worker_session_id,role,
                      allowed_write_roots_json,allowed_operations_json,grants_json,
                      read_only,status,expires_at,enforcement_verified
                 FROM leases WHERE task_id=? ORDER BY lease_id""",
            (task["task_id"],),
        ).fetchall()
        core = {
            "task_id": task["task_id"], "envelope_id": task["envelope_id"],
            "envelope_digest": task["envelope_digest"],
            "proposal_id": task["proposal_id"],
            "disclosure_id": task["disclosure_id"],
            "actor_verified": task["actor_verified"],
            "disclosure_verified": task["disclosure_verified"],
            "sequence_verified": task["sequence_verified"],
            "enforcement_verified": task["enforcement_verified"],
            "allowed_write_roots": json.loads(task["allowed_write_roots_json"] or "[]"),
            "allowed_operations": json.loads(task["allowed_operations_json"] or "[]"),
            "grants": json.loads(task["grants_json"] or "[]"),
            "task_updated_at": task["updated_at"], "runtime_status": task["runtime_status"],
            "delivery_id": delivery_id,
            "latest_receipt": dict(latest_receipt) if latest_receipt is not None else None,
            "leases": [dict(row) for row in leases],
        }
        return _digest(core), delivery_id

    def create_session_handoff(
        self, *, task_id: str, session_id: str, payload: Mapping[str, Any],
        expires_at: datetime | str, created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        required = {"task_summary", "completed", "current_state", "next_steps", "pitfalls"}
        missing = sorted(key for key in required if not payload.get(key))
        if missing:
            raise StateConflict(f"handoff 缺少字段：{missing}")
        created = _as_utc(created_at)
        expiry = _as_utc(expires_at)
        if expiry <= created:
            raise StateConflict("handoff expires_at 必须晚于 created_at")
        authority_fields = {
            "allowed_write_roots", "allowed_operations", "grants", "lease_id",
            "worker_lease", "authorization", "authority", "authority_inherited",
            "permissions", "permission", "scopes", "operations",
        }
        def contains_authority_field(value: Any) -> bool:
            if isinstance(value, Mapping):
                return any(
                    str(key).strip().lower() in authority_fields
                    or str(key).strip().lower().startswith(("allowed_", "authorized_", "lease_"))
                    or contains_authority_field(item)
                    for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_authority_field(item) for item in value)
            return False
        if contains_authority_field(payload):
            raise StateConflict("handoff payload 只能传递上下文，不能携带权限字段")
        handoff_id = _new_id("HO")
        with self.transaction(immediate=True) as connection:
            task, _, _ = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id,
                at=_timestamp(created), require_write=False,
            )
            state_version, delivery_id = self._handoff_state_version_in_connection(connection, task)
            frozen = {
                **dict(payload),
                "task_id": task_id,
                "envelope_id": task["envelope_id"],
                "runtime_status": task["runtime_status"],
                "authority_inherited": False,
                "authority_notice": "handoff is context only; task/lease envelope remains authoritative",
            }
            connection.execute(
                """UPDATE session_handoffs SET status='superseded',superseded_at=?
                   WHERE task_id=? AND session_id=? AND status='active'""",
                (_timestamp(created), task_id, session_id),
            )
            connection.execute(
                """INSERT INTO session_handoffs(
                       handoff_id,workspace_id,session_id,task_id,envelope_id,delivery_id,
                       state_version,payload_json,status,created_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?, 'active',?,?)""",
                (handoff_id, task["workspace_id"], session_id, task_id, task["envelope_id"],
                 delivery_id, state_version, _json(frozen), _timestamp(created), _timestamp(expiry)),
            )
        return self.get_session_handoff(handoff_id) or {}

    def get_session_handoff(
        self, handoff_id: str, *, at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        current = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            handoff = connection.execute(
                "SELECT * FROM session_handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            if handoff is None:
                return None
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (handoff["task_id"],)
            ).fetchone()
            stale_reason = None
            if handoff["status"] == "active" and handoff["expires_at"] <= current:
                stale_reason = "expired"
            elif handoff["status"] == "active" and task is None:
                stale_reason = "task_missing"
            elif handoff["status"] == "active" and task is not None:
                state_version, delivery_id = self._handoff_state_version_in_connection(connection, task)
                if state_version != handoff["state_version"] or delivery_id != handoff["delivery_id"]:
                    stale_reason = "authoritative_state_advanced"
            if stale_reason:
                connection.execute(
                    "UPDATE session_handoffs SET status='stale',superseded_at=? WHERE handoff_id=? AND status='active'",
                    (current, handoff_id),
                )
                handoff = connection.execute(
                    "SELECT * FROM session_handoffs WHERE handoff_id=?", (handoff_id,)
                ).fetchone()
            result = _row(handoff) or {}
            result["valid"] = result.get("status") == "active"
            if stale_reason:
                result["stale_reason"] = stale_reason
            return result

    def latest_session_handoff(
        self, *, workspace_id: str, task_id: str,
        source_session_id: str | None = None, at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        if not task_id or not task_id.strip():
            raise StateConflict("latest handoff 必须明确 task_id，禁止 workspace 全局猜测")
        clauses = ["workspace_id=?", "status='active'"]
        params: list[Any] = [workspace_id]
        clauses.append("task_id=?")
        params.append(task_id)
        if source_session_id:
            clauses.append("session_id=?")
            params.append(source_session_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT handoff_id FROM session_handoffs WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC", params,
            ).fetchall()
        for row in rows:
            handoff = self.get_session_handoff(row["handoff_id"], at=at)
            if handoff and handoff.get("valid"):
                return handoff
        return None

    def consume_session_handoff(
        self, *, handoff_id: str, consumer_session_id: str, consumer_agent_id: str,
        consumed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if not str(consumer_session_id).strip() or not str(consumer_agent_id).strip():
            raise StateConflict("handoff 消费必须提供当前 session 与 agent 标识")
        current = _timestamp(consumed_at)
        failure: str | None = None
        result: dict[str, Any] = {}
        consumption_id = ""
        access_kind = ""
        access_lease_id: str | None = None
        consumption_event_id = ""
        with self.transaction(immediate=True) as connection:
            handoff_row = connection.execute(
                "SELECT * FROM session_handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            if handoff_row is None or handoff_row["status"] != "active":
                raise StateConflict("handoff 不存在或不是 active")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (handoff_row["task_id"],)
            ).fetchone()
            if task is None:
                stale_reason = "task_missing"
            elif handoff_row["expires_at"] <= current:
                stale_reason = "expired"
            else:
                state_version, delivery_id = self._handoff_state_version_in_connection(connection, task)
                stale_reason = (
                    "authoritative_state_advanced"
                    if state_version != handoff_row["state_version"]
                    or delivery_id != handoff_row["delivery_id"] else None
                )
            if stale_reason:
                connection.execute(
                    "UPDATE session_handoffs SET status='stale',superseded_at=? WHERE handoff_id=?",
                    (current, handoff_id),
                )
                failure = stale_reason
            else:
                _, _, access_lease_id = self._task_access_in_connection(
                    connection,
                    task_id=handoff_row["task_id"],
                    session_id=consumer_session_id,
                    at=current,
                    require_write=False,
                )
                access_kind = "lease" if access_lease_id else "owner"
                existing = connection.execute(
                    """SELECT consumption_id,consumed_at FROM handoff_consumptions
                         WHERE handoff_id=? AND consumer_session_id=? AND consumer_agent_id=?""",
                    (handoff_id, consumer_session_id, consumer_agent_id),
                ).fetchone()
                consumption_id = existing["consumption_id"] if existing else _new_id("HC")
                if existing is None:
                    connection.execute(
                        """INSERT INTO handoff_consumptions(
                               consumption_id,handoff_id,consumer_session_id,consumer_agent_id,
                               consumed_at,verified_task_updated_at
                           ) VALUES (?,?,?,?,?,?)""",
                        (consumption_id, handoff_id, consumer_session_id, consumer_agent_id,
                         current, task["updated_at"]),
                    )
                    consumption_event_id = self._append_event(
                        connection,
                        "handoff_consumed",
                        {
                            "consumption_id": consumption_id,
                            "handoff_id": handoff_id,
                            "consumer_session_id": consumer_session_id,
                            "consumer_agent_id": consumer_agent_id,
                            "access_kind": access_kind,
                            "lease_id": access_lease_id,
                            "actor_verified": False,
                            "authority_inherited": False,
                        },
                        event_id=f"E-{consumption_id}",
                        occurred_at=current,
                        workspace_id=task["workspace_id"],
                        session_id=consumer_session_id,
                        task_id=task["task_id"],
                    )
                else:
                    consumption_event_id = f"E-{consumption_id}"
                result = _row(handoff_row) or {}
                result["valid"] = True
                result["actor_verified"] = False
        if failure:
            raise StateConflict(f"handoff 已失效：{failure}")
        return {"ok": True, "handoff": result, "consumption_id": consumption_id,
                "consumer_session_id": consumer_session_id,
                "consumer_agent_id": consumer_agent_id,
                "access_kind": access_kind, "lease_id": access_lease_id,
                "consumption_event_id": consumption_event_id,
                "actor_verified": False, "authority_inherited": False}

    @staticmethod
    def _task_access_in_connection(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        session_id: str,
        at: str,
        require_write: bool = False,
    ) -> tuple[sqlite3.Row, list[str], str | None]:
        task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            raise StateNotFound(f"任务不存在：{task_id}")
        if require_write:
            if (
                task["lifecycle_status"] not in {"authorized", "in_progress", "blocked"}
                or task["runtime_status"] in {"canceled", "submitted", "completed"}
            ):
                raise StateConflict("终态任务不能继续写入或补记写入证据")
            if not StateStore._execution_authority_in_connection(connection, task):
                raise StateConflict("任务缺少可验证的用户授权执行包络")
        task_roots = json.loads(task["allowed_write_roots_json"])
        if task["session_id"] == session_id:
            return task, task_roots, None
        lease = connection.execute(
            """SELECT * FROM leases
               WHERE task_id = ? AND worker_session_id = ? AND status = 'active'
                  AND issued_at <= ? AND expires_at > ?
                  AND (? = 0 OR read_only = 0)
               ORDER BY issued_at LIMIT 1""",
            (task_id, session_id, at, at, _bool(require_write)),
        ).fetchone()
        if lease is None:
            raise StateConflict("当前 session 既不是任务 owner，也没有有效 worker lease")
        return task, json.loads(lease["allowed_write_roots_json"]), lease["lease_id"]

    def record_write_receipt(
        self,
        *,
        receipt_id: str,
        task_id: str,
        session_id: str,
        path: str,
        operation: str,
        sha256: str | None,
        exists_after: bool,
        created_at: datetime | str | None = None,
        event_id: str | None = None,
        predecessor_receipt_id: str | None = None,
    ) -> str:
        stamp = _timestamp(created_at)
        normalized = canonical_scope(path)
        with self.transaction(immediate=True) as connection:
            task, access_roots, lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=True
            )
            if task["runtime_status"] not in {"implementing", "repairing", "revalidating"}:
                raise StateConflict(f"当前阶段禁止写入收据：{task['runtime_status']}")
            normalized_operation = canonical_operation(operation)
            task_operations = canonical_operations(
                json.loads(task["allowed_operations_json"] or "[]"),
                task_kind=task["task_kind"],
            )
            if normalized_operation not in task_operations:
                raise ScopeViolation(f"写入操作超出任务授权：{normalized_operation}")
            task_roots = json.loads(task["allowed_write_roots_json"])
            if not any(scope_covers(root, normalized) for root in task_roots):
                raise ScopeViolation(f"写入收据路径超出任务授权根：{normalized}")
            if not any(scope_covers(root, normalized) for root in access_roots):
                raise ScopeViolation(f"写入收据路径超出当前 session/lease 范围：{normalized}")
            if lease_id:
                lease_row = connection.execute(
                    "SELECT grants_json FROM leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                access_grants = json.loads(lease_row["grants_json"] or "[]") if lease_row else []
            else:
                access_grants = json.loads(task["grants_json"] or "[]")
            if not access_grants:
                access_grants = canonical_grants(
                    access_roots, task_kind=task["task_kind"], operations=task_operations,
                )
            if not any(
                scope_covers(str(grant.get("path") or ""), normalized)
                and normalized_operation in (grant.get("operations") or [])
                for grant in access_grants
            ):
                raise ScopeViolation(
                    f"写入收据缺少逐路径操作授权：{normalized} + {normalized_operation}"
                )
            if predecessor_receipt_id:
                predecessor = connection.execute(
                    "SELECT * FROM write_receipts WHERE receipt_id = ?", (predecessor_receipt_id,)
                ).fetchone()
                if predecessor is None:
                    raise StateNotFound(f"前序收据不存在：{predecessor_receipt_id}")
                if predecessor["task_id"] != task_id or predecessor["path"] != normalized:
                    raise StateConflict("前序收据必须属于同一任务和路径")
                if predecessor["status"] != "effective" or predecessor["superseded_by_receipt_id"]:
                    raise StateConflict("前序收据已经失效或已有唯一后继")
            connection.execute(
                """INSERT INTO write_receipts(
                       receipt_id, task_id, lease_id, event_id, session_id, path,
                       operation, sha256, exists_after, predecessor_receipt_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id,
                    task_id,
                    lease_id,
                    event_id,
                    session_id,
                    normalized,
                    normalized_operation,
                    sha256,
                    _bool(exists_after),
                    predecessor_receipt_id,
                    stamp,
                ),
            )
            if predecessor_receipt_id:
                updated = connection.execute(
                    """UPDATE write_receipts
                       SET status = 'superseded', superseded_by_receipt_id = ?
                       WHERE receipt_id = ? AND status = 'effective'
                         AND superseded_by_receipt_id IS NULL""",
                    (receipt_id, predecessor_receipt_id),
                )
                if updated.rowcount != 1:
                    raise StateConflict("前序收据并发替代冲突")
            return receipt_id

    @staticmethod
    def _takeover_reconciliation_item_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        required = {
            "schema_version", "artifact_type", "reconciliation_id", "task_id",
            "source_task_ids", "observed_at", "original_actor_verified",
            "original_write_times_known", "evidence_sources", "items",
        }
        if set(manifest) != required:
            raise StateConflict("接管对账清单字段不完整或包含未登记字段")
        schema_version = manifest.get("schema_version")
        if schema_version not in {1, 2} or manifest.get("artifact_type") != "takeover_reconciliation":
            raise StateConflict("接管对账清单契约无效")
        if manifest.get("original_actor_verified") is not False:
            raise StateConflict("接管对账不得伪造原执行者身份")
        if manifest.get("original_write_times_known") is not False:
            raise StateConflict("接管对账不得伪造历史写入时间")
        if not isinstance(manifest.get("source_task_ids"), list) or not manifest["source_task_ids"]:
            raise StateConflict("接管对账必须列出来源任务")
        if manifest.get("task_id") in manifest["source_task_ids"]:
            raise StateConflict("接管对账不能把当前任务伪装成历史来源任务")
        if not isinstance(manifest.get("evidence_sources"), list) or not manifest["evidence_sources"]:
            raise StateConflict("接管对账必须绑定可复核的现场证据")
        source_ids: set[str] = set()
        for source in manifest["evidence_sources"]:
            if not isinstance(source, dict) or set(source) != {"id", "path", "sha256"}:
                raise StateConflict("接管对账证据源字段无效")
            source_id = str(source.get("id") or "")
            source_path = Path(str(source.get("path") or "")).expanduser().resolve()
            expected_sha = str(source.get("sha256") or "")
            if not source_id or source_id in source_ids or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                raise StateConflict("接管对账证据源标识或哈希无效")
            if not source_path.is_file() or _file_sha256(source_path) != expected_sha:
                raise StateConflict(f"接管对账证据源缺失或漂移：{source_path}")
            source_ids.add(source_id)
        if not isinstance(manifest.get("items"), list) or not manifest["items"]:
            raise StateConflict("接管对账清单不能为空")
        item_map: dict[str, dict[str, Any]] = {}
        legacy_fields = {
            "path", "operation", "exists_after", "sha256", "source_task_id",
            "evidence_source_ids",
        }
        current_fields = {
            "path", "operation", "exists_after", "sha256",
            "authorization_basis_task_id", "historical_source_task_id",
            "historical_source_verified", "evidence_source_ids",
        }
        for raw in manifest["items"]:
            expected_fields = legacy_fields if schema_version == 1 else current_fields
            if not isinstance(raw, dict) or set(raw) != expected_fields:
                raise StateConflict("接管对账文件项字段无效")
            path = canonical_scope(str(raw.get("path") or ""))
            if Path(path).is_absolute():
                raise StateConflict("接管对账仅允许当前 workspace 内的相对路径")
            operation = canonical_operation(str(raw.get("operation") or ""))
            exists_after = raw.get("exists_after")
            digest = raw.get("sha256")
            if not isinstance(exists_after, bool):
                raise StateConflict("接管对账 exists_after 必须是布尔值")
            if exists_after:
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise StateConflict("接管对账现存文件必须包含 SHA-256")
            elif digest is not None:
                raise StateConflict("接管对账删除项不能声明最终文件哈希")
            if schema_version == 1:
                historical_source_task_id = str(raw.get("source_task_id") or "")
                historical_source_verified = False
                authorization_basis_task_id = str(manifest.get("task_id") or "")
            else:
                authorization_basis_task_id = str(raw.get("authorization_basis_task_id") or "")
                raw_historical_source = raw.get("historical_source_task_id")
                historical_source_task_id = (
                    str(raw_historical_source) if raw_historical_source is not None else None
                )
                historical_source_verified = raw.get("historical_source_verified")
                if authorization_basis_task_id != manifest.get("task_id"):
                    raise StateConflict("接管对账文件项的授权基础必须是当前接管任务")
                if not isinstance(historical_source_verified, bool):
                    raise StateConflict("接管对账历史来源验证标记无效")
                if historical_source_verified != bool(historical_source_task_id):
                    raise StateConflict("未验证历史来源必须留空，已验证来源必须明确")
            if (
                historical_source_task_id is not None
                and historical_source_task_id not in manifest["source_task_ids"]
            ):
                raise StateConflict("接管对账文件项引用了未登记的历史来源任务")
            evidence_ids = raw.get("evidence_source_ids")
            if (
                not isinstance(evidence_ids, list) or not evidence_ids
                or any(str(item) not in source_ids for item in evidence_ids)
            ):
                raise StateConflict("接管对账文件项缺少有效证据源绑定")
            if path in item_map:
                raise StateConflict(f"接管对账路径重复：{path}")
            item_map[path] = {
                "path": path,
                "operation": operation,
                "exists_after": exists_after,
                "sha256": digest,
                "authorization_basis_task_id": authorization_basis_task_id,
                "historical_source_task_id": historical_source_task_id,
                "historical_source_verified": historical_source_verified,
                "evidence_source_ids": [str(item) for item in evidence_ids],
            }
        return item_map

    @staticmethod
    def _normalize_recorded_takeover_items(items: Any, *, task_id: str) -> list[dict[str, Any]]:
        """Normalize immutable v1 audit payloads into the truthful v2 provenance model."""
        if not isinstance(items, list):
            raise StateConflict("接管对账记录文件项无效")
        result: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                raise StateConflict("接管对账记录文件项无效")
            item = dict(raw)
            if "source_task_id" in item:
                source_task_id = str(item.pop("source_task_id") or "")
                item["authorization_basis_task_id"] = task_id
                item["historical_source_task_id"] = source_task_id or None
                item["historical_source_verified"] = False
            expected_fields = {
                "path", "operation", "exists_after", "sha256",
                "authorization_basis_task_id", "historical_source_task_id",
                "historical_source_verified", "evidence_source_ids",
            }
            if set(item) != expected_fields or item.get("authorization_basis_task_id") != task_id:
                raise StateConflict("接管对账记录文件项字段无效")
            result.append(item)
        return sorted(result, key=lambda item: str(item["path"]))

    def _validate_takeover_reconciliation_manifest(
        self,
        *,
        manifest_path: str | Path,
        workspace_root: str | Path,
        expected_task_id: str,
        expected_reconciliation_id: str,
    ) -> tuple[Path, str, dict[str, Any], dict[str, dict[str, Any]]]:
        root, bound_workspace_id = _validate_projection_workspace_binding(self, workspace_root)
        resolved = Path(manifest_path).expanduser()
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise StateConflict("接管对账清单必须位于绑定的 workspace 内") from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise StateConflict("接管对账清单必须是现存普通文件")
        try:
            manifest = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateConflict("接管对账清单不可读或不是有效 JSON") from exc
        if not isinstance(manifest, dict):
            raise StateConflict("接管对账清单顶层必须是对象")
        if manifest.get("task_id") != expected_task_id:
            raise StateConflict("接管对账清单任务绑定不一致")
        if manifest.get("reconciliation_id") != expected_reconciliation_id:
            raise StateConflict("接管对账清单标识不一致")
        item_map = self._takeover_reconciliation_item_map(manifest)
        with self.connect(readonly=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (expected_task_id,)).fetchone()
            if task is None or task["workspace_id"] != bound_workspace_id:
                raise StateConflict("接管对账任务与 workspace cutover 绑定不一致")
        return resolved, _file_sha256(resolved), manifest, item_map

    def record_takeover_reconciliation(
        self,
        *,
        reconciliation_id: str,
        task_id: str,
        session_id: str,
        manifest_path: str | Path,
        workspace_root: str | Path,
        recorder_agent_id: str,
        observed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"TR-[A-Za-z0-9._-]{8,96}", reconciliation_id):
            raise StateConflict("接管对账标识无效")
        if not recorder_agent_id.strip():
            raise StateConflict("接管对账必须记录执行 Agent")
        resolved, manifest_sha, manifest, item_map = self._validate_takeover_reconciliation_manifest(
            manifest_path=manifest_path,
            workspace_root=workspace_root,
            expected_task_id=task_id,
            expected_reconciliation_id=reconciliation_id,
        )
        stamp = _timestamp(observed_at or str(manifest["observed_at"]))
        root = Path(workspace_root).expanduser().resolve()
        relative_manifest = resolved.relative_to(root).as_posix()
        with self.transaction(immediate=True) as connection:
            task, access_roots, lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=True,
            )
            if task["runtime_status"] not in {"implementing", "repairing", "revalidating"}:
                raise StateConflict("当前阶段禁止登记接管对账")
            source_rows = connection.execute(
                """SELECT task_id,workspace_id,created_at,lifecycle_status,runtime_status,
                          task_kind,allowed_write_roots_json,allowed_operations_json,grants_json
                   FROM tasks WHERE task_id IN (%s)""" % ",".join("?" * len(manifest["source_task_ids"])),
                tuple(manifest["source_task_ids"]),
            ).fetchall()
            if {row["task_id"] for row in source_rows} != set(manifest["source_task_ids"]):
                raise StateConflict("接管对账引用了不存在的来源任务")
            for source in source_rows:
                if source["workspace_id"] != task["workspace_id"] or source["task_id"] == task_id:
                    raise StateConflict("接管对账来源任务必须属于同一工作区且不能是当前任务")
                if source["created_at"] > task["created_at"]:
                    raise StateConflict("接管对账来源任务不能晚于当前任务")
                if (
                    source["lifecycle_status"] not in {
                        "submitted", "completed", "canceled", "archived",
                        "legacy_unreviewed", "invalid_envelope",
                    }
                    and source["runtime_status"] not in {
                        "submitted", "completed", "canceled", "archived", "invalid_envelope",
                    }
                ):
                    raise StateConflict("接管对账来源任务必须已经终止执行")
            task_operations = canonical_operations(
                json.loads(task["allowed_operations_json"] or "[]"), task_kind=task["task_kind"],
            )
            task_roots = json.loads(task["allowed_write_roots_json"] or "[]")
            if lease_id:
                lease_row = connection.execute(
                    "SELECT grants_json FROM leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                grants = json.loads(lease_row["grants_json"] or "[]") if lease_row else []
            else:
                grants = json.loads(task["grants_json"] or "[]")
            source_by_id = {str(source["task_id"]): source for source in source_rows}
            for path, item in item_map.items():
                operation = item["operation"]
                if operation not in task_operations:
                    raise ScopeViolation(f"接管对账操作超出任务授权：{path} + {operation}")
                if not any(scope_covers(root_path, path) for root_path in task_roots):
                    raise ScopeViolation(f"接管对账路径超出任务授权：{path}")
                if not any(scope_covers(root_path, path) for root_path in access_roots):
                    raise ScopeViolation(f"接管对账路径超出当前 session/lease：{path}")
                if not any(
                    scope_covers(str(grant.get("path") or ""), path)
                    and operation in (grant.get("operations") or []) for grant in grants
                ):
                    raise ScopeViolation(f"接管对账缺少逐路径操作授权：{path} + {operation}")
                if item["historical_source_verified"]:
                    source = source_by_id.get(str(item["historical_source_task_id"] or ""))
                    if source is None:
                        raise StateConflict(f"接管对账历史来源任务不存在：{path}")
                    source_operations = canonical_operations(
                        json.loads(source["allowed_operations_json"] or "[]"),
                        task_kind=source["task_kind"],
                    )
                    source_roots = json.loads(source["allowed_write_roots_json"] or "[]")
                    source_grants = json.loads(source["grants_json"] or "[]")
                    if (
                        operation not in source_operations
                        or not any(scope_covers(root_path, path) for root_path in source_roots)
                        or not any(
                            scope_covers(str(grant.get("path") or ""), path)
                            and operation in (grant.get("operations") or [])
                            for grant in source_grants
                        )
                    ):
                        raise ScopeViolation(
                            f"接管对账声称的历史来源没有路径与操作授权：{path} + {operation}"
                        )
                    source_receipt = connection.execute(
                        """SELECT receipt_id FROM write_receipts
                           WHERE task_id=? AND path=? AND operation=?
                             AND exists_after=? AND sha256 IS ?
                           ORDER BY created_at DESC,receipt_id DESC LIMIT 1""",
                        (
                            source["task_id"], path, operation,
                            _bool(bool(item["exists_after"])), item["sha256"],
                        ),
                    ).fetchone()
                    if source_receipt is None:
                        raise StateConflict(
                            f"接管对账历史来源只有授权、没有匹配写入证据：{path} + {operation}"
                        )
                absolute = root / path
                if item["exists_after"]:
                    if not absolute.is_file() or absolute.is_symlink() or _file_sha256(absolute) != item["sha256"]:
                        raise StateConflict(f"接管对账当前文件缺失或哈希漂移：{path}")
                elif absolute.exists() or absolute.is_symlink():
                    raise StateConflict(f"接管对账删除项当前仍存在：{path}")
            receipt = connection.execute(
                """SELECT * FROM write_receipts
                   WHERE task_id=? AND path=? AND status='effective'""",
                (task_id, relative_manifest),
            ).fetchone()
            if (
                receipt is None or not bool(receipt["exists_after"])
                or receipt["sha256"] != manifest_sha
            ):
                raise StateConflict("接管对账清单本身缺少当前任务的有效 file_write 收据")
            payload = {
                "reconciliation_id": reconciliation_id,
                "manifest_path": str(resolved),
                "manifest_sha256": manifest_sha,
                "source_task_ids": list(manifest["source_task_ids"]),
                "recorder_agent_id": recorder_agent_id.strip(),
                "actor_verified": False,
                "original_actor_verified": False,
                "original_write_times_known": False,
                "evidence_sources": list(manifest["evidence_sources"]),
                "items": [item_map[path] for path in sorted(item_map)],
            }
            event_id = f"EV-{reconciliation_id}-RECORDED"
            existing = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing is not None:
                if existing["event_type"] != "takeover_reconciliation_recorded" or json.loads(existing["payload_json"]) != payload:
                    raise StateConflict("接管对账标识已绑定不同内容")
                return {"event_id": event_id, **payload, "idempotent": True}
            self._append_event(
                connection,
                "takeover_reconciliation_recorded",
                payload,
                event_id=event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=session_id,
                task_id=task_id,
            )
            return {"event_id": event_id, **payload, "idempotent": False}

    def confirm_takeover_reconciliation(
        self,
        *,
        reconciliation_id: str,
        task_id: str,
        session_id: str,
        workspace_root: str | Path,
        reviewer_agent_id: str,
        reviewed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if not reviewer_agent_id.strip():
            raise StateConflict("接管对账复核必须记录审核 Agent")
        record_event_id = f"EV-{reconciliation_id}-RECORDED"
        with self.connect(readonly=True) as connection:
            record = connection.execute(
                "SELECT * FROM events WHERE event_id=? AND event_type='takeover_reconciliation_recorded'",
                (record_event_id,),
            ).fetchone()
        if record is None or record["task_id"] != task_id:
            raise StateNotFound("待复核的接管对账不存在")
        record_payload = json.loads(record["payload_json"])
        if reviewer_agent_id.strip() == record_payload.get("recorder_agent_id"):
            raise StateConflict("接管对账执行者不能审核自己的证据")
        resolved, manifest_sha, _manifest, item_map = self._validate_takeover_reconciliation_manifest(
            manifest_path=record_payload["manifest_path"],
            workspace_root=workspace_root,
            expected_task_id=task_id,
            expected_reconciliation_id=reconciliation_id,
        )
        if manifest_sha != record_payload.get("manifest_sha256"):
            raise StateConflict("接管对账清单在复核前发生漂移")
        recorded_items = self._normalize_recorded_takeover_items(
            record_payload.get("items"), task_id=task_id,
        )
        if [item_map[path] for path in sorted(item_map)] != recorded_items:
            raise StateConflict("接管对账文件项在复核前发生漂移")
        stamp = _timestamp(reviewed_at)
        root = Path(workspace_root).expanduser().resolve()
        for path, item in item_map.items():
            absolute = root / path
            if item["exists_after"]:
                if not absolute.is_file() or absolute.is_symlink() or _file_sha256(absolute) != item["sha256"]:
                    raise StateConflict(f"接管对账复核发现文件漂移：{path}")
            elif absolute.exists() or absolute.is_symlink():
                raise StateConflict(f"接管对账复核发现删除项复活：{path}")
        with self.transaction(immediate=True) as connection:
            task, _access_roots, _lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=False,
            )
            payload = {
                "reconciliation_id": reconciliation_id,
                "record_event_id": record_event_id,
                "manifest_path": str(resolved),
                "manifest_sha256": manifest_sha,
                "reviewer_agent_id": reviewer_agent_id.strip(),
                "actor_verified": False,
                "independent_from_recorder": True,
            }
            event_id = f"EV-{reconciliation_id}-CONFIRMED"
            existing = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing is not None:
                if existing["event_type"] != "takeover_reconciliation_confirmed" or json.loads(existing["payload_json"]) != payload:
                    raise StateConflict("接管对账复核标识已绑定不同内容")
                return {"event_id": event_id, **payload, "idempotent": True}
            self._append_event(
                connection,
                "takeover_reconciliation_confirmed",
                payload,
                event_id=event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=session_id,
                task_id=task_id,
            )
            return {"event_id": event_id, **payload, "idempotent": False}

    def supersede_takeover_reconciliation(
        self,
        *,
        task_id: str,
        session_id: str,
        old_reconciliation_id: str,
        new_reconciliation_id: str,
        old_manifest_snapshot_path: str | Path,
        workspace_root: str | Path,
        superseder_agent_id: str,
        superseded_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Explicitly replace one fully confirmed reconciliation with another.

        The old audit events stay immutable.  Only a confirmed, current, complete
        replacement with the same path/action/provenance set can become the new
        effective tail.  Hash changes additionally require a real current-task
        write receipt, so supersession cannot launder unrelated worktree drift.
        """
        if old_reconciliation_id == new_reconciliation_id:
            raise StateConflict("接管对账不能替代自身")
        if not superseder_agent_id.strip():
            raise StateConflict("接管对账替代必须记录执行 Agent")
        for value in (old_reconciliation_id, new_reconciliation_id):
            if not re.fullmatch(r"TR-[A-Za-z0-9._-]{8,96}", value):
                raise StateConflict("接管对账替代标识无效")
        stamp = _timestamp(superseded_at)
        old_record_id = f"EV-{old_reconciliation_id}-RECORDED"
        old_confirm_id = f"EV-{old_reconciliation_id}-CONFIRMED"
        new_record_id = f"EV-{new_reconciliation_id}-RECORDED"
        new_confirm_id = f"EV-{new_reconciliation_id}-CONFIRMED"
        with self.transaction(immediate=True) as connection:
            task, _access_roots, _lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=True,
            )
            if task["runtime_status"] not in {"implementing", "repairing", "revalidating"}:
                raise StateConflict("当前阶段禁止替代接管对账")
            rows = {
                row["event_id"]: row
                for row in connection.execute(
                    "SELECT * FROM events WHERE event_id IN (?,?,?,?)",
                    (old_record_id, old_confirm_id, new_record_id, new_confirm_id),
                ).fetchall()
            }
            if set(rows) != {old_record_id, old_confirm_id, new_record_id, new_confirm_id}:
                raise StateConflict("接管对账替代要求新旧两代都已记录并确认")
            if any(row["task_id"] != task_id for row in rows.values()):
                raise StateConflict("接管对账替代不能跨任务")
            old_record = json.loads(rows[old_record_id]["payload_json"])
            old_confirm = json.loads(rows[old_confirm_id]["payload_json"])
            new_record = json.loads(rows[new_record_id]["payload_json"])
            new_confirm = json.loads(rows[new_confirm_id]["payload_json"])
            expected_types = {
                old_record_id: "takeover_reconciliation_recorded",
                old_confirm_id: "takeover_reconciliation_confirmed",
                new_record_id: "takeover_reconciliation_recorded",
                new_confirm_id: "takeover_reconciliation_confirmed",
            }
            if any(rows[event_id]["event_type"] != event_type for event_id, event_type in expected_types.items()):
                raise StateConflict("接管对账替代引用了错误事件类型")
            for reconciliation_id, record_id, confirm, record in (
                (old_reconciliation_id, old_record_id, old_confirm, old_record),
                (new_reconciliation_id, new_record_id, new_confirm, new_record),
            ):
                if (
                    record.get("reconciliation_id") != reconciliation_id
                    or confirm.get("reconciliation_id") != reconciliation_id
                    or confirm.get("record_event_id") != record_id
                    or confirm.get("manifest_path") != record.get("manifest_path")
                    or confirm.get("manifest_sha256") != record.get("manifest_sha256")
                ):
                    raise StateConflict("接管对账替代的记录/确认绑定不一致")
            if rows[new_confirm_id]["occurred_at"] < rows[old_confirm_id]["occurred_at"]:
                raise StateConflict("接管对账替代顺序倒置")
            _resolved, new_manifest_sha, new_manifest, new_item_map = (
                self._validate_takeover_reconciliation_manifest(
                    manifest_path=new_record["manifest_path"],
                    workspace_root=workspace_root,
                    expected_task_id=task_id,
                    expected_reconciliation_id=new_reconciliation_id,
                )
            )
            if (
                new_manifest_sha != new_record.get("manifest_sha256")
                or [new_item_map[path] for path in sorted(new_item_map)]
                   != self._normalize_recorded_takeover_items(
                       new_record.get("items"), task_id=task_id,
                   )
            ):
                raise StateConflict("替代后的接管对账清单或文件项漂移")
            if old_record.get("source_task_ids") != new_record.get("source_task_ids"):
                raise StateConflict("接管对账替代改变了来源任务")
            old_snapshot = Path(old_manifest_snapshot_path).expanduser().resolve()
            if (
                not old_snapshot.is_file() or old_snapshot.is_symlink()
                or _file_sha256(old_snapshot) != old_record.get("manifest_sha256")
            ):
                raise StateConflict("旧接管对账缺少内容寻址的不可变清单快照")
            try:
                old_manifest = json.loads(old_snapshot.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise StateConflict("旧接管对账不可变清单快照不可读") from exc
            if (
                old_manifest.get("task_id") != task_id
                or old_manifest.get("reconciliation_id") != old_reconciliation_id
                or old_manifest.get("source_task_ids") != old_record.get("source_task_ids")
            ):
                raise StateConflict("旧接管对账不可变清单快照绑定不一致")
            old_item_map = self._takeover_reconciliation_item_map(old_manifest)
            if [old_item_map[path] for path in sorted(old_item_map)] != (
                self._normalize_recorded_takeover_items(
                    old_record.get("items"), task_id=task_id,
                )
            ):
                raise StateConflict("旧接管对账不可变清单快照与记录项不一致")
            if old_manifest.get("evidence_sources") != new_manifest.get("evidence_sources"):
                raise StateConflict("接管对账替代改变了证据源定义")
            if new_record.get("evidence_sources") not in (
                None, new_manifest.get("evidence_sources"),
            ):
                raise StateConflict("替代后的接管对账记录与证据源定义不一致")
            if set(old_item_map) != set(new_item_map) or "" in old_item_map:
                raise StateConflict("接管对账替代改变了完整路径集合")
            changed_paths: list[str] = []
            fixed_fields = (
                "path", "operation", "exists_after", "authorization_basis_task_id",
                "evidence_source_ids",
            )
            for path in sorted(new_item_map):
                old_item = old_item_map[path]
                new_item = new_item_map[path]
                if any(old_item.get(field) != new_item.get(field) for field in fixed_fields):
                    raise StateConflict(f"接管对账替代改变了路径动作或归属：{path}")
                old_source = old_item.get("historical_source_task_id")
                new_source = new_item.get("historical_source_task_id")
                old_verified = bool(old_item.get("historical_source_verified"))
                new_verified = bool(new_item.get("historical_source_verified"))
                if old_verified:
                    provenance_valid = (
                        (new_verified and new_source == old_source)
                        or (not new_verified and new_source is None)
                    )
                elif old_source:
                    provenance_valid = (
                        (new_verified and new_source == old_source)
                        or (not new_verified and new_source is None)
                        or (
                            old_manifest.get("schema_version") == 1
                            and new_manifest.get("schema_version") == 1
                            and not new_verified and new_source == old_source
                        )
                    )
                else:
                    provenance_valid = not new_verified and new_source is None
                if not provenance_valid:
                    raise StateConflict(f"接管对账替代扩大或改写了历史来源声明：{path}")
                if old_item.get("sha256") == new_item.get("sha256"):
                    continue
                changed_paths.append(path)
                receipt = connection.execute(
                    """SELECT * FROM write_receipts
                       WHERE task_id=? AND path=? AND status='effective'""",
                    (task_id, path),
                ).fetchone()
                if (
                    receipt is None
                    or bool(receipt["exists_after"]) != bool(new_item["exists_after"])
                    or receipt["sha256"] != new_item["sha256"]
                    or canonical_operation(receipt["operation"])
                       not in {new_item["operation"], "control_plane_patch"}
                ):
                    raise StateConflict(f"接管对账替代的哈希变化缺少当前写入收据：{path}")
            existing_links = connection.execute(
                """SELECT * FROM events
                   WHERE task_id=? AND event_type='takeover_reconciliation_superseded'""",
                (task_id,),
            ).fetchall()
            forward: dict[str, str] = {}
            reverse: dict[str, str] = {}
            for link in existing_links:
                link_payload = json.loads(link["payload_json"])
                old_id = str(link_payload.get("old_reconciliation_id") or "")
                new_id = str(link_payload.get("new_reconciliation_id") or "")
                if not old_id or not new_id:
                    raise StateConflict("已有接管对账替代事件损坏")
                if old_id in forward and forward[old_id] != new_id:
                    raise StateConflict("接管对账替代链发生分叉")
                if new_id in reverse and reverse[new_id] != old_id:
                    raise StateConflict("接管对账替代链发生汇合")
                forward[old_id] = new_id
                reverse[new_id] = old_id
            if old_reconciliation_id in forward and forward[old_reconciliation_id] != new_reconciliation_id:
                raise StateConflict("旧接管对账已经被另一代替代")
            if new_reconciliation_id in reverse and reverse[new_reconciliation_id] != old_reconciliation_id:
                raise StateConflict("新接管对账已经接替另一条链")
            candidate_forward = dict(forward)
            candidate_forward[old_reconciliation_id] = new_reconciliation_id
            cursor = old_reconciliation_id
            visited: set[str] = set()
            while cursor in candidate_forward:
                if cursor in visited:
                    raise StateConflict("接管对账替代链形成环")
                visited.add(cursor)
                cursor = candidate_forward[cursor]
            if cursor in visited:
                raise StateConflict("接管对账替代链形成环")
            payload = {
                "old_reconciliation_id": old_reconciliation_id,
                "new_reconciliation_id": new_reconciliation_id,
                "old_record_event_id": old_record_id,
                "old_confirmation_event_id": old_confirm_id,
                "old_manifest_sha256": old_record["manifest_sha256"],
                "old_manifest_snapshot_path": str(old_snapshot),
                "evidence_sources_sha256": _digest({
                    "evidence_sources": new_manifest["evidence_sources"],
                }),
                "new_record_event_id": new_record_id,
                "new_confirmation_event_id": new_confirm_id,
                "new_manifest_sha256": new_record["manifest_sha256"],
                "changed_paths": changed_paths,
                "superseder_agent_id": superseder_agent_id.strip(),
                "actor_verified": False,
            }
            event_id = "EV-TRS-" + _digest({
                "task_id": task_id,
                "old_reconciliation_id": old_reconciliation_id,
                "new_reconciliation_id": new_reconciliation_id,
            })[:24]
            existing = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing is not None:
                if (
                    existing["event_type"] != "takeover_reconciliation_superseded"
                    or json.loads(existing["payload_json"]) != payload
                ):
                    raise StateConflict("接管对账替代标识已绑定不同内容")
                return {"event_id": event_id, **payload, "idempotent": True}
            self._append_event(
                connection,
                "takeover_reconciliation_superseded",
                payload,
                event_id=event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=session_id,
                task_id=task_id,
            )
            return {"event_id": event_id, **payload, "idempotent": False}

    def list_effective_write_receipts(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            roots = json.loads(task["allowed_write_roots_json"])
            current_rows = connection.execute(
                """SELECT * FROM write_receipts
                   WHERE task_id = ? AND status = 'effective' ORDER BY created_at, receipt_id""",
                (task_id,),
            ).fetchall()
            inherited_rows: list[sqlite3.Row] = []
            inherited_from_task_id: str | None = None
            repair = connection.execute(
                """SELECT * FROM envelope_repairs
                   WHERE new_task_id=? AND reason=?""",
                (task_id, OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON),
            ).fetchone()
            if repair is not None:
                if not StateStore._execution_authority_in_connection(connection, task):
                    raise StateConflict("包络修复后继缺少可验证执行权威，不能继承写入收据")
                inherited_from_task_id = str(repair["old_task_id"])
                inherited_rows = connection.execute(
                    """SELECT * FROM write_receipts
                       WHERE task_id=? AND status='effective' AND created_at<=?
                       ORDER BY created_at,receipt_id""",
                    (inherited_from_task_id, repair["created_at"]),
                ).fetchall()
            reconciliation_rows: dict[str, dict[str, Any]] = {}
            records = connection.execute(
                """SELECT * FROM events
                   WHERE task_id=? AND event_type='takeover_reconciliation_recorded'
                   ORDER BY occurred_at,event_id""",
                (task_id,),
            ).fetchall()
            confirmations = {
                json.loads(row["payload_json"])["record_event_id"]: row
                for row in connection.execute(
                    """SELECT * FROM events
                       WHERE task_id=? AND event_type='takeover_reconciliation_confirmed'""",
                    (task_id,),
                ).fetchall()
            }
            record_by_id = {row["event_id"]: row for row in records}
            superseded_record_ids: set[str] = set()
            replacement_record_ids: set[str] = set()
            reconciliation_forward: dict[str, str] = {}
            reconciliation_reverse: dict[str, str] = {}
            for link in connection.execute(
                """SELECT * FROM events
                   WHERE task_id=? AND event_type='takeover_reconciliation_superseded'
                   ORDER BY occurred_at,event_id""",
                (task_id,),
            ).fetchall():
                link_payload = json.loads(link["payload_json"])
                old_record_id = str(link_payload.get("old_record_event_id") or "")
                new_record_id = str(link_payload.get("new_record_event_id") or "")
                old_record = record_by_id.get(old_record_id)
                new_record = record_by_id.get(new_record_id)
                old_confirmation = confirmations.get(old_record_id)
                new_confirmation = confirmations.get(new_record_id)
                if (
                    old_record is None or new_record is None
                    or old_confirmation is None or new_confirmation is None
                    or old_record_id in superseded_record_ids
                    or new_record_id in replacement_record_ids
                    or old_record_id == new_record_id
                    or link_payload.get("old_confirmation_event_id") != old_confirmation["event_id"]
                    or link_payload.get("new_confirmation_event_id") != new_confirmation["event_id"]
                    or link_payload.get("old_manifest_sha256")
                       != json.loads(old_record["payload_json"]).get("manifest_sha256")
                    or link_payload.get("new_manifest_sha256")
                       != json.loads(new_record["payload_json"]).get("manifest_sha256")
                ):
                    raise StateConflict("接管对账替代链损坏或发生分叉/汇合")
                old_payload = json.loads(old_record["payload_json"])
                new_payload = json.loads(new_record["payload_json"])
                old_id = str(link_payload.get("old_reconciliation_id") or "")
                new_id = str(link_payload.get("new_reconciliation_id") or "")
                old_snapshot = Path(
                    str(link_payload.get("old_manifest_snapshot_path") or "")
                ).expanduser().resolve()
                if (
                    old_id != old_payload.get("reconciliation_id")
                    or new_id != new_payload.get("reconciliation_id")
                    or not old_snapshot.is_file() or old_snapshot.is_symlink()
                    or _file_sha256(old_snapshot) != old_payload.get("manifest_sha256")
                    or old_id in reconciliation_forward
                    or new_id in reconciliation_reverse
                ):
                    raise StateConflict("接管对账替代链来源快照缺失或图结构损坏")
                try:
                    old_manifest = json.loads(old_snapshot.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise StateConflict("接管对账替代链旧清单不可读") from exc
                new_manifest_path = Path(str(new_payload.get("manifest_path") or "")).expanduser().resolve()
                try:
                    new_manifest = json.loads(new_manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise StateConflict("接管对账替代链新清单不可读") from exc
                if (
                    link_payload.get("evidence_sources_sha256") != _digest({
                        "evidence_sources": old_manifest.get("evidence_sources"),
                    })
                    or old_manifest.get("evidence_sources") != new_manifest.get("evidence_sources")
                ):
                    raise StateConflict("接管对账替代链证据源定义漂移")
                reconciliation_forward[old_id] = new_id
                reconciliation_reverse[new_id] = old_id
                superseded_record_ids.add(old_record_id)
                replacement_record_ids.add(new_record_id)
            for start in reconciliation_forward:
                cursor = start
                visited: set[str] = set()
                while cursor in reconciliation_forward:
                    if cursor in visited:
                        raise StateConflict("接管对账替代链形成环")
                    visited.add(cursor)
                    cursor = reconciliation_forward[cursor]
                if cursor in visited:
                    raise StateConflict("接管对账替代链形成环")
            for record in records:
                confirmation = confirmations.get(record["event_id"])
                if confirmation is None:
                    continue
                if record["event_id"] in superseded_record_ids:
                    continue
                payload = json.loads(record["payload_json"])
                confirmed = json.loads(confirmation["payload_json"])
                manifest_path = Path(str(payload.get("manifest_path") or "")).expanduser().resolve()
                manifest_sha = str(payload.get("manifest_sha256") or "")
                if (
                    confirmed.get("reconciliation_id") != payload.get("reconciliation_id")
                    or confirmed.get("manifest_path") != str(manifest_path)
                    or confirmed.get("manifest_sha256") != manifest_sha
                    or not manifest_path.is_file()
                    or _file_sha256(manifest_path) != manifest_sha
                ):
                    raise StateConflict("已确认的接管对账清单缺失或漂移")
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise StateConflict("已确认的接管对账清单不可读") from exc
                item_map = self._takeover_reconciliation_item_map(manifest)
                recorded_items = self._normalize_recorded_takeover_items(
                    payload.get("items"), task_id=task_id,
                )
                if [item_map[path] for path in sorted(item_map)] != recorded_items:
                    raise StateConflict("已确认的接管对账文件项发生漂移")
                for path, item in item_map.items():
                    if path in reconciliation_rows:
                        raise StateConflict(f"多个接管对账同时声明同一路径：{path}")
                    reconciliation_rows[path] = {
                        "receipt_id": "RC-" + _digest({
                            "reconciliation_id": payload["reconciliation_id"], "path": path,
                        })[:24],
                        "task_id": task_id,
                        "lease_id": None,
                        "event_id": confirmation["event_id"],
                        "session_id": confirmation["session_id"],
                        "path": path,
                        "operation": item["operation"],
                        "sha256": item["sha256"],
                        "exists_after": _bool(bool(item["exists_after"])),
                        "predecessor_receipt_id": None,
                        "status": "effective",
                        "superseded_by_receipt_id": None,
                        "created_at": confirmation["occurred_at"],
                        "evidence_kind": "takeover_reconciliation",
                        "reconciliation_id": payload["reconciliation_id"],
                        "reconciliation_manifest": str(manifest_path),
                        "reconciliation_manifest_sha256": manifest_sha,
                        "reconciliation_record_event_id": record["event_id"],
                        "reconciliation_confirmation_event_id": confirmation["event_id"],
                        "reviewer_agent_id": confirmed["reviewer_agent_id"],
                        "authorization_basis_task_id": item["authorization_basis_task_id"],
                        "historical_source_task_id": item["historical_source_task_id"],
                        "historical_source_verified": item["historical_source_verified"],
                    }
            by_path: dict[str, tuple[Mapping[str, Any], str | None]] = {
                path: (row, None) for path, row in reconciliation_rows.items()
            }
            by_path.update({
                row["path"]: (row, inherited_from_task_id) for row in inherited_rows
            })
            # A real write under the repaired successor is the current authority
            # for that path and therefore supersedes the inherited observation.
            by_path.update({row["path"]: (row, None) for row in current_rows})
            task_operations = canonical_operations(
                json.loads(task["allowed_operations_json"] or "[]"),
                task_kind=task["task_kind"],
            )
            grants = json.loads(task["grants_json"] or "[]")
            result = []
            for path in sorted(by_path):
                receipt, source_task_id = by_path[path]
                if not any(scope_covers(root, receipt["path"]) for root in roots):
                    raise ScopeViolation(f"有效收据已漂移出任务范围：{receipt['path']}")
                if source_task_id:
                    operation = canonical_operation(receipt["operation"])
                    if operation not in task_operations or not any(
                        scope_covers(str(grant.get("path") or ""), receipt["path"])
                        and operation in (grant.get("operations") or [])
                        for grant in grants
                    ):
                        raise ScopeViolation(
                            f"继承收据操作已漂移出后继授权：{receipt['path']} + {operation}"
                        )
                item = dict(receipt) if isinstance(receipt, dict) else (_row(receipt) or {})
                if source_task_id:
                    item["inherited_from_task_id"] = source_task_id
                    item["inheritance_repair_id"] = repair["repair_id"]
                result.append(item)
            return result

    def supersede_write_receipt(
        self,
        *,
        predecessor_receipt_id: str,
        successor_receipt_id: str,
    ) -> bool:
        with self.transaction(immediate=True) as connection:
            predecessor = connection.execute(
                "SELECT * FROM write_receipts WHERE receipt_id = ?", (predecessor_receipt_id,)
            ).fetchone()
            successor = connection.execute(
                "SELECT * FROM write_receipts WHERE receipt_id = ?", (successor_receipt_id,)
            ).fetchone()
            if predecessor is None or successor is None:
                raise StateNotFound("前序或后继收据不存在")
            if predecessor["status"] != "effective":
                return False
            if successor["status"] != "effective" or successor["predecessor_receipt_id"]:
                raise StateConflict("后继收据已绑定其他前序或已经失效")
            if predecessor["task_id"] != successor["task_id"] or predecessor["path"] != successor["path"]:
                raise StateConflict("收据替代链必须属于同一任务和路径")
            connection.execute(
                "UPDATE write_receipts SET predecessor_receipt_id = ? WHERE receipt_id = ?",
                (predecessor_receipt_id, successor_receipt_id),
            )
            connection.execute(
                """UPDATE write_receipts SET status = 'superseded', superseded_by_receipt_id = ?
                   WHERE receipt_id = ?""",
                (successor_receipt_id, predecessor_receipt_id),
            )
            return True

    def begin_controlled_delivery_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        session_id: str,
        delivery_id: str,
        request_digest: str,
        repository_head: str,
        started_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"CDA-[A-Za-z0-9._-]{8,96}", attempt_id):
            raise StateConflict("受控交付尝试标识无效")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", delivery_id):
            raise StateConflict("受控交付标识无效")
        if not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise StateConflict("受控交付请求摘要无效")
        if not re.fullmatch(r"[0-9a-f]{40,64}", repository_head):
            raise StateConflict("受控交付基线提交无效")
        stamp = _timestamp(started_at)
        event_id = f"EV-{attempt_id}-STARTED"
        with self.transaction(immediate=True) as connection:
            task, _access_roots, _lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=True,
            )
            stage = connection.execute(
                """SELECT * FROM stage_runs WHERE task_id=? AND status='active'
                   ORDER BY started_at DESC,rowid DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if stage is None or stage["stage"] != "committing":
                raise StateConflict("只能在 committing 阶段开始受控交付尝试")
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone() is not None:
                raise StateConflict("交付记录已存在，不得重建提交前尝试")
            payload = {
                "attempt_id": attempt_id,
                "delivery_id": delivery_id,
                "stage_run_id": stage["stage_run_id"],
                "request_digest": request_digest,
                "repository_head": repository_head,
                "actor_verified": False,
            }
            existing = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["event_type"] != "controlled_delivery_attempt_started"
                    or existing["task_id"] != task_id
                    or existing["session_id"] != session_id
                    or json.loads(existing["payload_json"]) != payload
                ):
                    raise StateConflict("受控交付尝试标识已绑定其他请求")
                return {"event_id": event_id, **payload, "idempotent": True}
            self._append_event(
                connection,
                "controlled_delivery_attempt_started",
                payload,
                event_id=event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=session_id,
                task_id=task_id,
            )
            return {"event_id": event_id, **payload, "idempotent": False}

    def fail_controlled_delivery_attempt(
        self,
        *,
        attempt_event_id: str,
        task_id: str,
        session_id: str,
        repository_head_after: str,
        partial_commits: Sequence[Mapping[str, Any]],
        error_class: str,
        error_message: str,
        failed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{40,64}", repository_head_after):
            raise StateConflict("受控交付失败后的仓库提交无效")
        if isinstance(partial_commits, (str, bytes)) or not isinstance(partial_commits, Sequence):
            raise StateConflict("受控交付部分提交证据必须是逐项数组")
        normalized_partial_commits: list[dict[str, Any]] = []
        seen_commits: set[str] = set()
        seen_paths: set[str] = set()
        for raw in partial_commits:
            if not isinstance(raw, Mapping) or set(raw) != {"commit", "subject", "paths"}:
                raise StateConflict("受控交付部分提交证据字段无效")
            commit = str(raw.get("commit") or "")
            subject = str(raw.get("subject") or "")
            raw_paths = raw.get("paths")
            if (
                not re.fullmatch(r"[0-9a-f]{40,64}", commit)
                or commit in seen_commits
                or not subject
                or len(subject) > 500
                or any(token in subject for token in ("\n", "\r"))
                or isinstance(raw_paths, (str, bytes))
                or not isinstance(raw_paths, Sequence)
                or not raw_paths
            ):
                raise StateConflict("受控交付部分提交证据内容无效")
            paths = canonical_scopes([str(path) for path in raw_paths])
            if (
                len(paths) != len(raw_paths)
                or any(path == "." or path.startswith("/") for path in paths)
                or seen_paths.intersection(paths)
            ):
                raise StateConflict("受控交付部分提交路径必须相对、唯一且互不重叠")
            seen_commits.add(commit)
            seen_paths.update(paths)
            normalized_partial_commits.append({
                "commit": commit,
                "subject": subject,
                "paths": paths,
            })
        stamp = _timestamp(failed_at)
        with self.transaction(immediate=True) as connection:
            started = connection.execute(
                """SELECT * FROM events
                   WHERE event_id=? AND event_type='controlled_delivery_attempt_started'""",
                (attempt_event_id,),
            ).fetchone()
            if (
                started is None
                or started["task_id"] != task_id
                or started["session_id"] != session_id
            ):
                raise StateConflict("受控交付失败缺少同一任务与会话的不可变尝试记录")
            start_payload = json.loads(started["payload_json"])
            repository_head_before = str(start_payload.get("repository_head") or "")
            if repository_head_after == repository_head_before:
                if normalized_partial_commits:
                    raise StateConflict("仓库提交未变化时不得声明部分提交")
            elif (
                not normalized_partial_commits
                or normalized_partial_commits[-1]["commit"] != repository_head_after
            ):
                raise StateConflict("仓库提交已变化但缺少连续到当前 HEAD 的部分提交证据")
            failure_event_id = attempt_event_id.removesuffix("-STARTED") + "-FAILED"
            payload = {
                "attempt_event_id": attempt_event_id,
                "attempt_id": start_payload["attempt_id"],
                "delivery_id": start_payload["delivery_id"],
                "stage_run_id": start_payload["stage_run_id"],
                "request_digest": start_payload["request_digest"],
                "repository_head": repository_head_before,
                "repository_head_after": repository_head_after,
                "partial_commits": normalized_partial_commits,
                "error_class": str(error_class)[:160],
                "error_message": str(error_message)[:1000],
                "delivery_created": False,
                "actor_verified": False,
            }
            existing = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (failure_event_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"]) != payload:
                    raise StateConflict("受控交付失败事件已绑定其他仓库结果")
                return {"event_id": failure_event_id, **payload, "idempotent": True}
            task, _access_roots, _lease_id = self._task_access_in_connection(
                connection, task_id=task_id, session_id=session_id, at=stamp, require_write=True,
            )
            stage = connection.execute(
                """SELECT * FROM stage_runs WHERE task_id=? AND status='active'
                   ORDER BY started_at DESC,rowid DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if (
                stage is None
                or stage["stage"] != "committing"
                or stage["stage_run_id"] != start_payload.get("stage_run_id")
            ):
                raise StateConflict("受控交付失败与当前 committing 阶段不一致")
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id=?",
                (start_payload["delivery_id"],),
            ).fetchone() is not None:
                raise StateConflict("交付记录已建立，禁止冒充提交前失败退回")
            self._append_event(
                connection,
                "controlled_delivery_attempt_failed",
                payload,
                event_id=failure_event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=session_id,
                task_id=task_id,
            )
            connection.execute(
                "UPDATE stage_runs SET status='completed',finished_at=? WHERE stage_run_id=?",
                (stamp, stage["stage_run_id"]),
            )
            repair_stage_run_id = _new_id("SR")
            connection.execute(
                """INSERT INTO stage_runs(
                       stage_run_id,task_id,stage,review_round,status,started_at,details_json
                   ) VALUES (?,?, 'repairing',?, 'active',?,?)""",
                (
                    repair_stage_run_id,
                    task_id,
                    int(stage["review_round"]),
                    stamp,
                    _json({
                        "controlled_delivery_attempt_failed_event_id": failure_event_id,
                        "delivery_id": start_payload["delivery_id"],
                    }),
                ),
            )
            connection.execute(
                """UPDATE tasks SET lifecycle_status='in_progress',runtime_status='repairing',
                       updated_at=? WHERE task_id=?""",
                (stamp, task_id),
            )
            return {
                "event_id": failure_event_id,
                "repair_stage_run_id": repair_stage_run_id,
                **payload,
                "idempotent": False,
            }

    def transition_stage(
        self,
        *,
        task_id: str,
        to_stage: str,
        review_round: int | None = None,
        details: dict[str, Any] | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            current = connection.execute(
                """SELECT * FROM stage_runs WHERE task_id = ? AND status = 'active'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if current is None:
                raise StateConflict("任务没有活动阶段")
            current_stage = current["stage"]
            current_details = json.loads(current["details_json"])
            if current_stage in BLOCKED_STAGES:
                allowed = {current_details.get("resume_stage")}
            else:
                allowed = set(STAGE_TRANSITIONS.get(current_stage, set())) | BLOCKED_STAGES
            if to_stage not in allowed:
                raise StateConflict(f"非法阶段迁移：{current_stage} -> {to_stage}")
            current_round = int(current["review_round"])
            next_round = current_round if review_round is None else int(review_round)
            if to_stage in {"discovery_review", "confirmation_review"} and review_round is None:
                next_round = max(1, current_round)
            if next_round < current_round:
                raise StateConflict("review_round 不能倒退")
            next_details = dict(details or {})
            if to_stage in BLOCKED_STAGES:
                next_details["resume_stage"] = current_stage
            connection.execute(
                "UPDATE stage_runs SET status = 'completed', finished_at = ? WHERE stage_run_id = ?",
                (stamp, current["stage_run_id"]),
            )
            stage_run_id = _new_id("SR")
            connection.execute(
                """INSERT INTO stage_runs(
                       stage_run_id, task_id, stage, review_round, status, started_at, details_json
                   ) VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (stage_run_id, task_id, to_stage, next_round, stamp, _json(next_details)),
            )
            lifecycle = task["lifecycle_status"]
            review = task["review_status"]
            if to_stage in BLOCKED_STAGES:
                lifecycle = "blocked"
            elif current_stage in BLOCKED_STAGES:
                lifecycle = "in_progress"
            elif to_stage == "submitted":
                lifecycle, review = "submitted", "submitted"
            connection.execute(
                """UPDATE tasks SET lifecycle_status = ?, runtime_status = ?,
                       review_status = ?, updated_at = ? WHERE task_id = ?""",
                (lifecycle, to_stage, review, stamp, task_id),
            )
            return _row(connection.execute(
                "SELECT * FROM stage_runs WHERE stage_run_id = ?", (stage_run_id,)
            ).fetchone()) or {}

    def _ensure_no_git_recovery_continuation_lease(
        self,
        connection: sqlite3.Connection,
        *,
        task: sqlite3.Row,
        path_value: str,
        operation: str,
        maintenance_task_id: str,
        maintenance_session_id: str,
        repair_event_id: str,
        stamp: str,
    ) -> dict[str, str]:
        """Create/reuse one exact worker delegation derived from a proven repair."""
        if operation != "update":
            raise StateConflict("无 Git 交付恢复 delegation 只允许目标原授权的 update 子集")
        task_operations = canonical_operations(
            json.loads(task["allowed_operations_json"] or "[]"),
            task_kind=task["task_kind"],
        )
        task_roots = json.loads(task["allowed_write_roots_json"] or "[]")
        task_grants = json.loads(task["grants_json"] or "[]")
        if (
            operation not in task_operations
            or not any(scope_covers(root, path_value) for root in task_roots)
            or not any(
                scope_covers(str(grant.get("path") or ""), path_value)
                and operation in (grant.get("operations") or [])
                for grant in task_grants
            )
        ):
            raise StateConflict("无 Git 交付恢复 delegation 超出目标原执行包络")

        lease_id = "L-NOGIT-" + _digest({
            "repair_event_id": repair_event_id,
            "worker_session_id": maintenance_session_id,
        })[:24]
        delegation_event_id = "EV-NOGIT-DEL-" + _digest({
            "repair_event_id": repair_event_id,
            "lease_id": lease_id,
        })[:20]
        exact_roots = [path_value]
        exact_operations = ["update"]
        exact_grants = [{"path": path_value, "operations": ["update"]}]
        role = "maintenance_recovery_continuation"
        existing_delegation = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (delegation_event_id,),
        ).fetchone()
        if existing_delegation is not None:
            delegation = json.loads(existing_delegation["payload_json"] or "{}")
            issued_at = str(delegation.get("issued_at") or "")
            expires_at = str(delegation.get("expires_at") or "")
            expected_static = {
                "repair_event_id": repair_event_id,
                "target_task_id": task["task_id"],
                "maintenance_task_id": maintenance_task_id,
                "maintenance_session_id": maintenance_session_id,
                "lease_id": lease_id,
                "source_session_id": task["session_id"],
                "worker_session_id": maintenance_session_id,
                "role": role,
                "allowed_write_roots": exact_roots,
                "allowed_operations": exact_operations,
                "grants": exact_grants,
                "read_only": False,
                "actor_verified": False,
                "enforcement_verified": False,
                "derived_from_envelope_repair": True,
            }
            if (
                existing_delegation["event_type"] != "maintenance_recovery_delegation"
                or existing_delegation["task_id"] != task["task_id"]
                or any(delegation.get(key) != value for key, value in expected_static.items())
                or not issued_at or not expires_at
                or _as_utc(expires_at) <= _as_utc(stamp)
            ):
                raise StateConflict("无 Git 恢复 delegation 审计证据漂移或已过期")
        else:
            issued_at = stamp
            expires_at = _timestamp(_as_utc(stamp) + timedelta(minutes=30))
            delegation = {
                "repair_event_id": repair_event_id,
                "target_task_id": task["task_id"],
                "maintenance_task_id": maintenance_task_id,
                "maintenance_session_id": maintenance_session_id,
                "lease_id": lease_id,
                "source_session_id": task["session_id"],
                "worker_session_id": maintenance_session_id,
                "role": role,
                "allowed_write_roots": exact_roots,
                "allowed_operations": exact_operations,
                "grants": exact_grants,
                "read_only": False,
                "actor_verified": False,
                "enforcement_verified": False,
                "derived_from_envelope_repair": True,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }

        competing = connection.execute(
            """SELECT * FROM leases
               WHERE task_id=? AND role=? AND status='active'""",
            (task["task_id"], role),
        ).fetchall()
        if any(row["worker_session_id"] != maintenance_session_id for row in competing):
            raise StateConflict("无 Git 恢复已存在不同 worker 的活动 continuation lease")
        active = connection.execute(
            """SELECT * FROM leases
               WHERE task_id=? AND worker_session_id=? AND status='active'""",
            (task["task_id"], maintenance_session_id),
        ).fetchone()
        if active is not None:
            if (
                active["lease_id"] != lease_id
                or active["source_session_id"] != task["session_id"]
                or active["role"] != role
                or json.loads(active["allowed_write_roots_json"] or "[]") != exact_roots
                or json.loads(active["allowed_operations_json"] or "[]") != exact_operations
                or json.loads(active["grants_json"] or "[]") != exact_grants
                or bool(active["read_only"])
                or bool(active["enforcement_verified"])
                or active["issued_at"] != issued_at
                or active["expires_at"] != expires_at
            ):
                raise StateConflict("无 Git 恢复 continuation lease 已存在范围、worker 或身份漂移")
        else:
            stale = connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,),
            ).fetchone()
            if stale is not None:
                raise StateConflict("无 Git 恢复 continuation lease 已终止，不得静默刷新")
            connection.execute(
                """INSERT INTO leases(
                       lease_id,task_id,source_session_id,worker_session_id,role,
                       allowed_write_roots_json,allowed_operations_json,grants_json,
                       read_only,status,issued_at,expires_at,enforcement_verified
                   ) VALUES (?,?,?,?,?,?,?,?,0,'active',?,?,0)""",
                (
                    lease_id, task["task_id"], task["session_id"], maintenance_session_id,
                    role, _json(exact_roots), _json(exact_operations), _json(exact_grants),
                    issued_at, expires_at,
                ),
            )
        if existing_delegation is None:
            self._append_event(
                connection,
                "maintenance_recovery_delegation",
                delegation,
                event_id=delegation_event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=maintenance_session_id,
                task_id=task["task_id"],
            )
        return {
            "continuation_lease_id": lease_id,
            "delegation_event_id": delegation_event_id,
            "continuation_lease_expires_at": expires_at,
        }

    def recover_tracked_no_git_delivery(
        self,
        *,
        target_task_id: str,
        maintenance_task_id: str,
        maintenance_session_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Atomically recover a machine-proven tracked-file delivery blocked by a Git exclusion."""
        stamp = _timestamp(at)
        if target_task_id == maintenance_task_id:
            raise StateConflict("被阻断任务不得兼任无 Git 交付修复 maintainer")
        event_id = "EV-NOGIT-" + _digest({"task_id": target_task_id})[:24]
        with self.transaction(immediate=True) as connection:
            maintainer, maintainer_roots, maintainer_lease_id = self._task_access_in_connection(
                connection, task_id=maintenance_task_id, session_id=maintenance_session_id,
                at=stamp, require_write=True,
            )
            if canonical_task_kind(maintainer["task_kind"]) != "control_plane_maintenance":
                raise StateConflict("无 Git 交付修复必须由独立活动控制面维护任务执行")
            maintainer_operations = canonical_operations(
                json.loads(maintainer["allowed_operations_json"] or "[]"),
                task_kind=maintainer["task_kind"],
            )
            if maintainer_lease_id:
                lease_row = connection.execute(
                    "SELECT grants_json FROM leases WHERE lease_id=?", (maintainer_lease_id,),
                ).fetchone()
                maintainer_grants = json.loads(lease_row["grants_json"] or "[]") if lease_row else []
            else:
                maintainer_grants = json.loads(maintainer["grants_json"] or "[]")
            state_path = str(self.path.expanduser().resolve())
            if (
                "envelope_repair" not in maintainer_operations
                or not any(scope_covers(root, state_path) for root in maintainer_roots)
                or not any(
                    scope_covers(str(grant.get("path") or ""), state_path)
                    and "envelope_repair" in (grant.get("operations") or [])
                    for grant in maintainer_grants
                )
            ):
                raise StateConflict("独立 maintainer 缺少 StateStore envelope_repair 对应 grant")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (target_task_id,),
            ).fetchone()
            if task is None:
                raise StateNotFound(f"被阻断任务不存在：{target_task_id}")
            if not self._execution_authority_in_connection(connection, task):
                raise StateConflict("被阻断任务缺少原始用户授权执行包络")
            existing_event = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,),
            ).fetchone()
            metadata = json.loads(task["metadata_json"] or "{}")
            active = connection.execute(
                """SELECT * FROM stage_runs WHERE task_id=? AND status='active'
                   ORDER BY started_at DESC,rowid DESC LIMIT 1""",
                (target_task_id,),
            ).fetchone()
            if existing_event is not None:
                payload = json.loads(existing_event["payload_json"] or "{}")
                if (
                    existing_event["event_type"] != "tracked_no_git_delivery_recovered"
                    or existing_event["task_id"] != target_task_id
                    or payload.get("task_id") != target_task_id
                    or payload.get("maintenance_task_id") != maintenance_task_id
                    or payload.get("maintenance_session_id") != maintenance_session_id
                    or canonical_delivery_mode(metadata.get("delivery_mode")) != "files_no_git"
                    or active is None
                    or active["stage"] != payload.get("resume_stage")
                ):
                    raise StateConflict("无 Git 交付恢复事件与当前任务状态漂移")
                delegation = self._ensure_no_git_recovery_continuation_lease(
                    connection,
                    task=task,
                    path_value=canonical_scope(str(payload.get("path") or "")),
                    operation=canonical_operation(str(payload.get("operation") or "")),
                    maintenance_task_id=maintenance_task_id,
                    maintenance_session_id=maintenance_session_id,
                    repair_event_id=event_id,
                    stamp=stamp,
                )
                return {"event_id": event_id, **payload, **delegation, "idempotent": True}

            if canonical_delivery_mode(metadata.get("delivery_mode")) != "files":
                raise StateConflict("只有被 files 模式错误阻断的任务可以升级为 files_no_git")
            excluded_actions = json.loads(task["excluded_actions_json"] or "[]")
            if not _git_commit_is_excluded(excluded_actions):
                raise StateConflict("执行包络没有明确排除 Git 提交，不得改写交付模式")
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE task_id=? LIMIT 1", (target_task_id,),
            ).fetchone() is not None:
                raise StateConflict("任务已经存在交付，不得事后改写交付模式")
            if active is None or active["stage"] != "blocked_external_dependency":
                raise StateConflict("任务不是由外部交付能力阻断的活动阶段")
            details = json.loads(active["details_json"] or "{}")
            resume_stage = str(details.get("resume_stage") or "")
            if (
                resume_stage not in {"confirmation_review", "committing"}
                or details.get("probe_name") != "xirang_delivery_no_git_tracked_file"
                or "tracked" not in str(details.get("blocker") or "").casefold()
                or "git" not in str(details.get("blocker") or "").casefold()
            ):
                raise StateConflict("活动阻断没有机器可证明的 tracked no-Git 原因")
            path_value = canonical_scope(str(details.get("file") or ""))
            receipt_id = str(details.get("write_receipt_id") or "")
            receipt = connection.execute(
                """SELECT * FROM write_receipts
                   WHERE receipt_id=? AND task_id=? AND status='effective'""",
                (receipt_id, target_task_id),
            ).fetchone()
            if receipt is None or receipt["path"] != path_value:
                raise StateConflict("无 Git 交付恢复缺少当前任务的有效写入收据")
            operation = canonical_operation(receipt["operation"])
            task_operations = canonical_operations(
                json.loads(task["allowed_operations_json"] or "[]"),
                task_kind=task["task_kind"],
            )
            roots = json.loads(task["allowed_write_roots_json"] or "[]")
            grants = json.loads(task["grants_json"] or "[]")
            if (
                operation not in task_operations
                or not any(scope_covers(root, path_value) for root in roots)
                or not any(
                    scope_covers(str(grant.get("path") or ""), path_value)
                    and operation in (grant.get("operations") or [])
                    for grant in grants
                )
            ):
                raise StateConflict("无 Git 交付恢复目标超出原执行包络")
            root = _bound_delivery_workspace_root(self, task)
            if subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path_value],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                raise StateConflict("阻断目标不是 Git 已跟踪文件")
            target = root / path_value
            exists_after = bool(receipt["exists_after"])
            current_sha = _file_sha256(target) if target.is_file() and not target.is_symlink() else None
            if (
                exists_after != target.is_file()
                or (target.is_symlink())
                or current_sha != receipt["sha256"]
                or details.get("current_sha256") != receipt["sha256"]
            ):
                raise StateConflict("阻断目标已经偏离有效写入收据")
            recovery_path = Path(str(details.get("recovery_manifest") or "")).expanduser().resolve()
            recovery = _read_file_preimage_binding(
                recovery_path, logical_path=path_value, workspace_root=root,
            )
            try:
                captured_at = _as_utc(str(recovery.get("captured_at") or ""))
                receipt_at = _as_utc(receipt["created_at"])
            except (TypeError, ValueError) as exc:
                raise StateConflict("pre-image 或写入收据缺少可比较时间") from exc
            if captured_at > receipt_at:
                raise StateConflict("pre-image 晚于写入收据，不能证明写前恢复点")

            metadata["delivery_mode"] = "files_no_git"
            payload = {
                "task_id": target_task_id,
                "maintenance_task_id": maintenance_task_id,
                "maintenance_session_id": maintenance_session_id,
                "maintenance_capability": "envelope_repair",
                "old_delivery_mode": "files",
                "new_delivery_mode": "files_no_git",
                "path": path_value,
                "operation": operation,
                "write_receipt_id": receipt_id,
                "write_sha256": receipt["sha256"],
                "recovery_manifest": str(recovery_path),
                "recovery_manifest_sha256": recovery["manifest_sha256"],
                "preimage_sha256": recovery["sha256"],
                "blocked_stage_run_id": active["stage_run_id"],
                "resume_stage": resume_stage,
                "target_scope_unchanged": True,
                "authorization_replayed": False,
                "actor_verified": False,
            }
            connection.execute(
                "UPDATE tasks SET metadata_json=?,lifecycle_status='in_progress',runtime_status=?,updated_at=? WHERE task_id=?",
                (_json(metadata), resume_stage, stamp, target_task_id),
            )
            connection.execute(
                "UPDATE stage_runs SET status='completed',finished_at=? WHERE stage_run_id=?",
                (stamp, active["stage_run_id"]),
            )
            stage_run_id = _new_id("SR")
            connection.execute(
                """INSERT INTO stage_runs(
                       stage_run_id,task_id,stage,review_round,status,started_at,details_json
                   ) VALUES (?,?,?,?, 'active',?,?)""",
                (
                    stage_run_id, target_task_id, resume_stage, int(active["review_round"]), stamp,
                    _json({"no_git_delivery_recovery_event_id": event_id}),
                ),
            )
            self._append_event(
                connection,
                "tracked_no_git_delivery_recovered",
                payload,
                event_id=event_id,
                occurred_at=stamp,
                workspace_id=task["workspace_id"],
                session_id=maintenance_session_id,
                task_id=target_task_id,
            )
            delegation = self._ensure_no_git_recovery_continuation_lease(
                connection,
                task=task,
                path_value=path_value,
                operation=operation,
                maintenance_task_id=maintenance_task_id,
                maintenance_session_id=maintenance_session_id,
                repair_event_id=event_id,
                stamp=stamp,
            )
            return {
                "event_id": event_id,
                "resume_stage_run_id": stage_run_id,
                **payload,
                **delegation,
                "idempotent": False,
            }

    def create_delivery(
        self,
        *,
        task_id: str,
        manifest: Sequence[dict[str, Any]],
        submitted_at: datetime | str,
        delivery_id: str | None = None,
        implementation_commit: str | None = None,
        implementation_tree: str | None = None,
        tag_object: str | None = None,
        validation_summary: str | None = None,
        adversarial_review_summary: str | None = None,
    ) -> str:
        delivery_id = delivery_id or _new_id("DEL")
        stamp = _timestamp(submitted_at)
        manifest_json = _json(list(manifest))
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise StateNotFound(f"任务不存在：{task_id}")
            if not self._execution_authority_in_connection(connection, task):
                raise StateConflict("无有效用户授权执行包络的任务不能登记交付")
            metadata = json.loads(task["metadata_json"] or "{}")
            delivery_mode = canonical_delivery_mode(metadata.get("delivery_mode"))
            if delivery_mode == "chat":
                if manifest or any((implementation_commit, implementation_tree, tag_object)):
                    raise StateConflict("聊天交付不能携带文件 manifest 或 Git 身份")
            elif delivery_mode == "files":
                if _git_commit_is_excluded(json.loads(task["excluded_actions_json"] or "[]")):
                    raise StateConflict("执行包络明确排除 Git 提交，不得登记 files Git 交付")
                if not manifest or not all((implementation_commit, implementation_tree, tag_object)):
                    raise StateConflict(
                        "文件交付必须包含非空 manifest、commit、tree 和 annotated tag object"
                    )
                _verify_controlled_delivery_tag(
                    self,
                    task=task,
                    delivery_id=delivery_id,
                    manifest=manifest,
                    implementation_commit=str(implementation_commit),
                    implementation_tree=str(implementation_tree),
                    tag_object=str(tag_object),
                )
            elif delivery_mode == "files_no_git":
                if any((implementation_commit, implementation_tree, tag_object)):
                    raise StateConflict("files_no_git 交付不得携带 commit、tree 或 tag 身份")
                if not _git_commit_is_excluded(json.loads(task["excluded_actions_json"] or "[]")):
                    raise StateConflict("files_no_git 必须来自明确排除 Git 提交的执行包络")
                _verify_files_no_git_manifest(self, task=task, manifest=manifest)
            existing = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if existing is None and implementation_commit:
                existing = connection.execute(
                    """SELECT * FROM deliveries
                       WHERE task_id=? AND implementation_commit=? AND implementation_tree=?
                         AND manifest_json=? ORDER BY submitted_at DESC LIMIT 1""",
                    (task_id, implementation_commit, implementation_tree, manifest_json),
                ).fetchone()
            if existing is not None:
                if not (
                    existing["task_id"] == task_id
                    and existing["manifest_json"] == manifest_json
                    and existing["implementation_commit"] == implementation_commit
                    and existing["implementation_tree"] == implementation_tree
                    and existing["tag_object"] == tag_object
                ):
                    raise StateConflict("delivery_id 已绑定不同的不可变交付内容")
                connection.execute(
                    """INSERT OR IGNORE INTO outbox(
                           dedupe_key, event_type, aggregate_type, aggregate_id,
                           payload_json, available_at
                       ) VALUES (?, 'authority_committed', 'delivery', ?, ?, ?)""",
                    (f"delivery-authority:{existing['delivery_id']}", existing["delivery_id"],
                     _json({"delivery_id": existing["delivery_id"], "task_id": task_id}), stamp),
                )
                return str(existing["delivery_id"])
            stage = connection.execute(
                """SELECT stage FROM stage_runs WHERE task_id = ? AND status = 'active'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""", (task_id,),
            ).fetchone()
            if stage is None or stage["stage"] != "committing":
                raise StateConflict("只有 committing 阶段可以登记交付")
            if task["review_status"] == "accepted":
                raise StateConflict("已验收任务不能创建新交付")
            for item in manifest:
                receipt_id = str(item.get("receipt_id") or "")
                receipt = connection.execute(
                    "SELECT * FROM write_receipts WHERE receipt_id=? AND status='effective'",
                    (receipt_id,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["path"] != item.get("path")
                        or receipt["sha256"] != item.get("sha256")
                        or bool(receipt["exists_after"]) != bool(item.get("exists_after"))
                    ):
                        raise StateConflict("交付 manifest 必须引用当前有效且一致的写入收据")
                    if receipt["task_id"] != task_id:
                        repair = connection.execute(
                            """SELECT * FROM envelope_repairs
                               WHERE new_task_id=? AND old_task_id=? AND reason=?""",
                            (
                                task_id, receipt["task_id"],
                                OMITTED_AGENT_REGISTRY_DEPENDENCY_REPAIR_REASON,
                            ),
                        ).fetchone()
                        if repair is None or receipt["created_at"] > repair["created_at"]:
                            raise StateConflict("交付 manifest 引用了无继承权的前序写入收据")
                        operation = canonical_operation(receipt["operation"])
                        task_operations = canonical_operations(
                            json.loads(task["allowed_operations_json"] or "[]"),
                            task_kind=task["task_kind"],
                        )
                        roots = json.loads(task["allowed_write_roots_json"] or "[]")
                        grants = json.loads(task["grants_json"] or "[]")
                        if (
                            operation not in task_operations
                            or not any(scope_covers(root, receipt["path"]) for root in roots)
                            or not any(
                                scope_covers(str(grant.get("path") or ""), receipt["path"])
                                and operation in (grant.get("operations") or []) for grant in grants
                            )
                        ):
                            raise StateConflict("继承写入收据已超出后继交付权限")
                    if delivery_mode == "files_no_git":
                        recovery = _read_file_preimage_binding(
                            Path(str(item.get("recovery_manifest") or "")),
                            logical_path=str(item.get("path") or ""),
                            workspace_root=_bound_delivery_workspace_root(self, task),
                        )
                        try:
                            captured_at = _as_utc(str(recovery.get("captured_at") or ""))
                            receipt_at = _as_utc(receipt["created_at"])
                        except (TypeError, ValueError) as exc:
                            raise StateConflict("无 Git 交付 pre-image 缺少可比较的写前时间") from exc
                        if captured_at > receipt_at:
                            raise StateConflict("无 Git 交付 pre-image 晚于写入收据")
                    continue
                if delivery_mode == "files_no_git":
                    raise StateConflict("files_no_git 必须引用带写入时间的正式有效收据")
                if item.get("evidence_kind") != "takeover_reconciliation":
                    raise StateConflict("交付 manifest 必须引用当前有效且一致的写入收据")
                reconciliation_id = str(item.get("reconciliation_id") or "")
                expected_receipt_id = "RC-" + _digest({
                    "reconciliation_id": reconciliation_id,
                    "path": str(item.get("path") or ""),
                })[:24]
                if receipt_id != expected_receipt_id:
                    raise StateConflict("接管对账交付证据标识无效")
                record = connection.execute(
                    """SELECT * FROM events WHERE event_id=? AND task_id=?
                       AND event_type='takeover_reconciliation_recorded'""",
                    (item.get("reconciliation_record_event_id"), task_id),
                ).fetchone()
                confirmation = connection.execute(
                    """SELECT * FROM events WHERE event_id=? AND task_id=?
                       AND event_type='takeover_reconciliation_confirmed'""",
                    (item.get("reconciliation_confirmation_event_id"), task_id),
                ).fetchone()
                if record is None or confirmation is None:
                    raise StateConflict("接管对账交付缺少独立确认事件")
                record_payload = json.loads(record["payload_json"])
                confirmation_payload = json.loads(confirmation["payload_json"])
                manifest_path = Path(str(item.get("reconciliation_manifest") or "")).expanduser().resolve()
                manifest_sha = str(item.get("reconciliation_manifest_sha256") or "")
                if (
                    record_payload.get("reconciliation_id") != reconciliation_id
                    or record_payload.get("manifest_path") != str(manifest_path)
                    or record_payload.get("manifest_sha256") != manifest_sha
                    or confirmation_payload.get("record_event_id") != record["event_id"]
                    or confirmation_payload.get("manifest_sha256") != manifest_sha
                    or confirmation_payload.get("reviewer_agent_id")
                       != item.get("reconciliation_reviewer_agent_id")
                    or not manifest_path.is_file()
                    or _file_sha256(manifest_path) != manifest_sha
                ):
                    raise StateConflict("接管对账交付证据绑定无效或已经漂移")
                manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
                item_map = self._takeover_reconciliation_item_map(manifest_document)
                evidence_item = item_map.get(str(item.get("path") or ""))
                if (
                    evidence_item is None
                    or evidence_item["sha256"] != item.get("sha256")
                    or bool(evidence_item["exists_after"]) != bool(item.get("exists_after"))
                    or evidence_item["authorization_basis_task_id"]
                       != item.get("reconciliation_authorization_basis_task_id")
                    or evidence_item["historical_source_task_id"]
                       != item.get("reconciliation_historical_source_task_id")
                    or bool(evidence_item["historical_source_verified"])
                       != bool(item.get("reconciliation_historical_source_verified"))
                    or item.get("original_actor_verified") is not False
                    or item.get("original_write_times_known") is not False
                ):
                    raise StateConflict("接管对账交付文件项与确认清单不一致")
            connection.execute(
                """INSERT INTO deliveries(
                       delivery_id, task_id, implementation_commit, implementation_tree,
                       tag_object, manifest_json, validation_summary,
                       adversarial_review_summary, submitted_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    delivery_id,
                    task_id,
                    implementation_commit,
                    implementation_tree,
                    tag_object,
                    manifest_json,
                    validation_summary,
                    adversarial_review_summary,
                    stamp,
                ),
            )
            connection.execute(
                """UPDATE tasks SET lifecycle_status = 'submitted', runtime_status = 'submitted',
                       review_status = 'submitted', updated_at = ? WHERE task_id = ?""",
                (stamp, task_id),
            )
            connection.execute(
                """UPDATE leases SET status = 'completed'
                   WHERE task_id = ? AND status = 'active'""",
                (task_id,),
            )
            connection.execute(
                """UPDATE review_focus SET status='superseded', superseded_at=?
                   WHERE task_id=? AND status='active'""",
                (stamp, task_id),
            )
            connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id,
                       payload_json, available_at
                   ) VALUES (?, 'authority_committed', 'delivery', ?, ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (f"delivery-authority:{delivery_id}", delivery_id,
                 _json({"task_id": task_id, "delivery_id": delivery_id}), stamp),
            )
            return delivery_id

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self.connect(readonly=True) as connection:
            return _row(connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone())

    def get_latest_delivery(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute(
                "SELECT * FROM deliveries WHERE task_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (task_id,),
            ).fetchone())

    def create_review_focus(
        self,
        *,
        focus_id: str,
        task_id: str,
        delivery_id: str,
        conversation_id: str,
        presented_at: datetime | str,
        ttl_seconds: int = 86_400,
    ) -> str:
        presented = _as_utc(presented_at)
        with self.transaction(immediate=True) as connection:
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if delivery is None or delivery["task_id"] != task_id:
                raise StateConflict("review focus 必须绑定该任务的真实交付")
            latest = connection.execute(
                "SELECT delivery_id FROM deliveries WHERE task_id=? ORDER BY submitted_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if latest is None or latest["delivery_id"] != delivery_id or delivery["status"] not in {"submitted", "reviewing"}:
                raise StateConflict("review focus 只能绑定任务的当前 submitted 交付")
            connection.execute(
                """UPDATE review_focus SET status='superseded', superseded_at=?
                   WHERE delivery_id=? AND status='active'""",
                (_timestamp(presented), delivery_id),
            )
            connection.execute(
                """INSERT INTO review_focus(
                       focus_id, task_id, delivery_id, conversation_id, submitted_at,
                       presented_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    focus_id,
                    task_id,
                    delivery_id,
                    conversation_id,
                    delivery["submitted_at"],
                    _timestamp(presented),
                    _timestamp(presented + timedelta(seconds=int(ttl_seconds))),
                ),
            )
            return focus_id

    def get_review_focus(self, focus_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _row(connection.execute(
                "SELECT * FROM review_focus WHERE focus_id = ?", (focus_id,)
            ).fetchone())

    def get_active_review_focus(
        self,
        conversation_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        stamp = _timestamp(at)
        with self.connect() as connection:
            return _row(connection.execute(
                """SELECT * FROM review_focus
                   WHERE conversation_id = ? AND status = 'active' AND expires_at > ?
                   ORDER BY presented_at DESC LIMIT 1""",
                (conversation_id, stamp),
            ).fetchone())

    def consume_review_focus(self, focus_id: str, *, at: datetime | str | None = None) -> bool:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE review_focus SET status = 'consumed', consumed_at = ?
                   WHERE focus_id = ? AND status = 'active' AND expires_at > ?""",
                (stamp, focus_id, stamp),
            )
            return cursor.rowcount == 1

    def supersede_review_focus(
        self,
        focus_id: str,
        *,
        superseded_by_focus_id: str | None = None,
        at: datetime | str | None = None,
    ) -> bool:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            focus = connection.execute("SELECT * FROM review_focus WHERE focus_id = ?", (focus_id,)).fetchone()
            if focus is None:
                raise StateNotFound(f"review focus 不存在：{focus_id}")
            if superseded_by_focus_id:
                successor = connection.execute(
                    "SELECT * FROM review_focus WHERE focus_id = ?", (superseded_by_focus_id,)
                ).fetchone()
                if successor is None or successor["task_id"] != focus["task_id"]:
                    raise StateConflict("替代焦点必须属于同一任务")
            cursor = connection.execute(
                """UPDATE review_focus
                   SET status = 'superseded', superseded_at = ?, superseded_by_focus_id = ?
                   WHERE focus_id = ? AND status = 'active'""",
                (stamp, superseded_by_focus_id, focus_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _consume_preference_user_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        workspace_id: str,
        consumer: str,
        stamp: str,
    ) -> sqlite3.Row:
        event = connection.execute("SELECT * FROM user_events WHERE event_id = ?", (event_id,)).fetchone()
        if event is None:
            raise StateNotFound(f"用户事件不存在：{event_id}")
        if event["workspace_id"] != workspace_id:
            raise StateConflict("偏好事件不属于当前 workspace")
        if event["consumed_at"] is not None:
            raise StateConflict("偏好事件已经消费")
        if _as_utc(event["expires_at"]) <= _as_utc(stamp):
            raise ExpiredUserEvent(f"用户事件已过期：{event_id}")
        cursor = connection.execute(
            """UPDATE user_events SET consumed_at = ?, consumed_by = ?
               WHERE event_id = ? AND consumed_at IS NULL AND expires_at > ?""",
            (stamp, consumer, event_id, stamp),
        )
        if cursor.rowcount != 1:
            raise StateConflict("偏好事件并发消费冲突")
        return event

    def set_interaction_preference(
        self,
        *,
        workspace_id: str,
        user_scope: str,
        intermediate_confirmation_policy: str,
        review_prompt_policy: str,
        source_user_event_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        preference_id = _digest({"workspace_id": workspace_id, "user_scope": user_scope})[:24]
        with self.transaction(immediate=True) as connection:
            event = self._consume_preference_user_event(
                connection,
                event_id=source_user_event_id,
                workspace_id=workspace_id,
                consumer="set_interaction_preference",
                stamp=stamp,
            )
            connection.execute(
                """INSERT INTO preferences(
                       preference_id, workspace_id, user_scope,
                       intermediate_confirmation_policy, review_prompt_policy,
                       source_user_event_id, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id, user_scope) DO UPDATE SET
                       intermediate_confirmation_policy = excluded.intermediate_confirmation_policy,
                       review_prompt_policy = excluded.review_prompt_policy,
                       source_user_event_id = excluded.source_user_event_id,
                       updated_at = excluded.updated_at""",
                (
                    preference_id,
                    workspace_id,
                    user_scope,
                    intermediate_confirmation_policy,
                    review_prompt_policy,
                    source_user_event_id,
                    stamp,
                ),
            )
            self._append_event(
                connection,
                "interaction_preference_set",
                {"user_scope": user_scope},
                workspace_id=workspace_id,
                session_id=event["session_id"],
                occurred_at=stamp,
            )
            return _row(connection.execute(
                "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = ?",
                (workspace_id, user_scope),
            ).fetchone()) or {}

    def clear_interaction_preference(
        self,
        *,
        workspace_id: str,
        user_scope: str,
        source_user_event_id: str,
        at: datetime | str | None = None,
    ) -> bool:
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            event = self._consume_preference_user_event(
                connection,
                event_id=source_user_event_id,
                workspace_id=workspace_id,
                consumer="clear_interaction_preference",
                stamp=stamp,
            )
            cursor = connection.execute(
                "DELETE FROM preferences WHERE workspace_id = ? AND user_scope = ?",
                (workspace_id, user_scope),
            )
            self._append_event(
                connection,
                "interaction_preference_cleared",
                {"user_scope": user_scope, "existed": cursor.rowcount == 1},
                workspace_id=workspace_id,
                session_id=event["session_id"],
                occurred_at=stamp,
            )
            return cursor.rowcount == 1

    def get_interaction_preference(self, workspace_id: str, user_scope: str) -> dict[str, Any] | None:
        with self.connect(readonly=True) as connection:
            return _row(connection.execute(
                "SELECT * FROM preferences WHERE workspace_id = ? AND user_scope = ?",
                (workspace_id, user_scope),
            ).fetchone())

    def validate_external_target(self, *, task_id: str, target: str) -> dict[str, str]:
        path = Path(target).expanduser().resolve()
        task = self.get_task(task_id)
        if task is None or not self.task_execution_authorized(task_id):
            raise ScopeViolation("外部目标没有有效任务执行权限")
        for item in task.get("external_write_targets") or []:
            root = Path(str(item.get("path") or "")).expanduser().resolve()
            kind = str(item.get("kind") or "")
            if kind == "file" and path == root:
                if root.exists() and not root.is_file():
                    raise ScopeViolation("授权为 file 的外部目标类型已漂移")
                return {"path": str(root), "kind": kind}
            if kind == "dir" and (path == root or root in path.parents):
                if root.exists() and not root.is_dir():
                    raise ScopeViolation("授权为 dir 的外部目标类型已漂移")
                return {"path": str(root), "kind": kind}
        raise ScopeViolation("目标未包含在冻结的外部 file/dir 权限中")

    def get_projection_manifest(self, task_id: str) -> dict[str, Any] | None:
        with self.connect(readonly=True) as connection:
            return _row(connection.execute(
                "SELECT * FROM task_projection_manifest WHERE task_id=?", (task_id,)
            ).fetchone())

    def get_delivery_task_record(self, delivery_id: str) -> dict[str, Any] | None:
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return None
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                """SELECT payload_json FROM events
                   WHERE task_id=? AND event_type='delivery_task_record_committed'
                   ORDER BY sequence DESC""",
                (delivery["task_id"],),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("delivery_id") == delivery_id:
                return payload
        return None

    def record_delivery_task_record(
        self, *, delivery_id: str, task_id: str, path: str, sha256: str,
        commit: str, tree: str, tag_object: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        core = {
            "delivery_id": delivery_id, "task_id": task_id, "path": path,
            "sha256": sha256, "task_record_commit": commit,
            "task_record_tree": tree, "task_record_tag_object": tag_object,
        }
        digest = _digest(core)
        event_id = f"EV-DTR-{digest[:24]}"
        payload = {**core, "task_record_digest": digest}
        with self.transaction(immediate=True) as connection:
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id=? AND task_id=?",
                (delivery_id, task_id),
            ).fetchone()
            projection = connection.execute(
                "SELECT * FROM task_projection_manifest WHERE task_id=?", (task_id,)
            ).fetchone()
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if delivery is None or task is None or delivery["status"] != "submitted":
                raise StateConflict("task_record requires the authority-committed submitted delivery")
            if projection is None or projection["status"] != "projected" or projection["path"] != path or projection["sha256"] != sha256:
                raise StateConflict("task_record does not match the deterministic task projection manifest")
            existing = connection.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"] or "{}") != payload:
                    raise StateConflict("task_record audit metadata conflicts")
                return {**payload, "idempotent": True}
            # The commit/tag receipt is deliberately stored in the append-only event,
            # not folded back into task metadata.  Mutating task.updated_at here would
            # instantly make the just-committed deterministic task-card projection
            # stale and create an impossible self-referential follow-up commit.
            self._append_event(
                connection, "delivery_task_record_committed", payload,
                event_id=event_id, occurred_at=stamp,
                workspace_id=task["workspace_id"], session_id=task["session_id"], task_id=task_id,
            )
        return {**payload, "idempotent": False}

    def prepare_task_projection(
        self, *, task_id: str, path: str, workspace_root: str | Path,
        expected_authority_updated_at: str,
    ) -> dict[str, Any]:
        """Reserve a task-card target before replacing any bytes.

        A crash after this point leaves the manifest pending, never falsely green.
        """
        supplied_path = Path(path).expanduser().resolve()
        root, bound_workspace_id = _validate_projection_workspace_binding(self, workspace_root)
        try:
            supplied_path.relative_to(root)
        except ValueError as exc:
            raise StateConflict("任务投影必须位于绑定的 workspace_root") from exc
        if supplied_path.stem != task_id:
            raise StateConflict("任务投影文件名必须与 owner task_id 一致")
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            manifest = connection.execute(
                "SELECT * FROM task_projection_manifest WHERE task_id=?", (task_id,),
            ).fetchone()
            if task is None or manifest is None:
                raise StateNotFound("任务或投影 manifest 不存在")
            if task["workspace_id"] != bound_workspace_id:
                raise StateConflict("任务 workspace_id 与 cutover workspace 绑定不一致")
            if (
                task["updated_at"] != expected_authority_updated_at
                or manifest["authority_updated_at"] != expected_authority_updated_at
            ):
                raise StateConflict("投影基于过期权威版本，拒绝预留")
            expected_path = Path(str(manifest["path"] or "")).expanduser().resolve()
            try:
                expected_path.relative_to(root)
            except ValueError as exc:
                raise StateConflict("冻结的 card_path 位于 workspace_root 外") from exc
            if supplied_path != expected_path:
                raise StateConflict("投影路径与任务冻结的 card_path 不一致")
            for row in connection.execute(
                "SELECT task_id,path FROM task_projection_manifest WHERE task_id<>? AND path IS NOT NULL",
                (task_id,),
            ):
                if Path(str(row["path"])).expanduser().resolve() == supplied_path:
                    raise StateConflict(f"任务投影路径已被其他任务占用：{row['task_id']}")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projection_manifest (
                     path TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
                     rendered_at TEXT NOT NULL
                   )"""
            )
            collision = connection.execute(
                "SELECT kind FROM projection_manifest WHERE path=?", (str(supplied_path),),
            ).fetchone()
            if collision is not None and collision["kind"] != "task_card":
                raise StateConflict("任务投影路径已被其他投影种类占用")
            previous = dict(manifest)
            connection.execute(
                """UPDATE task_projection_manifest
                   SET status='pending',sha256=NULL,projected_at=NULL
                   WHERE task_id=? AND authority_updated_at=?""",
                (task_id, expected_authority_updated_at),
            )
            if collision is not None:
                connection.execute(
                    "DELETE FROM projection_manifest WHERE path=? AND kind='task_card'",
                    (str(supplied_path),),
                )
            return previous

    def restore_task_projection_reservation(
        self, *, task_id: str, expected_authority_updated_at: str,
        previous: Mapping[str, Any],
    ) -> None:
        with self.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT updated_at FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if task is None or task["updated_at"] != expected_authority_updated_at:
                return
            connection.execute(
                """UPDATE task_projection_manifest
                   SET projection_kind=?,path=?,authority_updated_at=?,status=?,sha256=?,projected_at=?
                   WHERE task_id=?""",
                (
                    previous["projection_kind"], previous["path"],
                    previous["authority_updated_at"], previous["status"],
                    previous["sha256"], previous["projected_at"], task_id,
                ),
            )
            if previous.get("status") == "projected" and previous.get("path") and previous.get("sha256"):
                connection.execute(
                    """INSERT INTO projection_manifest(path,kind,sha256,rendered_at)
                       VALUES (?,'task_card',?,?)
                       ON CONFLICT(path) DO UPDATE SET kind='task_card',sha256=excluded.sha256,
                         rendered_at=excluded.rendered_at""",
                    (previous["path"], previous["sha256"], previous.get("projected_at") or _timestamp()),
                )

    def record_task_projection(
        self, *, task_id: str, path: str, sha256: str, workspace_root: str | Path,
        expected_authority_updated_at: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        stamp = _timestamp(at)
        resolved_path = str(Path(path).expanduser().resolve())
        resolved_root, bound_workspace_id = _validate_projection_workspace_binding(
            self, workspace_root,
        )
        try:
            Path(resolved_path).relative_to(resolved_root)
        except ValueError as exc:
            raise StateConflict("任务投影必须位于绑定的 workspace_root") from exc
        with self.transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            manifest = connection.execute(
                "SELECT * FROM task_projection_manifest WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or manifest is None:
                raise StateNotFound("任务或投影 manifest 不存在")
            if task["workspace_id"] != bound_workspace_id:
                raise StateConflict("任务 workspace_id 与 cutover workspace 绑定不一致")
            if (
                task["updated_at"] != expected_authority_updated_at
                or manifest["authority_updated_at"] != expected_authority_updated_at
            ):
                raise StateConflict("投影基于过期权威版本，拒绝登记")
            previous_path = str(manifest["path"] or "")
            if not previous_path:
                raise StateConflict("任务尚未冻结 card_path，拒绝登记投影")
            expected_path = Path(previous_path).expanduser().resolve()
            supplied_path = Path(path).expanduser().resolve()
            if supplied_path.stem != task_id:
                raise StateConflict("任务投影文件名必须与 owner task_id 一致")
            try:
                expected_path.relative_to(resolved_root)
            except ValueError as exc:
                raise StateConflict("冻结的 card_path 位于 workspace_root 外") from exc
            if supplied_path != expected_path:
                raise StateConflict("投影路径与任务冻结的 card_path 不一致")
            if not supplied_path.is_file():
                raise StateConflict("任务投影文件不存在")
            actual_digest = hashlib.sha256(supplied_path.read_bytes()).hexdigest()
            if sha256 != actual_digest:
                raise StateConflict("任务投影 Hash 与当前文件不一致")
            expected_bytes = render_task_card_projection(
                task_projection_view(self, _task_view(task), supplied_path)
            ).encode("utf-8")
            if supplied_path.read_bytes() != expected_bytes:
                raise StateConflict("任务投影内容不是当前权威状态的确定性渲染")
            for row in connection.execute(
                "SELECT task_id,path FROM task_projection_manifest WHERE task_id<>? AND path IS NOT NULL",
                (task_id,),
            ):
                if Path(str(row["path"])).expanduser().resolve() == supplied_path:
                    raise StateConflict(f"任务投影路径已被其他任务占用：{row['task_id']}")
            updated = connection.execute(
                """UPDATE task_projection_manifest
                   SET path=?, sha256=?, projected_at=?, status='projected'
                   WHERE task_id=? AND authority_updated_at=?""",
                (resolved_path, actual_digest, stamp, task_id, expected_authority_updated_at),
            )
            if updated.rowcount != 1:
                raise StateConflict("投影基于过期权威版本，拒绝登记")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projection_manifest (
                     path TEXT PRIMARY KEY,
                     kind TEXT NOT NULL,
                     sha256 TEXT NOT NULL,
                     rendered_at TEXT NOT NULL
                   )"""
            )
            collision = connection.execute(
                "SELECT kind FROM projection_manifest WHERE path=?", (resolved_path,)
            ).fetchone()
            if collision is not None and collision["kind"] != "task_card":
                raise StateConflict("任务投影路径已被其他投影种类占用")
            connection.execute(
                """INSERT INTO projection_manifest(path, kind, sha256, rendered_at)
                   VALUES (?, 'task_card', ?, ?)
                   ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,
                     sha256=excluded.sha256, rendered_at=excluded.rendered_at""",
                (resolved_path, actual_digest, stamp),
            )
            if previous_path and str(Path(previous_path).expanduser().resolve()) != resolved_path:
                connection.execute(
                    "DELETE FROM projection_manifest WHERE path=? AND kind='task_card'",
                    (str(Path(previous_path).expanduser().resolve()),),
                )
        return self.get_projection_manifest(task_id) or {}

    def apply_review_decision_atomically(
        self,
        *,
        user_event_id: str,
        focus_id: str,
        task_id: str,
        delivery_id: str,
        decision_receipt_id: str,
        decision: str,
        explicit_target: bool = False,
        reason: str | None = None,
        decided_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"accept", "request_changes"}:
            raise StateConflict(f"不支持的验收决定：{decision}")
        stamp = _timestamp(decided_at)
        with self.transaction(immediate=True) as connection:
            event = connection.execute(
                "SELECT * FROM user_events WHERE event_id = ?", (user_event_id,)
            ).fetchone()
            focus = connection.execute("SELECT * FROM review_focus WHERE focus_id = ?", (focus_id,)).fetchone()
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if None in (event, focus, task, delivery):
                raise StateNotFound("验收事件、焦点、任务或交付不存在")
            if event["consumed_at"] is not None or focus["status"] != "active":
                return {"applied": False, "reason": "already_consumed"}
            if _as_utc(event["expires_at"]) <= _as_utc(stamp) or _as_utc(focus["expires_at"]) <= _as_utc(stamp):
                raise ExpiredUserEvent("验收用户事件或展示焦点已过期")
            if focus["task_id"] != task_id or focus["delivery_id"] != delivery_id:
                raise StateConflict("验收焦点与显式任务/交付版本不一致")
            if delivery["task_id"] != task_id or delivery["submitted_at"] != focus["submitted_at"]:
                raise StateConflict("交付版本与焦点提交版本不一致")
            latest = connection.execute(
                "SELECT delivery_id FROM deliveries WHERE task_id=? ORDER BY submitted_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if latest is None or latest["delivery_id"] != delivery_id:
                raise StateConflict("旧交付版本不能覆盖当前交付")
            if task["review_status"] == "accepted" or delivery["status"] == "accepted":
                raise StateConflict("accepted 是终态，不能再次决定")
            if task["review_status"] not in {"submitted", "reviewing"} or delivery["status"] not in {"submitted", "reviewing"}:
                raise StateConflict("任务或交付当前不处于可验收状态")
            if event["workspace_id"] != task["workspace_id"]:
                raise StateConflict("验收事件与任务不属于同一 workspace")
            bindings = json.loads(event["bindings_json"])
            for key, actual in (("task_id", task_id), ("delivery_id", delivery_id), ("focus_id", focus_id)):
                if bindings.get(key) not in {None, actual}:
                    raise StateConflict(f"用户事件冻结的 {key} 与验收目标不一致")
            if event["session_id"] != focus["conversation_id"]:
                if bindings.get("task_id") != task_id or bindings.get("delivery_id") != delivery_id:
                    raise StateConflict("跨会话验收事件必须冻结精确 task_id 和 delivery_id")
                proof = bindings.get("review_target_reference")
                if not isinstance(proof, dict):
                    raise StateConflict("跨会话验收缺少来自用户原文的 target_reference 证明")
                core = {
                    "task_id": task_id, "delivery_id": delivery_id,
                    "target_reference": proof.get("target_reference"),
                    "source_prompt_sha256": event["prompt_sha256"],
                }
                if (
                    not core["target_reference"]
                    or proof != {**core, "reference_digest": _digest(core)}
                ):
                    raise StateConflict("跨会话 target_reference 证明无效")

            connection.execute(
                """INSERT INTO decision_receipts(
                       decision_receipt_id, user_event_id, focus_id, task_id,
                       delivery_id, decision, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision_receipt_id, user_event_id, focus_id, task_id, delivery_id, decision, reason, stamp),
            )
            event_update = connection.execute(
                """UPDATE user_events SET consumed_at = ?, consumed_by = 'review_decision'
                   WHERE event_id = ? AND consumed_at IS NULL AND expires_at > ?""",
                (stamp, user_event_id, stamp),
            )
            focus_update = connection.execute(
                """UPDATE review_focus SET status = 'consumed', consumed_at = ?
                   WHERE focus_id = ? AND status = 'active' AND expires_at > ?""",
                (stamp, focus_id, stamp),
            )
            if event_update.rowcount != 1 or focus_update.rowcount != 1:
                raise StateConflict("验收事件或焦点并发消费冲突")
            connection.execute(
                """UPDATE review_focus SET status='superseded', superseded_at=?
                   WHERE delivery_id=? AND focus_id<>? AND status='active'""",
                (stamp, delivery_id, focus_id),
            )
            if decision == "accept":
                lifecycle, runtime, review, delivery_status = "completed", "completed", "accepted", "accepted"
            else:
                lifecycle, runtime, review, delivery_status = (
                    "in_progress", "repairing", "changes_requested", "changes_requested"
                )
            connection.execute(
                """UPDATE tasks SET lifecycle_status = ?, runtime_status = ?,
                       review_status = ?, updated_at = ? WHERE task_id = ?""",
                (lifecycle, runtime, review, stamp, task_id),
            )
            connection.execute(
                "UPDATE deliveries SET status = ? WHERE delivery_id = ?",
                (delivery_status, delivery_id),
            )
            payload = {
                "decision_receipt_id": decision_receipt_id,
                "decision": decision,
                "task_id": task_id,
                "delivery_id": delivery_id,
            }
            self._append_event(
                connection,
                "review_decision_applied",
                payload,
                workspace_id=task["workspace_id"],
                session_id=event["session_id"],
                task_id=task_id,
                occurred_at=stamp,
            )
            connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id,
                       payload_json, available_at
                   ) VALUES (?, 'review_decision_applied', 'delivery', ?, ?, ?)""",
                (f"review-decision:{decision_receipt_id}", delivery_id, _json(payload), stamp),
            )
            return {"applied": True, **payload, "review_status": review}

    def apply_task_action_atomically(
        self,
        user_event_id: str,
        task_id: str,
        action: str,
        explicit_target: bool = False,
        reason: str | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if action not in {"cancel", "resubmit"}:
            raise StateConflict(f"不支持的任务动作：{action}")
        stamp = _timestamp(at)
        with self.transaction(immediate=True) as connection:
            event = connection.execute("SELECT * FROM user_events WHERE event_id = ?", (user_event_id,)).fetchone()
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if event is None or task is None:
                raise StateNotFound("用户事件或任务不存在")
            if event["consumed_at"] is not None:
                return {"applied": False, "reason": "already_consumed", "task_id": task_id, "action": action}
            if _as_utc(event["expires_at"]) <= _as_utc(stamp):
                raise ExpiredUserEvent(f"用户事件已过期：{user_event_id}")
            if event["workspace_id"] != task["workspace_id"]:
                raise StateConflict("用户事件与任务不属于同一 workspace")
            bindings = json.loads(event["bindings_json"])
            if bindings.get("task_id") not in {None, task_id}:
                raise StateConflict("用户事件冻结的 task_id 与任务动作目标不一致")
            if event["session_id"] != task["session_id"] and not explicit_target:
                raise StateConflict("跨会话任务动作必须显式指定 task_id")
            if action == "cancel" and task["review_status"] == "accepted":
                raise StateConflict("已验收任务不能取消")
            if action == "resubmit" and task["review_status"] != "changes_requested":
                raise StateConflict("只有 changes_requested 任务可以重新提交")

            consumed = connection.execute(
                """UPDATE user_events SET consumed_at = ?, consumed_by = ?
                   WHERE event_id = ? AND consumed_at IS NULL AND expires_at > ?""",
                (stamp, f"task_action:{action}", user_event_id, stamp),
            )
            if consumed.rowcount != 1:
                raise StateConflict("用户事件并发消费冲突")
            metadata = json.loads(task["metadata_json"])
            if action == "cancel":
                lifecycle, runtime, review = "canceled", "canceled", "canceled"
                active_stage = connection.execute(
                    """SELECT * FROM stage_runs WHERE task_id = ? AND status = 'active'
                       ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if active_stage is not None and active_stage["stage"] != "canceled":
                    connection.execute(
                        "UPDATE stage_runs SET status = 'completed', finished_at = ? WHERE stage_run_id = ?",
                        (stamp, active_stage["stage_run_id"]),
                    )
                    connection.execute(
                        """INSERT INTO stage_runs(
                               stage_run_id, task_id, stage, review_round, status,
                               started_at, details_json
                           ) VALUES (?, ?, 'canceled', ?, 'active', ?, ?)""",
                        (
                            _new_id("SR"),
                            task_id,
                            int(active_stage["review_round"]),
                            stamp,
                            _json({
                                "source_user_event_id": user_event_id,
                                "reason": reason or "用户明确取消",
                            }),
                        ),
                    )
                revoked_lease_ids = [
                    row["lease_id"] for row in connection.execute(
                        "SELECT lease_id FROM leases WHERE task_id = ? AND status = 'active'",
                        (task_id,),
                    ).fetchall()
                ]
                connection.execute(
                    "UPDATE leases SET status = 'revoked' WHERE task_id = ? AND status IN ('active','completed')",
                    (task_id,),
                )
                connection.execute(
                    """UPDATE review_focus SET status = 'superseded', superseded_at = ?
                       WHERE task_id = ? AND status = 'active'""",
                    (stamp, task_id),
                )
                connection.execute(
                    """UPDATE deliveries SET status = 'canceled'
                       WHERE task_id = ? AND status IN ('submitted', 'reviewing')""",
                    (task_id,),
                )
            else:
                lifecycle, runtime, review = "submitted", "submitted", "submitted"
                revoked_lease_ids = []
                metadata["submitted_at"] = stamp
                previous_delivery = connection.execute(
                    "SELECT * FROM deliveries WHERE task_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if previous_delivery is None:
                    raise StateConflict("重新提交需要一个既有交付版本")
                delivery_id = _new_id("DEL")
                connection.execute(
                    """INSERT INTO deliveries(
                           delivery_id, task_id, implementation_commit, implementation_tree,
                           tag_object, manifest_json, validation_summary,
                           adversarial_review_summary, status, submitted_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?)""",
                    (delivery_id, task_id, previous_delivery["implementation_commit"],
                     previous_delivery["implementation_tree"], previous_delivery["tag_object"],
                     previous_delivery["manifest_json"], previous_delivery["validation_summary"],
                     previous_delivery["adversarial_review_summary"], stamp),
                )
                metadata["delivery_id"] = delivery_id
                connection.execute(
                    """UPDATE review_focus SET status = 'superseded', superseded_at = ?
                       WHERE task_id = ? AND status = 'active'""",
                    (stamp, task_id),
                )
            connection.execute(
                """UPDATE tasks SET lifecycle_status = ?, runtime_status = ?, review_status = ?,
                       metadata_json = ?, updated_at = ? WHERE task_id = ?""",
                (lifecycle, runtime, review, _json(metadata), stamp, task_id),
            )
            additional_intents = sorted({
                str(value) for value in (bindings.get("additional_intents") or [])
                if str(value) in MAINTENANCE_INTENTS
            })
            successor_proposal_id = None
            if action == "cancel" and "continue_execution" in additional_intents:
                candidate_id = str(bindings.get("maintenance_proposal_id") or "")
                if candidate_id:
                    proposal = connection.execute(
                        "SELECT * FROM maintenance_proposals WHERE proposal_id = ?",
                        (candidate_id,),
                    ).fetchone()
                    if proposal is None or proposal["status"] != "pending":
                        raise StateConflict("继续执行所绑定的后继提案不存在或已失效")
                    if proposal["workspace_id"] != task["workspace_id"]:
                        raise StateConflict("后继提案与取消任务不属于同一 workspace")
                    disclosure = connection.execute(
                        "SELECT * FROM disclosures WHERE disclosure_id = ?",
                        (proposal["disclosure_id"],),
                    ).fetchone()
                    if disclosure is None or bindings.get("disclosure_id") != proposal["disclosure_id"]:
                        raise StateConflict("继续执行事件未冻结绑定后继范围展示")
                    if (
                        _as_utc(event["first_observed_at"]) < _as_utc(disclosure["displayed_at"])
                        or _as_utc(event["first_observed_at"]) < _as_utc(proposal["created_at"])
                    ):
                        raise StateConflict("继续执行用户事件早于后继范围展示")
                    cross_agent = (
                        event["session_id"] != proposal["session_id"]
                        or event["platform"] != proposal["platform"]
                    )
                    if cross_agent and bindings.get("explicit_proposal_reference") is not True:
                        raise StateConflict("跨 Agent 后继授权必须显式绑定不可变提案")
                    changed = connection.execute(
                        """UPDATE maintenance_proposals
                           SET status = 'authorized', authorized_by_event_id = ?,
                               authorized_at = ?, additional_intents_json = ?
                           WHERE proposal_id = ? AND status = 'pending' AND consumed_at IS NULL""",
                        (
                            user_event_id,
                            stamp,
                            _json(additional_intents),
                            candidate_id,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise StateConflict("后继提案并发授权冲突")
                    successor_proposal_id = candidate_id
                    successor_payload = {
                        "proposal_id": candidate_id,
                        "user_event_id": user_event_id,
                        "consumer_id": f"task_action:{action}",
                        "actor_verified": bool(proposal["actor_verified"]),
                        "disclosure_verified": bool(proposal["disclosure_verified"]),
                        "sequence_verified": bool(proposal["sequence_verified"]),
                        "enforcement_verified": bool(proposal["enforcement_verified"]),
                        "additional_intents": additional_intents,
                        "predecessor_task_id": task_id,
                    }
                    self._append_event(
                        connection,
                        "maintenance_authorized",
                        successor_payload,
                        workspace_id=task["workspace_id"],
                        session_id=event["session_id"],
                        occurred_at=stamp,
                    )
                    connection.execute(
                        """INSERT INTO outbox(
                               dedupe_key, event_type, aggregate_type, aggregate_id,
                               payload_json, available_at
                           ) VALUES (?, 'maintenance_authorized', 'maintenance_proposal', ?, ?, ?)""",
                        (
                            f"maintenance-authorized:{candidate_id}",
                            candidate_id,
                            _json(successor_payload),
                            stamp,
                        ),
                    )
            payload = {
                "task_id": task_id,
                "action": action,
                "reason": reason,
                "review_status": review,
                "revoked_lease_ids": revoked_lease_ids,
                "additional_intents": additional_intents,
                "successor_proposal_id": successor_proposal_id,
            }
            self._append_event(
                connection,
                "task_action_applied",
                payload,
                workspace_id=task["workspace_id"],
                session_id=event["session_id"],
                task_id=task_id,
                occurred_at=stamp,
            )
            connection.execute(
                """INSERT INTO outbox(
                       dedupe_key, event_type, aggregate_type, aggregate_id, payload_json, available_at
                   ) VALUES (?, 'task_action_applied', 'task', ?, ?, ?)""",
                (f"task-action:{user_event_id}", task_id, _json(payload), stamp),
            )
            return {"applied": True, **payload, "submitted_at": metadata.get("submitted_at"),
                    "delivery_id": metadata.get("delivery_id")}

    def export_events_jsonl(self, destination: str | Path) -> int:
        """Atomically overwrite a diagnostic JSONL projection from SQLite."""
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for item in rows:
                    record = {
                        "sequence": item["sequence"],
                        "event_id": item["event_id"],
                        "event": item["event_type"],
                        "ts": item["occurred_at"],
                        "workspace_id": item["workspace_id"],
                        "session_id": item["session_id"],
                        "task_id": item["task_id"],
                        "payload": json.loads(item["payload_json"]),
                    }
                    handle.write(_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return len(rows)

    @staticmethod
    def _integrity_check_path(path: Path) -> list[str]:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        finally:
            connection.close()

    def integrity_check(self) -> list[str]:
        return self._integrity_check_path(self.path)

    def backup(self, destination: str | Path) -> Path:
        """Create an online-consistent snapshot using sqlite3.Connection.backup."""
        destination = Path(destination).expanduser().resolve()
        if destination == self.path:
            raise StateConflict("备份路径不能与运行数据库相同")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(fd)
        os.unlink(temporary_raw)
        temporary = Path(temporary_raw)
        source = self.connect()
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.commit()
            result = [str(row[0]) for row in target.execute("PRAGMA integrity_check").fetchall()]
            if result != ["ok"]:
                raise StateError(f"备份完整性检查失败：{result}")
        finally:
            target.close()
            source.close()
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def restore_from_backup(self, backup_path: str | Path) -> None:
        """Restore through the SQLite backup API after validating the snapshot."""
        lock_path = Path(str(self.path) + ".cutover.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as cutover_lock:
            fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_EX)
            try:
                self._restore_from_backup_locked(backup_path)
            finally:
                fcntl.flock(cutover_lock.fileno(), fcntl.LOCK_UN)

    def _restore_from_backup_locked(self, backup_path: str | Path) -> None:
        backup_path = Path(backup_path).expanduser().resolve()
        if backup_path == self.path:
            raise StateConflict("恢复源不能是运行数据库本身")
        if self._integrity_check_path(backup_path) != ["ok"]:
            raise StateError("备份数据库完整性检查失败")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_raw = tempfile.mkstemp(prefix=f".{self.path.name}.restore.", dir=self.path.parent)
        os.close(fd)
        os.unlink(temporary_raw)
        temporary = Path(temporary_raw)
        source_uri = f"file:{backup_path.as_posix()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=5)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.commit()
            result = [str(row[0]) for row in target.execute("PRAGMA integrity_check").fetchall()]
            if result != ["ok"]:
                raise StateError(f"恢复副本完整性检查失败：{result}")
        finally:
            target.close()
            source.close()
        try:
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def refresh_events_projection(
    store: StateStore,
    *,
    workspace_root: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    """Atomically refresh the explicit one-way event projection from SQLite."""
    if not store.is_backend_active():
        raise StateConflict("SQLite backend 未激活；禁止刷新诊断投影")
    root, _ = _validate_projection_workspace_binding(
        store, workspace_root, allow_cutover_freeze=True,
    )
    if not root.is_dir():
        raise StateConflict(f"workspace root 不存在或不是目录：{root}")
    destination = Path(output).expanduser().resolve()
    runtime_root = store.path.parent.parent if store.path.parent.name == "state" else store.path.parent
    expected_destination = runtime_root / "events/events.jsonl"
    if destination != expected_destination.resolve():
        raise StateConflict("事件投影只能写入 cutover 绑定 runtime 的固定 events/events.jsonl")
    lock = store.path.resolve().with_suffix(store.path.suffix + ".projection.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    previous_manifest: dict[str, Any] | None = None
    existed = False
    previous_bytes = b""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existed = destination.is_file()
        previous_bytes = destination.read_bytes() if existed else b""
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projection_manifest (
                     path TEXT PRIMARY KEY,
                     kind TEXT NOT NULL,
                     sha256 TEXT NOT NULL,
                     rendered_at TEXT NOT NULL
                   )"""
            )
            task_manifest_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_projection_manifest'"
            ).fetchone()
            if task_manifest_exists is not None:
                for row in connection.execute(
                    "SELECT task_id,path FROM task_projection_manifest WHERE path IS NOT NULL"
                ):
                    if Path(str(row["path"])).expanduser().resolve() == destination:
                        raise StateConflict(f"事件投影路径已被任务卡占用：{row['task_id']}")
            existing = connection.execute(
                "SELECT * FROM projection_manifest WHERE path=?", (str(destination),),
            ).fetchone()
            if existing is not None and existing["kind"] != "events_jsonl":
                raise StateConflict("事件投影路径已被其他投影种类占用")
            previous_manifest = dict(existing) if existing is not None else None
            connection.execute(
                "DELETE FROM projection_manifest WHERE path=? AND kind='events_jsonl'",
                (str(destination),),
            )
        try:
            event_count = store.export_events_jsonl(destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            with store.transaction(immediate=True) as connection:
                collision = connection.execute(
                    "SELECT kind FROM projection_manifest WHERE path=?", (str(destination),),
                ).fetchone()
                if collision is not None:
                    raise StateConflict("事件投影登记期间目标所有权发生冲突")
                connection.execute(
                    """INSERT INTO projection_manifest(path, kind, sha256, rendered_at)
                       VALUES (?, 'events_jsonl', ?, ?)""",
                    (str(destination), digest, _timestamp()),
                )
        except Exception:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if existed:
                fd, rollback_raw = tempfile.mkstemp(
                    prefix=f".{destination.name}.rollback.", dir=destination.parent,
                )
                rollback = Path(rollback_raw)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(previous_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(rollback, destination)
                finally:
                    rollback.unlink(missing_ok=True)
            else:
                destination.unlink(missing_ok=True)
            if previous_manifest is not None:
                with store.transaction(immediate=True) as connection:
                    connection.execute(
                        """INSERT INTO projection_manifest(path,kind,sha256,rendered_at)
                           VALUES (?,?,?,?) ON CONFLICT(path) DO NOTHING""",
                        (
                            previous_manifest["path"], previous_manifest["kind"],
                            previous_manifest["sha256"], previous_manifest["rendered_at"],
                        ),
                    )
            raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return {
        "path": str(destination),
        "workspace_root": str(root),
        "sha256": digest,
        "event_count": event_count,
        "authority": "sqlite",
        "direction": "one_way_diagnostic_projection",
    }


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "ExpiredUserEvent",
    "SCHEMA_VERSION",
    "ScopeViolation",
    "StateConflict",
    "StateError",
    "StateNotFound",
    "StateStore",
    "canonical_execution_budget",
    "canonical_scope",
    "canonical_scopes",
    "refresh_events_projection",
    "scope_covers",
]
