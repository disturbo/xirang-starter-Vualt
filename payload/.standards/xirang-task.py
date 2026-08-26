#!/usr/bin/env python3
"""Create and submit ordinary XiRang tasks without exposing the state machine."""

from __future__ import annotations

import argparse
import fcntl
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

from xirang_state import StateStore, canonical_scope, refresh_events_projection
from xirang_state_cli import backend_operation_guard, probe_backend, sqlite_authority_artifacts_present
from xirang_task_projection import write_task_card_projection


PROTECTED = (".xirang", ".standards", ".codex", ".claude", "02-项目管理/任务卡")
ORDINARY_CONTENT_ROOTS = (
    "00-MOC", "10-项目", "20-资料", "30-规范", "40-决策", "50-经验",
    "60-归档", "70-模板", "知识库工程化",
)
AUTHORIZATION_DEFAULT_INTENTS = {
    "continue_execution", "adversarial_review", "no_intermediate_confirmation",
}
AUTHORIZATION_ALLOWED_INTENTS = AUTHORIZATION_DEFAULT_INTENTS | {"report_once_no_prompt"}

CONTRACT_DOC_ROOT = Path("50-经验/Agent协作方法论")
CONTRACT_METHOD = CONTRACT_DOC_ROOT / "息壤方法论-V9.md"
CONTRACT_RUNTIME_CARD = CONTRACT_DOC_ROOT / "息壤V9-运行时契约卡.md"
CONTRACT_README = CONTRACT_DOC_ROOT / "README.md"
CONTRACT_POLICY = Path(".xirang/contract/policy.yaml")
CONTRACT_TOOL_REGISTRY = Path(".xirang/contract/tool-registry.json")


def _contract_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    try:
        block = text.split("---\n", 2)[1]
    except IndexError:
        return {}
    result: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if match:
            result[match.group(1)] = match.group(2).strip('"')
    return result


def _contract_policy_scalar(text: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^#\n]+?)\s*$", text)
    return match.group(1).strip().strip('"') if match else ""


def _contract_policy_inline_list(text: str, key: str) -> list[str]:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*\[([^\]]*)\]\s*$", text)
    if not match:
        return []
    return [item.strip().strip('"\'') for item in match.group(1).split(",") if item.strip()]


def _contract_values_are_ordered(text: str, values: list[str]) -> bool:
    cursor = -1
    for value in values:
        cursor = text.find(value, cursor + 1)
        if cursor < 0:
            return False
    return True


def check_contract_alignment(root: Path) -> list[dict[str, str]]:
    """Fail closed when controlled human-readable XiRang assertions drift."""
    issues: list[dict[str, str]] = []

    def fail(rule: str, path: Path, message: str) -> None:
        issues.append({"rule": rule, "path": str(path), "message": message})

    required = (
        CONTRACT_POLICY, CONTRACT_TOOL_REGISTRY, CONTRACT_METHOD,
        CONTRACT_RUNTIME_CARD, CONTRACT_README,
    )
    for relative in required:
        if not (root / relative).is_file():
            fail("required_file", relative, "受控真源或说明文件不存在")
    if issues:
        return issues

    policy_text = (root / CONTRACT_POLICY).read_text(encoding="utf-8")
    method_text = (root / CONTRACT_METHOD).read_text(encoding="utf-8")
    runtime_text = (root / CONTRACT_RUNTIME_CARD).read_text(encoding="utf-8")
    readme_text = (root / CONTRACT_README).read_text(encoding="utf-8")
    try:
        registry = json.loads((root / CONTRACT_TOOL_REGISTRY).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError) as exc:
        fail("tool_registry_json", CONTRACT_TOOL_REGISTRY, f"JSON 无法解析：{exc}")
        return issues

    version = _contract_policy_scalar(policy_text, "policy_version")
    for relative, text in (
        (CONTRACT_METHOD, method_text),
        (CONTRACT_RUNTIME_CARD, runtime_text),
        (CONTRACT_README, readme_text),
    ):
        actual = _contract_frontmatter(text).get("version")
        if actual != version:
            fail("version_alignment", relative, f"frontmatter version={actual!r}，policy={version!r}")

    disclosure_fields = _contract_policy_inline_list(policy_text, "disclosure_fields")
    missing = [field for field in disclosure_fields if f"`{field}`" not in runtime_text]
    if missing:
        fail("disclosure_fields", CONTRACT_RUNTIME_CARD, f"缺少 Policy 披露字段：{missing}")

    main_flow = _contract_policy_inline_list(policy_text, "main_flow")
    if not main_flow or not _contract_values_are_ordered(runtime_text, main_flow):
        fail("runtime_main_flow", CONTRACT_RUNTIME_CARD, "阶段顺序与 Policy main_flow 不一致")

    retired = _contract_policy_inline_list(policy_text, "current_capabilities_forbidden")
    absent_retired = [value for value in retired if f"`{value}`" not in method_text]
    if absent_retired:
        fail("retired_capabilities", CONTRACT_METHOD, f"受控退役快照缺少：{absent_retired}")

    method_requirements = {
        "authority_fallback": "发生冲突时，立即停止采用本页",
        "human_flow_view": "面向人的压缩视图，不是第二套状态机",
        "task_card_notes": ".notes.md",
        "fixed_agents_classification": "被当前架构取代，但不属于 Policy 明确退役清单",
        "minimal_package": "## 最小息壤包",
        "installation_status": "## 安装、更新与卸载",
    }
    for rule, phrase in method_requirements.items():
        if phrase not in method_text:
            fail(rule, CONTRACT_METHOD, f"缺少受控说明：{phrase}")

    runtime_requirements = {
        "runtime_fallback": "发生冲突时停止采用本卡",
        "runtime_recovery": "恢复路径",
        "runtime_notes": ".notes.md",
        "runtime_task_card": "## 任务卡",
    }
    for rule, phrase in runtime_requirements.items():
        if phrase not in runtime_text:
            fail(rule, CONTRACT_RUNTIME_CARD, f"缺少运行时约束：{phrase}")

    tools = {
        str(item.get("id")): item
        for item in registry.get("tools", []) if isinstance(item, dict)
    }
    gate = tools.get("gate-enforce") or {}
    if gate.get("state") != "migration_legacy":
        fail("legacy_gate_state", CONTRACT_TOOL_REGISTRY,
             "gate-enforce 必须登记为 migration_legacy")
    lint_tool = tools.get("contract-lint") or {}
    if lint_tool.get("state") != "current":
        fail("contract_lint_registration", CONTRACT_TOOL_REGISTRY,
             "contract-lint 未登记为 current")
    for tool_id, item in tools.items():
        if item.get("state") != "current":
            continue
        for raw_path in item.get("paths") or []:
            if not (root / str(raw_path)).is_file():
                fail("current_tool_path", Path(str(raw_path)),
                     f"current 工具 {tool_id} 的实现不存在")

    source_text = (root / ".standards/xirang-task.py").read_text(encoding="utf-8")
    for option in ("--irreversible-effect", "--external-effect", "--acceptance-owner"):
        if option not in source_text:
            fail("disclosure_cli", Path(".standards/xirang-task.py"), f"任务入口缺少 {option}")
    projection_text = (root / ".standards/xirang_state.py").read_text(encoding="utf-8")
    for field in (
        "execution_owner_session_id", "active_worker_leases", "latest_handoff_id",
        "latest_delivery_id", "user_notes_path",
    ):
        if field not in projection_text:
            fail("task_card_projection", Path(".standards/xirang_state.py"),
                 f"任务卡投影缺少 {field}")
    return issues


def authorization_intents(values: list[str] | None = None) -> list[str]:
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("additional_intents 必须是字符串数组")
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ValueError(f"additional_intents[{index}] 必须是字符串")
        if value not in AUTHORIZATION_ALLOWED_INTENTS:
            raise ValueError(f"不支持的 additional_intent：{value}")
    requested = set(values)
    return sorted(AUTHORIZATION_DEFAULT_INTENTS | requested)


def authorized_execution_retry(
    store: StateStore, *, proposal_id: str, session_id: str, platform: str,
    source_event_id: str, intents: list[str],
) -> dict | None:
    """Return only an exact immutable authorization retry; reject all drift."""
    with store.connect(readonly=True) as connection:
        proposal = connection.execute(
            "SELECT * FROM maintenance_proposals WHERE proposal_id=?", (proposal_id,),
        ).fetchone()
        if proposal is None or proposal["status"] not in {"authorized", "consumed"}:
            return None
        event = connection.execute(
            "SELECT * FROM user_events WHERE event_id=?", (source_event_id,),
        ).fetchone()
    if proposal["session_id"] != session_id or proposal["platform"] != platform:
        raise PermissionError("已授权执行提案不属于当前会话或平台")
    if proposal["authorized_by_event_id"] != source_event_id:
        raise PermissionError("已授权执行提案绑定了不同用户事件")
    if json.loads(proposal["additional_intents_json"] or "[]") != intents:
        raise PermissionError("已授权执行提案的 additional_intents 不允许漂移")
    if event is None or event["consumed_at"] is None:
        raise PermissionError("已授权执行提案缺少已消费的原用户事件")
    bindings = json.loads(event["bindings_json"] or "{}")
    if (
        bindings.get("maintenance_proposal_id") != proposal_id
        or bindings.get("disclosure_id") != proposal["disclosure_id"]
    ):
        raise PermissionError("已授权执行提案与原用户事件冻结目标不一致")
    return {
        "ok": True, "proposal_id": proposal_id, "authorized": True,
        "additional_intents": intents, "idempotent": True,
    }


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def root_default() -> Path:
    explicit = os.environ.get("VAULT_ROOT") or os.environ.get("XIRANG_WORKSPACE_ROOT")
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


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


def digest_payload(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_task_projection(root: Path, task: dict) -> Path:
    store = StateStore(runtime_dir(root) / "state" / "state.sqlite3")
    result = write_task_card_projection(
        store, workspace_root=root, task_id=task["task_id"],
    )
    return Path(result["path"])


def task_root(root: Path) -> Path:
    legacy = root / "02-项目管理/任务卡"
    return legacy if legacy.exists() and (root / "息壤-维护.md").exists() else root / ".xirang/tasks"


def clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def frontmatter(text: str) -> tuple[list[str], str]:
    if not text.startswith("---\n"):
        raise ValueError("任务卡缺少 frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("任务卡 frontmatter 未闭合")
    return text[4:end].splitlines(), text[end:]


def fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in frontmatter(text)[0]:
        if match := re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line):
            result[match.group(1)] = clean(match.group(2))
    return result


def encode(value: object) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def set_fields(text: str, updates: dict[str, object]) -> str:
    lines, rest = frontmatter(text)
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


def set_list_field(text: str, name: str, values: list[str]) -> str:
    """Render a frontmatter sequence in the block form consumed by list_field."""
    lines, rest = frontmatter(text)
    output: list[str] = []
    replaced = False
    skipping_children = False
    for line in lines:
        if re.match(rf"^{re.escape(name)}:\s*", line):
            output.append(f"{name}:")
            output.extend(f"  - {json.dumps(value, ensure_ascii=False)}" for value in values)
            replaced = True
            skipping_children = True
            continue
        if skipping_children and re.match(r"^\s+-\s+", line):
            continue
        skipping_children = False
        output.append(line)
    if not replaced:
        output.append(f"{name}:")
        output.extend(f"  - {json.dumps(value, ensure_ascii=False)}" for value in values)
    return "---\n" + "\n".join(output) + rest


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def append_event(root: Path, payload: dict) -> None:
    """Append an internal lifecycle event to the external, append-only runtime log."""
    path = runtime_dir(root) / "events/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def receipt_body(payload: dict) -> bytes:
    return json.dumps({key: payload[key] for key in sorted(payload) if key != "signature"},
                      ensure_ascii=False, separators=(",", ":")).encode()


def signing_secret(root: Path) -> bytes:
    path = runtime_dir(root) / "secret.key"
    if not path.is_file():
        raise RuntimeError("息壤本机密钥不存在；请重新运行 setup.sh")
    return path.read_text(encoding="utf-8").strip().encode()


def sign(root: Path, payload: dict) -> dict:
    payload["signature"] = hmac.new(signing_secret(root), receipt_body(payload), hashlib.sha256).hexdigest()
    return payload


def maintainer_profile(root: Path) -> bool:
    try:
        return json.loads((root / ".xirang/local-config.json").read_text(encoding="utf-8")).get("profile") == "maintainer"
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def proposal_path(root: Path, proposal_id: str) -> Path:
    if not re.fullmatch(r"M-[a-f0-9]{16}", proposal_id):
        raise ValueError("维护授权编号无效")
    return runtime_dir(root) / "maintenance-proposals" / f"{proposal_id}.json"


def load_signed_proposal(root: Path, proposal_id: str) -> tuple[Path, dict]:
    path = proposal_path(root, proposal_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    supplied = str(payload.get("signature") or "")
    expected = hmac.new(signing_secret(root), receipt_body(payload), hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise PermissionError("维护授权提案签名无效")
    return path, payload


def external_write_paths(values: list[str] | None) -> list[str]:
    """Normalize the explicitly authorized Vault-external targets.

    A target may be one existing file (the least-privilege default) or an
    existing directory when a whole directory is genuinely required.  Do not
    resolve a relative input before checking it: resolving first would turn a
    relative path into an absolute one and silently widen the caller's intent.
    """
    targets: list[str] = []
    for value in values or []:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ValueError(f"外部目标必须是绝对路径：{value}")
        path = raw.resolve()
        if not path.exists():
            raise ValueError(f"外部目标必须是已存在的文件或目录：{value}")
        normalized = str(path)
        if normalized not in targets:
            targets.append(normalized)
    return targets


def propose_execution(root: Path, title: str, scopes: list[str], excludes: list[str], session_id: str,
                      platform: str, executor: str, *, maintenance: bool,
                      external_roots: list[str] | None = None,
                      operations: list[str] | None = None,
                      delivery_mode: str = "files",
                      irreversible_effects: list[str] | None = None,
                      external_effects: list[str] | None = None,
                      acceptance_owner: str = "user") -> dict:
    if maintenance and not maintainer_profile(root):
        raise PermissionError("当前工作区不是维护者配置")
    if not session_id:
        raise ValueError("缺少当前 SessionStart 提供的内部会话号")
    allowed = [safe_scope(value, maintenance) for value in scopes]
    if not allowed:
        raise ValueError("执行提案至少需要一个写入范围")
    external = external_write_paths(external_roots)
    if external and not maintenance:
        raise ValueError("普通任务不能声明工作区外写入目标")
    if delivery_mode not in {"files", "files_no_git", "chat"}:
        raise ValueError(f"未知交付模式：{delivery_mode}")
    if delivery_mode == "files_no_git" and not any(
        "git" in str(value).casefold()
        and ("提交" in str(value) or "commit" in str(value).casefold())
        for value in excludes
    ):
        raise ValueError("files_no_git 必须在展示包络中明确排除 Git 提交")
    acceptance_owner = acceptance_owner.strip()
    if not acceptance_owner:
        raise ValueError("acceptance_owner 不能为空")
    task_kind = "control_plane_maintenance" if maintenance else "ordinary"
    store = active_state_store(root)
    if store is not None:
        wid = workspace_id(root)
        objective_id = store.create_objective(
            workspace_id=wid, original_text=title, conversation_id=session_id, created_at=now()
        )
        disclosure_payload = {
            "objective_record_id": objective_id, "task_kind": task_kind,
            "allowed_write_roots": allowed, "excluded_actions": sorted(set(excludes)),
            "external_write_roots": external, "delivery_mode": delivery_mode,
            "allowed_operations": sorted(set(operations or [])),
            "irreversible_effects": sorted(set(irreversible_effects or [])),
            "external_effects": sorted(set(external_effects or [])),
            "acceptance_owner": acceptance_owner,
        }
        disclosure_id = store.create_disclosure(
            objective_id=objective_id, workspace_id=wid, session_id=session_id,
            task_kind=task_kind, payload=disclosure_payload, displayed_at=now(),
            actor_verified=False, disclosure_verified=False, sequence_verified=False,
        )
        proposal_payload = {
            **disclosure_payload, "title": title, "executor": executor,
            "scopes": allowed, "excludes": sorted(set(excludes)), "external_roots": external,
        }
        scope_digest = digest_payload({
            "allowed_write_roots": allowed, "excluded_actions": sorted(set(excludes)),
            "external_write_roots": external, "allowed_operations": sorted(set(operations or [])),
            "task_kind": task_kind, "delivery_mode": delivery_mode,
        })
        proposal_id = store.create_maintenance_proposal(
            disclosure_id=disclosure_id, workspace_id=wid, session_id=session_id,
            platform=platform, scope_digest=scope_digest, payload=proposal_payload, created_at=now(),
            actor_verified=False, disclosure_verified=False, sequence_verified=False,
            enforcement_verified=False,
        )
        return {"ok": True, "proposal_id": proposal_id, "status": "pending",
                "objective_record_id": objective_id, "disclosure_id": disclosure_id,
                "disclosure_digest": digest_payload(disclosure_payload), "scope_digest": scope_digest}
    if not maintenance:
        raise RuntimeError("普通任务授权提案需要已激活的 V3 SQLite StateStore")
    proposal_id = f"M-{uuid.uuid4().hex[:16]}"
    payload = sign(root, {
        "schema_version": 1, "proposal_id": proposal_id, "action": "authorize_maintenance",
        "title": title, "scopes": allowed, "excludes": excludes, "session_id": session_id,
        "platform": platform, "executor": executor, "external_roots": external, "status": "pending",
        "delivery_mode": delivery_mode,
        "created_at": now_iso(), "expires_at": (now() + timedelta(minutes=30)).isoformat(timespec="seconds"),
        "authorized_at": None, "consumed_at": None,
    })
    path = proposal_path(root, proposal_id)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "proposal_id": proposal_id, "status": "pending"}


def propose(root: Path, title: str, scopes: list[str], excludes: list[str], session_id: str,
            platform: str, executor: str, operations: list[str] | None = None,
            delivery_mode: str = "files", *,
            irreversible_effects: list[str] | None = None,
            external_effects: list[str] | None = None,
            acceptance_owner: str = "user") -> dict:
    return propose_execution(
        root, title, scopes, excludes, session_id, platform, executor,
        maintenance=False, operations=operations, delivery_mode=delivery_mode,
        irreversible_effects=irreversible_effects, external_effects=external_effects,
        acceptance_owner=acceptance_owner,
    )


def propose_maintenance(root: Path, title: str, scopes: list[str], excludes: list[str], session_id: str,
                        platform: str, executor: str, external_roots: list[str] | None = None,
                        operations: list[str] | None = None,
                        delivery_mode: str = "files", *,
                        irreversible_effects: list[str] | None = None,
                        external_effects: list[str] | None = None,
                        acceptance_owner: str = "user") -> dict:
    return propose_execution(
        root, title, scopes, excludes, session_id, platform, executor,
        maintenance=True, external_roots=external_roots, operations=operations,
        delivery_mode=delivery_mode, irreversible_effects=irreversible_effects,
        external_effects=external_effects, acceptance_owner=acceptance_owner,
    )


def authorize_execution(root: Path, proposal_id: str, session_id: str, platform: str,
                        source_event_id: str = "", prompt_sha256: str = "",
                        additional_intents: list[str] | None = None) -> dict:
    intents = authorization_intents(additional_intents)
    store = active_state_store(root)
    if store is not None:
        if not source_event_id:
            raise PermissionError("执行授权必须绑定已登记的当前用户事件")
        retry = authorized_execution_retry(
            store, proposal_id=proposal_id, session_id=session_id, platform=platform,
            source_event_id=source_event_id, intents=intents,
        )
        if retry is not None:
            return retry
        candidates = store.list_pending_maintenance_proposals(session_id, platform)
        if proposal_id not in {row["proposal_id"] for row in candidates}:
            retry = authorized_execution_retry(
                store, proposal_id=proposal_id, session_id=session_id, platform=platform,
                source_event_id=source_event_id, intents=intents,
            )
            if retry is not None:
                return retry
            raise PermissionError("执行提案不属于当前会话、平台或已过期")
        applied = store.authorize_maintenance_from_user_event(
            event_id=source_event_id, proposal_id=proposal_id, consumer_id="xirang-task",
            additional_intents=intents,
        )
        if not applied:
            retry = authorized_execution_retry(
                store, proposal_id=proposal_id, session_id=session_id, platform=platform,
                source_event_id=source_event_id, intents=intents,
            )
            if retry is not None:
                return retry
            raise PermissionError("执行授权发生并发冲突，或已绑定不同用户事件/目标")
        return {"ok": True, "proposal_id": proposal_id, "authorized": applied,
                "additional_intents": intents, "idempotent": False}
    if os.environ.get("XIRANG_USER_PROMPT_HOOK") != "1":
        raise PermissionError("执行授权必须来自 UserPromptSubmit Hook，或人工校验平台经一次性用户事件绑定的显式适配器转发")
    path, payload = load_signed_proposal(root, proposal_id)
    expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")).astimezone()
    if payload.get("status") in {"authorized", "consumed"}:
        if (
            payload.get("session_id") != session_id
            or payload.get("platform") != platform
            or payload.get("source_event_id") != (source_event_id or None)
            or payload.get("additional_intents") != intents
        ):
            raise PermissionError("已授权执行提案的用户事件、会话、平台或 additional_intents 已漂移")
        return {"ok": True, "proposal_id": proposal_id, "authorized": True,
                "additional_intents": intents, "idempotent": True}
    if payload.get("status") != "pending" or now() > expires:
        raise PermissionError("维护授权提案已失效或已消费")
    if payload.get("session_id") != session_id or payload.get("platform") != platform:
        raise PermissionError("维护授权提案与当前会话不匹配")
    payload.update({"status": "authorized", "authorized_at": now_iso(),
                    "authorization_source": "manual_guard_forwarded", "actor_verified": False,
                    "source_event_id": source_event_id or None, "raw_prompt_sha256": prompt_sha256 or None,
                    "additional_intents": intents})
    atomic_write(path, json.dumps(sign(root, payload), ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "proposal_id": proposal_id, "authorized": True,
            "additional_intents": intents, "idempotent": False}


def authorize_maintenance(root: Path, proposal_id: str, session_id: str, platform: str,
                          source_event_id: str = "", prompt_sha256: str = "",
                          additional_intents: list[str] | None = None) -> dict:
    return authorize_execution(
        root, proposal_id, session_id, platform, source_event_id, prompt_sha256,
        additional_intents,
    )


def consume_maintenance_authorization(root: Path, proposal_id: str, *, title: str, scopes: list[str], excludes: list[str],
                                      session_id: str, platform: str, executor: str, external_roots: list[str] | None = None) -> dict:
    path, payload = load_signed_proposal(root, proposal_id)
    expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")).astimezone()
    expected = {"title": title, "scopes": scopes, "excludes": excludes, "session_id": session_id,
                "platform": platform, "executor": executor, "external_roots": external_roots or []}
    if payload.get("status") != "authorized" or payload.get("consumed_at") or now() > expires:
        raise PermissionError("维护授权未生效、已过期或已消费")
    if any(payload.get(key) != value for key, value in expected.items()):
        raise PermissionError("维护任务与用户授权提案不一致")
    payload.update({"status": "consumed", "consumed_at": now_iso()})
    atomic_write(path, json.dumps(sign(root, payload), ensure_ascii=False, indent=2) + "\n")
    return payload


def safe_scope(value: str, maintenance: bool) -> str:
    path = value.replace("\\", "/").strip()
    path = path[2:] if path.startswith("./") else path
    path = path.rstrip("/")
    if not path or path.startswith("../") or Path(path).is_absolute():
        raise ValueError(f"非法授权路径：{value}")
    path = canonical_scope(path)
    if not maintenance and any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED):
        raise ValueError(f"普通任务不能授权息壤控制路径：{path}")
    if not maintenance and not any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ORDINARY_CONTENT_ROOTS
    ):
        raise ValueError(f"普通包只允许写入已登记的 Vault 内容根：{path}")
    return path


def make_task_id() -> str:
    return f"T-{now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def card_path(root: Path, task_id: str) -> Path:
    matches = sorted(task_root(root).glob(f"**/{task_id}.md"))
    if len(matches) != 1:
        raise FileNotFoundError(f"任务卡匹配数必须为 1，实际 {len(matches)}：{task_id}")
    return matches[0]


def list_field(text: str, name: str) -> list[str]:
    result: list[str] = []
    active = False
    for line in frontmatter(text)[0]:
        if re.fullmatch(rf"{re.escape(name)}:\s*", line):
            active = True
            continue
        if active and re.match(r"^\s+-\s+", line):
            result.append(clean(re.sub(r"^\s+-\s+", "", line)))
            continue
        if active and line and not line.startswith(" "):
            break
    return result


def json_string_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def under(rel: str, prefix: str) -> bool:
    normalized = prefix.strip().replace("\\", "/").removeprefix("./").rstrip("/")
    return bool(normalized) and (rel == normalized or rel.startswith(normalized + "/"))


@contextmanager
def review_debt_lock(root: Path):
    lock = runtime_dir(root) / "locks/review-debt.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_active_maintenance(root: Path, task_id: str, session_id: str, target: Path) -> dict[str, str]:
    if active_state_store(root) is not None:
        raise PermissionError(
            "SQLite 权威后端已激活；禁止从 Markdown 任务卡判断维护权限或反向修改状态"
        )
    try:
        maintenance_path = card_path(root, task_id)
    except FileNotFoundError as exc:
        raise PermissionError("缺少当前会话的活动维护任务") from exc
    text = maintenance_path.read_text(encoding="utf-8")
    data = fields(text)
    if (data.get("maintenance") != "true" or data.get("status") != "in_progress"
            or data.get("review_status") not in {"draft", "changes_requested"}
            or data.get("session_id") != session_id):
        raise PermissionError("债务迁移只能由当前会话的活动维护任务执行")
    receipt = data.get("maintenance_authorization_receipt")
    if receipt in {None, "", "null", "none", "~"}:
        raise PermissionError("活动维护任务缺少用户授权凭证")
    rel = target.resolve(strict=False).relative_to(root.resolve()).as_posix()
    if not any(under(rel, prefix) for prefix in list_field(text, "allowed_write_roots")):
        raise PermissionError(f"债务迁移目标超出维护授权范围：{rel}")
    return data


def governable_task(data: dict[str, str]) -> bool:
    return (bool(data.get("session_id")) and data.get("delivery_mode") in {"files", "files_no_git", "chat"}
            and "changed_paths_json" in data and "write_receipt_ids_json" in data)


def supersede_task(root: Path, task_id: str, replacement_task_id: str, maintenance_task_id: str,
                   session_id: str, reason: str) -> dict:
    if active_state_store(root) is not None:
        raise PermissionError("SQLite 权威后端已激活；legacy supersede 卡片写回入口已关闭")
    if task_id in {replacement_task_id, maintenance_task_id}:
        raise ValueError("原任务、替代任务和维护任务必须彼此不同")
    if not reason.strip():
        raise ValueError("替代关系必须写明可审计原因")
    target = card_path(root, task_id)
    replacement = card_path(root, replacement_task_id)
    require_active_maintenance(root, maintenance_task_id, session_id, target)
    with review_debt_lock(root):
        old = target.read_text(encoding="utf-8")
        data = fields(old)
        replacement_data = fields(replacement.read_text(encoding="utf-8"))
        if data.get("review_status") not in {"submitted", "reviewing", "changes_requested"}:
            raise ValueError(f"当前 review_status={data.get('review_status') or '<missing>'}，不能标记为已替代")
        if replacement_data.get("status") != "completed" or replacement_data.get("review_status") != "accepted":
            raise PermissionError("替代任务必须已经由用户独立验收")
        original_paths = set(json_string_list(data.get("changed_paths_json", "[]")))
        replacement_paths = set(json_string_list(replacement_data.get("changed_paths_json", "[]")))
        covered = sorted(original_paths & replacement_paths)
        residual = sorted(original_paths - replacement_paths)
        if original_paths and not covered:
            raise ValueError("原任务与替代任务没有交付路径交集，不能自动建立替代关系")
        stamp = now_iso()
        updated = set_fields(old, {
            "status": "superseded", "review_status": "superseded",
            "acceptance_result": "superseded", "acceptance_note": reason.strip(),
            "accepted_by": None, "accepted_at": None, "superseded_by": replacement_task_id,
            "superseded_at": stamp, "superseded_via": maintenance_task_id,
            "superseded_original_status": data.get("status") or None,
            "superseded_original_review_status": data.get("review_status") or None,
            "superseded_original_acceptance_result": data.get("acceptance_result") or None,
            "superseded_original_acceptance_note": data.get("acceptance_note") or None,
            "superseded_original_accepted_by": data.get("accepted_by") or None,
            "superseded_original_accepted_at": data.get("accepted_at") or None,
            "supersession_coverage_json": covered, "residual_paths_json": residual, "updated_at": stamp,
        })
        before_sha = hashlib.sha256(old.encode()).hexdigest()
        atomic_write(target, updated)
        after_sha = hashlib.sha256(updated.encode()).hexdigest()
        append_event(root, {
            "ts": stamp, "event": "task_superseded", "platform": "xirang", "agent": "xirang-task",
            "session_id": session_id, "maintenance_task_id": maintenance_task_id,
            "task_id": task_id, "superseded_by": replacement_task_id, "reason": reason.strip(),
            "covered_count": len(covered), "residual_count": len(residual), "residual_paths": residual,
            "before_sha256": before_sha, "after_sha256": after_sha,
        })
    refresh(root)
    return {"ok": True, "task_id": task_id, "review_status": "superseded",
            "superseded_by": replacement_task_id, "covered_paths": covered, "residual_paths": residual}


def classify_legacy_review_debt(root: Path, maintenance_task_id: str, session_id: str,
                                *, apply: bool = False) -> dict:
    if active_state_store(root) is not None:
        raise PermissionError("SQLite 权威后端已激活；legacy review-debt 卡片入口已关闭")
    base = task_root(root)
    require_active_maintenance(root, maintenance_task_id, session_id, base)
    candidates: list[Path] = []
    for path in sorted(base.glob("**/T-*.md")):
        data = fields(path.read_text(encoding="utf-8"))
        if (data.get("review_status") in {"submitted", "reviewing"} and not governable_task(data)
                and (data.get("task_id") or path.stem) != maintenance_task_id):
            candidates.append(path)
    task_ids = [fields(path.read_text(encoding="utf-8")).get("task_id") or path.stem for path in candidates]
    if not apply:
        return {"ok": True, "applied": False, "candidate_count": len(task_ids), "candidate_task_ids": task_ids}
    classified: list[str] = []
    with review_debt_lock(root):
        for path in candidates:
            old = path.read_text(encoding="utf-8")
            data = fields(old)
            if data.get("review_status") not in {"submitted", "reviewing"} or governable_task(data):
                continue
            task_id = data.get("task_id") or path.stem
            stamp = now_iso()
            updated = set_fields(old, {
                "status": "historical", "review_status": "legacy_unreviewed",
                "acceptance_result": "legacy_unreviewed",
                "acceptance_note": "缺少V9会话与交付证据；只读保留，不视为已验收",
                "accepted_by": None, "accepted_at": None,
                "legacy_original_status": data.get("status") or None,
                "legacy_original_review_status": data.get("review_status") or None,
                "legacy_original_acceptance_result": data.get("acceptance_result") or None,
                "legacy_original_acceptance_note": data.get("acceptance_note") or None,
                "legacy_original_accepted_by": data.get("accepted_by") or None,
                "legacy_original_accepted_at": data.get("accepted_at") or None,
                "legacy_classified_at": stamp, "legacy_classified_via": maintenance_task_id,
                "updated_at": stamp,
            })
            atomic_write(path, updated)
            append_event(root, {
                "ts": stamp, "event": "legacy_review_classified", "platform": "xirang", "agent": "xirang-task",
                "session_id": session_id, "maintenance_task_id": maintenance_task_id, "task_id": task_id,
                "original_status": data.get("status"), "original_review_status": data.get("review_status"),
                "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
                "after_sha256": hashlib.sha256(updated.encode()).hexdigest(),
            })
            classified.append(task_id)
    refresh(root)
    return {"ok": True, "applied": True, "classified_count": len(classified),
            "classified_task_ids": classified, "candidate_task_ids": task_ids}


def restore_review_debt(root: Path, task_id: str, maintenance_task_id: str, session_id: str) -> dict:
    if active_state_store(root) is not None:
        raise PermissionError("SQLite 权威后端已激活；legacy review-debt 恢复入口已关闭")
    target = card_path(root, task_id)
    require_active_maintenance(root, maintenance_task_id, session_id, target)
    with review_debt_lock(root):
        old = target.read_text(encoding="utf-8")
        data = fields(old)
        if data.get("review_status") == "superseded":
            prefix = "superseded_original_"
            classification = "superseded"
        elif data.get("review_status") == "legacy_unreviewed":
            prefix = "legacy_original_"
            classification = "legacy_unreviewed"
        else:
            raise ValueError("目标任务没有可恢复的债务迁移状态")
        original_status = data.get(prefix + "status")
        original_review = data.get(prefix + "review_status")
        if not original_status or original_status in {"null", "none", "~"} or not original_review or original_review in {"null", "none", "~"}:
            raise ValueError("债务迁移缺少原始状态，拒绝猜测恢复")
        def original(name: str) -> str | None:
            value = data.get(prefix + name)
            return None if value in {None, "", "null", "none", "~"} else value
        stamp = now_iso()
        updated = set_fields(old, {
            "status": original_status, "review_status": original_review,
            "acceptance_result": original("acceptance_result"),
            "acceptance_note": original("acceptance_note"),
            "accepted_by": original("accepted_by"), "accepted_at": original("accepted_at"),
            "debt_restored_at": stamp, "debt_restored_via": maintenance_task_id, "updated_at": stamp,
        })
        atomic_write(target, updated)
        append_event(root, {
            "ts": stamp, "event": "review_debt_restored", "platform": "xirang", "agent": "xirang-task",
            "session_id": session_id, "maintenance_task_id": maintenance_task_id,
            "task_id": task_id, "restored_from": classification,
            "restored_status": original_status, "restored_review_status": original_review,
            "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(updated.encode()).hexdigest(),
        })
    refresh(root)
    return {"ok": True, "task_id": task_id, "restored_from": classification,
            "status": original_status, "review_status": original_review}


def refresh(root: Path) -> None:
    subprocess.run([sys.executable, str(root / ".standards/xirang-user-status.py"), "--root", str(root), "--write", "--trigger", "task"], capture_output=True, check=False)


def start(root: Path, title: str, scopes: list[str], excludes: list[str], session_id: str, platform: str,
          executor: str, delivery_mode: str, maintenance: bool, authorization_receipt: str | None = None,
          external_roots: list[str] | None = None) -> dict:
    if not session_id:
        raise ValueError("缺少当前 SessionStart 提供的内部会话号；不得创建无会话任务")
    allowed = [safe_scope(value, maintenance) for value in scopes]
    if not allowed:
        raise ValueError("至少需要一个写入范围")
    external = external_write_paths(external_roots)
    if external and not maintenance:
        raise ValueError("工作区外目标仅可由已授权维护任务声明")
    store = active_state_store(root)
    if store is not None:
        replace_blocked_task_id: str | None = None
        active = store.find_active_tasks(session_id)
        if active:
            if len(active) != 1:
                raise RuntimeError("当前 owner session 同时存在多个主任务，按歧义失败关闭")
            inherited = active[0]
            if maintenance and authorization_receipt and inherited.get("lifecycle_status") == "blocked":
                replace_blocked_task_id = str(inherited["task_id"])
            else:
                return {"ok": True, "task_id": inherited["task_id"], "status": inherited["lifecycle_status"],
                        "inherited": True, "access_kind": "owner",
                        "allowed_write_roots": inherited["allowed_write_roots"],
                        "external_write_roots": inherited.get("external_write_roots") or []}
        leases = store.find_valid_leases(session_id)
        if leases and not replace_blocked_task_id:
            task_ids = sorted({lease["task_id"] for lease in leases})
            if len(task_ids) != 1:
                raise RuntimeError("当前 worker session 同时持有多个主任务 lease，按歧义失败关闭")
            inherited = store.get_task(task_ids[0])
            if inherited is None:
                raise RuntimeError("worker lease 引用的主任务不存在")
            access = store.resolve_task_access(session_id=session_id, task_id=task_ids[0])
            return {"ok": True, "task_id": task_ids[0], "status": inherited["lifecycle_status"],
                    "inherited": True, "access_kind": "read_only_lease" if leases[0].get("read_only") else "worker_lease",
                    "allowed_write_roots": access["allowed_write_roots"] if access else [],
                    "external_write_roots": inherited.get("external_write_roots") or []}
        if not authorization_receipt:
            raise PermissionError("V3 任务创建必须消费绑定用户事件与不可变范围展示的授权提案")
        task_id = make_task_id()
        path = task_root(root) / now().strftime("%Y-%m") / f"{task_id}.md"
        if maintenance and not maintainer_profile(root):
            raise PermissionError("控制面任务需要 maintainer profile")
        expected_kind = "control_plane_maintenance" if maintenance else "ordinary"
        created = store.create_task_from_authorized_proposal(
            proposal_id=authorization_receipt, task_id=task_id,
            envelope_digest="", platform=platform, created_at=now(),
            execution_session_id=session_id, expected_task_kind=expected_kind,
            expected_allowed_write_roots=allowed + external,
            replace_blocked_task_id=replace_blocked_task_id,
        )
        task_from_authority = store.get_task(task_id)
        if task_from_authority is None or task_from_authority["task_kind"] != expected_kind:
            raise PermissionError("授权提案 task_kind 与启动任务类型不一致")
        authorized_roots = task_from_authority["allowed_write_roots"]
        authorized_external = task_from_authority.get("external_write_roots") or []
        if sorted(allowed + external) != sorted(authorized_roots):
            raise PermissionError("启动范围与用户授权包络不一致")
        if task_from_authority.get("delivery_mode") != delivery_mode:
            raise PermissionError("启动交付模式与用户授权包络不一致")
        allowed = [value for value in authorized_roots if value not in authorized_external]
        external = list(authorized_external)
        envelope_digest = task_from_authority["envelope_digest"]
        task = store.set_task_metadata(task_id, {
            "title": title, "card_path": str(path),
            "maintenance": maintenance, "proposal_id": authorization_receipt,
            "external_write_roots": external,
        })
        projection_degraded = False
        try:
            write_task_projection(root, task)
        except Exception as exc:
            projection_degraded = True
            store.set_task_metadata(task_id, {"projection_degraded": True})
            store.enqueue_outbox(
                dedupe_key=f"task-projection:{task_id}", event_type="projection_degraded",
                aggregate_type="task", aggregate_id=task_id,
                payload={"authority_committed": True, "error": str(exc)},
            )
        return {"ok": True, "task_id": task_id, "card": str(path), "status": "in_progress",
                "allowed_write_roots": allowed, "external_write_roots": external,
                "envelope_digest": envelope_digest, "projection_degraded": projection_degraded,
                "start_ceremony": {"objective": title, "write_scope": allowed,
                                   "excluded_scope": excludes, "authorization_carryover": maintenance,
                                   "irreversible_effects": task.get("irreversible_effects") or [],
                                   "external_effects": task.get("external_effects") or [],
                                   "acceptance_owner": task.get("acceptance_owner") or "user",
                                   "recovery_required_for_material_change": True,
                                   "reviewer": task.get("acceptance_owner") or "user",
                                   "blocking": False}}
    if maintenance:
        if not maintainer_profile(root) or not authorization_receipt:
            raise PermissionError("维护任务必须消费本会话 UserPromptSubmit 维护授权")
        authorization = None
    else:
        if authorization_receipt:
            raise ValueError("普通任务不能携带维护授权")
        authorization = None
    for existing in task_root(root).glob("**/T-*.md"):
        try:
            data = fields(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") in {"in_progress", "blocked"} and session_id and data.get("session_id") == session_id:
            raise RuntimeError(f"当前会话已有活动任务：{data.get('task_id') or existing.stem}")
    if maintenance:
        authorization = consume_maintenance_authorization(
            root, authorization_receipt or "", title=title, scopes=allowed, excludes=excludes,
            session_id=session_id, platform=platform, executor=executor, external_roots=external,
        )
    task_id = make_task_id()
    stamp = now_iso()
    lines = [
        "---", f"task_id: {json.dumps(task_id)}", f"title: {json.dumps(title, ensure_ascii=False)}",
        "requester: user", f"executor: {json.dumps(executor)}", f"author: {json.dumps(executor)}", "reviewer: user",
        f"session_id: {json.dumps(session_id)}", f"platform: {json.dumps(platform)}", f"delivery_mode: {delivery_mode}",
        f"maintenance: {'true' if maintenance else 'false'}",
        f"maintenance_authorization_receipt: {json.dumps(authorization.get('proposal_id')) if authorization else 'null'}",
        "status: in_progress", "review_status: draft", f"created_at: {json.dumps(stamp)}", f"updated_at: {json.dumps(stamp)}",
        "continuation_policy: continue_until_terminal_condition",
        "stop_conditions_json: [\"scope_expansion\", \"irreversible_effect\", \"external_send_or_publish\", \"material_user_choice\", \"recovery_unavailable\"]",
        "allowed_write_roots:", *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in allowed],
        "external_write_roots:", *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in external],
        "excluded_scope:", *[f"  - {json.dumps(value, ensure_ascii=False)}" for value in excludes],
        "changed_paths_json: []", "write_receipt_ids_json: []", "verification_summary: null", "submission_summary: null",
        "accepted_by: null", "accepted_at: null", "acceptance_result: null", "acceptance_note: null", "resubmit_count: 0",
        "---", "", f"# {title}", "", "本卡由息壤任务入口创建；普通文件工具不得直接修改。", "",
    ]
    path = task_root(root) / now().strftime("%Y-%m") / f"{task_id}.md"
    atomic_write(path, "\n".join(lines))
    refresh(root)
    return {
        "ok": True,
        "task_id": task_id,
        "card": str(path),
        "status": "in_progress",
        "allowed_write_roots": allowed,
        "external_write_roots": external,
        "start_ceremony": {
            "objective": title,
            "write_scope": allowed,
            "excluded_scope": excludes,
            "authorization_carryover": bool(authorization),
            "reviewer": "user",
            "blocking": False,
        },
    }


def events(root: Path) -> list[dict]:
    path = runtime_dir(root) / "events/events.jsonl"
    result: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in raw:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def submit(root: Path, task_id: str, summary: str, verification: str, session_id: str) -> dict:
    store = active_state_store(root)
    if store is not None:
        raise PermissionError(
            "V3 submit 旁路已删除；只有 committing 阶段的 xirang_delivery.py 可以登记交付"
        )
    path = card_path(root, task_id)
    old = path.read_text(encoding="utf-8")
    data = fields(old)
    if not session_id or data.get("session_id") != session_id:
        raise PermissionError("只能提交当前 SessionStart 会话创建的任务")
    if data.get("status") not in {"in_progress", "blocked", "submitted"} or data.get("review_status") not in {"draft", "changes_requested", "submitted"}:
        raise ValueError(f"当前状态不能提交：status={data.get('status')}, review_status={data.get('review_status')}")
    rows = [row for row in events(root) if row.get("event") == "file_write" and row.get("task_id") == task_id]
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("file"):
            latest[str(row["file"])] = row
    superseded = {str(row.get("replaces_receipt")) for row in latest.values() if row.get("replaces_receipt")}
    superseded |= {str(row.get("supersedes_receipt")) for row in latest.values() if row.get("supersedes_receipt")}
    latest = {key: row for key, row in latest.items() if str(row.get("receipt_id")) not in superseded}
    changed = sorted(latest)
    receipt_ids = [str(latest[path].get("receipt_id")) for path in changed if latest[path].get("receipt_id")]
    if data.get("delivery_mode", "files") == "chat" and changed:
        raise RuntimeError("聊天交付出现了文件写入证据；必须改用文件交付任务，不能降级验收")
    if data.get("delivery_mode", "files") in {"files", "files_no_git"} and (not changed or len(receipt_ids) != len(changed)):
        raise RuntimeError("没有完整的 post-write 证据，不能提交文件交付")
    stamp = now_iso()
    updated = set_fields(old, {
        "status": "submitted", "review_status": "submitted", "submitted_at": stamp, "updated_at": stamp,
        "submission_summary": summary, "verification_summary": verification,
        "changed_paths_json": changed, "write_receipt_ids_json": receipt_ids,
    })
    atomic_write(path, updated)
    refresh(root)
    return {"ok": True, "task_id": task_id, "status": "submitted", "changed_paths": changed, "write_receipt_count": len(receipt_ids)}


def repair_active_maintenance_scope(root: Path, task_id: str, scopes: list[str], session_id: str) -> dict:
    """Repair a comma-collapsed scope on the current authorized maintenance task."""
    if active_state_store(root) is not None:
        raise PermissionError("SQLite 权威后端已激活；范围修复只能走原子 rescue 事务")
    path = card_path(root, task_id)
    old = path.read_text(encoding="utf-8")
    data = fields(old)
    if not session_id or data.get("session_id") != session_id:
        raise PermissionError("只能修复当前 SessionStart 会话创建的任务")
    if data.get("maintenance") != "true" or not data.get("maintenance_authorization_receipt"):
        raise PermissionError("只有已授权维护任务可修复范围")
    if data.get("status") != "in_progress" or data.get("review_status") != "draft":
        raise ValueError("只能修复尚未交付的活动维护任务")
    previous = list_field(old, "allowed_write_roots")
    if not previous:
        try:
            inline = json.loads(data.get("allowed_write_roots", "[]"))
            previous = [str(value) for value in inline] if isinstance(inline, list) else []
        except json.JSONDecodeError:
            previous = []
    if len(previous) != 1 or ("," not in previous[0] and previous[0] != "."):
        raise ValueError("当前任务不存在范围归一化缺陷")
    allowed = [safe_scope(value, True) for value in scopes]
    if not allowed:
        raise ValueError("修复后的写入范围不能为空")
    updated = set_list_field(old, "allowed_write_roots", allowed)
    atomic_write(path, set_fields(updated, {"updated_at": now_iso()}))
    append_event(root, {
        "ts": now_iso(), "event": "maintenance_scope_repaired", "platform": "xirang-task",
        "agent": "xirang-task", "session_id": session_id, "turn_id": "", "task_id": task_id,
        "previous_scopes": previous, "allowed_write_roots": allowed,
        "reason": "comma_delimited_scope_serialization",
    })
    return {"ok": True, "task_id": task_id, "allowed_write_roots": allowed}


def repair_submitted_evidence(root: Path, task_id: str, commits: list[str], session_id: str) -> dict:
    """Rebuild a submitted maintenance task's receipts from NUL-delimited Git paths.

    This is deliberately narrow: same session, authorized maintenance task, submitted
    delivery, and paths already captured by explicit commits.  It exists to recover
    from Git's quoted-path display being mistaken for a literal filename.
    """
    if active_state_store(root) is not None:
        raise PermissionError("SQLite 权威后端已激活；交付证据修复只能走权威状态事务")
    path = card_path(root, task_id)
    old = path.read_text(encoding="utf-8")
    data = fields(old)
    if not session_id or data.get("session_id") != session_id:
        raise PermissionError("只能修复当前 SessionStart 会话创建的交付证据")
    if data.get("maintenance") != "true" or not data.get("maintenance_authorization_receipt"):
        raise PermissionError("只有已授权维护任务可修复交付证据")
    if data.get("status") != "submitted" or data.get("review_status") != "submitted":
        raise ValueError("只能修复已提交、待验收的维护交付")
    if not commits:
        raise ValueError("至少需要一个已提交的 Git commit")
    allowed = list_field(old, "allowed_write_roots")
    changed: set[str] = set()
    for commit in commits:
        proc = subprocess.run(
            ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit],
            cwd=root, capture_output=True, check=True,
        )
        for raw in proc.stdout.split(b"\0"):
            if raw:
                changed.add(raw.decode("utf-8", errors="strict"))
    invalid = [value for value in json.loads(data.get("changed_paths_json", "[]"))
               if value.startswith('"') or "\\" in value]
    receipts: list[str] = []
    stamp = now_iso()
    for rel in sorted(changed):
        if not any(under(rel, prefix) for prefix in allowed):
            raise PermissionError(f"提交路径超出任务授权范围：{rel}")
        target = root / rel
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        receipt_id = hashlib.sha256(f"{task_id}:{rel}:{stamp}:evidence-repair".encode()).hexdigest()[:24]
        append_event(root, {
            "ts": stamp, "event": "file_write", "platform": "xirang-task", "agent": "xirang-task",
            "session_id": session_id, "turn_id": "", "task_id": task_id, "receipt_id": receipt_id,
            "file": rel, "operation": "update" if target.exists() else "delete",
            "exists": target.exists(), "sha256": digest, "tool_name": "git-evidence-repair",
        })
        receipts.append(receipt_id)
    updated = set_fields(old, {
        "updated_at": stamp, "changed_paths_json": sorted(changed),
        "write_receipt_ids_json": receipts,
        "verification_summary": data.get("verification_summary", "") +
            f"；对抗性复核已用 NUL 分隔 Git 路径重建证据，纠正 {len(invalid)} 条转义路径。",
    })
    atomic_write(path, updated)
    append_event(root, {
        "ts": stamp, "event": "submitted_evidence_repaired", "platform": "xirang-task",
        "agent": "xirang-task", "session_id": session_id, "turn_id": "", "task_id": task_id,
        "commits": commits, "invalid_receipts_replaced": len(invalid), "receipt_count": len(receipts),
        "reason": "quoted_git_paths_were_recorded_as_literal_paths",
    })
    refresh(root)
    return {"ok": True, "task_id": task_id, "changed_paths": sorted(changed),
            "write_receipt_count": len(receipts), "invalid_paths_replaced": len(invalid)}


def present_review(root: Path, task_id: str, session_id: str, platform: str) -> dict:
    """Record that the assistant explicitly presented this delivery for the user's decision.

    This makes the just-shown delivery the conversation focus, so a natural
    acceptance in the next turn binds to *this* delivery — not merely to "the
    newest submission in the session". It never changes task state; it only
    appends an append-only `review_presented` event.
    """
    store = active_state_store(root)
    if store is not None:
        task = store.get_task(task_id)
        delivery = store.get_latest_delivery(task_id)
        if task is None or delivery is None or task["review_status"] not in {"submitted", "reviewing"}:
            raise ValueError("任务没有可展示的当前交付")
        if store.resolve_task_access(session_id=session_id, task_id=task_id) is None:
            raise PermissionError("只有任务 owner 或有效 worker lease 可以呈现交付")
        preference = task.get("interaction_preference_snapshot") or {}
        if (preference.get("review_prompt_policy") == "report_once_no_prompt"
                and task.get("review_prompt_consumed_at")):
            return {"ok": True, "task_id": task_id, "delivery_id": delivery["delivery_id"],
                    "presented": False, "suppressed_by_preference": True}
        previous = store.get_active_review_focus(session_id)
        if previous is not None:
            store.supersede_review_focus(previous["focus_id"])
        focus_id = f"F-{uuid.uuid4().hex[:16]}"
        store.create_review_focus(
            focus_id=focus_id, task_id=task_id, delivery_id=delivery["delivery_id"],
            conversation_id=session_id, presented_at=now(), ttl_seconds=86400,
        )
        if preference.get("review_prompt_policy") == "report_once_no_prompt":
            store.set_task_metadata(task_id, {"review_prompt_consumed_at": now_iso()})
        return {"ok": True, "task_id": task_id, "delivery_id": delivery["delivery_id"],
                "presented": True, "focus_id": focus_id,
                "review_status": task["review_status"], "submitted_at": delivery["submitted_at"]}
    path = card_path(root, task_id)
    data = fields(path.read_text(encoding="utf-8"))
    if not session_id or data.get("session_id") != session_id:
        raise PermissionError("只能展示当前 SessionStart 会话创建的交付")
    if data.get("review_status") not in {"submitted", "reviewing"}:
        raise ValueError(f"当前 review_status={data.get('review_status') or '<missing>'}，没有待验收交付可展示")
    focus_id = f"RF-{uuid.uuid4().hex}"
    expires_at = (now() + timedelta(hours=24)).isoformat(timespec="seconds")
    append_event(root, {
        "ts": now_iso(), "event": "review_presented", "platform": platform, "agent": "xirang-task",
        "session_id": session_id, "turn_id": "", "focus_id": focus_id, "task_id": task_id,
        "review_status": data.get("review_status", ""), "submitted_at": data.get("submitted_at", ""),
        "expires_at": expires_at,
    })
    return {"ok": True, "task_id": task_id, "presented": True, "focus_id": focus_id,
            "review_status": data.get("review_status", ""), "expires_at": expires_at}


def _dispatch(args: argparse.Namespace, root: Path) -> dict:
    if args.action == "contract-lint":
        issues = check_contract_alignment(root)
        return {"ok": not issues, "issue_count": len(issues), "issues": issues}
    if args.action == "start":
        if not args.title:
            raise ValueError("start 需要 --title")
        return start(root, args.title, args.scope, args.exclude, args.session_id, args.platform, args.executor,
                     args.delivery_mode, args.maintenance, args.authorization_receipt, args.external_root)
    if args.action == "submit":
        if not args.task_id or not args.summary or not args.verification:
            raise ValueError("submit 需要 task_id、--summary 和 --verification")
        return submit(root, args.task_id, args.summary, args.verification, args.session_id)
    if args.action == "present-review":
        if not args.task_id:
            raise ValueError("present-review 需要 task_id")
        return present_review(root, args.task_id, args.session_id, args.platform)
    if args.action == "repair-active-maintenance-scope":
        if not args.task_id:
            raise ValueError("repair-active-maintenance-scope 需要 task_id")
        return repair_active_maintenance_scope(root, args.task_id, args.scope, args.session_id)
    if args.action == "repair-submitted-evidence":
        if not args.task_id:
            raise ValueError("repair-submitted-evidence 需要 task_id")
        return repair_submitted_evidence(root, args.task_id, args.commit, args.session_id)
    if args.action == "supersede":
        if not args.task_id or not args.replacement_task_id or not args.maintenance_task_id:
            raise ValueError("supersede 需要原任务、替代任务和当前维护任务")
        return supersede_task(root, args.task_id, args.replacement_task_id,
                              args.maintenance_task_id, args.session_id, args.reason)
    if args.action == "classify-legacy-review-debt":
        maintenance_task_id = args.maintenance_task_id or args.task_id or ""
        if not maintenance_task_id:
            raise ValueError("classify-legacy-review-debt 需要当前维护任务")
        return classify_legacy_review_debt(root, maintenance_task_id, args.session_id, apply=args.apply)
    if args.action == "restore-review-debt":
        if not args.task_id or not args.maintenance_task_id:
            raise ValueError("restore-review-debt 需要目标任务和当前维护任务")
        return restore_review_debt(root, args.task_id, args.maintenance_task_id, args.session_id)
    if args.action == "propose-maintenance":
        if not args.title:
            raise ValueError("propose-maintenance 需要 --title")
        return propose_maintenance(root, args.title, args.scope, args.exclude, args.session_id,
                                   args.platform, args.executor, args.external_root, args.operation,
                                   args.delivery_mode,
                                   irreversible_effects=args.irreversible_effect,
                                   external_effects=args.external_effect,
                                   acceptance_owner=args.acceptance_owner)
    if args.action == "propose":
        if not args.title:
            raise ValueError("propose 需要 --title")
        return propose(
            root, args.title, args.scope, args.exclude, args.session_id,
            args.platform, args.executor, args.operation, args.delivery_mode,
            irreversible_effects=args.irreversible_effect,
            external_effects=args.external_effect,
            acceptance_owner=args.acceptance_owner,
        )
    if not args.task_id or not args.from_user_prompt:
        raise ValueError("执行授权需要提案号和 --from-user-prompt")
    return authorize_execution(
        root, args.task_id, args.session_id, args.platform,
        args.source_event_id, args.prompt_sha256, args.additional_intent,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "submit", "present-review", "contract-lint", "propose",
                                           "propose-maintenance", "authorize-execution",
                                           "authorize-maintenance", "supersede", "classify-legacy-review-debt",
                                           "restore-review-debt", "repair-active-maintenance-scope",
                                           "repair-submitted-evidence"))
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--root", type=Path, default=root_default())
    parser.add_argument("--title")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--session-id", default="")
    parser.add_argument("--platform", default="unknown")
    parser.add_argument("--executor", default="ai")
    parser.add_argument("--delivery-mode", choices=("files", "files_no_git", "chat"), default="files")
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--authorization-receipt")
    parser.add_argument("--external-root", action="append", default=[],
                        help="维护任务可写入的 Vault 外绝对目录（仅维护者配置可用）")
    parser.add_argument("--operation", action="append", default=[],
                        help="逐路径范围共同允许的操作类型；省略时按任务类型使用安全默认值")
    parser.add_argument("--irreversible-effect", action="append", default=[],
                        help="本执行包络已披露的不可逆影响；可重复")
    parser.add_argument("--external-effect", action="append", default=[],
                        help="本执行包络已披露的外部影响；可重复")
    parser.add_argument("--acceptance-owner", default="user",
                        help="具名验收责任方；默认 user")
    parser.add_argument("--additional-intent", action="append", default=[],
                        help="授权后一并固化的连续执行意图；可重复")
    parser.add_argument("--from-user-prompt", action="store_true")
    parser.add_argument("--source-event-id", default="")
    parser.add_argument("--prompt-sha256", default="")
    parser.add_argument("--summary")
    parser.add_argument("--verification")
    parser.add_argument("--replacement-task-id", default="")
    parser.add_argument("--maintenance-task-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--commit", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        with backend_operation_guard(root, component="xirang-task"):
            result = _dispatch(args, root)
            mutating = args.action not in {"contract-lint", "classify-legacy-review-debt"} or (
                args.action == "classify-legacy-review-debt" and args.apply
            )
            store = active_state_store(root)
            if mutating and store is not None:
                refresh_events_projection(
                    store,
                    workspace_root=root,
                    output=runtime_dir(root) / "events/events.jsonl",
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
