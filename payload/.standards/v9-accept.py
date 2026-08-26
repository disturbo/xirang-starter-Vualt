#!/usr/bin/env python3
"""Internal acceptance backend. A signed UserPromptSubmit receipt is mandatory."""

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
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from xirang_state import StateConflict, StateStore, refresh_events_projection, scope_covers
from xirang_task_projection import write_task_card_projection
from xirang_recovery_roots import RecoveryRootError, load_registry, require_registered


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
    from xirang_state_cli import sqlite_authority_artifacts_present

    path = runtime_dir(root) / "state" / "state.sqlite3"
    if not path.exists():
        if sqlite_authority_artifacts_present(path):
            raise RuntimeError("SQLite 权威状态目录存在但数据库缺失；拒绝回退 legacy backend")
        return None
    store = StateStore(path)
    try:
        if store.is_backend_active():
            return store
    except Exception as exc:
        raise RuntimeError("SQLite 权威状态数据库不可读；拒绝回退 legacy backend") from exc
    raise RuntimeError("SQLite 权威状态数据库存在但未激活；拒绝回退 legacy backend")


def verify_delivery_preimage(manifest_path: Path) -> dict:
    manifest_path = manifest_path.expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateConflict("交付 pre-image manifest 不可读") from exc
    object_path = Path(str(manifest.get("object") or "")).expanduser().resolve()
    expected_sha = str(manifest.get("sha256") or "")
    if (
        manifest.get("artifact_type") != "file_preimage"
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or not object_path.is_file() or object_path.is_symlink()
        or object_path.name != expected_sha
        or hashlib.sha256(object_path.read_bytes()).hexdigest() != expected_sha
    ):
        raise StateConflict("交付 pre-image 对象缺失或哈希漂移")
    return {**manifest, "manifest": str(manifest_path)}


def verify_annotated_tag_identity(root: Path, delivery: dict) -> None:
    commit = str(delivery.get("implementation_commit") or "")
    tree = str(delivery.get("implementation_tree") or "")
    tag_object = str(delivery.get("tag_object") or "")
    if not commit:
        raise StateConflict("文件交付缺少 implementation commit")
    if not tree:
        raise StateConflict("文件交付缺少 implementation tree")
    if not tag_object:
        raise StateConflict("交付缺少 annotated tag identity")
    kind = subprocess.run(
        ["git", "cat-file", "-t", tag_object], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if kind.returncode != 0 or kind.stdout.strip() != "tag":
        raise StateConflict("交付 tag_object 不是 annotated tag object")
    peeled = subprocess.run(
        ["git", "rev-parse", f"{tag_object}^{{commit}}"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if peeled.returncode != 0 or peeled.stdout.strip() != commit:
        raise StateConflict("交付 annotated tag 未绑定登记的 implementation commit")
    resolved_tree = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{tree}}"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if resolved_tree.returncode != 0 or resolved_tree.stdout.strip() != tree:
        raise StateConflict("交付 implementation tree 与 commit 不一致")
    tag_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/xirang/submitted/{delivery['delivery_id']}^{{tag}}"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if tag_ref.returncode != 0 or tag_ref.stdout.strip() != tag_object:
        raise StateConflict("交付 annotated tag ref 与登记对象不一致")
    raw = subprocess.run(
        ["git", "cat-file", "-p", tag_object], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if raw.returncode != 0:
        raise StateConflict("交付 annotated tag payload 不可读")
    _headers, separator, body = raw.stdout.partition("\n\n")
    body = body.rstrip("\n")
    try:
        payload = json.loads(body) if separator else None
    except json.JSONDecodeError as exc:
        raise StateConflict("交付 annotated tag payload 不是有效 JSON") from exc
    expected = {
        "kind": "xirang_controlled_delivery_manifest",
        "schema_version": 1,
        "delivery_id": delivery["delivery_id"],
        "task_id": delivery["task_id"],
        "implementation_commit": commit,
        "implementation_tree": tree,
        "manifest": delivery.get("manifest") or [],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not isinstance(payload, dict) or canonical != body or payload != expected:
        raise StateConflict("交付 annotated tag payload 与数据库登记不一致")


def verify_no_git_delivery_identity(root: Path, delivery: dict) -> None:
    """Verify an immutable receipt/pre-image manifest without claiming Git recovery."""
    if any(delivery.get(key) for key in (
        "implementation_commit", "implementation_tree", "tag_object",
    )):
        raise StateConflict("files_no_git 交付不得携带 commit、tree 或 tag 身份")
    manifest = delivery.get("manifest") or []
    if not manifest:
        raise StateConflict("files_no_git 交付缺少不可变逐文件 manifest")
    core = [
        {key: value for key, value in item.items() if key != "no_git_manifest_sha256"}
        for item in manifest
    ]
    manifest_sha = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    tag_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/xirang/submitted/{delivery['delivery_id']}"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if tag_ref.returncode == 0:
        raise StateConflict("files_no_git 交付不得存在 submitted Git tag")
    try:
        registry = load_registry(root / ".xirang/contract/recovery-roots.yaml")
    except (OSError, RecoveryRootError) as exc:
        raise StateConflict("files_no_git 交付缺少登记恢复根") from exc
    seen: set[str] = set()
    for item in manifest:
        path_value = str(item.get("path") or "")
        if not path_value or path_value in seen or Path(path_value).is_absolute():
            raise StateConflict("files_no_git manifest 路径缺失、重复或不是相对路径")
        seen.add(path_value)
        if (
            item.get("delivery_mode") != "no_git"
            or item.get("git_effect") is not False
            or item.get("evidence_only") is not False
            or item.get("git_recovery_available") is not False
            or item.get("no_git_manifest_sha256") != manifest_sha
        ):
            raise StateConflict(f"files_no_git manifest 冒充 Git/evidence-only 能力：{path_value}")
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path_value],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if tracked.returncode != 0:
            raise StateConflict(f"files_no_git manifest 不是已跟踪文件：{path_value}")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", path_value], cwd=root, check=False,
        )
        unstaged = subprocess.run(
            ["git", "diff", "--quiet", "--", path_value], cwd=root, check=False,
        )
        if staged.returncode != 0 or unstaged.returncode != 1:
            raise StateConflict(f"files_no_git 验收时必须仍是未暂存工作树变更：{path_value}")
        recovery_path = Path(str(item.get("recovery_manifest") or "")).expanduser().resolve()
        try:
            require_registered(recovery_path, registry, kind="manifests")
            recovery_bytes = recovery_path.read_bytes()
            recovery = verify_delivery_preimage(recovery_path)
            require_registered(
                Path(str(recovery.get("object") or "")), registry, kind="objects",
            )
        except Exception as exc:
            raise StateConflict(f"files_no_git pre-image 缺失或漂移：{path_value}") from exc
        if (
            recovery.get("logical_path") != path_value
            or recovery.get("sha256") != item.get("preimage_sha256")
            or hashlib.sha256(recovery_bytes).hexdigest() != item.get("recovery_manifest_sha256")
        ):
            raise StateConflict(f"files_no_git pre-image 与 manifest 不一致：{path_value}")


def accept_from_state(
    root: Path,
    store: StateStore,
    task_id: str,
    *,
    user_event_id: str,
    focus_id: str,
    delivery_id: str,
    explicit_target: bool = False,
) -> dict:
    task = store.get_task(task_id)
    delivery = store.get_delivery(delivery_id)
    if task is None or delivery is None or delivery["task_id"] != task_id:
        raise StateConflict("任务或交付版本不存在")
    if task["review_status"] not in {"submitted", "reviewing"}:
        raise StateConflict(f"当前 review_status={task['review_status']}，不能验收")
    receipts = {row["receipt_id"]: row for row in store.list_effective_write_receipts(task_id)}
    manifest = delivery.get("manifest") or []
    delivery_mode = str(task.get("delivery_mode") or "files")
    if delivery_mode != "chat" and not manifest:
        raise StateConflict("文件交付缺少数据库 manifest")
    for item in manifest:
        receipt_id = str(item.get("receipt_id") or "")
        receipt = receipts.get(receipt_id)
        if receipt is None:
            raise StateConflict(f"交付引用的有效写入收据不存在：{receipt_id}")
        path_value = str(item.get("path") or "")
        if (receipt["path"] != path_value or receipt["sha256"] != item.get("sha256")
                or bool(receipt.get("exists_after")) != bool(item.get("exists_after"))):
            raise StateConflict(f"交付 manifest 与写入收据不一致：{path_value}")
        if not any(scope_covers(root_value, path_value) for root_value in task["allowed_write_roots"]):
            raise StateConflict(f"交付路径超出任务授权根：{path_value}")
        path = Path(path_value) if Path(path_value).is_absolute() else root / path_value
        if bool(item.get("exists_after")) != path.exists():
            raise StateConflict(f"交付文件存在性漂移：{path_value}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if digest != receipt["sha256"]:
            raise StateConflict(f"交付文件哈希漂移：{path_value}")
        if not bool(item.get("exists_after")) and not bool(item.get("git_effect")):
            try:
                recovery = verify_delivery_preimage(Path(str(item.get("recovery_manifest") or "")))
            except Exception as exc:
                raise StateConflict(f"交付删除项 pre-image 缺失或漂移：{path_value}") from exc
            if (
                recovery.get("logical_path") != path_value
                or recovery.get("sha256") != item.get("preimage_sha256")
            ):
                raise StateConflict(f"交付删除项 pre-image 绑定不一致：{path_value}")
    commit = delivery.get("implementation_commit")
    if delivery_mode == "files":
        verify_annotated_tag_identity(root, delivery)
        exists = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=root,
                                capture_output=True, text=True, check=False)
        if exists.returncode != 0:
            raise StateConflict("交付 Git commit 已不存在")
        tree = subprocess.run(["git", "rev-parse", f"{commit}^{{tree}}"], cwd=root,
                              capture_output=True, text=True, check=False)
        if tree.returncode != 0 or tree.stdout.strip() != (delivery.get("implementation_tree") or ""):
            raise StateConflict("交付 Git tree 与数据库记录不一致")
        for item in manifest:
            path_value = str(item.get("path") or "")
            if Path(path_value).is_absolute() or not bool(item.get("git_effect")):
                continue
            entry = subprocess.run(
                ["git", "ls-tree", "-z", commit, "--", path_value], cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            rows = entry.stdout.rstrip(b"\0").split(b"\0") if entry.stdout else []
            if not bool(item.get("exists_after")):
                if entry.returncode != 0 or rows:
                    raise StateConflict(f"交付 commit 未落实删除：{path_value}")
                continue
            if entry.returncode != 0 or len(rows) != 1 or b"\t" not in rows[0]:
                raise StateConflict(f"交付 commit 不包含唯一文件：{path_value}")
            metadata, stored_path = rows[0].split(b"\t", 1)
            parts = metadata.split()
            if len(parts) != 3 or parts[1] != b"blob" or stored_path.decode("utf-8") != path_value:
                raise StateConflict(f"交付 commit 条目不是精确 blob：{path_value}")
            blob = subprocess.run(
                ["git", "cat-file", "blob", parts[2].decode("ascii")], cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != item.get("sha256"):
                raise StateConflict(f"交付 commit blob 与 manifest 不一致：{path_value}")
    elif delivery_mode == "files_no_git":
        verify_no_git_delivery_identity(root, delivery)
    elif delivery_mode != "chat":
        raise StateConflict(f"未知 delivery_mode：{delivery_mode}")
    result = store.apply_review_decision_atomically(
        user_event_id=user_event_id, focus_id=focus_id, task_id=task_id,
        delivery_id=delivery_id, decision_receipt_id=f"DR-{uuid.uuid4().hex[:16]}",
        decision="accept", explicit_target=explicit_target,
        reason="UserPromptSubmit 明确验收",
    )
    if result.get("applied") and task.get("card_path") and Path(task["card_path"]).exists():
        try:
            write_task_card_projection(store, workspace_root=root, task_id=task_id)
        except Exception as exc:
            store.set_task_metadata(task_id, {"projection_degraded": True})
            store.enqueue_outbox(
                dedupe_key=f"accept-projection:{delivery_id}", event_type="projection_degraded",
                aggregate_type="delivery", aggregate_id=delivery_id,
                payload={"authority_committed": True, "error": str(exc)},
            )
    return {"ok": True, "decision_applied": bool(result.get("applied")),
            "decision": "accept", "task_id": task_id, "delivery_id": delivery_id,
            "accepted_by": "user" if result.get("applied") else None}


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
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split(":", 1)[0] if ":" in line else ""
        if key in values and line.startswith(f"{key}:"):
            result.append(f"{key}: {values[key]}")
            seen.add(key)
        else:
            result.append(line)
    result.extend(f"{key}: {value}" for key, value in values.items() if key not in seen)
    return "---\n" + "\n".join(result) + rest


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


def find_card(root: Path, task_id: str) -> Path:
    roots = [root / ".xirang/tasks", root / "02-项目管理/任务卡"]
    matches = [path for base in roots if base.exists() for path in base.glob(f"**/{task_id}.md")]
    if len(matches) != 1:
        raise FileNotFoundError(f"任务卡匹配数必须为 1，实际 {len(matches)}：{task_id}")
    return matches[0]


def receipt_body(payload: dict) -> bytes:
    return json.dumps({key: payload[key] for key in sorted(payload) if key != "signature"}, ensure_ascii=False, separators=(",", ":")).encode()


def verify_receipt(root: Path, path: Path, task_id: str) -> dict:
    runtime = runtime_dir(root).resolve()
    receipt = path.expanduser().resolve()
    try:
        receipt.relative_to(runtime / "receipts")
    except ValueError as exc:
        raise PermissionError("验收凭证不在受控运行目录") from exc
    data = json.loads(receipt.read_text(encoding="utf-8"))
    secret = (runtime / "secret.key").read_text(encoding="utf-8").strip().encode()
    expected = hmac.new(secret, receipt_body(data), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(data.get("signature") or ""), expected):
        raise PermissionError("验收凭证签名无效")
    if data.get("action") != "accept" or data.get("task_id") != task_id or data.get("actor") != "user":
        raise PermissionError("验收凭证动作、任务或身份不匹配")
    if data.get("consumed_at"):
        raise PermissionError("验收凭证已经消费")
    if data.get("aborted_at"):
        raise PermissionError("验收凭证已经中止")
    expires = datetime.fromisoformat(str(data.get("expires_at")).replace("Z", "+00:00")).astimezone()
    if now() > expires:
        raise PermissionError("验收凭证已过期")
    return data


@contextmanager
def task_lock(root: Path, task_id: str):
    lock_dir = runtime_dir(root) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"decision-{task_id}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_events(root: Path) -> list[dict]:
    path = runtime_dir(root) / "events/events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def json_field(data: dict[str, str], key: str) -> list:
    try:
        value = json.loads(data.get(key, "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"任务卡 {key} 不是合法 JSON") from exc
    if not isinstance(value, list):
        raise ValueError(f"任务卡 {key} 必须是列表")
    return value


def list_field(text: str, name: str) -> list[str]:
    result: list[str] = []
    active = False
    for line in split_frontmatter(text)[0]:
        if re.fullmatch(rf"{re.escape(name)}:\s*", line):
            active = True
            continue
        if active and re.match(r"^\s+-\s+", line):
            result.append(clean(re.sub(r"^\s+-\s+", "", line)))
            continue
        if active and line and not line.startswith(" "):
            break
    return result


def allowed_external_target(path: Path, targets: list[Path]) -> bool:
    """Allow an exact external file, or a descendant of an authorized directory."""
    return any(path == target or (target.is_dir() and target in path.parents) for target in targets)


def _resolve_repair_chain(task_events: list[dict], task_id: str, file: str, receipt_id: str) -> dict:
    """Resolve a receipt through evidence repair supersede chains.

    `xirang-evidence-repair` appends rows with `supersedes_receipt` instead of
    rewriting old ones, so acceptance must follow the chain to the latest row.
    """
    rows: dict[str, dict] = {}
    supersedes: dict[str, str] = {}
    for row in task_events:
        if row.get("event") != "file_write" or row.get("task_id") != task_id or not row.get("receipt_id"):
            continue
        value = str(row.get("receipt_id") or "")
        rows[value] = row
        old = str(row.get("supersedes_receipt") or "")
        if old:
            if old in supersedes and supersedes[old] != value:
                raise ValueError(f"证据修复链存在分叉：{old}")
            supersedes[old] = value
    current_id = str(receipt_id)
    visited: set[str] = set()
    last = rows.get(current_id)
    if not isinstance(last, dict):
        raise ValueError(f"写入证据不存在：{file}")
    while current_id and current_id not in visited:
        visited.add(current_id)
        row = rows.get(current_id)
        if not isinstance(row, dict):
            raise ValueError(f"证据修复链节点不存在：{current_id}")
        last = row
        next_id = supersedes.get(current_id)
        if not next_id:
            break
        if next_id in visited:
            raise ValueError(f"证据修复链存在循环：{next_id}")
        current_id = next_id
    return last


def verify_write_evidence(root: Path, task_id: str, data: dict[str, str]) -> None:
    task_events = [row for row in load_events(root) if row.get("event") == "file_write" and row.get("task_id") == task_id]
    if data.get("delivery_mode", "files") == "chat":
        if task_events:
            raise ValueError("聊天交付出现过文件写入，不能跳过 post-write 证据")
        if not data.get("verification_summary") or data.get("verification_summary") in {"null", "none", "~"}:
            raise ValueError("聊天交付缺少验证摘要")
        return
    paths = [str(value) for value in json_field(data, "changed_paths_json")]
    ids = [str(value) for value in json_field(data, "write_receipt_ids_json")]
    if not paths or len(paths) != len(ids):
        raise ValueError("文件交付缺少完整 post-write 证据")
    card_text = find_card(root, task_id).read_text(encoding="utf-8")
    external_roots = [Path(value).expanduser().resolve() for value in list_field(card_text, "external_write_roots")]
    for rel, receipt_id in zip(paths, ids):
        row = _resolve_repair_chain(task_events, task_id, rel, receipt_id)
        if not row or row.get("file") != rel:
            raise ValueError(f"写入证据不存在或路径不匹配：{rel}")
        raw = Path(rel)
        if raw.is_absolute():
            receipt = data.get("maintenance_authorization_receipt")
            if (data.get("maintenance") != "true"
                    or receipt in {None, "", "null", "none", "~"}):
                raise ValueError("工作区外交付文件只能来自已授权维护任务")
            path = raw.expanduser().resolve()
            if not allowed_external_target(path, external_roots):
                raise ValueError(f"交付文件不在工作区且未列入维护外部根：{rel}")
        else:
            path = root / rel
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if current != row.get("sha256") or bool(path.exists()) != bool(row.get("exists")):
            raise ValueError(f"交付文件已在证据之后变化：{rel}")


def optional_maintainer_gate(root: Path, task_id: str, candidate: str) -> None:
    gate = root / ".standards/gate-enforce.py"
    if not (root / "息壤-维护.md").is_file() or not gate.is_file():
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(candidate)
        temp = Path(handle.name)
    try:
        proc = subprocess.run([sys.executable, str(gate), "pre-accept", "--candidate", str(temp), "--task-id", task_id, "--json"], cwd=root, capture_output=True, text=True, check=False, timeout=60)
    finally:
        temp.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"维护者验收 Gate 未通过：{(proc.stdout or proc.stderr).strip()[-1200:]}")


def accept(root: Path, task_id: str, receipt_path: Path | None = None, *,
           user_event_id: str = "", focus_id: str = "", delivery_id: str = "",
           explicit_target: bool = False) -> dict:
    store = active_state_store(root)
    if store is not None:
        if not user_event_id or not focus_id or not delivery_id:
            raise PermissionError("激活后的验收必须绑定数据库 user_event/focus/delivery")
        return accept_from_state(
            root, store, task_id, user_event_id=user_event_id, focus_id=focus_id,
            delivery_id=delivery_id, explicit_target=explicit_target,
        )
    if receipt_path is None:
        raise PermissionError("迁移前 legacy backend 需要 decision receipt")
    with task_lock(root, task_id):
        return accept_locked(root, task_id, receipt_path)


def accept_locked(root: Path, task_id: str, receipt_path: Path) -> dict:
    receipt = verify_receipt(root, receipt_path, task_id)
    card = find_card(root, task_id)
    old = card.read_text(encoding="utf-8")
    data = fields(old)
    if data.get("review_status") not in {"submitted", "reviewing"}:
        raise ValueError(f"当前 review_status={data.get('review_status') or '<missing>'}，不能验收")
    if "user" in {clean(data.get(key, "")).lower() for key in ("owner", "author", "executor")}:
        raise PermissionError("执行者/作者不能验收自己的交付")
    verify_write_evidence(root, task_id, data)
    stamp = now_iso()
    candidate = set_fields(old, {
        "status": "completed", "review_status": "accepted", "accepted_by": "user", "accepted_at": stamp,
        "acceptance_result": "accepted", "acceptance_note": "UserPromptSubmit 明确验收", "updated_at": stamp,
        "decision_receipt_id": receipt.get("receipt_id"),
    })
    optional_maintainer_gate(root, task_id, candidate)
    atomic_write(card, candidate)
    receipt["consumed_at"] = stamp
    secret = (runtime_dir(root) / "secret.key").read_text(encoding="utf-8").strip().encode()
    receipt["signature"] = hmac.new(secret, receipt_body(receipt), hashlib.sha256).hexdigest()
    atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "decision_applied": True, "decision": "accept", "task_id": task_id, "accepted_by": "user"}


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤内部验收后端；普通用户不要直接调用")
    parser.add_argument("task_id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--event-id", default="")
    parser.add_argument("--focus-id", default="")
    parser.add_argument("--delivery-id", default="")
    parser.add_argument("--explicit-target", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        root = args.root.expanduser().resolve()
        result = accept(root, args.task_id, args.receipt,
                        user_event_id=args.event_id, focus_id=args.focus_id,
                        delivery_id=args.delivery_id, explicit_target=args.explicit_target)
        store = active_state_store(root)
        if result.get("decision_applied") and store is not None:
            refresh_events_projection(
                store,
                workspace_root=root,
                output=runtime_dir(root) / "events/events.jsonl",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)
        return 0
    except Exception as exc:
        result = {"ok": False, "decision_applied": False, "task_id": args.task_id, "error": type(exc).__name__, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
