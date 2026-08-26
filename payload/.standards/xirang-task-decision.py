#!/usr/bin/env python3
"""Consume an explicit UserPromptSubmit decision and update one task."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xirang_state import StateConflict, StateStore, refresh_events_projection
from xirang_state_cli import cutover_guarded, probe_backend, sqlite_authority_artifacts_present
from xirang_task_projection import write_task_card_projection


ACCEPT_RE = re.compile(r"^接受本次交付(?:\s+(T-[A-Za-z0-9_-]+))?$")
RETURN_RE = re.compile(r"^退回修改[：:]\s*(.+)$")
CANCEL_RE = re.compile(r"^取消本次任务(?:\s+(T-[A-Za-z0-9_-]+))?$")
RESUBMIT_RE = re.compile(r"^重新提交本次交付(?:\s+(T-[A-Za-z0-9_-]+))?$")


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()[:12]


def runtime_dir(root: Path) -> Path:
    try:
        value = json.loads((root / ".xirang/local-config.json").read_text(encoding="utf-8")).get("runtime_dir")
    except (OSError, json.JSONDecodeError, TypeError):
        value = None
    return Path(value).expanduser() if value else Path.home() / ".xirang/workspaces" / workspace_id(root)


def active_state_store(root: Path) -> StateStore | None:
    path = runtime_dir(root) / "state" / "state.sqlite3"
    if not path.exists():
        if sqlite_authority_artifacts_present(path):
            raise RuntimeError("SQLite authority artifacts exist but database is missing; legacy fallback denied")
        return None
    probe = probe_backend(root, path)
    if probe.active is False:
        return None
    if probe.active is not True:
        raise RuntimeError(f"SQLite authority unavailable; legacy fallback denied: {probe.reason}")
    return StateStore(path)


def project_task_from_state(store: StateStore, task_id: str, workspace_root: Path) -> None:
    task = store.get_task(task_id)
    if task is None or not task.get("card_path"):
        return
    try:
        write_task_card_projection(store, workspace_root=workspace_root, task_id=task_id)
    except Exception as exc:
        store.set_task_metadata(task_id, {"projection_degraded": True})
        store.enqueue_outbox(
            dedupe_key=f"task-decision-projection:{task_id}:{task['updated_at']}",
            event_type="projection_degraded", aggregate_type="task", aggregate_id=task_id,
            payload={"authority_committed": True, "error": str(exc)},
        )


def apply_from_state(
    root: Path,
    store: StateStore,
    session_id: str,
    raw_text: str,
    event_id: str,
    proposed_action: str,
    proposed_reason: str,
    bound_task_id: str,
    bound_delivery_id: str,
    bound_focus_id: str,
    explicit_target: bool,
) -> dict:
    if not event_id:
        raise PermissionError("决定必须绑定已登记的当前用户事件")
    if proposed_action:
        if proposed_action not in {"accept", "request_changes", "cancel", "resubmit",
                                   "set_interaction_preference", "clear_interaction_preference"}:
            raise ValueError("模型动作提议不在息壤允许的状态迁移集合内")
        kind, embedded, reason = proposed_action, bound_task_id or None, proposed_reason or None
    else:
        kind, embedded, reason = decision_kind(raw_text)
    if kind == "no_decision":
        return {"ok": True, "decision_applied": False, "reason": "no_explicit_decision"}
    if kind == "set_interaction_preference":
        task_id = bound_task_id or str((store.get_user_event(event_id) or {}).get("bindings", {}).get("task_id") or "")
        if not task_id:
            raise PermissionError("交互偏好必须绑定当前执行包络")
        value = store.reconcile_interaction_preference_history(
            task_id=task_id, user_event_id=event_id,
        )
        return {"ok": True, "decision_applied": True, "decision": kind, "preference": value}
    if kind == "clear_interaction_preference":
        cleared = store.clear_interaction_preference(
            workspace_id=workspace_id(root), user_scope="default", source_user_event_id=event_id,
        )
        return {"ok": True, "decision_applied": True, "decision": kind, "cleared": cleared}

    task_id = bound_task_id or embedded or ""
    if not task_id:
        if kind in {"accept", "request_changes"}:
            candidates = store.list_tasks(session_id=session_id, review_statuses=["submitted", "reviewing"])
        else:
            candidates = [row for row in store.list_tasks(session_id=session_id)
                          if row["lifecycle_status"] not in {"completed", "canceled"}]
        if len(candidates) != 1:
            raise StateConflict("当前会话无法唯一确定目标任务")
        task_id = candidates[0]["task_id"]
    task = store.get_task(task_id)
    if task is None:
        raise StateConflict(f"任务不存在：{task_id}")
    cross_session = task["session_id"] != session_id
    if cross_session:
        event = store.get_user_event(event_id) or {}
        bindings = event.get("bindings") or {}
        proof = bindings.get("review_target_reference")
        if (
            bindings.get("task_id") != task_id
            or bindings.get("delivery_id") != bound_delivery_id
            or not isinstance(proof, dict)
        ):
            raise PermissionError("跨会话决定必须绑定用户原文冻结的 task_id/delivery_id 引用证明")
    if kind in {"cancel", "resubmit"}:
        result = store.apply_task_action_atomically(
            event_id, task_id, kind, explicit_target=explicit_target, reason=reason,
        )
        if result.get("applied"):
            project_task_from_state(store, task_id, root)
        return {"ok": True, "decision_applied": bool(result.get("applied")), "decision": kind,
                "task_id": task_id, **result}

    delivery = store.get_delivery(bound_delivery_id) if bound_delivery_id else store.get_latest_delivery(task_id)
    if delivery is None or delivery["task_id"] != task_id:
        raise StateConflict("决定未绑定任务的当前交付版本")
    if cross_session and not bound_delivery_id:
        raise PermissionError("跨会话验收必须显式指定 delivery_id")
    focus = store.get_review_focus(bound_focus_id) if bound_focus_id else store.get_active_review_focus(session_id)
    if focus is None:
        focus_id = f"F-{uuid.uuid4().hex[:16]}"
        store.create_review_focus(
            focus_id=focus_id, task_id=task_id, delivery_id=delivery["delivery_id"],
            conversation_id=session_id, presented_at=now(), ttl_seconds=600,
        )
    else:
        focus_id = focus["focus_id"]
    if kind == "accept":
        proc = subprocess.run(
            [sys.executable, str(root / ".standards/v9-accept.py"), task_id,
             "--root", str(root), "--event-id", event_id, "--focus-id", focus_id,
             "--delivery-id", delivery["delivery_id"], "--json",
             *(["--explicit-target"] if explicit_target else [])],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout or proc.stderr).strip())
        result = json.loads(proc.stdout)
    else:
        result = store.apply_review_decision_atomically(
            user_event_id=event_id, focus_id=focus_id, task_id=task_id,
            delivery_id=delivery["delivery_id"], decision_receipt_id=f"DR-{uuid.uuid4().hex[:16]}",
            decision="request_changes", explicit_target=explicit_target,
            reason=validate_reason(reason or "用户明确要求修改；详细原因待补充"),
        )
        result = {"ok": True, "decision_applied": bool(result.get("applied")),
                  "decision": kind, "task_id": task_id, **result}
    if result.get("decision_applied") or result.get("applied"):
        project_task_from_state(store, task_id, root)
    return result


def clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def split_frontmatter(text: str) -> tuple[list[str], str]:
    if not text.startswith("---\n"):
        raise ValueError("任务卡缺少 frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("任务卡 frontmatter 未闭合")
    return text[4:end].splitlines(), text[end:]


def fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in split_frontmatter(text)[0]:
        if match := re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line):
            result[match.group(1)] = clean(match.group(2))
    return result


def encode(value: object) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def set_fields(text: str, updates: dict[str, object]) -> str:
    lines, rest = split_frontmatter(text)
    values = {key: encode(value) for key, value in updates.items()}
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split(":", 1)[0] if ":" in line else ""
        if key in values and line.startswith(f"{key}:"):
            output.append(f"{key}: {values[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}: {value}" for key, value in values.items() if key not in seen)
    return "---\n" + "\n".join(output) + rest


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


@contextmanager
def task_lock(path: Path):
    """Serialize decision application per task card via an O_EXCL lock file.

    A single delivery must never be closed twice concurrently: two decisions
    racing on the same card could double-apply or clobber each other. The lock
    is exclusive-create and always removed on exit.
    """
    lock = path.with_suffix(path.suffix + ".decision-lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"该交付正在被另一处理占用，请稍后重试：{path.name}") from exc
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def task_cards(root: Path) -> list[tuple[Path, dict[str, str]]]:
    result: list[tuple[Path, dict[str, str]]] = []
    for base in (root / ".xirang/tasks", root / "02-项目管理/任务卡"):
        if not base.exists():
            continue
        for path in sorted(base.glob("**/T-*.md")):
            try:
                result.append((path, fields(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return result


def decision_kind(text: str) -> tuple[str, str | None, str | None]:
    """Parse only the first non-empty standalone line.

    Later paragraphs remain ordinary conversation. We deliberately avoid
    scanning the whole prompt for action words: quoted examples, corrections,
    and unrelated approvals must never close a delivery.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no_decision", None, None
    value = lines[0]
    if "---" in text or any(re.match(r"^(review_status|status|accepted_by|accepted_at|acceptance_result):", line) for line in lines[1:]):
        raise ValueError("决定尚未应用；后续正文包含受保护的任务状态字段")
    if any(ACCEPT_RE.fullmatch(line) or RETURN_RE.fullmatch(line) or CANCEL_RE.fullmatch(line) for line in lines[1:]):
        raise ValueError("决定尚未应用；同一条消息包含多个决定")
    if value.startswith((">", "- ", "* ", "```")) or any(ch in value for ch in ("\x00", "\u200b", "\u202e")):
        return "no_decision", None, None
    if match := ACCEPT_RE.fullmatch(value):
        return "accept", match.group(1), None
    natural = value.rstrip("。！!")
    first_clause, separator, remainder = natural.partition("，")
    if not separator:
        first_clause, separator, remainder = natural.partition(",")
    revoked = any(token in remainder for token in ("不验收", "不要收口", "暂不", "先别", "取消验收", "只是测试"))
    if natural in {"接受", "验收", "验收通过"} or (first_clause in {"验收", "验收通过"} and remainder and not revoked):
        return "accept", None, None
    if match := RETURN_RE.fullmatch(value):
        reason = match.group(1).strip()
        embedded = None
        if task_match := re.fullmatch(r"(.+?)\s+(T-[A-Za-z0-9_-]+)", reason):
            reason, embedded = task_match.group(1).strip(), task_match.group(2)
        return "request_changes", embedded, reason
    if natural in {"不通过", "退回"}:
        return "request_changes", None, "用户明确退回；详细原因待补充"
    if value.startswith(("接受", "验收", "退回修改：", "退回修改:")):
        raise ValueError("决定尚未应用；请把接受或退回决定单独放在第一行")
    if match := CANCEL_RE.fullmatch(value):
        return "cancel", match.group(1), None
    if match := RESUBMIT_RE.fullmatch(value):
        return "resubmit", match.group(1), None
    return "no_decision", None, None


def validate_reason(value: str) -> str:
    reason = value.strip()
    if not reason or "\n" in reason or "\r" in reason or "---" in reason or len(reason) > 500:
        raise ValueError("退回原因必须是 1–500 字的单行文本，且不能包含 frontmatter 分隔符")
    return reason


def choose_task(root: Path, kind: str, embedded: str | None, session_id: str) -> tuple[Path, dict[str, str]]:
    rows = task_cards(root)
    if embedded:
        matches = [row for row in rows if row[1].get("task_id", row[0].stem) == embedded]
    else:
        allowed_reviews = {"submitted", "reviewing"} if kind in {"accept", "request_changes"} else {"draft", "changes_requested", "submitted", "reviewing"}
        matches = [row for row in rows if row[1].get("review_status") in allowed_reviews and session_id and row[1].get("session_id") == session_id]
    if len(matches) != 1:
        if not matches:
            raise ValueError("当前对话没有正在确认的交付，因此没有改变任何任务状态")
        titles = "、".join(row[1].get("title") or "未命名任务" for row in matches[:3])
        raise ValueError(f"当前对话有多项交付等待确认，因此没有自动选择：{titles}")
    return matches[0]


def receipt_body(payload: dict) -> bytes:
    return json.dumps({key: payload[key] for key in sorted(payload) if key != "signature"}, ensure_ascii=False, separators=(",", ":")).encode()


def create_receipt(root: Path, kind: str, task_id: str, session_id: str, platform: str, text: str, event_id: str = "") -> Path:
    runtime = runtime_dir(root)
    secret_path = runtime / "secret.key"
    if not secret_path.is_file():
        raise RuntimeError("息壤本机密钥不存在；请重新运行 setup.sh")
    payload = {
        "schema_version": 1, "receipt_id": uuid.uuid4().hex, "action": kind, "task_id": task_id,
        "actor": "user", "session_id": session_id, "platform": platform,
        "actor_verified": False, "authorization_source": "manual_guard_forwarded",
        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "source_event_id": event_id or None, "created_at": now_iso(),
        "expires_at": (now() + timedelta(minutes=10)).isoformat(timespec="seconds"), "consumed_at": None,
    }
    secret = secret_path.read_text(encoding="utf-8").strip().encode()
    payload["signature"] = hmac.new(secret, receipt_body(payload), hashlib.sha256).hexdigest()
    path = runtime / "receipts" / f"decision-{payload['receipt_id']}.json"
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def abort_receipt(root: Path, path: Path, message: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["aborted_at"] = now_iso()
        payload["abort_reason"] = message[:500]
        secret = (runtime_dir(root) / "secret.key").read_text(encoding="utf-8").strip().encode()
        payload["signature"] = hmac.new(secret, receipt_body(payload), hashlib.sha256).hexdigest()
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass


def refresh(root: Path) -> tuple[bool, str | None]:
    proc = subprocess.run([sys.executable, str(root / ".standards/xirang-user-status.py"), "--root", str(root), "--write", "--trigger", "decision"], capture_output=True, text=True, check=False)
    return proc.returncode == 0, None if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()[-800:]


@cutover_guarded("xirang-task-decision")
def apply_from_hook(root: Path, session_id: str, platform: str, raw_text: str, event_id: str = "",
                    proposed_action: str = "", proposed_reason: str = "", bound_task_id: str = "",
                    bound_submitted_at: str = "", bound_delivery_id: str = "",
                    bound_focus_id: str = "", explicit_target: bool = False) -> dict:
    store = active_state_store(root)
    if store is not None:
        return apply_from_state(
            root, store, session_id, raw_text, event_id, proposed_action, proposed_reason,
            bound_task_id, bound_delivery_id, bound_focus_id, explicit_target,
        )
    if proposed_action:
        if proposed_action not in {"accept", "request_changes", "cancel", "resubmit"}:
            raise ValueError("模型动作提议不在息壤允许的状态迁移集合内")
        kind, embedded, reason = proposed_action, bound_task_id or None, proposed_reason or None
        if kind == "request_changes":
            reason = validate_reason(reason or "用户明确要求修改；详细原因待补充")
    else:
        kind, embedded, reason = decision_kind(raw_text)
    if kind == "no_decision":
        return {"ok": True, "decision_applied": False, "reason": "no_explicit_decision"}
    if os.environ.get("XIRANG_USER_PROMPT_HOOK") != "1":
        raise PermissionError("决定只能由 UserPromptSubmit Hook 提交")
    if event_id:
        for existing in (runtime_dir(root) / "receipts").glob("decision-*.json"):
            try:
                previous = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if previous.get("source_event_id") == event_id:
                if previous.get("prompt_sha256") != hashlib.sha256(raw_text.encode()).hexdigest() or previous.get("session_id") != session_id:
                    raise PermissionError("重复事件内容不一致，已拒绝处理")
                return {"ok": True, "decision_applied": bool(previous.get("consumed_at")), "decision": previous.get("action"), "task_id": previous.get("task_id"), "idempotent_replay": True}
    path, data = choose_task(root, kind, embedded, session_id)
    with task_lock(path):
        if proposed_action and bound_submitted_at != data.get("submitted_at", ""):
            raise PermissionError("模型动作提议绑定的交付版本已经变化")
        task_id = data.get("task_id") or path.stem
        receipt = create_receipt(root, kind, task_id, session_id, platform, raw_text, event_id)
        if kind == "accept":
            proc = subprocess.run([sys.executable, str(root / ".standards/v9-accept.py"), task_id, "--receipt", str(receipt), "--root", str(root), "--json"], capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                message = (proc.stdout or proc.stderr).strip()
                abort_receipt(root, receipt, message)
                raise RuntimeError(message)
            result = json.loads(proc.stdout)
        else:
            old = path.read_text(encoding="utf-8")
            review, status = data.get("review_status", ""), data.get("status", "")
            if kind == "request_changes":
                if review not in {"submitted", "reviewing"}:
                    raise ValueError(f"当前 review_status={review or '<missing>'}，不能退回")
                updates = {"status": "in_progress", "review_status": "changes_requested", "acceptance_result": "changes_requested",
                           "acceptance_note": validate_reason(reason or ""), "accepted_by": None, "accepted_at": None, "updated_at": now_iso()}
            elif kind == "cancel":
                if review == "accepted":
                    raise ValueError("已验收任务不能取消")
                updates = {"status": "cancelled", "review_status": "cancelled", "acceptance_result": "cancelled",
                           "acceptance_note": "用户明确取消", "accepted_by": None, "accepted_at": None, "updated_at": now_iso()}
            else:
                if review != "changes_requested" or status == "cancelled":
                    raise ValueError(f"当前状态不能重新提交：status={status}, review_status={review}")
                try:
                    count = int(data.get("resubmit_count") or 0) + 1
                except ValueError:
                    count = 1
                updates = {"status": "submitted", "review_status": "submitted", "submitted_at": now_iso(),
                           "resubmit_count": count, "acceptance_result": None, "acceptance_note": None, "updated_at": now_iso()}
            atomic_write(path, set_fields(old, updates))
            consumed = json.loads(receipt.read_text(encoding="utf-8"))
            consumed["consumed_at"] = now_iso()
            secret = (runtime_dir(root) / "secret.key").read_text(encoding="utf-8").strip().encode()
            consumed["signature"] = hmac.new(secret, receipt_body(consumed), hashlib.sha256).hexdigest()
            atomic_write(receipt, json.dumps(consumed, ensure_ascii=False, indent=2) + "\n")
            result = {"ok": True, "decision_applied": True, "decision": kind, "task_id": task_id}
    refreshed, error = refresh(root)
    result.update({"status_refresh": refreshed, "status_refresh_error": error})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-user-prompt", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--platform", default="unknown")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--text", required=True)
    parser.add_argument("--proposed-action", default="")
    parser.add_argument("--proposed-reason", default="")
    parser.add_argument("--bound-task-id", default="")
    parser.add_argument("--bound-submitted-at", default="")
    parser.add_argument("--bound-delivery-id", default="")
    parser.add_argument("--bound-focus-id", default="")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--explicit-target", action="store_true")
    target_group.add_argument("--implicit-target", action="store_true")
    args = parser.parse_args()
    try:
        if not args.from_user_prompt:
            raise PermissionError("只允许 UserPromptSubmit 入口")
        root = args.root.expanduser().resolve()
        result = apply_from_hook(root, args.session_id, args.platform, args.text,
                                 args.event_id, args.proposed_action, args.proposed_reason, args.bound_task_id,
                                 args.bound_submitted_at, args.bound_delivery_id, args.bound_focus_id,
                                 args.explicit_target)
        store = active_state_store(root)
        if result.get("decision_applied") and store is not None:
            refresh_events_projection(
                store,
                workspace_root=root,
                output=runtime_dir(root) / "events/events.jsonl",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "decision_applied": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
