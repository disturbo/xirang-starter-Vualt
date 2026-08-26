#!/usr/bin/env python3
"""XiRang V9.7 universal, fail-closed installer for macOS."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


VERSION = "9.7.0"
MANAGED_START = "<!-- XIRANG-V97-MANAGED-START -->"
MANAGED_END = "<!-- XIRANG-V97-MANAGED-END -->"
PLATFORM_START = "<!-- XIRANG-V97-PLATFORM-START -->"
PLATFORM_END = "<!-- XIRANG-V97-PLATFORM-END -->"
TERMINAL_STATES = {"completed", "rolled_back"}
GENERATED_PATHS = {
    ".codex/hooks.json",
    ".claude/settings.json",
    ".claude/CLAUDE.md",
    ".xirang/local-config.json",
    ".xirang/contract/recovery-roots.yaml",
    ".xirang/install/installed-manifest.json",
}


class InstallError(RuntimeError):
    def __init__(self, message: str, *, code: str = "install_failed") -> None:
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def atomic_json(path: Path, value: object, mode: int = 0o644) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        mode,
    )


def safe_relative(raw: str) -> Path:
    posix = PurePosixPath(raw)
    if not raw or posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise InstallError(f"Manifest 含不安全路径：{raw!r}", code="manifest_invalid")
    if "\\" in raw or raw.startswith("~"):
        raise InstallError(f"Manifest 路径格式非法：{raw!r}", code="manifest_invalid")
    return Path(*posix.parts)


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def verify_package(root: Path) -> dict:
    manifest_path = root / "manifests/package-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise InstallError("安装包 Manifest 缺失或损坏", code="manifest_invalid") from exc
    if manifest.get("version") != VERSION or manifest.get("schema_version") != 1:
        raise InstallError("安装包版本与 Manifest 不一致", code="manifest_invalid")
    expected: set[str] = set()
    for row in manifest.get("files") or []:
        if not isinstance(row, dict):
            raise InstallError("Manifest 文件项格式错误", code="manifest_invalid")
        relative = safe_relative(str(row.get("path") or ""))
        logical = relative.as_posix()
        if logical in expected:
            raise InstallError(f"Manifest 重复路径：{logical}", code="manifest_invalid")
        expected.add(logical)
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise InstallError(f"安装包文件缺失或类型非法：{logical}", code="manifest_invalid")
        if path.stat().st_size != int(row.get("size", -1)) or sha256(path) != row.get("sha256"):
            raise InstallError(f"安装包文件校验失败：{logical}", code="manifest_invalid")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    allowed = expected | {"manifests/package-manifest.json"}
    if actual != allowed:
        raise InstallError(
            f"安装包文件闭包不一致：extra={sorted(actual - allowed)}, missing={sorted(allowed - actual)}",
            code="manifest_invalid",
        )
    return manifest


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise InstallError(f"无法读取 {path.name}", code="package_invalid") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path.name} 必须是 JSON 对象", code="package_invalid")
    return value


def payload_files(root: Path) -> list[Path]:
    payload = root / "payload"
    paths = [path for path in payload.rglob("*") if path.is_file() and not path.is_symlink()]
    if any(path.is_symlink() for path in payload.rglob("*")):
        raise InstallError("Payload 不允许符号链接", code="package_invalid")
    return sorted(paths)


def workspace_id(target: Path) -> str:
    return hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:12]


def xirang_user_root() -> Path:
    override = os.environ.get("XIRANG_INSTALL_ROOT")
    return Path(override).expanduser().resolve() if override else Path.home() / ".xirang"


def platform_config(package: Path) -> dict:
    config = load_json(package / "templates/platforms.json")
    if config.get("schema_version") != 1 or not isinstance(config.get("platforms"), dict):
        raise InstallError("平台模板注册表无效", code="package_invalid")
    return config


def known_platforms(package: Path) -> set[str]:
    return set(platform_config(package)["platforms"])


def normalize_platform(value: str, package: Path) -> str:
    value = value.strip().lower().replace("-", "_") or "generic"
    for platform, row in platform_config(package)["platforms"].items():
        aliases = {str(item).lower().replace("-", "_") for item in (row.get("aliases") or [])}
        if value == platform or value in aliases:
            return platform
    return value


def detect_platform(requested: str, package: Path) -> str:
    if requested != "auto":
        return normalize_platform(requested, package)
    declared = normalize_platform(str(os.environ.get("XIRANG_PLATFORM") or ""), package)
    if declared in known_platforms(package):
        return declared
    probes = (
        ("codex", ("CODEX_HOME", "CODEX_THREAD_ID")),
        ("claude", ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")),
        ("openclaw", ("OPENCLAW_HOME", "OPENCLAW_SESSION_ID")),
        ("hermes", ("HERMES_HOME", "HERMES_SESSION_ID")),
        ("deepseek_harness", ("DSH_HOME", "DEEPSEEK_HARNESS")),
        ("workbuddy", ("CODEBUDDY_HOME", "WORKBUDDY_SESSION_ID")),
    )
    for platform, keys in probes:
        if any(os.environ.get(key) for key in keys):
            return platform
    return "generic"


def external_entry_path(platform: str, package: Path) -> Path | None:
    row = platform_config(package)["platforms"].get(platform)
    if not isinstance(row, dict) or row.get("kind") != "external":
        return None
    if os.environ.get("XIRANG_INSTALL_ROOT"):
        return xirang_user_root() / "platform-entries" / platform / Path(str(row["target"])).name
    return Path(str(row["target"])).expanduser().resolve()


def platform_template(package: Path, platform: str) -> Path | None:
    config = platform_config(package)
    row = config["platforms"].get(platform) or config.get("generic") or {}
    name = row.get("template") if isinstance(row, dict) else None
    return package / "templates" / name if name else None


def has_xirang_signature(target: Path) -> bool:
    return any(
        (target / relative).exists()
        for relative in (
            ".xirang/contract/policy.yaml",
            ".standards/xirang_state.py",
            ".standards/xirang-sm.py",
            "息壤.md",
        )
    ) or (
        (target / "AGENTS.md").is_file()
        and "XIRANG-" in (target / "AGENTS.md").read_text(encoding="utf-8", errors="ignore")
    )


def detect_install(target: Path, package: Path) -> dict:
    version_path = target / "VERSION"
    version = version_path.read_text(encoding="utf-8", errors="replace").strip() if version_path.is_file() else None
    if version == VERSION and has_xirang_signature(target):
        return {"mode": "current", "from_version": VERSION, "result": "current_verified"}

    supported = load_json(package / "baselines/supported.json").get("supported") or []
    candidates = [row for row in supported if not version or str(row.get("version")) == version]
    for row in candidates:
        hashes = row.get("required_hashes") or {}
        observed: dict[str, str | None] = {}
        for logical, expected in hashes.items():
            path = target / safe_relative(str(logical))
            observed[str(logical)] = sha256(path) if path.is_file() and not path.is_symlink() else None
        if hashes and all(observed[key] == expected for key, expected in hashes.items()):
            return {
                "mode": "upgrade",
                "from_version": str(row.get("version")),
                "result": "upgraded",
                "baseline": str(row.get("version")),
            }

    if has_xirang_signature(target):
        reason = "旧版核心文件与受支持基线不一致" if version else "发现无法识别的息壤安装"
        return {"mode": "assistance_required", "from_version": version, "reason": reason}
    return {"mode": "fresh", "from_version": None, "result": "installed"}


def merge_agents(existing: str, managed: str, *, known_legacy_hash: str | None = None) -> str:
    managed = managed.strip() + "\n"
    pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(managed.strip(), existing, count=1).rstrip() + "\n"
    if known_legacy_hash and hashlib.sha256(existing.encode()).hexdigest() == known_legacy_hash:
        return managed
    if not existing.strip():
        return managed
    return existing.rstrip() + "\n\n" + managed


def managed_paths(package: Path) -> list[str]:
    values = {
        path.relative_to(package / "payload").as_posix()
        for path in payload_files(package)
    }
    return sorted(values | GENERATED_PATHS)


def runtime_path(target: Path) -> Path:
    config = target / ".xirang/local-config.json"
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
        raw = value.get("runtime_dir")
    except (OSError, json.JSONDecodeError, TypeError):
        raw = None
    return Path(str(raw)).expanduser().resolve() if raw else xirang_user_root() / "workspaces" / workspace_id(target)


def launchd_path(target: Path) -> Path:
    if os.environ.get("XIRANG_INSTALL_ROOT"):
        return xirang_user_root() / "launch-agents" / f"com.xirang.{workspace_id(target)}.status-refresh.plist"
    return Path.home() / "Library/LaunchAgents" / f"com.xirang.{workspace_id(target)}.status-refresh.plist"


def recovery_roots() -> dict[str, Path]:
    base = xirang_user_root()
    roots = {
        "objects": base / "recovery-objects",
        "manifests": base / "recovery-manifests",
        "audit": base / "rescue-audit",
        "transactions": base / "install-transactions",
        "locks": base / "install-locks",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


def append_audit(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def install_lock(target: Path):
    roots = recovery_roots()
    lock_path = roots["locks"] / f"{workspace_id(target)}.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def journal_paths(target: Path) -> list[Path]:
    directory = xirang_user_root() / "install-transactions"
    if not directory.is_dir():
        return []
    result = []
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if value.get("target") == str(target):
            result.append(path)
    return sorted(result, key=lambda path: path.stat().st_mtime, reverse=True)


def pending_journal(target: Path) -> tuple[Path, dict] | None:
    for path in journal_paths(target):
        value = load_json(path)
        if value.get("state") not in TERMINAL_STATES:
            return path, value
    return None


def update_journal(path: Path, value: dict, state: str, **extra: object) -> dict:
    updated = {**value, **extra, "state": state, "updated_at": now_iso()}
    atomic_json(path, updated, 0o600)
    return updated


def _tar_add_file(archive: tarfile.TarFile, source: Path, logical: str) -> None:
    info = archive.gettarinfo(str(source), arcname=logical)
    if not info.isfile():
        raise InstallError(f"恢复快照只接受普通文件：{source}", code="unsafe_target")
    with source.open("rb") as handle:
        archive.addfile(info, handle)


def create_snapshot(target: Path, package: Path, transaction_id: str, platform: str) -> tuple[Path, dict]:
    roots = recovery_roots()
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    object_path = roots["objects"] / f"xirang-install-{workspace_id(target)}-{timestamp}-{transaction_id[:8]}.tar.gz"
    absent: list[str] = []
    captured: list[str] = []
    runtime = runtime_path(target)
    plist = launchd_path(target)
    external = external_entry_path(platform, package)
    with tarfile.open(object_path, "w:gz") as archive:
        for logical in managed_paths(package):
            path = target / safe_relative(logical)
            if path.is_symlink():
                raise InstallError(f"受管目标是符号链接：{logical}", code="unsafe_target")
            if path.is_file():
                _tar_add_file(archive, path, f"workspace/{logical}")
                captured.append(f"workspace/{logical}")
            elif path.exists():
                raise InstallError(f"受管目标不是普通文件：{logical}", code="unsafe_target")
            else:
                absent.append(f"workspace/{logical}")
        if runtime.exists():
            for path in sorted(runtime.rglob("*")):
                if path.is_symlink():
                    raise InstallError(f"运行目录含符号链接：{path}", code="unsafe_target")
                if path.is_file():
                    logical = f"runtime/{path.relative_to(runtime).as_posix()}"
                    _tar_add_file(archive, path, logical)
                    captured.append(logical)
        else:
            absent.append("runtime/")
        if plist.is_symlink():
            raise InstallError("launchd 配置是符号链接", code="unsafe_target")
        if plist.is_file():
            _tar_add_file(archive, plist, "launchd/status-refresh.plist")
            captured.append("launchd/status-refresh.plist")
        else:
            absent.append("launchd/status-refresh.plist")
        if external is not None:
            if external.is_symlink():
                raise InstallError(f"平台入口是符号链接：{external}", code="unsafe_target")
            if external.is_file():
                _tar_add_file(archive, external, "external/platform-entry.md")
                captured.append("external/platform-entry.md")
            elif external.exists():
                raise InstallError(f"平台入口不是普通文件：{external}", code="unsafe_target")
            else:
                absent.append("external/platform-entry.md")
    object_sha = sha256(object_path)
    manifest = {
        "schema_version": 1,
        "artifact_type": "xirang_install_preimage",
        "transaction_id": transaction_id,
        "target": str(target),
        "workspace_id": workspace_id(target),
        "runtime": str(runtime),
        "launchd": str(plist),
        "platform": platform,
        "external_entry": str(external) if external is not None else None,
        "object": str(object_path),
        "sha256": object_sha,
        "size": object_path.stat().st_size,
        "captured": captured,
        "absent": absent,
        "created_at": now_iso(),
    }
    manifest_path = roots["manifests"] / f"{object_path.stem}.json"
    atomic_json(manifest_path, manifest, 0o600)
    append_audit(roots["audit"] / "xirang-install.jsonl", {"event": "snapshot_created", **manifest, "manifest": str(manifest_path)})
    return manifest_path, manifest


def restore_snapshot(manifest_path: Path, target: Path, package: Path) -> None:
    manifest = load_json(manifest_path)
    if manifest.get("target") != str(target) or manifest.get("workspace_id") != workspace_id(target):
        raise InstallError("恢复快照与目标工作区不匹配", code="recovery_invalid")
    object_path = Path(str(manifest.get("object"))).expanduser().resolve()
    if not object_path.is_file() or object_path.is_symlink() or sha256(object_path) != manifest.get("sha256"):
        raise InstallError("恢复对象缺失或哈希不匹配", code="recovery_invalid")
    runtime = Path(str(manifest.get("runtime"))).expanduser().resolve()
    plist = Path(str(manifest.get("launchd"))).expanduser().resolve()
    external_raw = manifest.get("external_entry")
    external = Path(str(external_raw)).expanduser().resolve() if external_raw else None
    for logical in managed_paths(package):
        path = target / safe_relative(logical)
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.exists():
            raise InstallError(f"恢复目标类型异常：{logical}", code="recovery_invalid")
    if runtime.exists():
        if runtime.is_symlink() or not runtime.is_dir():
            raise InstallError("恢复运行目录类型异常", code="recovery_invalid")
        shutil.rmtree(runtime)
    if plist.is_file() or plist.is_symlink():
        plist.unlink()
    if external is not None:
        if external.is_file() or external.is_symlink():
            external.unlink()
        elif external.exists():
            raise InstallError("恢复平台入口类型异常", code="recovery_invalid")
    with tarfile.open(object_path, "r:gz") as archive:
        for member in archive.getmembers():
            logical = safe_relative(member.name)
            if not member.isfile():
                raise InstallError(f"恢复对象含非普通文件：{member.name}", code="recovery_invalid")
            if logical.parts[0] == "workspace":
                destination = target.joinpath(*logical.parts[1:])
            elif logical.parts[0] == "runtime":
                destination = runtime.joinpath(*logical.parts[1:])
            elif logical.as_posix() == "launchd/status-refresh.plist":
                destination = plist
            elif logical.as_posix() == "external/platform-entry.md" and external is not None:
                destination = external
            else:
                raise InstallError(f"恢复对象路径越界：{member.name}", code="recovery_invalid")
            source = archive.extractfile(member)
            if source is None:
                raise InstallError(f"恢复对象不可读：{member.name}", code="recovery_invalid")
            atomic_bytes(destination, source.read(), stat.S_IMODE(member.mode) or 0o644)


def write_recovery_registry(target: Path) -> None:
    base = xirang_user_root()
    value = "\n".join([
        "schema_version: 1",
        f"workspace_id: {workspace_id(target)}",
        "minimum_free_bytes: 1073741824",
        "primary:",
        f"  objects: {base / 'recovery-objects'}",
        f"  manifests: {base / 'recovery-manifests'}",
        f"  audit: {base / 'rescue-audit'}",
        "fallback:",
        f"  root: {base / 'rollback'}",
        "forbidden_roots:",
        f"  runtime: {base / 'workspaces'}",
        f"  cache: {base / 'cache'}",
        f"  logs: {base / 'logs'}",
        "policy:",
        "  require_existing: true",
        "  forbid_vault_descendant: true",
        "  forbid_ad_hoc_fallback: true",
        "  require_manifest_and_sha256: true",
        "  refuse_restore_overwrite: true",
        "",
    ])
    for relative in ("recovery-objects", "recovery-manifests", "rescue-audit", "rollback"):
        (base / relative).mkdir(parents=True, exist_ok=True)
    atomic_bytes(target / ".xirang/contract/recovery-roots.yaml", value.encode())


def run_json(command: list[str], *, env: dict[str, str] | None = None) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InstallError(
            f"子程序没有返回 JSON：{' '.join(command[:3])}; stderr={completed.stderr.strip()}",
            code="subprocess_failed",
        ) from exc
    if completed.returncode != 0 or result.get("ok") is False:
        raise InstallError(
            f"子程序失败：{' '.join(command[:3])}; {result.get('message') or result.get('findings') or completed.stderr.strip()}",
            code="subprocess_failed",
        )
    return result


def install_payload(target: Path, package: Path) -> None:
    payload = package / "payload"
    agents_source = payload / "AGENTS.md"
    existing_path = target / "AGENTS.md"
    existing = existing_path.read_text(encoding="utf-8") if existing_path.is_file() else ""
    known_legacy = "9910c6e7c7ef0b424276024bf26af8d6aa29377fe3df4304ad919fbd23f45e04"
    merged = merge_agents(existing, agents_source.read_text(encoding="utf-8"), known_legacy_hash=known_legacy)
    for source in payload_files(package):
        relative = source.relative_to(payload)
        if relative.as_posix() == "AGENTS.md":
            continue
        destination = target / relative
        if destination.is_symlink():
            raise InstallError(f"目标路径是符号链接：{relative.as_posix()}", code="unsafe_target")
        atomic_bytes(destination, source.read_bytes(), stat.S_IMODE(source.stat().st_mode) or 0o644)
    atomic_bytes(existing_path, merged.encode())
    write_recovery_registry(target)


def merge_platform_entry(existing: str, managed: str) -> str:
    block_match = re.search(
        re.escape(PLATFORM_START) + r".*?" + re.escape(PLATFORM_END),
        managed,
        re.DOTALL,
    )
    if not block_match:
        raise InstallError("平台模板缺少受管标记", code="package_invalid")
    block = block_match.group(0)
    pattern = re.compile(re.escape(PLATFORM_START) + r".*?" + re.escape(PLATFORM_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(block, existing, count=1).rstrip() + "\n"
    return (existing.rstrip() + "\n\n" if existing.strip() else "") + block + "\n"


def install_platform_entry(target: Path, package: Path, platform: str) -> dict:
    registry_path = target / ".xirang/adapters/registry.json"
    registry = load_json(registry_path)
    if platform == "generic":
        return {"platform": platform, "state": "workspace_contract_only", "path": str(target / "AGENTS.md")}
    known = platform in known_platforms(package)
    external = external_entry_path(platform, package)
    if external is None:
        entry = target / (".claude/CLAUDE.md" if platform == "claude" else "AGENTS.md")
        state = "workspace_entry_configured"
    else:
        template = platform_template(package, platform)
        if template is None or not template.is_file():
            raise InstallError(f"平台模板缺失：{platform}", code="package_invalid")
        old = external.read_text(encoding="utf-8") if external.is_file() else ""
        atomic_bytes(external, merge_platform_entry(old, template.read_text(encoding="utf-8")).encode())
        entry = external
        state = "native_entry_applied"
    platforms = registry.setdefault("platforms", {})
    row = platforms.get(platform)
    if not isinstance(row, dict):
        row = {
            "platform_id": platform,
            "allowed_mode": "contract_only",
            "application_state": "workspace_contract_only",
            "canary_state": "unverified",
            "connected": False,
            "instruction_entries": [{"path": "AGENTS.md", "scope": "workspace", "role": "project_contract", "management_state": "managed"}],
            "runtime_authority": "sqlite",
        }
        platforms[platform] = row
    row["application_state"] = "applied" if known else "workspace_contract_only"
    row["canary_state"] = "unverified"
    row["connected"] = False
    row["application_evidence"] = {
        "kind": "universal_installer",
        "path": str(entry),
        "sha256": sha256(entry),
        "applied_at": now_iso(),
    }
    atomic_json(registry_path, registry)
    return {"platform": platform, "registered_template": known, "state": state, "path": str(entry), "sha256": sha256(entry), "canary": "unverified"}


def bootstrap_runtime(target: Path, *, no_scheduler: bool) -> dict:
    python = str(Path(sys.executable).resolve())
    migrate = str(target / ".standards/xirang_state_migrate.py")
    runtime = runtime_path(target)
    environment = dict(os.environ)
    environment["XIRANG_RUNTIME_DIR"] = str(runtime)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    status_command = [python, migrate, "status", "--root", str(target), "--runtime", str(runtime)]
    status_probe = subprocess.run(status_command, capture_output=True, text=True, check=False, env=environment)
    try:
        current = json.loads(status_probe.stdout)
    except (json.JSONDecodeError, TypeError):
        current = {}
    if status_probe.returncode == 0 and current.get("ok") is True:
        shadow = {"ok": True, "mode": "existing_active", "database": current.get("database")}
        finalize = current
    else:
        shadow = run_json([python, migrate, "shadow", "--root", str(target), "--runtime", str(runtime)], env=environment)
        finalize = run_json([python, migrate, "finalize", "--root", str(target), "--runtime", str(runtime)], env=environment)
    configure_command = [python, str(target / ".standards/xirang-configure.py"), "install", "--root", str(target), "--python", python]
    if no_scheduler:
        configure_command.append("--no-scheduler")
    configured = run_json(configure_command, env=environment)
    return {"shadow": shadow, "finalize": finalize, "configured": configured}


def core_manifest(package: Path) -> dict:
    return load_json(package / "manifests/core-manifest.json")


def write_installed_manifest(target: Path, package: Path, detection: dict, transaction_id: str, platform: str) -> dict:
    core = core_manifest(package)
    block = re.search(
        re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END),
        (target / "AGENTS.md").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    value = {
        "schema_version": 1,
        "version": VERSION,
        "installed_at": now_iso(),
        "transaction_id": transaction_id,
        "mode": detection["mode"],
        "from_version": detection.get("from_version"),
        "platform": platform,
        "workspace_id": workspace_id(target),
        "core_manifest_sha256": sha256(package / "manifests/core-manifest.json"),
        "managed_agents_block_sha256": hashlib.sha256((block.group(0) if block else "").encode()).hexdigest(),
        "file_count": len(core.get("files") or []),
    }
    atomic_json(target / ".xirang/install/installed-manifest.json", value)
    return value


def verify_install(target: Path, package: Path, *, no_scheduler: bool, platform: str = "generic") -> dict:
    findings: list[str] = []
    core = core_manifest(package)
    for row in core.get("files") or []:
        logical = str(row.get("path") or "")
        if logical == "AGENTS.md":
            expected = (package / "payload/AGENTS.md").read_text(encoding="utf-8").strip()
            try:
                text = (target / "AGENTS.md").read_text(encoding="utf-8")
            except OSError:
                text = ""
            if expected not in text:
                findings.append("AGENTS.md:managed_block_mismatch")
            continue
        if logical == ".xirang/adapters/registry.json":
            try:
                registry = load_json(target / logical)
                registered = set((registry.get("platforms") or {}).keys())
            except InstallError:
                registered = set()
            if not known_platforms(package).issubset(registered):
                findings.append(f"{logical}:platform_registry_mismatch")
            continue
        path = target / safe_relative(logical)
        if not path.is_file() or path.is_symlink() or sha256(path) != row.get("sha256"):
            findings.append(f"{logical}:hash_mismatch")
    for logical in (".codex/hooks.json", ".claude/settings.json", ".claude/CLAUDE.md", ".xirang/local-config.json", ".xirang/contract/recovery-roots.yaml", ".xirang/install/installed-manifest.json"):
        if not (target / logical).is_file():
            findings.append(f"{logical}:missing")
    external = external_entry_path(platform, package)
    if external is not None:
        try:
            external_text = external.read_text(encoding="utf-8")
        except OSError:
            external_text = ""
        if PLATFORM_START not in external_text or PLATFORM_END not in external_text:
            findings.append(f"platform:{platform}:entry_missing")
    python = str(Path(sys.executable).resolve())
    state: dict = {}
    configure: dict = {}
    if not findings:
        try:
            environment = dict(os.environ)
            environment["XIRANG_RUNTIME_DIR"] = str(runtime_path(target))
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            state = run_json([python, str(target / ".standards/xirang_state_migrate.py"), "status", "--root", str(target)], env=environment)
            command = [python, str(target / ".standards/xirang-configure.py"), "check", "--root", str(target), "--python", python]
            if no_scheduler:
                command.append("--no-scheduler")
            configure = run_json(command, env=environment)
        except InstallError as exc:
            findings.append(f"runtime:{exc}")
    return {
        "ok": not findings,
        "status": "current_verified" if not findings else "verification_failed",
        "version": VERSION,
        "target": str(target),
        "workspace_id": workspace_id(target),
        "findings": findings,
        "state_store": state,
        "configuration": configure,
        "platform_canary": "awaiting_new_session",
        "platform": platform,
    }


def plan(target: Path, package: Path, platform: str) -> dict:
    verify_package(package)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise InstallError("目标工作区必须是非符号链接目录", code="unsafe_target")
    recovery = pending_journal(target)
    if recovery:
        return {"ok": False, "status": "recovery_required", "target": str(target), "transaction_id": recovery[1].get("transaction_id")}
    detection = detect_install(target, package)
    if detection["mode"] == "assistance_required":
        return {"ok": False, "status": "assistance_required", "target": str(target), **detection}
    action = "检查并修复" if detection["mode"] == "current" else ("从旧版升级" if detection["mode"] == "upgrade" else "全新安装")
    external = external_entry_path(platform, package)
    return {
        "ok": True,
        "status": "ready",
        "target": str(target),
        "version": VERSION,
        **detection,
        "platform": platform,
        "confirmation_card": {
            "判断": action,
            "目标": str(target),
            "保留": "业务文件、未知文件、Git 历史、AGENTS.md 中项目自定义规则",
            "备份": "写入前对受管文件、StateStore 运行目录和 launchd 配置建立带哈希快照",
            "修改": "V9.7 机器契约、完整方法论宪法、StateStore、通用 Agent 根规范与当前宿主入口",
            "平台入口": str(external) if external is not None else f"工作区内 {platform} 入口",
            "支持": "macOS + Python 3.11+；当前宿主自动识别，已登记平台使用对应入口，未登记平台安全降级；入口安装后待新会话验证",
            "下一步": "用户确认开始后执行 apply",
        },
    }


def apply(target: Path, package: Path, *, no_scheduler: bool, inject_failure: str | None, platform: str) -> dict:
    verify_package(package)
    target.mkdir(parents=True, exist_ok=True)
    with install_lock(target):
        if pending := pending_journal(target):
            return {"ok": False, "status": "recovery_required", "target": str(target), "transaction_id": pending[1].get("transaction_id")}
        detection = detect_install(target, package)
        if detection["mode"] == "assistance_required":
            return {"ok": False, "status": "assistance_required", "target": str(target), **detection}
        transaction_id = str(uuid.uuid4())
        roots = recovery_roots()
        journal_path = roots["transactions"] / f"{transaction_id}.json"
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "target": str(target),
            "workspace_id": workspace_id(target),
            "version": VERSION,
            "detection": detection,
            "platform": platform,
            "state": "preparing",
            "created_at": now_iso(),
        }
        atomic_json(journal_path, journal, 0o600)
        try:
            snapshot_path, snapshot = create_snapshot(target, package, transaction_id, platform)
            journal = update_journal(journal_path, journal, "prepared", snapshot_manifest=str(snapshot_path), snapshot_sha256=snapshot["sha256"])
            if inject_failure == "after_snapshot":
                raise InstallError("injected failure after snapshot", code="injected_failure")
            journal = update_journal(journal_path, journal, "writing")
            install_payload(target, package)
            platform_result = install_platform_entry(target, package, platform)
            if inject_failure == "after_payload":
                raise InstallError("injected failure after payload", code="injected_failure")
            runtime = bootstrap_runtime(target, no_scheduler=no_scheduler)
            journal = update_journal(journal_path, journal, "configured")
            installed = write_installed_manifest(target, package, detection, transaction_id, platform)
            verification = verify_install(target, package, no_scheduler=no_scheduler, platform=platform)
            if not verification["ok"]:
                raise InstallError(str(verification["findings"]), code="verification_failed")
            journal = update_journal(journal_path, journal, "completed", completed_at=now_iso())
            append_audit(roots["audit"] / "xirang-install.jsonl", {"event": "install_completed", "transaction_id": transaction_id, "target": str(target), "version": VERSION, "at": now_iso()})
            return {"ok": True, "status": detection["result"], "target": str(target), "version": VERSION, "transaction_id": transaction_id, "platform_entry": platform_result, "installed_manifest": installed, "runtime": runtime, "verification": verification}
        except Exception as exc:
            snapshot_raw = journal.get("snapshot_manifest")
            if snapshot_raw:
                try:
                    restore_snapshot(Path(str(snapshot_raw)), target, package)
                    update_journal(journal_path, journal, "rolled_back", error=str(exc), rolled_back_at=now_iso())
                    append_audit(roots["audit"] / "xirang-install.jsonl", {"event": "install_rolled_back", "transaction_id": transaction_id, "target": str(target), "error": str(exc), "at": now_iso()})
                    return {"ok": False, "status": "rolled_back", "target": str(target), "transaction_id": transaction_id, "error": str(exc)}
                except Exception as recovery_exc:
                    update_journal(journal_path, journal, "recovery_required", error=str(exc), recovery_error=str(recovery_exc))
                    return {"ok": False, "status": "recovery_required", "target": str(target), "transaction_id": transaction_id, "error": str(exc), "recovery_error": str(recovery_exc)}
            update_journal(journal_path, journal, "rolled_back", error=str(exc))
            return {"ok": False, "status": "rolled_back", "target": str(target), "transaction_id": transaction_id, "error": str(exc)}


def recover(target: Path, package: Path) -> dict:
    verify_package(package)
    with install_lock(target):
        pending = pending_journal(target)
        if not pending:
            return {"ok": True, "status": "nothing_to_recover", "target": str(target)}
        path, journal = pending
        manifest = journal.get("snapshot_manifest")
        if not manifest:
            raise InstallError("未完成事务没有恢复快照", code="recovery_invalid")
        restore_snapshot(Path(str(manifest)), target, package)
        update_journal(path, journal, "rolled_back", recovered_at=now_iso())
        append_audit(recovery_roots()["audit"] / "xirang-install.jsonl", {"event": "manual_recovery_completed", "transaction_id": journal.get("transaction_id"), "target": str(target), "at": now_iso()})
        return {"ok": True, "status": "rolled_back", "target": str(target), "transaction_id": journal.get("transaction_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description="息壤 V9.7 通用安装与升级器")
    parser.add_argument("action", choices=("plan", "apply", "verify", "recover"))
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--no-scheduler", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--platform", default="auto", help="Agent 自己识别的宿主 ID；未登记 ID 安全降级到通用工作区入口")
    parser.add_argument("--inject-failure", choices=("after_snapshot", "after_payload"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    package = package_root()
    target = args.target.expanduser().resolve()
    platform = detect_platform(args.platform, package)
    try:
        if sys.platform != "darwin":
            raise InstallError("当前正式支持 macOS", code="unsupported_platform")
        if sys.version_info < (3, 11):
            raise InstallError("需要 Python 3.11 或更高版本", code="unsupported_python")
        if args.action == "plan":
            result = plan(target, package, platform)
        elif args.action == "apply":
            result = apply(target, package, no_scheduler=args.no_scheduler, inject_failure=args.inject_failure, platform=platform)
        elif args.action == "verify":
            verify_package(package)
            result = verify_install(target, package, no_scheduler=args.no_scheduler, platform=platform)
        else:
            result = recover(target, package)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") is True else 2
    except InstallError as exc:
        print(json.dumps({"ok": False, "status": exc.code, "message": str(exc), "target": str(target)}, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "internal_error", "error": type(exc).__name__, "message": str(exc), "target": str(target)}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
