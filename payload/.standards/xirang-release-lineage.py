#!/usr/bin/env python3
"""Production-Tag release-lineage gate for XiRang.

This is a project-neutral, standard-library capability.  The local builder is
treated as untrusted: it can create a task lock and an envelope, while the host
independently verifies the protected baseline, Git ancestry/tag binding,
artifact closure, source-committed artifact specification, package digests and
baseline revision CAS before an atomic promotion.

The module deliberately has no site, client, Ops or server address baked in.
"""

from __future__ import annotations

import argparse
import ast
import base64
import fcntl
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence


BASELINE_SCHEMA = "xirang-production-baseline/v1"
TASK_LOCK_SCHEMA = "xirang-production-lineage-task-lock/v1"
ARTIFACT_SCHEMA = "xirang-production-artifact/v1"
ARTIFACT_SPEC_SCHEMA = "xirang-release-artifact-spec/v1"
BOOTSTRAP_ARTIFACT_SCHEMA = "xirang-bootstrap-artifact/v1"
BOOTSTRAP_RECEIPT_SCHEMA = "xirang-bootstrap-receipt/v1"
BOOTSTRAP_JOURNAL_SCHEMA = "xirang-bootstrap-journal/v1"
ENVELOPE_SCHEMA = "xirang-production-lineage-envelope/v1"
RECEIPT_SCHEMA = "xirang-production-lineage-receipt/v1"
JOURNAL_SCHEMA = "xirang-production-lineage-journal/v1"
ARTIFACT_SPEC_PATH = ".xirang/release-artifact.json"
CORE_ENTRY_PATH = ".standards/xirang-release-lineage.py"

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_ENTRIES = 10_000
MAX_CONTAINER_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024
MAX_RATIO = 100
GIT_TIMEOUT_SECONDS = 60
GIT_OUTPUT_LIMIT = 8 * 1024 * 1024
GIT_MEMORY_LIMIT = 2 * 1024 * 1024 * 1024
GIT_FILE_LIMIT = 1024 * 1024 * 1024
GIT_NOFILE_LIMIT = 256
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_DOS_TIME = 0
ZIP_DOS_DATE = 33
GIT_BINARY = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")


class LineageError(RuntimeError):
    """A stable, auditable gate failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_mapping(value: Any, code: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LineageError(code, f"{label}必须是 JSON object")
    return value


def require_digest(value: Any, code: str, label: str) -> str:
    text = str(value or "")
    if not DIGEST_RE.fullmatch(text):
        raise LineageError(code, f"{label}不是 SHA-256")
    return text


def require_commit(value: Any, code: str, label: str) -> str:
    text = str(value or "")
    if not COMMIT_RE.fullmatch(text):
        raise LineageError(code, f"{label}不是完整 Git commit")
    return text


def require_name(value: Any, code: str, label: str) -> str:
    text = str(value or "")
    if not NAME_RE.fullmatch(text) or text in {".", ".."}:
        raise LineageError(code, f"{label}不是安全的单段名称")
    return text


def require_repo_path(value: Any, code: str, label: str) -> str:
    text = str(value or "").replace("\\", "/")
    pure = PurePosixPath(text)
    if (
        not text
        or pure.is_absolute()
        or text.startswith("./")
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise LineageError(code, f"{label}不是安全的仓库相对路径")
    return pure.as_posix()


def approved_candidate_ref(task_id: str, baseline_revision: int) -> str:
    if not TASK_RE.fullmatch(str(task_id or "")):
        raise LineageError("TASK_ID_INVALID", "taskId 不能安全映射到授权 ref")
    if not isinstance(baseline_revision, int) or baseline_revision < 1:
        raise LineageError("BASELINE_INVALID", "baseline revision 无效")
    return f"refs/xirang/release-candidates/{task_id}/{baseline_revision}"


def load_json_bytes(value: bytes, code: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise LineageError(code, f"{label}不是有效 UTF-8 JSON：{exc}") from exc
    return dict(require_mapping(parsed, code, label))


def load_json_path(path: Path, code: str, label: str) -> dict[str, Any]:
    try:
        return load_json_bytes(path.read_bytes(), code, label)
    except OSError as exc:
        raise LineageError(code, f"{label}不可读：{exc}") from exc


def limit_git_child() -> None:
    os.umask(0o077)
    limits = (
        (resource.RLIMIT_CPU, GIT_TIMEOUT_SECONDS),
        (resource.RLIMIT_FSIZE, GIT_FILE_LIMIT),
        (resource.RLIMIT_NOFILE, GIT_NOFILE_LIMIT),
    )
    if hasattr(resource, "RLIMIT_AS"):
        limits += ((resource.RLIMIT_AS, GIT_MEMORY_LIMIT),)
    for kind, requested in limits:
        soft, hard = resource.getrlimit(kind)
        ceiling = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        selected = ceiling if soft == resource.RLIM_INFINITY else min(soft, ceiling)
        try:
            resource.setrlimit(kind, (selected, hard))
        except (OSError, ValueError):
            # Darwin exposes address/data constants that its kernel refuses to
            # lower for this process type. CPU/FSIZE/NOFILE still fail closed;
            # Linux additionally enforces RLIMIT_AS.
            if kind not in {
                getattr(resource, "RLIMIT_AS", -999),
                getattr(resource, "RLIMIT_DATA", -998),
                getattr(resource, "RLIMIT_RSS", -997),
            }:
                raise


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_git(repo: Path, args: Sequence[str]) -> tuple[int, bytes, bytes]:
    if not GIT_BINARY:
        raise LineageError("GIT_UNAVAILABLE", "系统 Git 不可用")
    try:
        process = subprocess.Popen(
            [GIT_BINARY, *args],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"},
            start_new_session=True,
            preexec_fn=limit_git_child,
        )
    except OSError as exc:
        raise LineageError("GIT_COMMAND_FAILED", f"无法启动 git：{exc}") from exc
    output = bytearray()
    errors = bytearray()
    overflow = threading.Event()

    def drain(stream: Any, bucket: bytearray) -> None:
        try:
            reader = getattr(stream, "read1", stream.read)
            for chunk in iter(lambda: reader(64 * 1024), b""):
                if len(bucket) + len(chunk) > GIT_OUTPUT_LIMIT:
                    overflow.set()
                    kill_process_group(process)
                    continue
                bucket.extend(chunk)
        finally:
            stream.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, output), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, errors), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        kill_process_group(process)
        process.wait()
        for thread in threads:
            thread.join()
        raise LineageError("GIT_TIMEOUT", f"git {args[0] if args else ''} 超时，已终止进程组") from exc
    for thread in threads:
        thread.join()
    if overflow.is_set():
        raise LineageError("GIT_OUTPUT_LIMIT", "Git 输出超过限制，已终止进程组")
    if return_code < 0:
        raise LineageError("GIT_RESOURCE_LIMIT", f"Git 因信号 {-return_code} 终止")
    return return_code, bytes(output), bytes(errors)


def git_bytes(repo: Path, args: Sequence[str], *, optional: bool = False) -> bytes:
    return_code, output, errors = run_git(repo, args)
    if return_code != 0:
        if optional:
            return b""
        detail = (errors or output).decode("utf-8", errors="replace").strip()
        raise LineageError(
            "GIT_COMMAND_FAILED",
            f"git {' '.join(args)} 失败" + (f"：{detail}" if detail else ""),
        )
    return output


def git_text(repo: Path, args: Sequence[str], *, optional: bool = False) -> str:
    return git_bytes(repo, args, optional=optional).decode("utf-8", errors="strict").strip()


def tag_commit(repo: Path, tag: str, *, optional: bool = False) -> str:
    require_name(tag, "TAG_INVALID", "Tag")
    return git_text(repo, ["rev-parse", f"refs/tags/{tag}^{{commit}}"], optional=optional)


def tag_object(repo: Path, tag: str, *, optional: bool = False) -> str:
    require_name(tag, "TAG_INVALID", "Tag")
    return git_text(repo, ["rev-parse", f"refs/tags/{tag}"], optional=optional)


def require_ancestor(repo: Path, base: str, source: str) -> None:
    return_code, _output, _errors = run_git(
        repo, ["merge-base", "--is-ancestor", base, source],
    )
    if return_code != 0:
        raise LineageError("NON_DESCENDANT_COMMIT", f"sourceCommit {source} 不是 baseCommit {base} 的后代")


def repository_state(repo: Path) -> dict[str, Any]:
    root = repo.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise LineageError("REPOSITORY_INVALID", f"仓库根不存在或为符号链接：{root}")
    top = Path(git_text(root, ["rev-parse", "--show-toplevel"])).resolve()
    if top != root:
        raise LineageError("ARBITRARY_BUILD_ROOT", f"必须从 Git 工作树根执行，实际根为 {top}")
    status = git_text(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    return {
        "root": root,
        "head": git_text(root, ["rev-parse", "HEAD"]),
        "clean": not status,
        "dirtyEntries": status.splitlines() if status else [],
    }


def read_baseline(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        raw = path.expanduser().resolve().read_bytes()
    except OSError as exc:
        raise LineageError("BASELINE_MISSING", f"生产基线不可读：{exc}") from exc
    baseline = load_json_bytes(raw, "BASELINE_INVALID", "production-baseline")
    current = require_mapping(baseline.get("current"), "BASELINE_INVALID", "production-baseline.current")
    if baseline.get("schema") != BASELINE_SCHEMA:
        raise LineageError("BASELINE_INVALID", "production-baseline schema 无效")
    if not isinstance(baseline.get("revision"), int) or baseline["revision"] < 1:
        raise LineageError("BASELINE_INVALID", "production-baseline revision 无效")
    require_name(current.get("release"), "BASELINE_INVALID", "current.release")
    require_name(current.get("sourceTag"), "BASELINE_INVALID", "current.sourceTag")
    require_commit(current.get("sourceTagObject"), "BASELINE_INVALID", "current.sourceTagObject")
    require_commit(current.get("sourceCommit"), "BASELINE_INVALID", "current.sourceCommit")
    artifact = require_repo_path(current.get("artifact"), "BASELINE_INVALID", "current.artifact")
    if not artifact.startswith("packages/"):
        raise LineageError("BASELINE_INVALID", "current.artifact 必须位于 packages/ 下")
    artifact_sha = require_digest(current.get("artifactSha256"), "BASELINE_INVALID", "current.artifactSha256")
    if current.get("packageSha256") not in {None, artifact_sha}:
        raise LineageError("BASELINE_INVALID", "artifactSha256 与 packageSha256 不一致")
    require_digest(
        current.get("productionTreeSha256"),
        "BASELINE_INVALID",
        "current.productionTreeSha256",
    )
    if current.get("dirtyWorktree") is not False:
        raise LineageError("BASELINE_INVALID", "生产基线必须明确 dirtyWorktree=false")
    return baseline, raw, sha256_bytes(raw)


def validate_baseline_tag(repo: Path, baseline: Mapping[str, Any]) -> None:
    current = require_mapping(baseline.get("current"), "BASELINE_INVALID", "current")
    tag = require_name(current.get("sourceTag"), "BASELINE_INVALID", "current.sourceTag")
    expected = require_commit(current.get("sourceCommit"), "BASELINE_INVALID", "current.sourceCommit")
    expected_object = require_commit(
        current.get("sourceTagObject"), "BASELINE_INVALID", "current.sourceTagObject",
    )
    actual = tag_commit(repo, tag, optional=True)
    if not actual:
        raise LineageError("BASE_TAG_UNKNOWN", "生产基线 Tag 不在可信仓库")
    if actual != expected:
        raise LineageError("TAG_REBIND", f"生产基线 Tag 已移动：{actual} != {expected}")
    require_annotated_tag(repo, tag, expected)
    actual_object = tag_object(repo, tag)
    if actual_object != expected_object:
        raise LineageError("TAG_REBIND", f"生产基线 Tag object 已移动：{actual_object} != {expected_object}")


def ensure_outside_repo(repo: Path, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return resolved
    raise LineageError("ARBITRARY_BUILD_ROOT", f"{label}必须位于源码仓库之外")


def write_new_file(path: Path, value: bytes) -> None:
    target = path.expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            str(target),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise LineageError("OUTPUT_EXISTS", f"目标已存在：{target}") from exc


def write_atomic_new_file(path: Path, value: bytes) -> None:
    target = path.expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(raw_temporary)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise LineageError("RECOVERY_REQUIRED", "已有发布事务日志，拒绝覆盖") from exc
        linked = True
        descriptor = os.open(str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
        if linked:
            descriptor = os.open(str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LineageError("FSYNC_UNSAFE", f"拒绝同步非普通文件：{path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    directories = [root]
    for item in sorted(root.rglob("*")):
        if item.is_symlink() or (not item.is_file() and not item.is_dir()):
            raise LineageError("FSYNC_UNSAFE", f"拒绝同步特殊文件：{item}")
        if item.is_file():
            fsync_file(item)
        else:
            directories.append(item)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def durable_update_ref(repo: Path, ref: str, new_value: str | None,
                       old_value: str | None = None) -> None:
    require_repo_path(ref, "TAG_INVALID", "Git ref")
    args = ["-c", "core.fsync=reference", "update-ref"]
    if new_value is None:
        args.extend(["-d", ref])
        if old_value is not None:
            require_commit(old_value, "COMMIT_INVALID", "old ref value")
            args.append(old_value)
    else:
        require_commit(new_value, "COMMIT_INVALID", "new ref value")
        args.extend([ref, new_value])
        if old_value is not None:
            require_commit(old_value, "COMMIT_INVALID", "old ref value")
            args.append(old_value)
    git_bytes(repo, args)
    ref_path = repo.joinpath(*ref.split("/"))
    if ref_path.is_file() and not ref_path.is_symlink():
        os.chmod(ref_path, 0o600)
        fsync_file(ref_path)
    packed_refs = repo / "packed-refs"
    if packed_refs.is_file() and not packed_refs.is_symlink():
        fsync_file(packed_refs)
    cursor = ref_path.parent
    while cursor == repo or repo in cursor.parents:
        if cursor.is_dir() and not cursor.is_symlink():
            descriptor = os.open(str(cursor), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if cursor == repo:
            break
        cursor = cursor.parent


def freeze_regular_file(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(source), flags)
    except OSError as exc:
        raise LineageError("ENVELOPE_INVALID", f"无法冻结 staging envelope：{exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LineageError("ENVELOPE_INVALID", "staging envelope 不是普通文件")
        if before.st_size > MAX_CONTAINER_BYTES:
            raise LineageError("ZIP_CONTAINER_LIMIT", "staging envelope 超过容器大小限制")
        with os.fdopen(os.dup(descriptor), "rb") as input_handle, destination.open("xb") as output:
            total = 0
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                total += len(chunk)
                if total > MAX_CONTAINER_BYTES:
                    destination.unlink(missing_ok=True)
                    raise LineageError("ZIP_CONTAINER_LIMIT", "staging envelope 读取时超过容器大小限制")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        fingerprint = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )
        if fingerprint(before) != fingerprint(after):
            destination.unlink(missing_ok=True)
            raise LineageError("ENVELOPE_CHANGED_DURING_READ", "staging envelope 在冻结期间发生变化")
    finally:
        os.close(descriptor)


def start_task_lock(repo: Path, baseline_path: Path, task_id: str, output: Path) -> dict[str, Any]:
    if not TASK_RE.fullmatch(task_id):
        raise LineageError("TASK_ID_INVALID", "task-id 格式无效")
    state = repository_state(repo)
    if not state["clean"]:
        raise LineageError("DIRTY_WORKTREE", f"任务启动拒绝脏工作树：{state['dirtyEntries'][:10]}")
    baseline, _raw, baseline_sha = read_baseline(baseline_path)
    validate_baseline_tag(state["root"], baseline)
    current = dict(require_mapping(baseline["current"], "BASELINE_INVALID", "current"))
    if state["head"] != current["sourceCommit"]:
        raise LineageError("WRONG_WORKTREE_HEAD", f"任务启动 HEAD={state['head']}，生产基线={current['sourceCommit']}")
    target = ensure_outside_repo(state["root"], output, "task lock")
    lock = {
        "schema": TASK_LOCK_SCHEMA,
        "taskId": task_id,
        "baseRelease": current["release"],
        "baseTag": current["sourceTag"],
        "baseTagObject": current["sourceTagObject"],
        "baseCommit": current["sourceCommit"],
        "baselineRevision": baseline["revision"],
        "baselineSha256": baseline_sha,
        "sourceStartCommit": state["head"],
        "dirtyWorktree": False,
        "capturedAt": utc_now(),
    }
    write_new_file(target, canonical_json(lock))
    return {"ok": True, "status": "task-baseline-locked", "taskLock": str(target), "lock": lock}


def read_task_lock(path: Path) -> dict[str, Any]:
    lock = load_json_path(path, "TASK_LOCK_INVALID", "task lock")
    expected = {
        "schema", "taskId", "baseRelease", "baseTag", "baseTagObject", "baseCommit", "baselineRevision",
        "baselineSha256", "sourceStartCommit", "dirtyWorktree", "capturedAt",
    }
    if set(lock) != expected or lock.get("schema") != TASK_LOCK_SCHEMA:
        raise LineageError("TASK_LOCK_INVALID", "task lock 字段闭包或 schema 无效")
    if not TASK_RE.fullmatch(str(lock.get("taskId") or "")):
        raise LineageError("TASK_LOCK_INVALID", "task lock taskId 无效")
    require_name(lock.get("baseRelease"), "TASK_LOCK_INVALID", "baseRelease")
    require_name(lock.get("baseTag"), "TASK_LOCK_INVALID", "baseTag")
    require_commit(lock.get("baseTagObject"), "TASK_LOCK_INVALID", "baseTagObject")
    require_commit(lock.get("baseCommit"), "TASK_LOCK_INVALID", "baseCommit")
    require_commit(lock.get("sourceStartCommit"), "TASK_LOCK_INVALID", "sourceStartCommit")
    require_digest(lock.get("baselineSha256"), "TASK_LOCK_INVALID", "baselineSha256")
    if not isinstance(lock.get("baselineRevision"), int) or lock["baselineRevision"] < 1:
        raise LineageError("TASK_LOCK_INVALID", "task lock baselineRevision 无效")
    if lock.get("dirtyWorktree") is not False:
        raise LineageError("DIRTY_WORKTREE", "task lock 必须明确 dirtyWorktree=false")
    return lock


def validate_zip_member(member: zipfile.ZipInfo, seen: set[str], label: str) -> str:
    raw = member.filename.replace("\\", "/")
    is_dir = member.is_dir() or raw.endswith("/")
    canonical = raw[:-1] if is_dir and raw.endswith("/") else raw
    parts = canonical.split("/")
    mode = (member.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if (
        not canonical
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or canonical in seen
        or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
    ):
        raise LineageError("ZIP_SLIP", f"{label}包含不安全、重复或特殊路径：{raw}")
    if member.flag_bits & 0x1:
        raise LineageError("ZIP_ENCRYPTED", f"{label}包含加密条目：{raw}")
    seen.add(canonical)
    return canonical


def safe_members(archive: zipfile.ZipFile, label: str) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise LineageError("ZIP_ENTRY_LIMIT", f"{label}条目数超过限制")
    seen: set[str] = set()
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for member in entries:
        name = validate_zip_member(member, seen, label)
        if not member.is_dir() and not member.filename.endswith("/"):
            total += member.file_size
            if member.file_size > MAX_MEMBER_BYTES:
                raise LineageError("ZIP_MEMBER_LIMIT", f"{label}单条目超过大小限制：{name}")
            if member.file_size / max(member.compress_size, 1) > MAX_RATIO:
                raise LineageError("ZIP_RATIO_LIMIT", f"{label}包含异常压缩比：{name}")
            if total > MAX_UNCOMPRESSED:
                raise LineageError("ZIP_SIZE_LIMIT", f"{label}解压后超过大小限制")
        result[name] = member
    portable: dict[str, str] = {}
    for name, member in result.items():
        key = unicodedata.normalize("NFC", name).casefold()
        if key in portable:
            raise LineageError(
                "ZIP_PATH_CONFLICT",
                f"{label}包含跨文件系统冲突路径：{portable[key]} / {name}",
            )
        portable[key] = name
        parts = name.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in result and not result[ancestor].is_dir():
                raise LineageError(
                    "ZIP_PATH_CONFLICT",
                    f"{label}同时包含文件及其子路径：{ancestor} / {name}",
                )
    return result


def read_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo,
                    *, label: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    if member.file_size > limit:
        raise LineageError("ZIP_MEMBER_LIMIT", f"{label}超过读取限制")
    with archive.open(member) as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise LineageError("ZIP_MEMBER_LIMIT", f"{label}超过读取限制")
    return value


def sha256_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(member) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zip_container(path: Path, label: str) -> None:
    """Reject ZIP preambles/trailers and multi-disk/ZIP64 ambiguity.

    The accepted packages are bounded below 2 GiB, so the classic EOCD record
    is sufficient and gives an exact byte-level container closure.
    """
    try:
        size = path.stat().st_size
        if size > MAX_CONTAINER_BYTES:
            raise LineageError("ZIP_CONTAINER_LIMIT", f"{label}容器超过大小限制")
        with path.open("rb") as handle:
            start = max(0, size - (65_535 + 22))
            handle.seek(start)
            tail = handle.read()
    except OSError as exc:
        raise LineageError("ZIP_CONTAINER_INVALID", f"{label}不可读：{exc}") from exc
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        raise LineageError("ZIP_CONTAINER_INVALID", f"{label}缺少 EOCD")
    eocd = start + offset
    record = tail[offset:offset + 22]
    disk = int.from_bytes(record[4:6], "little")
    central_disk = int.from_bytes(record[6:8], "little")
    entries_disk = int.from_bytes(record[8:10], "little")
    entries_total = int.from_bytes(record[10:12], "little")
    central_size = int.from_bytes(record[12:16], "little")
    central_offset = int.from_bytes(record[16:20], "little")
    comment_size = int.from_bytes(record[20:22], "little")
    if (
        disk != 0
        or central_disk != 0
        or entries_disk != entries_total
        or entries_total == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or eocd + 22 + comment_size != size
        or central_offset + central_size != eocd
    ):
        raise LineageError(
            "ZIP_CONTAINER_INVALID",
            f"{label}含前后缀、多磁盘或不受支持的 ZIP64 容器",
        )
    try:
        with zipfile.ZipFile(path) as archive, path.open("rb") as raw:
            infos = archive.infolist()
            if (
                len(infos) != entries_total
                or archive.comment
                or [info.filename for info in infos] != sorted(info.filename for info in infos)
            ):
                raise LineageError("ZIP_CONTAINER_INVALID", f"{label}中央目录数量、注释或顺序无效")
            local_cursor = 0
            for info in infos:
                if (
                    info.header_offset != local_cursor
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.flag_bits not in {0, 0x800}
                    or info.extra
                    or info.comment
                    or info.date_time != ZIP_EPOCH
                    or info.create_system != 3
                    or info.create_version != 20
                    or info.extract_version != 20
                    or info.internal_attr != 0
                    or info.volume != 0
                ):
                    raise LineageError("ZIP_CONTAINER_INVALID", f"{label}包含非规范 ZIP 元数据")
                encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
                name_raw = info.filename.encode(encoding)
                raw.seek(local_cursor)
                local = raw.read(30)
                if len(local) != 30:
                    raise LineageError("ZIP_CONTAINER_INVALID", f"{label}本地文件头截断")
                (
                    signature, version, flags, compression, _time, _date,
                    crc, compressed, uncompressed, name_length, extra_length,
                ) = struct.unpack("<IHHHHHIIIHH", local)
                if (
                    signature != 0x04034B50
                    or version != 20
                    or flags != info.flag_bits
                    or compression != zipfile.ZIP_STORED
                    or _time != ZIP_DOS_TIME
                    or _date != ZIP_DOS_DATE
                    or crc != info.CRC
                    or compressed != info.compress_size
                    or uncompressed != info.file_size
                    or name_length != len(name_raw)
                    or extra_length != 0
                    or raw.read(name_length) != name_raw
                ):
                    raise LineageError("ZIP_CONTAINER_INVALID", f"{label}本地文件记录不规范")
                local_cursor = local_cursor + 30 + name_length + info.compress_size
            if local_cursor != central_offset:
                raise LineageError("ZIP_CONTAINER_INVALID", f"{label}本地记录之间存在前缀、空洞或尾随数据")

            central_cursor = central_offset
            for info in infos:
                encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
                name_raw = info.filename.encode(encoding)
                raw.seek(central_cursor)
                central = raw.read(46)
                if len(central) != 46:
                    raise LineageError("ZIP_CONTAINER_INVALID", f"{label}中央目录记录截断")
                fields = struct.unpack("<IHHHHHHIIIHHHHHII", central)
                (
                    signature, made_by, version, flags, compression, _time, _date,
                    crc, compressed, uncompressed, name_length, extra_length,
                    comment_length, disk_start, internal_attr, external_attr,
                    local_offset,
                ) = fields
                if (
                    signature != 0x02014B50
                    or made_by != (3 << 8) | 20
                    or version != 20
                    or flags != info.flag_bits
                    or compression != zipfile.ZIP_STORED
                    or _time != ZIP_DOS_TIME
                    or _date != ZIP_DOS_DATE
                    or crc != info.CRC
                    or compressed != info.compress_size
                    or uncompressed != info.file_size
                    or name_length != len(name_raw)
                    or extra_length != 0
                    or comment_length != 0
                    or disk_start != 0
                    or internal_attr != 0
                    or external_attr != info.external_attr
                    or local_offset != info.header_offset
                    or raw.read(name_length) != name_raw
                ):
                    raise LineageError("ZIP_CONTAINER_INVALID", f"{label}中央目录记录不规范")
                central_cursor += 46 + name_length
            if central_cursor != eocd:
                raise LineageError("ZIP_CONTAINER_INVALID", f"{label}中央目录含未声明字节")
    except zipfile.BadZipFile as exc:
        raise LineageError("ZIP_CONTAINER_INVALID", f"{label}不是严格 ZIP") from exc


def artifact_file_mode(member: zipfile.ZipInfo, name: str) -> str:
    raw_mode = (member.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(raw_mode)
    mode = stat.S_IMODE(raw_mode)
    if kind != stat.S_IFREG or mode not in {0o644, 0o755}:
        raise LineageError(
            "ARTIFACT_MODE_INVALID",
            f"artifact 文件只允许显式 0644/0755 普通文件模式：{name}",
        )
    return f"{mode:04o}"


def safe_extract(archive: zipfile.ZipFile, members: Mapping[str, zipfile.ZipInfo],
                 destination: Path, *, file_modes: Mapping[str, str] | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    for name, member in members.items():
        target = destination.joinpath(*name.split("/"))
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise LineageError("ZIP_SLIP", f"解压目标越界：{name}")
        if member.is_dir() or member.filename.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        os.chmod(target, int(file_modes[name], 8) if file_modes is not None else 0o600)
    if file_modes is not None:
        for directory in sorted(
            (item for item in destination.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o755)
        os.chmod(destination, 0o755)


def parse_sums(value: bytes, expected_paths: set[str], label: str) -> dict[str, str]:
    try:
        text = value.decode("utf-8")
    except UnicodeError as exc:
        raise LineageError("MANIFEST_INVALID", f"{label}不是 UTF-8") from exc
    if not text.endswith("\n"):
        raise LineageError("MANIFEST_INVALID", f"{label}必须以换行结束")
    result: dict[str, str] = {}
    order: list[str] = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if not match:
            raise LineageError("MANIFEST_INVALID", f"{label}包含无效行")
        digest, raw_path = match.groups()
        path = require_repo_path(raw_path, "MANIFEST_INVALID", f"{label} path")
        if path in result:
            raise LineageError("MANIFEST_INVALID", f"{label}包含重复路径：{path}")
        result[path] = digest
        order.append(path)
    if order != sorted(order):
        raise LineageError("MANIFEST_INVALID", f"{label}必须按路径排序")
    if set(result) != expected_paths:
        raise LineageError("MANIFEST_CLOSURE_MISMATCH", f"{label}不是精确文件闭包")
    return result


def artifact_payload(artifact: Path, *, extract_to: Path | None = None) -> dict[str, Any]:
    if artifact.is_symlink() or not artifact.is_file():
        raise LineageError("ARTIFACT_INVALID", "artifact 必须是非符号链接普通文件")
    validate_zip_container(artifact, "artifact")
    try:
        with zipfile.ZipFile(artifact) as archive:
            members = safe_members(archive, "artifact")
            explicit_directories = [
                name for name, member in members.items()
                if member.is_dir() or member.filename.endswith("/")
            ]
            if explicit_directories:
                raise LineageError(
                    "ARTIFACT_DIRECTORY_ENTRY",
                    f"artifact 禁止未受清单约束的显式目录：{explicit_directories[:10]}",
                )
            if "release.json" not in members or "SHA256SUMS" not in members:
                raise LineageError("ARTIFACT_MANIFEST_MISSING", "artifact 缺少 release.json 或 SHA256SUMS")
            if members["release.json"].is_dir() or members["SHA256SUMS"].is_dir():
                raise LineageError("ARTIFACT_MANIFEST_MISSING", "artifact manifest 不能是目录")
            if (
                artifact_file_mode(members["release.json"], "release.json") != "0644"
                or artifact_file_mode(members["SHA256SUMS"], "SHA256SUMS") != "0644"
            ):
                raise LineageError("ARTIFACT_MODE_INVALID", "artifact manifest 模式必须固定为 0644")
            payload_members = {
                name: member for name, member in members.items()
                if not member.is_dir() and name not in {"release.json", "SHA256SUMS"}
            }
            payload_modes = {
                name: artifact_file_mode(member, name)
                for name, member in payload_members.items()
            }
            release_raw = read_zip_member(
                archive, members["release.json"], label="release.json",
            )
            sums_raw = read_zip_member(
                archive, members["SHA256SUMS"], label="artifact SHA256SUMS",
            )
            sums = parse_sums(sums_raw, set(payload_members), "artifact SHA256SUMS")
            for name, member in payload_members.items():
                if sha256_zip_member(archive, member) != sums[name]:
                    raise LineageError("ARTIFACT_SHA256SUMS_MISMATCH", f"artifact 内容哈希不一致：{name}")
            if extract_to is not None:
                manifest_modes = {
                    "release.json": "0644",
                    "SHA256SUMS": "0644",
                    **payload_modes,
                }
                safe_extract(archive, members, extract_to, file_modes=manifest_modes)
    except zipfile.BadZipFile as exc:
        raise LineageError("ARTIFACT_INVALID", "artifact 不是有效 ZIP") from exc
    return {
        "release": load_json_bytes(release_raw, "RELEASE_MANIFEST_INVALID", "release.json"),
        "releaseRaw": release_raw,
        "sumsRaw": sums_raw,
        "sums": sums,
        "modes": payload_modes,
    }


def expected_lineage(*, baseline: Mapping[str, Any], baseline_sha: str,
                     source_tag: str, source_commit: str, source_tag_object: str,
                     task_id: str) -> dict[str, Any]:
    current = require_mapping(baseline.get("current"), "BASELINE_INVALID", "current")
    return {
        "baseRelease": current["release"],
        "baseTag": current["sourceTag"],
        "baseTagObject": current["sourceTagObject"],
        "baseCommit": current["sourceCommit"],
        "sourceTag": source_tag,
        "sourceTagObject": source_tag_object,
        "sourceCommit": source_commit,
        "taskId": task_id,
        "candidateRef": approved_candidate_ref(task_id, baseline["revision"]),
        "dirtyWorktree": False,
        "baselineRevision": baseline["revision"],
        "baselineSha256": baseline_sha,
    }


def git_object_bytes(repo: Path, commit: str, relative: str) -> bytes:
    require_commit(commit, "UNKNOWN_COMMIT", "sourceCommit")
    path = require_repo_path(relative, "ARTIFACT_SPEC_INVALID", "artifactSpec.path")
    value = git_bytes(repo, ["show", f"{commit}:{path}"], optional=True)
    if not value:
        raise LineageError("ARTIFACT_SPEC_UNKNOWN", f"sourceCommit 不包含 artifact spec：{path}")
    return value


def read_artifact_spec(repo: Path, source_commit: str, spec_path: str) -> tuple[dict[str, Any], bytes]:
    spec_raw = git_object_bytes(repo, source_commit, spec_path)
    spec = load_json_bytes(spec_raw, "ARTIFACT_SPEC_INVALID", "artifact spec")
    if set(spec) != {"schema", "release", "files"} or spec.get("schema") != ARTIFACT_SPEC_SCHEMA:
        raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec schema 或字段闭包无效")
    require_name(spec.get("release"), "ARTIFACT_SPEC_INVALID", "artifact spec release")
    files = spec.get("files")
    if not isinstance(files, list):
        raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec files 必须是数组")
    normalized: list[dict[str, str]] = []
    declared: set[str] = set()
    order: list[str] = []
    for item in files:
        row = require_mapping(item, "ARTIFACT_SPEC_INVALID", "artifact spec file")
        if set(row) != {"path", "sha256", "mode"}:
            raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec file 字段闭包无效")
        path = require_repo_path(row.get("path"), "ARTIFACT_SPEC_INVALID", "artifact spec file.path")
        digest = require_digest(row.get("sha256"), "ARTIFACT_SPEC_INVALID", "artifact spec file.sha256")
        mode = str(row.get("mode") or "")
        if mode not in {"0644", "0755"}:
            raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec file.mode 只允许 0644/0755")
        if path in declared:
            raise LineageError("ARTIFACT_SPEC_INVALID", f"artifact spec 路径重复：{path}")
        declared.add(path)
        normalized.append({"path": path, "sha256": digest, "mode": mode})
        order.append(path)
    if not normalized or order != sorted(order):
        raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec files 必须非空且按路径排序")
    return {"schema": ARTIFACT_SPEC_SCHEMA, "release": spec["release"], "files": normalized}, spec_raw


def verify_artifact_spec(repo: Path, source_commit: str, release: Mapping[str, Any],
                         sums: Mapping[str, str], modes: Mapping[str, str]) -> dict[str, Any]:
    artifact_spec = require_mapping(release.get("artifactSpec"), "RELEASE_MANIFEST_INVALID", "artifactSpec")
    if set(artifact_spec) != {"path", "sha256"}:
        raise LineageError("RELEASE_MANIFEST_INVALID", "artifactSpec 字段闭包无效")
    spec_path = require_repo_path(artifact_spec.get("path"), "ARTIFACT_SPEC_INVALID", "artifactSpec.path")
    spec_sha = require_digest(artifact_spec.get("sha256"), "ARTIFACT_SPEC_INVALID", "artifactSpec.sha256")
    spec, spec_raw = read_artifact_spec(repo, source_commit, spec_path)
    if sha256_bytes(spec_raw) != spec_sha:
        raise LineageError("ARTIFACT_SPEC_TAMPERED", "sourceCommit 中的 artifact spec 哈希不一致")
    if spec.get("release") != release.get("release"):
        raise LineageError("ARTIFACT_SPEC_INVALID", "artifact spec release 不匹配")
    declared = {
        row["path"]: f"{row['mode']}:{row['sha256']}"
        for row in spec["files"]
    }
    actual = {path: f"{modes[path]}:{digest}" for path, digest in sums.items()}
    if declared != actual:
        raise LineageError("ARTIFACT_SOURCE_MISMATCH", "artifact 内容与 sourceCommit 中的 artifact spec 不一致")
    return {"path": spec_path, "sha256": spec_sha, "fileCount": len(declared)}


def verify_release_manifest(release: Mapping[str, Any], lineage: Mapping[str, Any]) -> None:
    if set(release) != {"schema", "release", "lineage", "artifactSpec"}:
        raise LineageError("RELEASE_MANIFEST_INVALID", "release.json 字段闭包无效")
    if release.get("schema") != ARTIFACT_SCHEMA:
        raise LineageError("RELEASE_MANIFEST_INVALID", "release.json schema 无效")
    require_name(release.get("release"), "RELEASE_MANIFEST_INVALID", "release")
    if release.get("lineage") != dict(lineage):
        raise LineageError("RELEASE_LINEAGE_MISMATCH", "release.json lineage 与受保护基线不一致")


def require_annotated_tag(repo: Path, tag: str, commit: str) -> None:
    if tag_commit(repo, tag, optional=True) != commit:
        raise LineageError("TAG_COMMIT_MISMATCH", "sourceTag 与 sourceCommit 不匹配")
    if git_text(repo, ["cat-file", "-t", f"refs/tags/{tag}"], optional=True) != "tag":
        raise LineageError("TAG_NOT_ANNOTATED", "生产 sourceTag 必须是 annotated Tag")


def git_blob(repo: Path, commit: str, path: str) -> tuple[bytes, str]:
    raw = git_bytes(repo, ["ls-tree", "-z", commit, "--", path])
    entries = [entry for entry in raw.rstrip(b"\0").split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise LineageError("ARTIFACT_SOURCE_MISMATCH", f"sourceCommit 不包含唯一普通文件：{path}")
    metadata, stored = entries[0].split(b"\t", 1)
    parts = metadata.split()
    if len(parts) != 3 or parts[1] != b"blob" or stored.decode("utf-8") != path:
        raise LineageError("ARTIFACT_SOURCE_MISMATCH", f"sourceCommit 条目类型无效：{path}")
    modes = {b"100644": "0644", b"100755": "0755"}
    mode = modes.get(parts[0])
    if mode is None:
        raise LineageError("ARTIFACT_MODE_INVALID", f"sourceCommit 文件模式无效：{path}")
    return git_bytes(repo, ["cat-file", "blob", parts[2].decode("ascii")]), mode


def verify_bootstrap_artifact(repo: Path, source_tag: str, source_commit: str,
                              payload: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    release = require_mapping(payload.get("release"), "BOOTSTRAP_MANIFEST_INVALID", "release.json")
    if set(release) != {"schema", "release", "lineage", "artifactSpec"}:
        raise LineageError("BOOTSTRAP_MANIFEST_INVALID", "bootstrap release.json 字段闭包无效")
    if release.get("schema") != BOOTSTRAP_ARTIFACT_SCHEMA:
        raise LineageError("BOOTSTRAP_MANIFEST_INVALID", "bootstrap release.json schema 无效")
    release_name = require_name(release.get("release"), "BOOTSTRAP_MANIFEST_INVALID", "release")
    lineage = require_mapping(release.get("lineage"), "BOOTSTRAP_MANIFEST_INVALID", "lineage")
    source_tag_object = tag_object(repo, source_tag, optional=True)
    expected = {
        "baseRelease": release_name,
        "baseTag": source_tag,
        "baseTagObject": source_tag_object,
        "baseCommit": source_commit,
        "sourceTag": source_tag,
        "sourceTagObject": source_tag_object,
        "sourceCommit": source_commit,
        "taskId": task_id,
        "dirtyWorktree": False,
        "bootstrap": True,
    }
    if dict(lineage) != expected or not TASK_RE.fullmatch(task_id):
        raise LineageError("BOOTSTRAP_LINEAGE_MISMATCH", "bootstrap lineage 与 Tag/Commit/任务不匹配")
    require_annotated_tag(repo, source_tag, source_commit)
    spec = verify_artifact_spec(repo, source_commit, release, payload["sums"], payload["modes"])
    return {"release": release_name, "lineage": expected, "artifactSpec": spec}


def build_bootstrap_artifact(*, repo: Path, source_tag: str, task_id: str,
                             output: Path) -> dict[str, Any]:
    state = repository_state(repo)
    if not state["clean"]:
        raise LineageError("DIRTY_WORKTREE", f"正式构建拒绝脏工作树：{state['dirtyEntries'][:10]}")
    source_tag = require_name(source_tag, "TAG_INVALID", "sourceTag")
    source_commit = require_commit(state["head"], "COMMIT_INVALID", "HEAD")
    if not TASK_RE.fullmatch(task_id):
        raise LineageError("TASK_ID_INVALID", "taskId 无效")
    require_annotated_tag(state["root"], source_tag, source_commit)
    source_tag_object = require_commit(
        tag_object(state["root"], source_tag), "TAG_INVALID", "sourceTag object",
    )
    spec, spec_raw = read_artifact_spec(state["root"], source_commit, ARTIFACT_SPEC_PATH)
    files: dict[str, bytes] = {}
    modes: dict[str, str] = {}
    for row in spec["files"]:
        path = row["path"]
        content, git_mode = git_blob(state["root"], source_commit, path)
        if git_mode != row["mode"] or sha256_bytes(content) != row["sha256"]:
            raise LineageError("ARTIFACT_SOURCE_MISMATCH", f"artifact spec 与 sourceCommit 不一致：{path}")
        files[path] = content
        modes[path] = git_mode
    lineage = {
        "baseRelease": spec["release"], "baseTag": source_tag,
        "baseTagObject": source_tag_object, "baseCommit": source_commit,
        "sourceTag": source_tag, "sourceTagObject": source_tag_object,
        "sourceCommit": source_commit, "taskId": task_id,
        "dirtyWorktree": False, "bootstrap": True,
    }
    release_raw = canonical_json({
        "schema": BOOTSTRAP_ARTIFACT_SCHEMA,
        "release": spec["release"],
        "lineage": lineage,
        "artifactSpec": {"path": ARTIFACT_SPEC_PATH, "sha256": sha256_bytes(spec_raw)},
    })
    sums_raw = "".join(
        f"{sha256_bytes(files[path])}  {path}\n" for path in sorted(files)
    ).encode("utf-8")
    output_path = ensure_outside_repo(state["root"], output, "bootstrap artifact")
    if output_path.exists() or output_path.is_symlink():
        raise LineageError("OUTPUT_EXISTS", f"目标已存在：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="xirang-bootstrap-package-", dir=output_path.parent) as raw:
            temporary = Path(raw)
            package_files: dict[str, Path] = {}
            package_modes = {**modes, "release.json": "0644", "SHA256SUMS": "0644"}
            for path, content in {**files, "release.json": release_raw, "SHA256SUMS": sums_raw}.items():
                target = temporary.joinpath(*path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                os.chmod(target, int(package_modes[path], 8))
                package_files[path] = target
            deterministic_zip(output_path, package_files, package_modes)
        payload = artifact_payload(output_path)
        verified = verify_bootstrap_artifact(
            state["root"], source_tag, source_commit, payload, task_id,
        )
        final = repository_state(state["root"])
        if not final["clean"] or final["head"] != source_commit:
            raise LineageError("SOURCE_CHANGED_DURING_BUILD", "构建期间源码或工作树发生变化")
        require_annotated_tag(final["root"], source_tag, source_commit)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return {
        "ok": True, "status": "bootstrap-artifact-built", "artifact": str(output_path),
        "packageSha256": sha256_path(output_path), "sourceTag": source_tag,
        "sourceCommit": source_commit, "release": verified["release"],
        "dirtyWorktree": False,
    }


def deterministic_zip(output: Path, files: Mapping[str, Path],
                      file_modes: Mapping[str, str] | None = None) -> None:
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            mode = int((file_modes or {}).get(name, "0600"), 8)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, files[name].read_bytes())


def build_envelope(*, repo: Path, baseline_path: Path, task_lock_path: Path,
                   source_tag: str, artifact: Path, output: Path) -> dict[str, Any]:
    state = repository_state(repo)
    if not state["clean"]:
        raise LineageError("DIRTY_WORKTREE", f"正式构建拒绝脏工作树：{state['dirtyEntries'][:10]}")
    source_tag = require_name(source_tag, "TAG_INVALID", "sourceTag")
    source_commit = require_commit(state["head"], "COMMIT_INVALID", "HEAD")
    actual = tag_commit(state["root"], source_tag, optional=True)
    if not actual:
        raise LineageError("SOURCE_TAG_UNKNOWN", f"候选 Tag 不存在：{source_tag}")
    if actual != source_commit:
        raise LineageError("TAG_COMMIT_MISMATCH", f"sourceTag 指向 {actual}，不是 HEAD {source_commit}")
    require_annotated_tag(state["root"], source_tag, source_commit)
    source_tag_object = require_commit(
        tag_object(state["root"], source_tag), "TAG_INVALID", "sourceTag object",
    )
    baseline, _baseline_raw, baseline_sha = read_baseline(baseline_path)
    validate_baseline_tag(state["root"], baseline)
    lock = read_task_lock(task_lock_path)
    current = dict(require_mapping(baseline["current"], "BASELINE_INVALID", "current"))
    expected_lock = {
        "baseRelease": current["release"], "baseTag": current["sourceTag"],
        "baseTagObject": current["sourceTagObject"],
        "baseCommit": current["sourceCommit"], "baselineRevision": baseline["revision"],
        "baselineSha256": baseline_sha, "sourceStartCommit": current["sourceCommit"],
    }
    if any(lock.get(key) != value for key, value in expected_lock.items()):
        raise LineageError("BASELINE_LOCK_STALE", "task lock 与当前 production-baseline 不一致")
    if source_tag == current["sourceTag"]:
        raise LineageError("RELEASE_REUSED", "sourceTag 不得复用生产 Tag")
    if source_commit == current["sourceCommit"]:
        raise LineageError("OLD_SOURCE_RELABEL", "候选只更换了 Tag/版本号，源码 commit 未前进")
    require_ancestor(state["root"], current["sourceCommit"], source_commit)
    artifact_path = ensure_outside_repo(state["root"], artifact, "artifact")
    output_path = ensure_outside_repo(state["root"], output, "envelope")
    if output_path.exists():
        raise LineageError("OUTPUT_EXISTS", f"目标 envelope 已存在：{output_path}")
    lineage = expected_lineage(
        baseline=baseline, baseline_sha=baseline_sha,
        source_tag=source_tag, source_commit=source_commit,
        source_tag_object=source_tag_object, task_id=lock["taskId"],
    )
    artifact_checked = artifact_payload(artifact_path)
    release = artifact_checked["release"]
    verify_release_manifest(release, lineage)
    spec = verify_artifact_spec(
        state["root"], source_commit, release,
        artifact_checked["sums"], artifact_checked["modes"],
    )
    with tempfile.TemporaryDirectory(prefix="xirang-lineage-build-") as raw_tmp:
        temporary = Path(raw_tmp)
        artifact_target = temporary / "artifact.zip"
        bundle_target = temporary / "source.bundle"
        manifest_target = temporary / "lineage.json"
        sums_target = temporary / "SHA256SUMS"
        shutil.copy2(artifact_path, artifact_target)
        git_bytes(
            state["root"],
            ["bundle", "create", str(bundle_target), f"refs/tags/{source_tag}", f"^{current['sourceCommit']}"],
        )
        manifest = {
            "schema": ENVELOPE_SCHEMA,
            "release": release["release"],
            **lineage,
            "packageSha256": sha256_path(artifact_target),
            "artifact": {
                "file": "artifact.zip",
                "releaseManifestSha256": sha256_bytes(artifact_checked["releaseRaw"]),
                "sha256SumsSha256": sha256_bytes(artifact_checked["sumsRaw"]),
                "artifactSpecPath": spec["path"],
                "artifactSpecSha256": spec["sha256"],
            },
            "sourceBundle": {"file": "source.bundle", "sha256": sha256_path(bundle_target)},
            "acceptance": {"status": "pending", "acceptedBy": None, "acceptedAt": None},
        }
        manifest_target.write_bytes(canonical_json(manifest))
        sums_target.write_text(
            "".join(
                f"{sha256_path(path)}  {name}\n"
                for name, path in sorted({
                    "artifact.zip": artifact_target,
                    "lineage.json": manifest_target,
                    "source.bundle": bundle_target,
                }.items())
            ),
            encoding="utf-8",
        )
        deterministic_zip(output_path, {
            "artifact.zip": artifact_target,
            "lineage.json": manifest_target,
            "source.bundle": bundle_target,
            "SHA256SUMS": sums_target,
        })
    final = repository_state(state["root"])
    if (
        not final["clean"] or final["head"] != source_commit
        or tag_commit(state["root"], source_tag) != source_commit
        or tag_object(state["root"], source_tag) != source_tag_object
    ):
        output_path.unlink(missing_ok=True)
        raise LineageError("SOURCE_CHANGED_DURING_BUILD", "构建期间源码、Tag 或工作树发生变化")
    require_annotated_tag(state["root"], source_tag, source_commit)
    return {
        "ok": True,
        "status": "envelope-built",
        "envelope": str(output_path),
        "envelopeSha256": sha256_path(output_path),
        "packageSha256": manifest["packageSha256"],
        "lineage": manifest,
    }


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    root_mode = stat.S_IMODE(root.stat().st_mode)
    digest.update(f"D\0.\0{root_mode:04o}\n".encode("ascii"))
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink() or (not item.is_dir() and not item.is_file()):
            raise LineageError("PRODUCTION_TREE_UNSAFE", f"生产树包含特殊文件：{relative}")
        mode = stat.S_IMODE(item.stat().st_mode)
        if item.is_dir():
            digest.update(b"D\0" + relative.encode("utf-8") + f"\0{mode:04o}\n".encode("ascii"))
        else:
            digest.update(b"F\0" + relative.encode("utf-8") + f"\0{mode:04o}\0".encode("ascii"))
            digest.update(sha256_path(item).encode("ascii") + b"\n")
    return digest.hexdigest()


class HostConfig:
    """A generic protected-host layout; callers choose all roots explicitly."""

    def __init__(self, root: Path, trusted_repo: Path):
        self.root = root.expanduser().resolve()
        self.trusted_repo = trusted_repo.expanduser().resolve()

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    @property
    def packages(self) -> Path:
        return self.root / "packages"

    @property
    def receipts(self) -> Path:
        return self.root / "receipts"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def baseline(self) -> Path:
        return self.root / "production-baseline.json"

    @property
    def lock(self) -> Path:
        return self.root / ".release-lineage.lock"

    @property
    def journal(self) -> Path:
        return self.root / ".release-lineage-transaction.json"


def core_health_runner(release: str, version: Path) -> dict[str, Any]:
    entry = version / CORE_ENTRY_PATH
    if entry.is_symlink() or not entry.is_file() or stat.S_IMODE(entry.stat().st_mode) != 0o755:
        raise LineageError("HEALTH_ENTRY_INVALID", f"{release} 缺少 0755 核心入口：{CORE_ENTRY_PATH}")
    try:
        source = entry.read_bytes()
        if len(source) > MAX_MANIFEST_BYTES:
            raise LineageError("STATIC_HEALTH_FAILED", "核心入口超过静态检查大小上限")
        ast.parse(source, filename=CORE_ENTRY_PATH, mode="exec")
    except (OSError, SyntaxError, ValueError) as exc:
        raise LineageError("STATIC_HEALTH_FAILED", f"核心入口静态语法检查失败：{exc}") from exc
    return {
        "kind": "python-ast-static",
        "entry": CORE_ENTRY_PATH,
        "entrySha256": sha256_path(entry),
        "executedCandidateCode": False,
    }


class ReleaseLineageGate:
    """Independent host verifier and atomic baseline-CAS promoter."""

    def __init__(self, config: HostConfig, *, failpoint: Callable[[str], None] | None = None,
                 health_runner: Callable[[str, Path], Mapping[str, Any] | None] | None = None):
        self.config = config
        self.failpoint = failpoint or (lambda _name: None)
        self.health_runner = health_runner or (lambda _release, _path: {})

    def validate_layout(self) -> None:
        for path in (
            self.config.root, self.config.versions, self.config.packages,
            self.config.receipts, self.config.staging, self.config.trusted_repo,
        ):
            if path.is_symlink() or not path.is_dir():
                raise LineageError("HOST_LAYOUT_INVALID", f"受保护目录缺失或为符号链接：{path}")
            if path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise LineageError("HOST_LAYOUT_INVALID", f"受保护目录不得组/公开可写：{path}")
        self.validate_authority_file(self.config.baseline, "production-baseline")
        for path, label in (
            (self.config.lock, "release lock"),
            (self.config.journal, "transaction journal"),
        ):
            if path.exists() or path.is_symlink():
                self.validate_authority_file(path, label)

    @staticmethod
    def validate_authority_file(path: Path, label: str,
                                allowed_modes: set[int] | None = None) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise LineageError("HOST_LAYOUT_INVALID", f"{label} 不可读：{path}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or stat.S_IMODE(metadata.st_mode) not in (allowed_modes or {0o600})
        ):
            raise LineageError(
                "HOST_LAYOUT_INVALID",
                f"{label} 必须是当前 gate 用户拥有且不可组/公开写的普通文件：{path}",
            )

    def validate_git_authority_ref(self, ref: str) -> None:
        loose = self.config.trusted_repo.joinpath(*ref.split("/"))
        if loose.exists() or loose.is_symlink():
            self.validate_authority_file(loose, f"trusted ref {ref}", {0o600, 0o644})
            return
        packed = self.config.trusted_repo / "packed-refs"
        if packed.exists() or packed.is_symlink():
            self.validate_authority_file(packed, "trusted packed-refs", {0o600, 0o644})

    def current_release(self) -> str:
        if not self.config.current.is_symlink():
            raise LineageError("CURRENT_INVALID", "current 不是符号链接")
        raw = os.readlink(self.config.current)
        target = (self.config.current.parent / raw).resolve()
        versions = self.config.versions.resolve()
        if target.parent != versions or not target.is_dir():
            raise LineageError("CURRENT_INVALID", "current 未指向 versions 下的单一版本目录")
        return target.name

    def ensure_no_bootstrap_transaction(self) -> None:
        journal = self.config.root / ".bootstrap-transaction.json"
        transients = [
            path.name for path in self.config.root.iterdir()
            if path.name.startswith(".bootstrap-stage-")
            or path.name.startswith("..bootstrap-transaction.json.")
        ]
        if journal.exists() or journal.is_symlink() or transients:
            raise LineageError("RECOVERY_REQUIRED", "bootstrap 事务尚未完成，拒绝并发发布")

    def status(self, *, allow_bootstrap_journal: bool = False) -> dict[str, Any]:
        bootstrap_journal = self.config.root / ".bootstrap-transaction.json"
        if (
            (bootstrap_journal.exists() or bootstrap_journal.is_symlink())
            and not allow_bootstrap_journal
        ):
            raise LineageError("RECOVERY_REQUIRED", "存在未完成 bootstrap 事务")
        self.validate_layout()
        descriptor = os.open(str(self.config.lock), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_SH)
            self.validate_layout()
            transients = [
                path.name for path in self.config.root.iterdir()
                if path.name.startswith(".bootstrap-stage-")
                or path.name.startswith("..bootstrap-transaction.json.")
            ]
            if (
                (bootstrap_journal.exists() or bootstrap_journal.is_symlink())
                and not allow_bootstrap_journal
            ) or transients:
                raise LineageError("RECOVERY_REQUIRED", "存在未完成 bootstrap 事务")
            if self.config.journal.exists() or self.config.journal.is_symlink():
                raise LineageError("RECOVERY_REQUIRED", "存在未完成发布事务")
            baseline, baseline_raw, baseline_sha = read_baseline(self.config.baseline)
            current = dict(require_mapping(baseline["current"], "BASELINE_INVALID", "current"))
            active = self.current_release()
            if active != current["release"]:
                raise LineageError("CURRENT_BASELINE_MISMATCH", "current 与 production-baseline 不一致")
            validate_baseline_tag(self.config.trusted_repo, baseline)
            self.validate_git_authority_ref(f"refs/tags/{current['sourceTag']}")
            artifact = self.config.root / current["artifact"]
            if artifact.is_symlink() or not artifact.is_file():
                raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包缺失或类型无效")
            resolved_artifact = artifact.resolve()
            try:
                resolved_artifact.relative_to(self.config.packages.resolve())
            except ValueError as exc:
                raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包路径越界") from exc
            cursor = artifact.parent
            while cursor != self.config.root:
                if cursor.is_symlink():
                    raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包路径包含符号链接")
                cursor = cursor.parent
            package_sha = sha256_path(artifact)
            if package_sha != current["artifactSha256"]:
                raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包 SHA 与 baseline 不一致")
            version = self.config.versions / active
            production_sha = tree_hash(version)
            if production_sha != current["productionTreeSha256"]:
                raise LineageError("PRODUCTION_TREE_MISMATCH", "当前生产树 SHA 与 baseline 不一致")
            with tempfile.TemporaryDirectory(prefix="xirang-lineage-status-") as raw_tmp:
                extracted = Path(raw_tmp) / "artifact"
                payload = artifact_payload(artifact, extract_to=extracted)
                release = require_mapping(
                    payload.get("release"), "PRODUCTION_ARTIFACT_MISMATCH", "release.json",
                )
                lineage = require_mapping(
                    release.get("lineage"), "PRODUCTION_ARTIFACT_MISMATCH", "release lineage",
                )
                if (
                    release.get("release") != active
                    or lineage.get("sourceTag") != current["sourceTag"]
                    or lineage.get("sourceTagObject") != current["sourceTagObject"]
                    or lineage.get("sourceCommit") != current["sourceCommit"]
                    or lineage.get("dirtyWorktree") is not False
                ):
                    raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "生产包 lineage 与 baseline 不一致")
                spec = verify_artifact_spec(
                    self.config.trusted_repo, current["sourceCommit"], release,
                    payload["sums"], payload["modes"],
                )
                if tree_hash(extracted) != production_sha:
                    raise LineageError("PACKAGE_TREE_MISMATCH", "生产包解压闭包与生产树不一致")
                if (
                    sha256_bytes(payload["releaseRaw"]) != require_digest(
                        current.get("releaseManifestSha256"), "BASELINE_INVALID", "releaseManifestSha256",
                    )
                    or sha256_bytes(payload["sumsRaw"]) != require_digest(
                        current.get("sha256SumsSha256"), "BASELINE_INVALID", "sha256SumsSha256",
                    )
                ):
                    raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "生产包 manifest 哈希与 baseline 不一致")
            receipt = (
                self.config.receipts / f"bootstrap-{current['sourceTag']}.json"
                if release.get("schema") == BOOTSTRAP_ARTIFACT_SCHEMA
                else self.config.receipts / f"{active}.json"
            )
            self.validate_authority_file(receipt, "current release receipt")
            receipt_value = load_json_path(receipt, "RECEIPT_INVALID", "current release receipt")
            if release.get("schema") == BOOTSTRAP_ARTIFACT_SCHEMA:
                receipt_fields = {
                    "schema", "status", "release", "sourceTag", "sourceTagObject",
                    "sourceCommit", "baseTag", "baseTagObject", "baseCommit",
                    "dirtyWorktree", "taskId", "baselineAfterRevision",
                    "baselineAfterSha256", "packageSha256", "productionTreeSha256",
                    "health", "at",
                }
                receipt_specific = (
                    receipt_value.get("schema") == BOOTSTRAP_RECEIPT_SCHEMA
                    and receipt_value.get("release") == active
                    and receipt_value.get("baseTag") == current.get("baseTag")
                    and receipt_value.get("baseTagObject") == current.get("baseTagObject")
                    and receipt_value.get("baseCommit") == current.get("baseCommit")
                    and receipt_value.get("taskId") == current.get("taskId")
                    and receipt_value.get("dirtyWorktree") is False
                )
            else:
                receipt_fields = {
                    "schema", "status", "from", "to", "envelopeSha256", "packageSha256",
                    "sourceTag", "sourceTagObject", "sourceCommit", "taskId", "candidateRef",
                    "baselineBeforeRevision", "baselineBeforeSha256",
                    "baselineAfterRevision", "baselineAfterSha256",
                    "productionTreeBeforeSha256", "productionTreeAfterSha256",
                    "health", "at",
                }
                receipt_specific = (
                    receipt_value.get("schema") == RECEIPT_SCHEMA
                    and receipt_value.get("from") == current.get("baseRelease")
                    and receipt_value.get("to") == active
                    and receipt_value.get("taskId") == current.get("taskId")
                    and receipt_value.get("candidateRef") == current.get("candidateRef")
                    and receipt_value.get("baselineBeforeRevision") == baseline["revision"] - 1
                    and DIGEST_RE.fullmatch(str(receipt_value.get("baselineBeforeSha256") or ""))
                    and DIGEST_RE.fullmatch(str(receipt_value.get("envelopeSha256") or ""))
                    and DIGEST_RE.fullmatch(str(receipt_value.get("productionTreeBeforeSha256") or ""))
                )
            common_receipt = (
                set(receipt_value) == receipt_fields
                and receipt_specific
                and receipt_value.get("status") == "deployed-pending-user-acceptance"
                and receipt_value.get("sourceTag") == current["sourceTag"]
                and receipt_value.get("sourceTagObject") == current["sourceTagObject"]
                and receipt_value.get("sourceCommit") == current["sourceCommit"]
                and receipt_value.get("packageSha256") == package_sha
                and receipt_value.get("productionTreeSha256", receipt_value.get("productionTreeAfterSha256")) == production_sha
                and receipt_value.get("baselineAfterRevision") == baseline["revision"]
                and receipt_value.get("baselineAfterSha256") == baseline_sha
            )
            if not common_receipt:
                raise LineageError("RECEIPT_INVALID", "current receipt 与 baseline/package/tree 不一致")
            health = dict(self.health_runner(active, version) or {})
            if tree_hash(version) != production_sha:
                raise LineageError("HEALTH_MUTATED_ARTIFACT", "status 健康检查改变了生产树")
            if (
                sha256_path(artifact) != package_sha
                or self.config.baseline.read_bytes() != baseline_raw
                or tag_object(self.config.trusted_repo, current["sourceTag"], optional=True)
                != current["sourceTagObject"]
            ):
                raise LineageError("STATUS_CHANGED_DURING_READ", "status 一致性读取期间生产闭包发生变化")
            return {
                "ok": True,
                "status": "active",
                "trustMode": "manual_guard",
                "strongIsolation": False,
                "baselineRevision": baseline["revision"],
                "baselineSha256": baseline_sha,
                "baselineBytes": len(baseline_raw),
                "release": active,
                "sourceTag": current["sourceTag"],
                "sourceTagObject": current["sourceTagObject"],
                "sourceCommit": current["sourceCommit"],
                "packageSha256": package_sha,
                "productionTreeSha256": production_sha,
                "artifactSpec": spec,
                "receipt": str(receipt),
                "health": health,
            }

    def baseline_snapshot(self) -> tuple[dict[str, Any], bytes, str]:
        return read_baseline(self.config.baseline)

    def verify_envelope(self, envelope: Path, temporary: Path) -> dict[str, Any]:
        if envelope.is_symlink() or not envelope.is_file():
            raise LineageError("ENVELOPE_INVALID", "envelope 必须是非符号链接普通文件")
        validate_zip_container(envelope, "envelope")
        root = temporary / "envelope"
        try:
            with zipfile.ZipFile(envelope) as archive:
                members = safe_members(archive, "envelope")
                expected = {"artifact.zip", "lineage.json", "source.bundle", "SHA256SUMS"}
                if set(members) != expected or any(member.is_dir() for member in members.values()):
                    raise LineageError("ENVELOPE_CLOSURE_MISMATCH", "envelope 只允许四个根级普通文件")
                lineage_raw = read_zip_member(
                    archive, members["lineage.json"], label="lineage.json",
                )
                sums = parse_sums(
                    read_zip_member(
                        archive, members["SHA256SUMS"], label="envelope SHA256SUMS",
                    ),
                    expected - {"SHA256SUMS"},
                    "envelope SHA256SUMS",
                )
                for name in sorted(expected - {"SHA256SUMS"}):
                    if sha256_zip_member(archive, members[name]) != sums[name]:
                        raise LineageError("ENVELOPE_TAMPERED", f"envelope 文件哈希不一致：{name}")
                safe_extract(archive, members, root)
        except zipfile.BadZipFile as exc:
            raise LineageError("ENVELOPE_INVALID", "envelope 不是有效 ZIP") from exc
        manifest = load_json_bytes(lineage_raw, "ENVELOPE_MANIFEST_INVALID", "lineage.json")
        required = {
            "schema", "release", "baseRelease", "baseTag", "baseTagObject", "baseCommit",
            "sourceTag", "sourceTagObject", "sourceCommit", "taskId", "candidateRef", "dirtyWorktree",
            "baselineRevision", "baselineSha256",
            "packageSha256", "artifact", "sourceBundle", "acceptance",
        }
        if set(manifest) != required or manifest.get("schema") != ENVELOPE_SCHEMA:
            raise LineageError("ENVELOPE_MANIFEST_INVALID", "lineage.json schema 或字段闭包无效")
        for key in ("release", "baseRelease", "baseTag", "sourceTag"):
            require_name(manifest.get(key), "ENVELOPE_MANIFEST_INVALID", key)
        if not TASK_RE.fullmatch(str(manifest.get("taskId") or "")):
            raise LineageError("ENVELOPE_MANIFEST_INVALID", "taskId 无效")
        expected_ref = approved_candidate_ref(manifest["taskId"], manifest["baselineRevision"])
        if manifest.get("candidateRef") != expected_ref:
            raise LineageError("CANDIDATE_REF_INVALID", "candidateRef 未绑定 taskId 与 baselineRevision")
        for key in ("baseTagObject", "baseCommit", "sourceTagObject", "sourceCommit"):
            require_commit(manifest.get(key), "ENVELOPE_MANIFEST_INVALID", key)
        for key in ("baselineSha256", "packageSha256"):
            require_digest(manifest.get(key), "ENVELOPE_MANIFEST_INVALID", key)
        if manifest.get("dirtyWorktree") is not False:
            raise LineageError("DIRTY_WORKTREE", "envelope 必须明确 dirtyWorktree=false")
        if not isinstance(manifest.get("baselineRevision"), int) or manifest["baselineRevision"] < 1:
            raise LineageError("ENVELOPE_MANIFEST_INVALID", "baselineRevision 无效")
        if manifest["sourceTag"] == manifest["baseTag"] or manifest["sourceCommit"] == manifest["baseCommit"]:
            raise LineageError("OLD_SOURCE_RELABEL", "sourceTag/sourceCommit 未相对生产基线前进")
        if manifest.get("acceptance") != {"status": "pending", "acceptedBy": None, "acceptedAt": None}:
            raise LineageError("ACCEPTANCE_FORGED", "envelope 不得伪造验收结果")
        artifact_meta = require_mapping(manifest.get("artifact"), "ENVELOPE_MANIFEST_INVALID", "artifact")
        bundle_meta = require_mapping(manifest.get("sourceBundle"), "ENVELOPE_MANIFEST_INVALID", "sourceBundle")
        if set(artifact_meta) != {
            "file", "releaseManifestSha256", "sha256SumsSha256",
            "artifactSpecPath", "artifactSpecSha256",
        } or artifact_meta.get("file") != "artifact.zip":
            raise LineageError("ENVELOPE_MANIFEST_INVALID", "artifact 元数据字段闭包无效")
        if set(bundle_meta) != {"file", "sha256"} or bundle_meta.get("file") != "source.bundle":
            raise LineageError("ENVELOPE_MANIFEST_INVALID", "sourceBundle 元数据字段闭包无效")
        for key in ("releaseManifestSha256", "sha256SumsSha256", "artifactSpecSha256"):
            require_digest(artifact_meta.get(key), "ENVELOPE_MANIFEST_INVALID", f"artifact.{key}")
        require_repo_path(artifact_meta.get("artifactSpecPath"), "ENVELOPE_MANIFEST_INVALID", "artifactSpecPath")
        require_digest(bundle_meta.get("sha256"), "ENVELOPE_MANIFEST_INVALID", "sourceBundle.sha256")
        if sha256_path(root / "artifact.zip") != manifest["packageSha256"]:
            raise LineageError("PACKAGE_SHA_MISMATCH", "artifact.zip 与 packageSha256 不一致")
        if sha256_path(root / "source.bundle") != bundle_meta["sha256"]:
            raise LineageError("SOURCE_BUNDLE_TAMPERED", "source.bundle 哈希不一致")
        return {"root": root, "manifest": manifest, "envelopeSha256": sha256_path(envelope)}

    def verify_git(self, checked: dict[str, Any], temporary: Path) -> Path:
        manifest = checked["manifest"]
        base_tag = manifest["baseTag"]
        base_commit = manifest["baseCommit"]
        source_tag = manifest["sourceTag"]
        source_commit = manifest["sourceCommit"]
        actual_base = tag_commit(self.config.trusted_repo, base_tag, optional=True)
        if not actual_base:
            raise LineageError("BASE_TAG_UNKNOWN", "可信仓库缺少生产 baseTag")
        if actual_base != base_commit:
            raise LineageError("TAG_REBIND", f"可信 baseTag 已移动：{actual_base} != {base_commit}")
        require_annotated_tag(self.config.trusted_repo, base_tag, base_commit)
        if tag_object(self.config.trusted_repo, base_tag) != manifest["baseTagObject"]:
            raise LineageError("TAG_REBIND", "可信 baseTag object 与基线不一致")
        self.validate_git_authority_ref(f"refs/tags/{base_tag}")
        existing = tag_commit(self.config.trusted_repo, source_tag, optional=True)
        if existing:
            code = "RELEASE_EXISTS" if existing == source_commit else "TAG_REBIND"
            raise LineageError(code, f"受保护 sourceTag 已存在：{source_tag}@{existing}")
        approved_source = git_text(
            self.config.trusted_repo,
            ["rev-parse", f"{manifest['candidateRef']}^{{commit}}"],
            optional=True,
        )
        if not approved_source:
            raise LineageError(
                "UNKNOWN_COMMIT",
                "sourceCommit 未经独立认证的 candidate ref 授权；bundle 不能引入信任",
            )
        if approved_source != source_commit:
            raise LineageError(
                "CANDIDATE_REF_MISMATCH",
                f"candidateRef 指向 {approved_source}，不是 sourceCommit {source_commit}",
            )
        self.validate_git_authority_ref(manifest["candidateRef"])
        require_ancestor(self.config.trusted_repo, base_commit, source_commit)
        verify_repo = temporary / "verify.git"
        git_bytes(temporary, ["init", "--bare", str(verify_repo)])
        git_bytes(verify_repo, ["fetch", "--no-tags", str(self.config.trusted_repo), f"refs/tags/{base_tag}:refs/tags/{base_tag}"])
        git_bytes(
            verify_repo,
            ["fetch", "--no-tags", str(self.config.trusted_repo),
             f"{manifest['candidateRef']}:refs/xirang/trusted-source"],
        )
        bundle = checked["root"] / "source.bundle"
        git_bytes(verify_repo, ["bundle", "verify", str(bundle)])
        heads = git_text(verify_repo, ["bundle", "list-heads", str(bundle)])
        advertised = {
            parts[1]
            for line in heads.splitlines()
            if len(parts := line.split(maxsplit=1)) == 2
        }
        if advertised != {f"refs/tags/{source_tag}"}:
            raise LineageError(
                "SOURCE_BUNDLE_HEADS_INVALID",
                "source.bundle 必须只 advertised 声明的 sourceTag",
            )
        git_bytes(verify_repo, ["fetch", "--no-tags", str(bundle), f"refs/tags/{source_tag}:refs/tags/{source_tag}"])
        resolved = git_text(verify_repo, ["rev-parse", f"{source_commit}^{{commit}}"], optional=True)
        if resolved != source_commit:
            raise LineageError("UNKNOWN_COMMIT", "sourceCommit 不在可信仓")
        if tag_commit(verify_repo, source_tag) != source_commit:
            raise LineageError("TAG_COMMIT_MISMATCH", "sourceTag 与 sourceCommit 不匹配")
        require_annotated_tag(verify_repo, source_tag, source_commit)
        if tag_object(verify_repo, source_tag) != manifest["sourceTagObject"]:
            raise LineageError("TAG_REBIND", "sourceTag object 与 envelope 不一致")
        checked["verifyRepo"] = verify_repo
        return verify_repo

    def verify_artifact(self, checked: dict[str, Any], verify_repo: Path, temporary: Path) -> None:
        manifest = checked["manifest"]
        artifact_root = temporary / "artifact"
        payload = artifact_payload(checked["root"] / "artifact.zip", extract_to=artifact_root)
        artifact_meta = manifest["artifact"]
        if sha256_bytes(payload["releaseRaw"]) != artifact_meta["releaseManifestSha256"]:
            raise LineageError("RELEASE_MANIFEST_TAMPERED", "release.json 哈希与 envelope 不一致")
        if sha256_bytes(payload["sumsRaw"]) != artifact_meta["sha256SumsSha256"]:
            raise LineageError("MANIFEST_TAMPERED", "artifact SHA256SUMS 哈希与 envelope 不一致")
        lineage = {key: manifest[key] for key in (
            "baseRelease", "baseTag", "baseTagObject", "baseCommit",
            "sourceTag", "sourceTagObject", "sourceCommit",
            "taskId", "candidateRef", "dirtyWorktree", "baselineRevision", "baselineSha256",
        )}
        verify_release_manifest(payload["release"], lineage)
        if payload["release"]["release"] != manifest["release"]:
            raise LineageError("RELEASE_LINEAGE_MISMATCH", "artifact release 与 envelope 不一致")
        spec = verify_artifact_spec(
            self.config.trusted_repo,
            manifest["sourceCommit"],
            payload["release"],
            payload["sums"],
            payload["modes"],
        )
        if spec["path"] != artifact_meta["artifactSpecPath"] or spec["sha256"] != artifact_meta["artifactSpecSha256"]:
            raise LineageError("ARTIFACT_SPEC_TAMPERED", "artifact spec 与 envelope 绑定不一致")
        checked.update({
            "artifactRoot": artifact_root,
            "releaseManifestSha256": artifact_meta["releaseManifestSha256"],
            "sha256SumsSha256": artifact_meta["sha256SumsSha256"],
            "artifactTreeSha256": tree_hash(artifact_root),
        })

    def preflight(self, envelope: Path, temporary: Path) -> dict[str, Any]:
        baseline, baseline_raw, baseline_sha = self.baseline_snapshot()
        checked = self.verify_envelope(envelope, temporary)
        manifest = checked["manifest"]
        current = dict(require_mapping(baseline["current"], "BASELINE_INVALID", "current"))
        release = manifest["release"]
        if (
            (self.config.versions / release).exists()
            or (self.config.packages / manifest["sourceTag"]).exists()
            or (self.config.receipts / f"{release}.json").exists()
        ):
            raise LineageError("RELEASE_EXISTS", f"目标 release 已存在：{release}")
        if manifest["baselineRevision"] != baseline["revision"]:
            raise LineageError("BASELINE_REVISION_CONFLICT", "候选使用了旧 baseline revision")
        if manifest["baselineSha256"] != baseline_sha:
            raise LineageError("BASELINE_SHA_CONFLICT", "候选绑定的 baseline 哈希已失效")
        if (
            manifest["baseRelease"] != current["release"]
            or manifest["baseTag"] != current["sourceTag"]
            or manifest["baseTagObject"] != current["sourceTagObject"]
            or manifest["baseCommit"] != current["sourceCommit"]
        ):
            raise LineageError("BASELINE_LINEAGE_MISMATCH", "候选 base lineage 与生产基线不一致")
        if self.current_release() != current["release"]:
            raise LineageError("CURRENT_CONFLICT", "current 指针与 production-baseline 不一致")
        artifact_relative = require_repo_path(
            current["artifact"], "BASELINE_INVALID", "current.artifact",
        )
        current_artifact = self.config.root.joinpath(*artifact_relative.split("/"))
        packages_root = self.config.packages.resolve()
        try:
            resolved_artifact = current_artifact.resolve(strict=True)
        except OSError as exc:
            raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", f"当前生产包不可读：{exc}") from exc
        if (
            current_artifact.is_symlink()
            or resolved_artifact.parent == packages_root
            or packages_root not in resolved_artifact.parents
            or not resolved_artifact.is_file()
        ):
            raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包路径越界或类型无效")
        relative_parts = Path(artifact_relative).parts
        cursor = self.config.root
        for part in relative_parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包路径包含符号链接")
        if sha256_path(resolved_artifact) != current["artifactSha256"]:
            raise LineageError("PRODUCTION_ARTIFACT_MISMATCH", "当前生产包 SHA 与 baseline 不一致")
        current_tree_sha = tree_hash(self.config.versions / current["release"])
        if current_tree_sha != current["productionTreeSha256"]:
            raise LineageError("PRODUCTION_TREE_MISMATCH", "当前生产树 SHA 与 baseline 不一致")
        checked.update({
            "baseline": baseline, "baselineRaw": baseline_raw, "baselineSha256": baseline_sha,
            "current": current["release"], "envelopePath": envelope,
            "productionTreeSha256": current_tree_sha,
        })
        verify_repo = self.verify_git(checked, temporary)
        self.verify_artifact(checked, verify_repo, temporary)
        return checked

    @staticmethod
    def fsync_directory(path: Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def atomic_current(self, release: str) -> None:
        temporary = self.config.root / f".current-{os.getpid()}.tmp"
        temporary.unlink(missing_ok=True)
        try:
            os.symlink(f"versions/{release}", temporary)
            os.replace(temporary, self.config.current)
            self.fsync_directory(self.config.root)
        finally:
            temporary.unlink(missing_ok=True)

    def atomic_baseline(self, value: bytes) -> None:
        temporary = self.config.root / f".baseline-{os.getpid()}.tmp"
        temporary.unlink(missing_ok=True)
        try:
            descriptor = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config.baseline)
            self.fsync_directory(self.config.root)
        finally:
            temporary.unlink(missing_ok=True)

    def clear_journal(self) -> None:
        self.config.journal.unlink(missing_ok=True)
        self.fsync_directory(self.config.root)

    def fsync_release_directories(self) -> None:
        for path in (
            self.config.receipts,
            self.config.staging,
            self.config.packages,
            self.config.versions,
        ):
            self.fsync_directory(path)

    def clear_journal_temporaries(self) -> None:
        changed = False
        for path in self.config.root.iterdir():
            is_journal = (
                path.name.startswith(f".{self.config.journal.name}.")
                and path.name.endswith(".tmp")
            )
            is_baseline = bool(re.fullmatch(r"\.baseline-[1-9][0-9]*\.tmp", path.name))
            is_current = bool(re.fullmatch(r"\.current-[1-9][0-9]*\.tmp", path.name))
            if not (is_journal or is_baseline or is_current):
                continue
            if is_current:
                if not path.is_symlink():
                    raise LineageError("ROLLBACK_INCOMPLETE", f"current 临时文件类型无效：{path}")
            elif path.is_symlink() or not path.is_file():
                raise LineageError("ROLLBACK_INCOMPLETE", f"事务临时文件类型无效：{path}")
            path.unlink()
            changed = True
        if changed:
            self.fsync_directory(self.config.root)

    def clear_orphan_staging(self) -> None:
        changed = False
        for path in self.config.staging.iterdir():
            is_release_stage = bool(
                re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,127}-[1-9][0-9]*", path.name)
            )
            is_host_verification = bool(
                re.fullmatch(r"\.xirang-lineage-host-[A-Za-z0-9_]{8,16}", path.name)
            )
            if not (is_release_stage or is_host_verification):
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise LineageError("ROLLBACK_INCOMPLETE", f"孤儿 staging 类型无效：{path}")
            shutil.rmtree(path)
            changed = True
        if changed:
            self.fsync_directory(self.config.staging)

    def recover(self) -> dict[str, Any]:
        self.validate_layout()
        descriptor = os.open(str(self.config.lock), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            self.ensure_no_bootstrap_transaction()
            self.clear_journal_temporaries()
            result = self.recover_incomplete()
            self.clear_orphan_staging()
            return {
                "ok": True,
                "status": "recovery-complete" if result else "no-recovery-needed",
                "recovery": result,
            }

    def recover_incomplete(self) -> dict[str, Any] | None:
        if not self.config.journal.exists():
            return None
        journal = load_json_path(
            self.config.journal,
            "ROLLBACK_INCOMPLETE",
            "release transaction journal",
        )
        required = {
            "schema", "target", "parent", "sourceTag", "sourceTagObject", "sourceCommit", "candidateRef",
            "taskId", "baselineBeforeRevision", "stageName",
            "baselineBeforeBase64", "baselineBeforeSha256", "baselineAfterSha256",
            "productionTreeBeforeSha256", "productionTreeAfterSha256",
            "packageSha256", "envelopeSha256", "createdAt",
        }
        if set(journal) != required or journal.get("schema") != JOURNAL_SCHEMA:
            raise LineageError("ROLLBACK_INCOMPLETE", "事务日志 schema 或字段闭包无效")
        target = require_name(journal.get("target"), "ROLLBACK_INCOMPLETE", "journal.target")
        parent = require_name(journal.get("parent"), "ROLLBACK_INCOMPLETE", "journal.parent")
        source_tag = require_name(journal.get("sourceTag"), "ROLLBACK_INCOMPLETE", "journal.sourceTag")
        source_commit = require_commit(
            journal.get("sourceCommit"), "ROLLBACK_INCOMPLETE", "journal.sourceCommit",
        )
        source_tag_object = require_commit(
            journal.get("sourceTagObject"), "ROLLBACK_INCOMPLETE", "journal.sourceTagObject",
        )
        task_id = str(journal.get("taskId") or "")
        revision = journal.get("baselineBeforeRevision")
        if (
            not TASK_RE.fullmatch(task_id)
            or not isinstance(revision, int)
            or revision < 1
            or journal.get("candidateRef") != approved_candidate_ref(task_id, revision)
        ):
            raise LineageError("ROLLBACK_INCOMPLETE", "事务日志 candidateRef 授权绑定无效")
        stage_name = str(journal.get("stageName") or "")
        if not re.fullmatch(rf"\.{re.escape(target)}-[1-9][0-9]*", stage_name):
            raise LineageError("ROLLBACK_INCOMPLETE", "事务日志 staging 名称无效")
        for key in (
            "baselineBeforeSha256", "baselineAfterSha256", "productionTreeBeforeSha256",
            "productionTreeAfterSha256", "packageSha256", "envelopeSha256",
        ):
            require_digest(journal.get(key), "ROLLBACK_INCOMPLETE", f"journal.{key}")
        try:
            baseline_before = base64.b64decode(
                str(journal["baselineBeforeBase64"]), validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise LineageError("ROLLBACK_INCOMPLETE", "事务日志 baseline preimage 无效") from exc
        if sha256_bytes(baseline_before) != journal["baselineBeforeSha256"]:
            raise LineageError("ROLLBACK_INCOMPLETE", "事务日志 baseline preimage SHA 无效")
        receipt = self.config.receipts / f"{target}.json"
        target_version = self.config.versions / target
        target_package = self.config.packages / source_tag
        stage = self.config.staging / stage_name
        receipt_prefix = f".{receipt.name}."
        for temporary in self.config.receipts.iterdir():
            if temporary.name.startswith(receipt_prefix) and temporary.name.endswith(".tmp"):
                if temporary.is_symlink() or not temporary.is_file():
                    raise LineageError("ROLLBACK_INCOMPLETE", f"receipt 临时文件类型无效：{temporary}")
                temporary.unlink()
        self.fsync_directory(self.config.receipts)

        committed = False
        try:
            receipt_value = load_json_path(receipt, "RECEIPT_INVALID", "deployment receipt")
            receipt_fields = {
                "schema", "status", "from", "to", "envelopeSha256", "packageSha256",
                "sourceTag", "sourceTagObject", "sourceCommit", "taskId", "candidateRef",
                "baselineBeforeRevision", "baselineBeforeSha256",
                "baselineAfterRevision", "baselineAfterSha256",
                "productionTreeBeforeSha256", "productionTreeAfterSha256",
                "health", "at",
            }
            committed = (
                set(receipt_value) == receipt_fields
                and self.current_release() == target
                and sha256_path(self.config.baseline) == journal["baselineAfterSha256"]
                and tag_commit(self.config.trusted_repo, source_tag, optional=True) == source_commit
                and tag_object(self.config.trusted_repo, source_tag, optional=True) == source_tag_object
                and receipt_value.get("schema") == RECEIPT_SCHEMA
                and receipt_value.get("status") == "deployed-pending-user-acceptance"
                and receipt_value.get("from") == parent
                and receipt_value.get("to") == target
                and receipt_value.get("envelopeSha256") == journal["envelopeSha256"]
                and receipt_value.get("packageSha256") == journal["packageSha256"]
                and receipt_value.get("sourceTag") == source_tag
                and receipt_value.get("sourceTagObject") == source_tag_object
                and receipt_value.get("sourceCommit") == source_commit
                and receipt_value.get("taskId") == task_id
                and receipt_value.get("candidateRef") == journal["candidateRef"]
                and receipt_value.get("baselineBeforeRevision") == revision
                and receipt_value.get("baselineBeforeSha256") == journal["baselineBeforeSha256"]
                and receipt_value.get("baselineAfterRevision") == revision + 1
                and receipt_value.get("baselineAfterSha256") == journal["baselineAfterSha256"]
                and receipt_value.get("productionTreeBeforeSha256") == journal["productionTreeBeforeSha256"]
                and receipt_value.get("productionTreeAfterSha256") == journal["productionTreeAfterSha256"]
                and isinstance(receipt_value.get("health"), Mapping)
                and isinstance(receipt_value.get("at"), str)
                and target_version.is_dir()
                and tree_hash(target_version) == journal["productionTreeAfterSha256"]
                and (target_package / "artifact.zip").is_file()
                and sha256_path(target_package / "artifact.zip") == journal["packageSha256"]
                and (target_package / "lineage-envelope.zip").is_file()
                and sha256_path(target_package / "lineage-envelope.zip") == journal["envelopeSha256"]
            )
        except (LineageError, OSError):
            committed = False
        try:
            current_after_crash = self.current_release()
        except LineageError as exc:
            raise LineageError("ROLLBACK_INCOMPLETE", f"事务恢复无法解析 current：{exc}") from exc
        if committed:
            if stage.is_symlink():
                raise LineageError("ROLLBACK_INCOMPLETE", "已提交事务的 staging 变成符号链接")
            if stage.exists():
                shutil.rmtree(stage)
            if stage.exists():
                raise LineageError("ROLLBACK_INCOMPLETE", "已提交事务的 staging 无法清理")
            self.fsync_directory(self.config.staging)
            self.clear_journal()
            return {
                "recovered": True,
                "action": "finalized-committed",
                "target": target,
                "sourceTag": source_tag,
                "sourceCommit": source_commit,
                "packageSha256": journal["packageSha256"],
                "envelopeSha256": journal["envelopeSha256"],
            }
        if current_after_crash == target:
            raise LineageError(
                "ROLLBACK_INCOMPLETE",
                "current 最终提交点已越过但提交闭包不完整；拒绝回滚已上线版本",
            )
        if current_after_crash != parent:
            raise LineageError("ROLLBACK_INCOMPLETE", "current 既不指向事务父版本也不指向目标版本")

        errors: list[str] = []
        actual_tag = tag_commit(self.config.trusted_repo, source_tag, optional=True)
        if actual_tag:
            if actual_tag != source_commit:
                errors.append("sourceTag 指向非事务 commit")
            else:
                try:
                    durable_update_ref(
                        self.config.trusted_repo,
                        f"refs/tags/{source_tag}",
                        None,
                        source_tag_object,
                    )
                except Exception as exc:
                    errors.append(f"tag={exc}")
        try:
            self.atomic_baseline(baseline_before)
        except Exception as exc:
            errors.append(f"baseline={exc}")
        try:
            if not (self.config.versions / parent).is_dir():
                raise LineageError("CURRENT_INVALID", "事务父版本目录不存在")
            self.atomic_current(parent)
        except Exception as exc:
            errors.append(f"current={exc}")
        try:
            receipt.unlink(missing_ok=True)
            for path in (stage, target_package, target_version):
                if path.is_symlink():
                    raise LineageError("ROLLBACK_INCOMPLETE", f"事务目标变成符号链接：{path}")
                if path.exists():
                    shutil.rmtree(path)
        except Exception as exc:
            errors.append(f"targets={exc}")
        try:
            self.fsync_release_directories()
        except Exception as exc:
            errors.append(f"target-fsync={exc}")
        try:
            if (
                self.current_release() != parent
                or sha256_path(self.config.baseline) != journal["baselineBeforeSha256"]
                or tree_hash(self.config.versions / parent) != journal["productionTreeBeforeSha256"]
                or stage.exists() or target_version.exists() or target_package.exists() or receipt.exists()
                or bool(tag_commit(self.config.trusted_repo, source_tag, optional=True))
            ):
                errors.append("恢复后 current/baseline/tree/目标闭包不一致")
        except Exception as exc:
            errors.append(f"verify={exc}")
        if errors:
            raise LineageError("ROLLBACK_INCOMPLETE", "崩溃恢复失败：" + "；".join(errors))
        self.clear_journal()
        return {"recovered": True, "action": "rolled-back", "target": target}

    def next_baseline(self, checked: Mapping[str, Any]) -> bytes:
        manifest = checked["manifest"]
        previous = dict(checked["baseline"]["current"])
        now = utc_now()
        value = {
            "schema": BASELINE_SCHEMA,
            "revision": checked["baseline"]["revision"] + 1,
            "status": "deployed-pending-acceptance",
            "current": {
                "release": manifest["release"],
                "artifact": f"packages/{manifest['sourceTag']}/artifact.zip",
                "artifactSha256": manifest["packageSha256"],
                "packageSha256": manifest["packageSha256"],
                "releaseManifestSha256": checked["releaseManifestSha256"],
                "sha256SumsSha256": checked["sha256SumsSha256"],
                "productionTreeSha256": checked["artifactTreeSha256"],
                "sourceTag": manifest["sourceTag"],
                "sourceTagObject": manifest["sourceTagObject"],
                "sourceCommit": manifest["sourceCommit"],
                "taskId": manifest["taskId"],
                "candidateRef": manifest["candidateRef"],
                "baseRelease": manifest["baseRelease"],
                "baseTag": manifest["baseTag"],
                "baseCommit": manifest["baseCommit"],
                "dirtyWorktree": False,
                "deployedAt": now,
            },
            "previous": previous,
            "acceptance": {"status": "pending", "acceptedBy": None, "acceptedAt": None},
        }
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"

    def promote(self, envelope: Path, *, check_only: bool = False) -> dict[str, Any]:
        self.validate_layout()
        candidate = envelope.expanduser().absolute()
        if candidate.is_symlink() or not candidate.is_file():
            raise LineageError("ENVELOPE_INVALID", "envelope 必须是非符号链接普通文件")
        if candidate.resolve().parent != self.config.staging.resolve():
            raise LineageError("ENVELOPE_LOCATION_INVALID", "envelope 必须位于受保护 staging 根级")
        descriptor = os.open(str(self.config.lock), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            self.ensure_no_bootstrap_transaction()
            self.clear_journal_temporaries()
            recovery = None
            if self.config.journal.exists():
                if check_only:
                    raise LineageError(
                        "RECOVERY_REQUIRED",
                        "存在未完成事务；只读 check 不执行恢复，请由授权 recover/promote 先恢复",
                    )
                recovery = self.recover_incomplete()
                if recovery and recovery["action"] == "finalized-committed":
                    self.clear_orphan_staging()
                    return {
                        "ok": True,
                        "status": "recovered-deployed-pending-user-acceptance",
                        **recovery,
                    }
            self.clear_orphan_staging()
            with tempfile.TemporaryDirectory(
                prefix=".xirang-lineage-host-",
                dir=self.config.staging,
            ) as raw_tmp:
                temporary = Path(raw_tmp)
                frozen = temporary / "candidate-envelope.zip"
                freeze_regular_file(candidate, frozen)
                checked = self.preflight(frozen, temporary)
                if check_only:
                    return {
                        "ok": True, "status": "checked", "current": checked["current"],
                        "target": checked["manifest"]["release"],
                        "baselineRevision": checked["baseline"]["revision"],
                        "baselineSha256": checked["baselineSha256"],
                        "productionTreeSha256": checked["productionTreeSha256"],
                        "packageSha256": checked["manifest"]["packageSha256"],
                    }
                result = self._commit(checked)
                if recovery:
                    result["recovery"] = recovery
                return result

    def _commit(self, checked: dict[str, Any]) -> dict[str, Any]:
        manifest = checked["manifest"]
        target = manifest["release"]
        parent = checked["current"]
        target_version = self.config.versions / target
        target_package = self.config.packages / manifest["sourceTag"]
        stage = self.config.staging / f".{target}-{os.getpid()}"
        receipt = self.config.receipts / f"{target}.json"
        target_installed = package_installed = current_changed = tag_created = baseline_changed = receipt_created = journal_created = False
        next_baseline: bytes | None = None
        try:
            if tag_commit(self.config.trusted_repo, manifest["baseTag"], optional=True) != manifest["baseCommit"]:
                raise LineageError("TAG_REBIND", "提交前生产 baseTag 已发生漂移")
            if tag_object(self.config.trusted_repo, manifest["baseTag"], optional=True) != manifest["baseTagObject"]:
                raise LineageError("TAG_REBIND", "提交前生产 baseTag object 已发生漂移")
            if tag_commit(self.config.trusted_repo, manifest["sourceTag"], optional=True):
                raise LineageError("RELEASE_EXISTS", "提交前 sourceTag 已被并发占用")
            if git_text(
                self.config.trusted_repo,
                ["rev-parse", f"{manifest['candidateRef']}^{{commit}}"],
                optional=True,
            ) != manifest["sourceCommit"]:
                raise LineageError("CANDIDATE_REF_MISMATCH", "提交前 candidateRef 已发生漂移")
            stage.mkdir(parents=True, exist_ok=False, mode=0o700)
            shutil.copytree(checked["artifactRoot"], stage / "version")
            (stage / "package").mkdir(mode=0o700)
            shutil.copy2(checked["root"] / "artifact.zip", stage / "package" / "artifact.zip")
            shutil.copy2(checked["envelopePath"], stage / "package" / "lineage-envelope.zip")
            health = dict(self.health_runner(target, stage / "version") or {})
            staged_tree_sha = tree_hash(stage / "version")
            if staged_tree_sha != checked["artifactTreeSha256"]:
                raise LineageError("HEALTH_MUTATED_ARTIFACT", "离线健康检查改变了待发布生产树")
            self.failpoint("after_offline_health")
            fsync_tree(stage / "version")
            fsync_tree(stage / "package")
            self.fsync_directory(stage)
            next_baseline = self.next_baseline(checked)
            journal_value = {
                "schema": JOURNAL_SCHEMA,
                "target": target,
                "parent": parent,
                "sourceTag": manifest["sourceTag"],
                "sourceTagObject": manifest["sourceTagObject"],
                "sourceCommit": manifest["sourceCommit"],
                "candidateRef": manifest["candidateRef"],
                "taskId": manifest["taskId"],
                "baselineBeforeRevision": checked["baseline"]["revision"],
                "stageName": stage.name,
                "baselineBeforeBase64": base64.b64encode(checked["baselineRaw"]).decode("ascii"),
                "baselineBeforeSha256": checked["baselineSha256"],
                "baselineAfterSha256": sha256_bytes(next_baseline),
                "productionTreeBeforeSha256": checked["productionTreeSha256"],
                "productionTreeAfterSha256": staged_tree_sha,
                "packageSha256": manifest["packageSha256"],
                "envelopeSha256": checked["envelopeSha256"],
                "createdAt": utc_now(),
            }
            write_atomic_new_file(
                self.config.journal,
                json.dumps(journal_value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n",
            )
            journal_created = True
            self.fsync_directory(self.config.root)
            self.failpoint("before_install")
            os.replace(stage / "version", target_version)
            target_installed = True
            os.replace(stage / "package", target_package)
            package_installed = True
            self.fsync_directory(self.config.versions)
            self.fsync_directory(self.config.packages)
            git_bytes(
                self.config.trusted_repo,
                ["fetch", "--no-tags", "--no-write-fetch-head", str(checked["verifyRepo"]),
                 f"refs/tags/{manifest['sourceTag']}"],
            )
            if (
                git_text(
                    self.config.trusted_repo,
                    ["cat-file", "-t", manifest["sourceTagObject"]],
                    optional=True,
                ) != "tag"
                or git_text(
                    self.config.trusted_repo,
                    ["rev-parse", f"{manifest['sourceTagObject']}^{{commit}}"],
                    optional=True,
                ) != manifest["sourceCommit"]
            ):
                raise LineageError("TAG_REBIND", "候选 annotated Tag object 未可信导入")
            durable_update_ref(
                self.config.trusted_repo,
                f"refs/tags/{manifest['sourceTag']}",
                manifest["sourceTagObject"],
                "0" * 40,
            )
            tag_created = True
            if (
                tag_commit(self.config.trusted_repo, manifest["baseTag"], optional=True) != manifest["baseCommit"]
                or tag_object(self.config.trusted_repo, manifest["baseTag"], optional=True) != manifest["baseTagObject"]
                or tag_commit(self.config.trusted_repo, manifest["sourceTag"], optional=True) != manifest["sourceCommit"]
                or tag_object(self.config.trusted_repo, manifest["sourceTag"], optional=True) != manifest["sourceTagObject"]
                or git_text(
                    self.config.trusted_repo,
                    ["rev-parse", f"{manifest['candidateRef']}^{{commit}}"],
                    optional=True,
                ) != manifest["sourceCommit"]
            ):
                raise LineageError("TAG_REBIND", "基线提交前受保护 Tag 绑定发生漂移")
            self.failpoint("after_tag")
            self.atomic_baseline(next_baseline)
            baseline_changed = True
            self.failpoint("after_baseline")
            receipt_value = {
                "schema": RECEIPT_SCHEMA,
                "status": "deployed-pending-user-acceptance",
                "from": parent,
                "to": target,
                "envelopeSha256": checked["envelopeSha256"],
                "packageSha256": manifest["packageSha256"],
                "sourceTag": manifest["sourceTag"],
                "sourceTagObject": manifest["sourceTagObject"],
                "sourceCommit": manifest["sourceCommit"],
                "taskId": manifest["taskId"],
                "candidateRef": manifest["candidateRef"],
                "baselineBeforeRevision": checked["baseline"]["revision"],
                "baselineBeforeSha256": checked["baselineSha256"],
                "baselineAfterRevision": checked["baseline"]["revision"] + 1,
                "baselineAfterSha256": sha256_bytes(next_baseline),
                "productionTreeBeforeSha256": checked["productionTreeSha256"],
                "productionTreeAfterSha256": tree_hash(target_version),
                "health": health,
                "at": utc_now(),
            }
            write_atomic_new_file(
                receipt,
                json.dumps(receipt_value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n",
            )
            receipt_created = True
            self.fsync_directory(self.config.receipts)
            self.failpoint("after_receipt")
            self.atomic_current(target)
            current_changed = True
            self.failpoint("after_switch")
            shutil.rmtree(stage, ignore_errors=True)
            self.fsync_directory(self.config.staging)
            self.clear_journal()
            journal_created = False
            return {"ok": True, "receipt": str(receipt), **receipt_value}
        except BaseException as exc:
            rollback_errors: list[str] = []
            journal_created = journal_created or self.config.journal.exists()
            try:
                crossed_commit_point = self.current_release() == target
            except (LineageError, OSError):
                crossed_commit_point = current_changed
            if crossed_commit_point:
                raise LineageError(
                    "PROMOTION_COMMITTED_RECOVERY_REQUIRED",
                    f"current 最终提交点已越过；保留完整新生产并等待 recover 收尾：{exc}",
                ) from exc
            target_installed = target_installed or target_version.exists()
            package_installed = package_installed or target_package.exists()
            receipt_created = receipt_created or receipt.exists()
            try:
                baseline_changed = baseline_changed or (
                    next_baseline is not None
                    and sha256_path(self.config.baseline) == sha256_bytes(next_baseline)
                )
            except OSError:
                pass
            try:
                tag_created = tag_created or (
                    tag_commit(
                        self.config.trusted_repo,
                        manifest["sourceTag"],
                        optional=True,
                    ) == manifest["sourceCommit"]
                )
            except LineageError:
                pass
            if receipt_created:
                try:
                    receipt.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"receipt={rollback_exc}")
            if current_changed:
                try:
                    self.atomic_current(parent)
                except Exception as rollback_exc:
                    rollback_errors.append(f"current={rollback_exc}")
            if baseline_changed:
                try:
                    self.atomic_baseline(checked["baselineRaw"])
                except Exception as rollback_exc:
                    rollback_errors.append(f"baseline={rollback_exc}")
            if tag_created:
                try:
                    durable_update_ref(
                        self.config.trusted_repo,
                        f"refs/tags/{manifest['sourceTag']}",
                        None,
                        manifest["sourceTagObject"],
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(f"tag={rollback_exc}")
            if package_installed:
                try:
                    shutil.rmtree(target_package)
                except Exception as rollback_exc:
                    rollback_errors.append(f"package={rollback_exc}")
            if target_installed:
                try:
                    shutil.rmtree(target_version)
                except Exception as rollback_exc:
                    rollback_errors.append(f"version={rollback_exc}")
            shutil.rmtree(stage, ignore_errors=True)
            try:
                self.fsync_release_directories()
            except Exception as rollback_exc:
                rollback_errors.append(f"target-fsync={rollback_exc}")
            try:
                if (
                    self.current_release() != parent
                    or self.config.baseline.read_bytes() != checked["baselineRaw"]
                    or tree_hash(self.config.versions / parent) != checked["productionTreeSha256"]
                    or target_version.exists() or target_package.exists() or receipt.exists()
                    or bool(tag_commit(self.config.trusted_repo, manifest["sourceTag"], optional=True))
                ):
                    rollback_errors.append("前后 current/baseline/production tree 或目标闭包不一致")
            except Exception as rollback_exc:
                rollback_errors.append(f"rollback-verify={rollback_exc}")
            if journal_created and not rollback_errors:
                try:
                    self.clear_journal()
                    journal_created = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"journal={rollback_exc}")
            if rollback_errors:
                raise LineageError(
                    "ROLLBACK_INCOMPLETE",
                    "发布失败且生产零变化校验失败：" + "；".join(rollback_errors) + f"；原始错误={exc}",
                ) from exc
            if isinstance(exc, LineageError):
                raise
            raise LineageError("PROMOTION_FAILED_ROLLED_BACK", f"发布失败，已恢复生产零变化：{exc}") from exc


BOOTSTRAP_TARGETS = (
    "versions", "packages", "receipts", "staging", "trusted.git",
    "production-baseline.json", ".release-lineage.lock", "current",
)


def _remove_bootstrap_owned(path: Path, *, expected: str) -> None:
    metadata = path.lstat()
    if metadata.st_uid != os.geteuid():
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap 目标非当前用户所有：{path}")
    if expected == "symlink":
        if not stat.S_ISLNK(metadata.st_mode):
            raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap current 类型无效：{path}")
        path.unlink()
    elif expected == "file":
        if not stat.S_ISREG(metadata.st_mode):
            raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap 文件类型无效：{path}")
        path.unlink()
    else:
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap 目录类型无效：{path}")
        shutil.rmtree(path)


def _clear_bootstrap_orphan_stages(root: Path) -> None:
    for path in list(root.iterdir()):
        if not re.fullmatch(r"\.bootstrap-stage-[1-9][0-9]*-[0-9a-f]{12}", path.name):
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700 or path.is_symlink()
        ):
            raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap orphan staging 类型无效：{path}")
        shutil.rmtree(path)
    ReleaseLineageGate.fsync_directory(root)


def _clear_bootstrap_journal_temporaries(root: Path) -> None:
    changed = False
    for path in list(root.iterdir()):
        if not re.fullmatch(r"\.\.bootstrap-transaction\.json\.[A-Za-z0-9_]{8}\.tmp", path.name):
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or path.is_symlink()
        ):
            raise LineageError(
                "BOOTSTRAP_RECOVERY_INCOMPLETE",
                f"bootstrap journal 临时文件类型或权限无效：{path}",
            )
        path.unlink()
        changed = True
    if changed:
        ReleaseLineageGate.fsync_directory(root)


def recover_bootstrap_install(
    config: HostConfig,
    health_runner: Callable[[str, Path], Mapping[str, Any] | None],
) -> dict[str, Any] | None:
    root = config.root
    journal_path = root / ".bootstrap-transaction.json"
    if not journal_path.exists():
        return None
    journal = load_json_path(journal_path, "BOOTSTRAP_RECOVERY_INCOMPLETE", "bootstrap journal")
    required = {
        "schema", "stageName", "release", "sourceTag", "sourceTagObject", "sourceCommit",
        "taskId", "packageSha256", "productionTreeSha256", "baselineSha256", "createdAt",
    }
    if set(journal) != required or journal.get("schema") != BOOTSTRAP_JOURNAL_SCHEMA:
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", "bootstrap journal schema 或字段闭包无效")
    stage_name = str(journal.get("stageName") or "")
    if not re.fullmatch(r"\.bootstrap-stage-[1-9][0-9]*-[0-9a-f]{12}", stage_name):
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", "bootstrap journal staging 名称无效")
    for key in ("release", "sourceTag"):
        require_name(journal.get(key), "BOOTSTRAP_RECOVERY_INCOMPLETE", key)
    require_commit(journal.get("sourceTagObject"), "BOOTSTRAP_RECOVERY_INCOMPLETE", "sourceTagObject")
    require_commit(journal.get("sourceCommit"), "BOOTSTRAP_RECOVERY_INCOMPLETE", "sourceCommit")
    if not TASK_RE.fullmatch(str(journal.get("taskId") or "")):
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", "bootstrap journal taskId 无效")
    for key in ("packageSha256", "productionTreeSha256", "baselineSha256"):
        require_digest(journal.get(key), "BOOTSTRAP_RECOVERY_INCOMPLETE", key)
    stage = root / stage_name
    allowed = set(BOOTSTRAP_TARGETS) | {journal_path.name, stage_name}
    unexpected = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if unexpected:
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", f"bootstrap root 含非事务路径：{unexpected}")
    if (root / "current").is_symlink():
        if stage.exists():
            _remove_bootstrap_owned(stage, expected="dir")
            ReleaseLineageGate.fsync_directory(root)
        status = ReleaseLineageGate(config, health_runner=health_runner).status(
            allow_bootstrap_journal=True,
        )
        if (
            status["release"] != journal["release"]
            or status["sourceTag"] != journal["sourceTag"]
            or status["sourceTagObject"] != journal["sourceTagObject"]
            or status["sourceCommit"] != journal["sourceCommit"]
            or status["packageSha256"] != journal["packageSha256"]
            or status["productionTreeSha256"] != journal["productionTreeSha256"]
            or status["baselineSha256"] != journal["baselineSha256"]
        ):
            raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", "已越过 bootstrap 提交点但闭包不匹配")
        journal_path.unlink()
        ReleaseLineageGate.fsync_directory(root)
        return {"action": "finalized-committed", "status": status}
    expected_types = {
        "production-baseline.json": "file",
        ".release-lineage.lock": "file",
        "current": "symlink",
    }
    for name in reversed(BOOTSTRAP_TARGETS):
        target = root / name
        if target.exists() or target.is_symlink():
            _remove_bootstrap_owned(target, expected=expected_types.get(name, "dir"))
    if stage.exists():
        _remove_bootstrap_owned(stage, expected="dir")
    journal_path.unlink()
    ReleaseLineageGate.fsync_directory(root)
    if list(root.iterdir()):
        raise LineageError("BOOTSTRAP_RECOVERY_INCOMPLETE", "bootstrap 回滚后 root 非空")
    return {"action": "rolled-back"}


def bootstrap_host(*, config: HostConfig, repo: Path, artifact: Path, task_id: str,
                   failpoint: Callable[[str], None] | None = None,
                   health_runner: Callable[[str, Path], Mapping[str, Any] | None] | None = None) -> dict[str, Any]:
    fail = failpoint or (lambda _name: None)
    health_check = health_runner or core_health_runner
    root = config.root
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise LineageError("BOOTSTRAP_ROOT_INVALID", f"bootstrap root 不可读：{root}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700 or root.is_symlink()
    ):
        raise LineageError("BOOTSTRAP_ROOT_INVALID", "bootstrap root 必须是当前用户拥有的 0700 非符号链接目录")
    if config.trusted_repo != root / "trusted.git":
        raise LineageError("BOOTSTRAP_ROOT_INVALID", "首版 trusted repo 必须固定为 host-root/trusted.git")
    if not TASK_RE.fullmatch(task_id):
        raise LineageError("TASK_ID_INVALID", "taskId 无效")
    package = artifact.expanduser().absolute()
    if package.is_symlink() or not package.is_file():
        raise LineageError("ARTIFACT_INVALID", "bootstrap artifact 必须是非符号链接普通文件")
    try:
        package.resolve().relative_to(root)
    except ValueError:
        pass
    else:
        raise LineageError("BOOTSTRAP_ROOT_INVALID", "bootstrap artifact 必须位于 host root 之外")
    root_descriptor = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    stage: Path | None = None
    recovered: dict[str, Any] | None = None
    try:
        fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        _clear_bootstrap_journal_temporaries(root)
        recovered = recover_bootstrap_install(config, health_check)
        _clear_bootstrap_orphan_stages(root)
        source = repository_state(repo)
        if not source["clean"]:
            raise LineageError("DIRTY_WORKTREE", f"bootstrap 拒绝脏工作树：{source['dirtyEntries'][:10]}")
        if list(root.iterdir()):
            with tempfile.TemporaryDirectory(prefix="xirang-bootstrap-idempotency-") as raw_tmp:
                frozen = Path(raw_tmp) / "artifact.zip"
                freeze_regular_file(package, frozen)
                payload = artifact_payload(frozen)
                release_manifest = require_mapping(
                    payload["release"], "BOOTSTRAP_MANIFEST_INVALID", "release.json",
                )
                lineage = require_mapping(
                    release_manifest.get("lineage"), "BOOTSTRAP_MANIFEST_INVALID", "lineage",
                )
                source_tag = require_name(
                    lineage.get("sourceTag"), "BOOTSTRAP_MANIFEST_INVALID", "sourceTag",
                )
                source_commit = require_commit(
                    lineage.get("sourceCommit"), "BOOTSTRAP_MANIFEST_INVALID", "sourceCommit",
                )
                verify_bootstrap_artifact(source["root"], source_tag, source_commit, payload, task_id)
                package_sha = sha256_path(frozen)
            status = ReleaseLineageGate(config, health_runner=health_check).status()
            if (
                status["sourceTag"] != source_tag or status["sourceCommit"] != source_commit
                or status["sourceTagObject"] != lineage.get("sourceTagObject")
                or status["packageSha256"] != package_sha
            ):
                raise LineageError("BOOTSTRAP_CONFLICT", "bootstrap root 已绑定不同 Tag/Commit/package")
            return {**status, "status": "already-active", "idempotent": True, "recovery": recovered}

        stage = root / f".bootstrap-stage-{os.getpid()}-{sha256_bytes(os.urandom(16))[:12]}"
        stage.mkdir(mode=0o700)
        frozen = stage / ".artifact.zip"
        freeze_regular_file(package, frozen)
        payload = artifact_payload(frozen)
        release_manifest = require_mapping(payload["release"], "BOOTSTRAP_MANIFEST_INVALID", "release.json")
        lineage = require_mapping(release_manifest.get("lineage"), "BOOTSTRAP_MANIFEST_INVALID", "lineage")
        source_tag = require_name(lineage.get("sourceTag"), "BOOTSTRAP_MANIFEST_INVALID", "sourceTag")
        source_commit = require_commit(lineage.get("sourceCommit"), "BOOTSTRAP_MANIFEST_INVALID", "sourceCommit")
        source_tag_object = require_commit(
            lineage.get("sourceTagObject"), "BOOTSTRAP_MANIFEST_INVALID", "sourceTagObject",
        )
        if source["head"] != source_commit:
            raise LineageError("TAG_COMMIT_MISMATCH", "bootstrap sourceCommit 不是干净工作树 HEAD")
        verified = verify_bootstrap_artifact(source["root"], source_tag, source_commit, payload, task_id)
        package_sha = sha256_path(frozen)
        versions_dir = stage / "versions"
        packages_dir = stage / "packages"
        version = versions_dir / verified["release"]
        package_dir = packages_dir / source_tag
        receipt_dir = stage / "receipts"
        staging_dir = stage / "staging"
        for directory in (versions_dir, packages_dir, receipt_dir, staging_dir):
            directory.mkdir(mode=0o700)
        package_dir.mkdir(mode=0o700)
        artifact_payload(frozen, extract_to=version)
        installed_package = package_dir / "artifact.zip"
        shutil.copyfile(frozen, installed_package)
        os.chmod(installed_package, 0o600)
        trusted = stage / "trusted.git"
        git_bytes(stage, ["init", "--bare", str(trusted)])
        git_bytes(
            trusted,
            ["fetch", "--no-tags", str(source["root"]),
             f"refs/tags/{source_tag}:refs/tags/{source_tag}"],
        )
        require_annotated_tag(trusted, source_tag, source_commit)
        if tag_object(trusted, source_tag) != source_tag_object:
            raise LineageError("TAG_REBIND", "bootstrap trusted Tag object 与 artifact 不一致")
        production_sha = tree_hash(version)
        health = dict(health_check(verified["release"], version) or {})
        if tree_hash(version) != production_sha:
            raise LineageError("HEALTH_MUTATED_ARTIFACT", "静态健康检查改变了 bootstrap 生产树")
        now = utc_now()
        baseline_value = {
            "schema": BASELINE_SCHEMA, "revision": 1,
            "status": "deployed-pending-acceptance",
            "current": {
                "release": verified["release"],
                "artifact": f"packages/{source_tag}/artifact.zip",
                "artifactSha256": package_sha, "packageSha256": package_sha,
                "releaseManifestSha256": sha256_bytes(payload["releaseRaw"]),
                "sha256SumsSha256": sha256_bytes(payload["sumsRaw"]),
                "productionTreeSha256": production_sha,
                "sourceTag": source_tag, "sourceTagObject": source_tag_object,
                "sourceCommit": source_commit, "taskId": task_id,
                "baseRelease": verified["release"], "baseTag": source_tag,
                "baseTagObject": source_tag_object, "baseCommit": source_commit,
                "dirtyWorktree": False, "bootstrappedAt": now,
            },
            "acceptance": {"status": "pending", "acceptedBy": None, "acceptedAt": None},
        }
        baseline_raw = json.dumps(
            baseline_value, ensure_ascii=False, indent=2, sort_keys=True,
        ).encode("utf-8") + b"\n"
        baseline_path = stage / "production-baseline.json"
        write_new_file(baseline_path, baseline_raw)
        receipt_value = {
            "schema": BOOTSTRAP_RECEIPT_SCHEMA,
            "status": "deployed-pending-user-acceptance", "release": verified["release"],
            "sourceTag": source_tag, "sourceTagObject": source_tag_object,
            "sourceCommit": source_commit, "baseTag": source_tag,
            "baseTagObject": source_tag_object, "baseCommit": source_commit,
            "dirtyWorktree": False, "taskId": task_id, "baselineAfterRevision": 1,
            "baselineAfterSha256": sha256_bytes(baseline_raw), "packageSha256": package_sha,
            "productionTreeSha256": production_sha, "health": health, "at": now,
        }
        receipt_path = receipt_dir / f"bootstrap-{source_tag}.json"
        write_new_file(
            receipt_path,
            json.dumps(receipt_value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        lock_path = stage / ".release-lineage.lock"
        write_new_file(lock_path, b"")
        os.symlink(f"versions/{verified['release']}", stage / "current")
        for target in (version, package_dir, receipt_dir, staging_dir, trusted):
            fsync_tree(target)
        for target in (baseline_path, lock_path):
            fsync_file(target)
        frozen.unlink()
        ReleaseLineageGate.fsync_directory(stage)
        source_final = repository_state(source["root"])
        if (
            not source_final["clean"] or source_final["head"] != source_commit
            or tag_commit(source["root"], source_tag, optional=True) != source_commit
            or tag_object(source["root"], source_tag, optional=True) != source_tag_object
        ):
            raise LineageError("SOURCE_CHANGED_DURING_BUILD", "bootstrap staging 期间源码或 Tag 发生变化")
        require_annotated_tag(source["root"], source_tag, source_commit)
        journal_value = {
            "schema": BOOTSTRAP_JOURNAL_SCHEMA, "stageName": stage.name,
            "release": verified["release"], "sourceTag": source_tag,
            "sourceTagObject": source_tag_object, "sourceCommit": source_commit,
            "taskId": task_id, "packageSha256": package_sha,
            "productionTreeSha256": production_sha,
            "baselineSha256": sha256_bytes(baseline_raw), "createdAt": now,
        }
        journal_path = root / ".bootstrap-transaction.json"
        write_atomic_new_file(
            journal_path,
            json.dumps(journal_value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n",
        )
        ReleaseLineageGate.fsync_directory(root)
        fail("before_publish")
        for name in BOOTSTRAP_TARGETS:
            os.replace(stage / name, root / name)
            ReleaseLineageGate.fsync_directory(root)
            fail(f"after_publish_{name}")
        stage.rmdir()
        ReleaseLineageGate.fsync_directory(root)
        status = ReleaseLineageGate(config, health_runner=health_check).status(
            allow_bootstrap_journal=True,
        )
        if status["packageSha256"] != package_sha or status["productionTreeSha256"] != production_sha:
            raise LineageError("BOOTSTRAP_VERIFY_FAILED", "bootstrap 后 package/tree 校验漂移")
        journal_path.unlink()
        ReleaseLineageGate.fsync_directory(root)
        return {
            **status, "status": "deployed-pending-user-acceptance", "idempotent": False,
            "recovery": recovered,
        }
    except BaseException as exc:
        try:
            recovery = recover_bootstrap_install(config, health_check)
            if recovery and recovery["action"] == "finalized-committed":
                return {
                    **recovery["status"], "status": "deployed-pending-user-acceptance",
                    "idempotent": False, "recovery": recovery,
                }
            if stage is not None and stage.exists():
                _clear_bootstrap_orphan_stages(root)
            if list(root.iterdir()):
                raise LineageError("BOOTSTRAP_ROLLBACK_INCOMPLETE", "bootstrap 失败后 root 非空")
        except Exception as recovery_exc:
            raise LineageError(
                "BOOTSTRAP_ROLLBACK_INCOMPLETE", f"bootstrap 失败且恢复不完整：{recovery_exc}",
            ) from exc
        if isinstance(exc, LineageError):
            raise
        raise LineageError("BOOTSTRAP_FAILED_ROLLED_BACK", f"bootstrap 失败，已恢复零变化：{exc}") from exc
    finally:
        os.close(root_descriptor)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    package = sub.add_parser("package", help="build the deterministic first Xirang-core artifact from an annotated Tag")
    package.add_argument("--repo", required=True, type=Path)
    package.add_argument("--source-tag", required=True)
    package.add_argument("--task-id", required=True)
    package.add_argument("--output", required=True, type=Path)
    bootstrap = sub.add_parser("bootstrap", help="atomically create the initial protected local release baseline")
    bootstrap.add_argument("--host-root", required=True, type=Path)
    bootstrap.add_argument("--trusted-repo", required=True, type=Path)
    bootstrap.add_argument("--repo", required=True, type=Path)
    bootstrap.add_argument("--artifact", required=True, type=Path)
    bootstrap.add_argument("--task-id", required=True)
    start = sub.add_parser("start", help="capture the production-tag baseline before release work")
    start.add_argument("--repo", required=True, type=Path)
    start.add_argument("--baseline", required=True, type=Path)
    start.add_argument("--task-id", required=True)
    start.add_argument("--output", required=True, type=Path)
    build = sub.add_parser("build", help="build a self-contained untrusted release envelope")
    build.add_argument("--repo", required=True, type=Path)
    build.add_argument("--baseline", required=True, type=Path)
    build.add_argument("--task-lock", required=True, type=Path)
    build.add_argument("--source-tag", required=True)
    build.add_argument("--artifact", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    for name in ("check", "promote"):
        command = sub.add_parser(name, help=f"host-side independent {name}")
        command.add_argument("--host-root", required=True, type=Path)
        command.add_argument("--trusted-repo", required=True, type=Path)
        command.add_argument("--envelope", required=True, type=Path)
    status = sub.add_parser("status", help="independently verify the active protected baseline")
    status.add_argument("--host-root", required=True, type=Path)
    status.add_argument("--trusted-repo", required=True, type=Path)
    recover = sub.add_parser("recover", help="recover or finalize a journaled host transaction")
    recover.add_argument("--host-root", required=True, type=Path)
    recover.add_argument("--trusted-repo", required=True, type=Path)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "package":
            result = build_bootstrap_artifact(
                repo=args.repo, source_tag=args.source_tag, task_id=args.task_id,
                output=args.output,
            )
        elif args.command == "bootstrap":
            result = bootstrap_host(
                config=HostConfig(args.host_root, args.trusted_repo), repo=args.repo,
                artifact=args.artifact, task_id=args.task_id,
            )
        elif args.command == "start":
            result = start_task_lock(args.repo, args.baseline, args.task_id, args.output)
        elif args.command == "build":
            result = build_envelope(
                repo=args.repo, baseline_path=args.baseline, task_lock_path=args.task_lock,
                source_tag=args.source_tag, artifact=args.artifact, output=args.output,
            )
        elif args.command in {"check", "promote"}:
            gate = ReleaseLineageGate(
                HostConfig(args.host_root, args.trusted_repo), health_runner=core_health_runner,
            )
            result = gate.promote(args.envelope, check_only=args.command == "check")
        elif args.command == "status":
            gate = ReleaseLineageGate(
                HostConfig(args.host_root, args.trusted_repo), health_runner=core_health_runner,
            )
            result = gate.status()
        else:
            gate = ReleaseLineageGate(
                HostConfig(args.host_root, args.trusted_repo), health_runner=core_health_runner,
            )
            result = gate.recover()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except LineageError as exc:
        print(json.dumps({"ok": False, "errorCode": exc.code, "message": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "errorCode": "INTERNAL_ERROR", "message": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
