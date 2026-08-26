#!/usr/bin/env python3
"""Deterministic one-way task-card projection from the authoritative StateStore."""

from __future__ import annotations

import hashlib
import fcntl
import os
import tempfile
from pathlib import Path
from xirang_state import (
    StateConflict,
    StateStore,
    render_task_card_projection,
    task_projection_view,
)


render_task_card = render_task_card_projection


def write_task_card_projection(
    store: StateStore,
    *,
    workspace_root: Path,
    task_id: str,
) -> dict[str, str]:
    task = store.get_task(task_id)
    if task is None:
        raise StateConflict(f"任务不存在：{task_id}")
    raw_path = task.get("card_path")
    if not raw_path:
        raise StateConflict(f"任务缺少 card_path：{task_id}")
    root = workspace_root.expanduser().absolute()
    path = Path(str(raw_path)).expanduser()
    path = path.absolute() if path.is_absolute() else (root / path).absolute()
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise StateConflict(f"任务卡必须位于当前 workspace：{path}") from exc
    lock = Path(str(store.path) + ".projection.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock_descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        previous_manifest = store.prepare_task_projection(
            task_id=task_id, path=str(path), workspace_root=root,
            expected_authority_updated_at=str(task["updated_at"]),
        )
        existed = path.is_file()
        previous_bytes = path.read_bytes() if existed else b""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = render_task_card(task_projection_view(store, task, path))
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            store.record_task_projection(
                task_id=task_id, path=str(path), sha256=digest, workspace_root=root,
                expected_authority_updated_at=str(task["updated_at"]),
            )
        except Exception:
            rollback_descriptor, rollback_raw = tempfile.mkstemp(
                prefix=f".{path.name}.rollback.", dir=path.parent,
            )
            rollback_path = Path(rollback_raw)
            try:
                if existed:
                    with os.fdopen(rollback_descriptor, "wb") as handle:
                        handle.write(previous_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(rollback_path, path)
                else:
                    os.close(rollback_descriptor)
                    rollback_path.unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                store.restore_task_projection_reservation(
                    task_id=task_id,
                    expected_authority_updated_at=str(task["updated_at"]),
                    previous=previous_manifest,
                )
            finally:
                rollback_path.unlink(missing_ok=True)
            raise
        finally:
            temporary_path.unlink(missing_ok=True)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    return {"path": str(path), "sha256": digest}
