#!/usr/bin/env python3
"""Receipt-gated, exact-path Git delivery for Xirang V3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from xirang_state import SCHEMA_VERSION, StateConflict, StateStore, refresh_events_projection
from xirang_state_backup import verify_file_preimage
from xirang_state_migrate import require_active
from xirang_task_projection import write_task_card_projection
from xirang_recovery_roots import RecoveryRootError, load_registry, require_registered


FORBIDDEN_PATH_TOKENS = {".", "./", "-A", "--all", "add .", "add -A"}
WILDCARD_RE = re.compile(r"[*?\[\]{}]")
SAFE_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TAG_PAYLOAD_KIND = "xirang_controlled_delivery_manifest"
TAG_PAYLOAD_SCHEMA_VERSION = 1


class DeliveryError(RuntimeError):
    """The requested delivery violates the receipt or Git safety contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(path: str, root: str) -> bool:
    clean_root = root.rstrip("/") or "."
    return clean_root == "." or path == clean_root or path.startswith(clean_root + "/")


class ControlledDelivery:
    """Create classified commits from the exact effective receipt set."""

    def __init__(self, store: StateStore | str | Path, repo: str | Path):
        self.store = store if isinstance(store, StateStore) else StateStore(store)
        if not self.store.path.is_file():
            raise DeliveryError("controlled delivery requires an existing StateStore")
        try:
            with self.store.connect(readonly=True) as connection:
                version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if int(version or 0) != SCHEMA_VERSION:
                raise DeliveryError(
                    f"controlled delivery requires schema {SCHEMA_VERSION}; maintenance migration is separate"
                )
            require_active(self.store, expected_database=self.store.path.resolve())
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(f"controlled delivery StateStore authority probe failed: {exc}") from exc
        self.repo = Path(repo).resolve()
        if not (self.repo / ".git").exists():
            raise DeliveryError(f"not a Git repository: {self.repo}")

    def _git(self, *args: str, check: bool = True) -> str:
        if not args or args[0] not in {"rev-parse", "status", "diff", "diff-tree", "add", "commit", "tag", "cat-file", "ls-files"}:
            raise DeliveryError("Git command is outside the delivery allowlist")
        forbidden = {"amend", "--amend", "reset", "clean", "push", "checkout", "restore", "--hard", "-A", "--all"}
        if any(token in forbidden for token in args):
            raise DeliveryError("destructive or broad Git command rejected")
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and proc.returncode != 0:
            raise DeliveryError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
        return proc.stdout

    def _commit_blob_sha256(self, commit: str, path: str) -> str:
        tree = subprocess.run(
            ["git", "ls-tree", "-z", commit, "--", path], cwd=self.repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if tree.returncode != 0 or not tree.stdout:
            raise DeliveryError(f"delivery commit does not contain path: {path}")
        entry = tree.stdout.rstrip(b"\0").split(b"\0")
        if len(entry) != 1 or b"\t" not in entry[0]:
            raise DeliveryError(f"ambiguous delivery tree entry: {path}")
        metadata, stored_path = entry[0].split(b"\t", 1)
        parts = metadata.split()
        if len(parts) != 3 or parts[1] != b"blob" or stored_path.decode("utf-8") != path:
            raise DeliveryError(f"delivery tree entry is not an exact blob: {path}")
        blob = subprocess.run(
            ["git", "cat-file", "blob", parts[2].decode("ascii")], cwd=self.repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if blob.returncode != 0:
            raise DeliveryError(f"cannot read delivery blob: {path}")
        return hashlib.sha256(blob.stdout).hexdigest()

    def _commit_path_sha256(self, commit: str, path: str) -> str | None:
        """Return the exact blob digest, or None when the path is absent."""
        present = subprocess.run(
            ["git", "ls-tree", "-z", commit, "--", path], cwd=self.repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if present.returncode != 0:
            raise DeliveryError(f"cannot inspect delivery tree path: {path}")
        return self._commit_blob_sha256(commit, path) if present.stdout else None

    @staticmethod
    def _normalize_paths(paths: Sequence[str], *, allow_empty: bool = False) -> list[str]:
        if not paths and not allow_empty:
            raise DeliveryError("at least one exact path is required")
        result: list[str] = []
        for raw in paths:
            value = str(raw).strip().replace(os.sep, "/")
            if value in FORBIDDEN_PATH_TOKENS or value.startswith("-"):
                raise DeliveryError(f"broad or option-like path rejected: {raw}")
            if WILDCARD_RE.search(value):
                raise DeliveryError(f"wildcard path rejected: {raw}")
            pure = PurePosixPath(value)
            if pure.is_absolute() or not value or ".." in pure.parts:
                raise DeliveryError(f"path must be repository-relative without traversal: {raw}")
            normalized = pure.as_posix()
            if normalized == ".":
                raise DeliveryError("repository-wide delivery is forbidden")
            result.append(normalized)
        if len(result) != len(set(result)):
            raise DeliveryError("duplicate delivery path")
        return result

    @staticmethod
    def _normalize_evidence_only_paths(paths: Sequence[str]) -> list[str]:
        """Normalize exact non-Git runtime paths without widening delivery authority."""
        result: list[str] = []
        for raw in paths:
            value = str(raw).strip().replace(os.sep, "/")
            if value in FORBIDDEN_PATH_TOKENS or value.startswith("-"):
                raise DeliveryError(f"broad or option-like evidence path rejected: {raw}")
            if WILDCARD_RE.search(value) or not value:
                raise DeliveryError(f"wildcard or empty evidence path rejected: {raw}")
            expanded = Path(value).expanduser()
            if expanded.is_absolute():
                normalized = str(expanded.resolve(strict=False))
            else:
                pure = PurePosixPath(value)
                if ".." in pure.parts or pure.as_posix() == ".":
                    raise DeliveryError(f"evidence path contains traversal: {raw}")
                normalized = pure.as_posix()
            result.append(normalized)
        if len(result) != len(set(result)):
            raise DeliveryError("duplicate evidence-only delivery path")
        return result

    def _evidence_path(self, path: str) -> Path:
        raw = Path(path).expanduser()
        return raw.resolve(strict=False) if raw.is_absolute() else (self.repo / raw).resolve(strict=False)

    def _repository_evidence_relative(self, path: str) -> str | None:
        raw = Path(path).expanduser()
        lexical = raw if raw.is_absolute() else self.repo / raw
        if lexical.is_symlink():
            raise DeliveryError(f"evidence-only path cannot be a symlink: {path}")
        absolute = lexical.resolve(strict=False)
        try:
            return absolute.relative_to(self.repo).as_posix()
        except ValueError:
            return None

    def _require_evidence_only_path_kind(self, path: str) -> None:
        """Repository evidence must be ignored and untracked; external files stay exact."""
        relative = self._repository_evidence_relative(path)
        if relative is None:
            return
        if self._is_git_tracked(relative):
            raise DeliveryError(f"tracked repository path cannot be evidence-only: {path}")
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "--", relative],
            cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        if ignored.returncode == 1:
            raise DeliveryError(f"repository evidence-only path must be Git-ignored: {path}")
        if ignored.returncode != 0:
            raise DeliveryError(
                ignored.stderr.strip() or f"cannot determine Git-ignore state: {path}"
            )

    def _is_git_tracked(self, path: str) -> bool:
        """Use Git's status code so quoted/non-ASCII output cannot change truth."""
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _requested_path_modes(
        exact_paths: Sequence[str], evidence_paths: Sequence[str], *, exact_mode: str = "git",
    ) -> dict[str, str]:
        if exact_mode not in {"git", "no_git"}:
            raise DeliveryError(f"invalid exact delivery path mode: {exact_mode}")
        return {
            **{path: exact_mode for path in exact_paths},
            **{path: "evidence_only" for path in evidence_paths},
        }

    @staticmethod
    def _manifest_path_modes(manifest: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        modes: dict[str, str] = {}
        for item in manifest:
            path = str(item.get("path") or "")
            mode = str(item.get("delivery_mode") or "")
            if not path or mode not in {"git", "no_git", "evidence_only"} or path in modes:
                raise DeliveryError("delivery manifest has missing, invalid, or duplicate path mode")
            if (mode == "evidence_only") != bool(item.get("evidence_only")):
                raise DeliveryError(f"delivery manifest path mode conflicts with evidence flag: {path}")
            if mode == "no_git" and bool(item.get("git_effect")):
                raise DeliveryError(f"no-Git manifest path cannot claim a Git effect: {path}")
            modes[path] = mode
        return modes

    @staticmethod
    def _no_git_manifest_digest(manifest: Sequence[Mapping[str, Any]]) -> str:
        core = [
            {key: value for key, value in item.items() if key != "no_git_manifest_sha256"}
            for item in manifest
        ]
        return hashlib.sha256(_json(core).encode("utf-8")).hexdigest()

    def _verify_no_git_manifest_item(self, item: Mapping[str, Any]) -> None:
        path = str(item.get("path") or "")
        if (
            item.get("delivery_mode") != "no_git"
            or item.get("git_effect") is not False
            or item.get("evidence_only") is not False
            or item.get("git_recovery_available") is not False
            or Path(path).is_absolute()
            or not self._is_git_tracked(path)
        ):
            raise DeliveryError(f"invalid tracked files_no_git manifest item: {path}")
        absolute = self.repo / path
        exists_after = bool(item.get("exists_after"))
        if exists_after:
            if not absolute.is_file() or absolute.is_symlink() or _sha256(absolute) != item.get("sha256"):
                raise DeliveryError(f"files_no_git path drifted: {path}")
        elif absolute.exists() or absolute.is_symlink() or item.get("sha256") is not None:
            raise DeliveryError(f"files_no_git deletion drifted: {path}")
        recovery_path = Path(str(item.get("recovery_manifest") or "")).expanduser().resolve()
        try:
            raw = recovery_path.read_bytes()
            recovery = verify_file_preimage(recovery_path)
        except Exception as exc:
            raise DeliveryError(f"files_no_git pre-image drifted: {path}: {exc}") from exc
        if (
            recovery.get("logical_path") != path
            or recovery.get("sha256") != item.get("preimage_sha256")
            or hashlib.sha256(raw).hexdigest() != item.get("recovery_manifest_sha256")
        ):
            raise DeliveryError(f"files_no_git pre-image binding mismatch: {path}")

    def _verify_no_git_manifest(self, manifest: Sequence[Mapping[str, Any]]) -> str:
        if not manifest:
            raise DeliveryError("files_no_git requires a non-empty immutable manifest")
        digest = self._no_git_manifest_digest(manifest)
        for item in manifest:
            if item.get("no_git_manifest_sha256") != digest:
                raise DeliveryError("files_no_git immutable manifest digest mismatch")
            self._verify_no_git_manifest_item(item)
        return digest

    @staticmethod
    def _tag_payload(
        *, delivery_id: str, task_id: str, commit: str, tree: str,
        manifest: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "kind": TAG_PAYLOAD_KIND,
            "schema_version": TAG_PAYLOAD_SCHEMA_VERSION,
            "delivery_id": delivery_id,
            "task_id": task_id,
            "implementation_commit": commit,
            "implementation_tree": tree,
            "manifest": list(manifest),
        }

    def _read_tag_payload(self, tag_object: str) -> dict[str, Any]:
        raw = self._git("cat-file", "-p", tag_object)
        _, separator, body = raw.partition("\n\n")
        if not separator:
            raise DeliveryError("annotated delivery tag has no immutable manifest payload")
        body = body.rstrip("\n")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DeliveryError("annotated delivery tag manifest payload is invalid") from exc
        if not isinstance(payload, dict) or _json(payload) != body:
            raise DeliveryError("annotated delivery tag manifest payload is not canonical")
        if (
            payload.get("kind") != TAG_PAYLOAD_KIND
            or payload.get("schema_version") != TAG_PAYLOAD_SCHEMA_VERSION
            or not isinstance(payload.get("manifest"), list)
        ):
            raise DeliveryError("annotated delivery tag manifest contract is invalid")
        self._manifest_path_modes(payload["manifest"])
        return payload

    def _verify_recovery_binding(self, item: Mapping[str, Any]) -> None:
        """Every non-Git deletion remains bound to a live content-addressed pre-image."""
        if bool(item.get("exists_after")):
            return
        recovery_path = item.get("recovery_manifest")
        requires_preimage = not bool(item.get("git_effect"))
        if not recovery_path:
            if requires_preimage:
                raise DeliveryError(f"non-Git deletion lost recoverable pre-image: {item.get('path')}")
            return
        try:
            recovery = verify_file_preimage(Path(str(recovery_path)))
        except Exception as exc:
            raise DeliveryError(f"delivery recovery pre-image drifted: {item.get('path')}: {exc}") from exc
        if (
            recovery.get("logical_path") != item.get("path")
            or recovery.get("manifest") != str(Path(str(recovery_path)).expanduser().resolve())
            or recovery.get("sha256") != item.get("preimage_sha256")
        ):
            raise DeliveryError(f"delivery recovery binding mismatch: {item.get('path')}")

    def _receipt_evidence_binding(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        kind = receipt.get("evidence_kind")
        if not kind:
            return {}
        if kind != "takeover_reconciliation":
            raise DeliveryError(f"unknown effective receipt evidence kind: {kind}")
        required = {
            "reconciliation_id", "reconciliation_manifest",
            "reconciliation_manifest_sha256", "reconciliation_record_event_id",
            "reconciliation_confirmation_event_id", "reviewer_agent_id",
            "authorization_basis_task_id", "historical_source_task_id",
            "historical_source_verified",
        }
        if any(field not in receipt for field in required) or any(
            not receipt.get(field) for field in required - {
                "historical_source_task_id", "historical_source_verified",
            }
        ):
            raise DeliveryError("takeover reconciliation receipt is missing an immutable evidence binding")
        manifest_path = Path(str(receipt["reconciliation_manifest"])).expanduser().resolve()
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise DeliveryError("takeover reconciliation manifest is unavailable")
        manifest_sha = _sha256(manifest_path)
        if manifest_sha != receipt["reconciliation_manifest_sha256"]:
            raise DeliveryError("takeover reconciliation manifest drifted")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeliveryError("takeover reconciliation manifest is unreadable") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") not in {1, 2}
            or manifest.get("artifact_type") != "takeover_reconciliation"
            or manifest.get("reconciliation_id") != receipt["reconciliation_id"]
            or manifest.get("task_id") != receipt["task_id"]
            or manifest.get("original_actor_verified") is not False
            or manifest.get("original_write_times_known") is not False
        ):
            raise DeliveryError("takeover reconciliation manifest contract is invalid")
        matching = [
            item for item in manifest.get("items", [])
            if isinstance(item, dict) and item.get("path") == receipt.get("path")
        ]
        if len(matching) != 1:
            raise DeliveryError("takeover reconciliation path binding is missing or ambiguous")
        item = matching[0]
        if manifest.get("schema_version") == 1:
            item_authorization_basis = manifest.get("task_id")
            item_historical_source = item.get("source_task_id")
            item_historical_verified = False
        else:
            item_authorization_basis = item.get("authorization_basis_task_id")
            item_historical_source = item.get("historical_source_task_id")
            item_historical_verified = item.get("historical_source_verified")
        if (
            item.get("operation") != receipt.get("operation")
            or bool(item.get("exists_after")) != bool(receipt.get("exists_after"))
            or item.get("sha256") != receipt.get("sha256")
            or item_authorization_basis != receipt.get("authorization_basis_task_id")
            or item_historical_source != receipt.get("historical_source_task_id")
            or bool(item_historical_verified) != bool(receipt.get("historical_source_verified"))
        ):
            raise DeliveryError("takeover reconciliation receipt differs from its manifest item")
        return {
            "evidence_kind": kind,
            "reconciliation_id": receipt["reconciliation_id"],
            "reconciliation_manifest": str(manifest_path),
            "reconciliation_manifest_sha256": manifest_sha,
            "reconciliation_record_event_id": receipt["reconciliation_record_event_id"],
            "reconciliation_confirmation_event_id": receipt["reconciliation_confirmation_event_id"],
            "reconciliation_reviewer_agent_id": receipt["reviewer_agent_id"],
            "reconciliation_authorization_basis_task_id": receipt["authorization_basis_task_id"],
            "reconciliation_historical_source_task_id": receipt["historical_source_task_id"],
            "reconciliation_historical_source_verified": bool(receipt["historical_source_verified"]),
            "original_actor_verified": False,
            "original_write_times_known": False,
        }

    def _verify_manifest_against_effective_receipt(
        self,
        item: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> None:
        binding = self._receipt_evidence_binding(receipt)
        if (
            item.get("receipt_id") != receipt.get("receipt_id")
            or item.get("sha256") != receipt.get("sha256")
            or bool(item.get("exists_after")) != bool(receipt.get("exists_after"))
            or any(item.get(key) != value for key, value in binding.items())
        ):
            raise DeliveryError(f"delivery manifest differs from effective evidence: {item.get('path')}")

    def _verify_evidence_manifest_item(self, item: Mapping[str, Any]) -> None:
        path = str(item.get("path") or "")
        self._require_evidence_only_path_kind(path)
        absolute = self._evidence_path(path)
        exists_after = bool(item.get("exists_after"))
        if exists_after:
            if not absolute.is_file() or absolute.is_symlink():
                raise DeliveryError(f"evidence-only path is no longer a regular file: {path}")
            if _sha256(absolute) != item.get("sha256"):
                raise DeliveryError(f"evidence-only path drifted: {path}")
        elif absolute.exists() or absolute.is_symlink():
            raise DeliveryError(f"evidence-only deletion drifted: {path}")
        self._verify_recovery_binding(item)

    def _verify_non_git_manifest_item(self, item: Mapping[str, Any]) -> None:
        if item.get("delivery_mode") == "evidence_only":
            self._verify_evidence_manifest_item(item)
            return
        if item.get("delivery_mode") == "no_git":
            self._verify_no_git_manifest_item(item)
            return
        path = str(item.get("path") or "")
        if item.get("delivery_mode") != "git" or bool(item.get("exists_after")):
            raise DeliveryError(f"invalid non-Git delivery manifest item: {path}")
        absolute = self.repo / path
        if absolute.exists() or absolute.is_symlink():
            raise DeliveryError(f"non-Git deletion drifted: {path}")
        self._verify_recovery_binding(item)

    def _dirty_paths(self) -> set[str]:
        output = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        chunks = output.split("\0")
        dirty: set[str] = set()
        index = 0
        while index < len(chunks):
            item = chunks[index]
            index += 1
            if not item:
                continue
            if len(item) < 4:
                raise DeliveryError("unparseable Git status entry")
            code, path = item[:2], item[3:]
            if "R" in code or "C" in code:
                raise DeliveryError("rename/copy delivery is not supported by exact-path mode")
            dirty.add(path)
        return dirty

    def _staged_paths(self) -> set[str]:
        return {
            item for item in self._git(
                "diff", "--cached", "--no-renames", "--name-only", "-z",
            ).split("\0") if item
        }

    def _stage(self) -> str:
        with self.store.transaction() as conn:
            row = conn.execute(
                """SELECT stage FROM stage_runs
                   WHERE task_id = ? AND status = 'active'
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (self.task_id,),
            ).fetchone()
        return str(row["stage"]) if row else ""

    def _task_card_relative(self, task: Mapping[str, Any]) -> str:
        raw = str(task.get("card_path") or "").strip()
        if not raw:
            raise DeliveryError("task has no deterministic task-card projection path")
        absolute = Path(raw).expanduser()
        if not absolute.is_absolute():
            absolute = self.repo / absolute
        absolute = absolute.resolve()
        try:
            relative = absolute.relative_to(self.repo).as_posix()
        except ValueError as exc:
            raise DeliveryError("task-card projection path is outside the repository") from exc
        if absolute.is_symlink() or not absolute.exists() or not absolute.is_file():
            raise DeliveryError("task-card projection path must be an existing regular file")
        return relative

    def _verify_exact_commit(self, commit: str, expected_paths: set[str]) -> None:
        changed = {
            item for item in self._git(
                "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-z", "-r", commit, "--",
            ).split("\0") if item
        }
        if changed != expected_paths:
            raise DeliveryError(
                f"commit tree escaped exact paths; changed={sorted(changed)}, expected={sorted(expected_paths)}"
            )

    def _recover_partial_classified_commit(
        self, *, paths: Sequence[str], subject: str, max_depth: int,
    ) -> str:
        expected = set(paths)
        current = self._git("rev-parse", "HEAD").strip()
        for _ in range(max_depth):
            shown = subprocess.run(
                ["git", "show", "-s", "--format=%s", current],
                cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if shown.returncode == 0 and shown.stdout.strip() == subject:
                try:
                    self._verify_exact_commit(current, expected)
                    return current
                except DeliveryError:
                    pass
            parent = subprocess.run(
                ["git", "rev-parse", f"{current}^"],
                cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if parent.returncode != 0:
                break
            current = parent.stdout.strip()
        raise DeliveryError(
            f"clean delivery group has no matching recent classified commit: {sorted(expected)}"
        )

    def _recover_classified_commit_chain(
        self,
        *,
        paths: Sequence[str],
        subject: str,
        repair_subject: str,
        max_depth: int = 64,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Recover one exact base commit plus any exact, linear repair subsets."""
        expected = set(paths)
        current = self._git("rev-parse", "HEAD").strip()
        base = ""
        for _ in range(max_depth):
            shown = subprocess.run(
                ["git", "show", "-s", "--format=%s", current],
                cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if shown.returncode == 0 and shown.stdout.strip() == subject:
                changed = {
                    item for item in self._git(
                        "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-z",
                        "-r", current, "--",
                    ).split("\0") if item
                }
                if changed == expected:
                    base = current
                    break
            parent = subprocess.run(
                ["git", "rev-parse", f"{current}^"],
                cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if parent.returncode != 0:
                break
            current = parent.stdout.strip()
        if not base:
            raise DeliveryError(
                f"clean delivery group has no matching recent classified base commit: {sorted(expected)}"
            )
        result = [{
            "commit": base,
            "subject": subject,
            "paths": sorted(expected),
            "repair": False,
        }]
        latest_by_path = {path: base for path in expected}
        listed = subprocess.run(
            ["git", "rev-list", "--first-parent", "--reverse", f"{base}..HEAD"],
            cwd=self.repo, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if listed.returncode != 0:
            raise DeliveryError("classified repair chain cannot be enumerated")
        for commit in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
            changed = {
                item for item in self._git(
                    "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-z",
                    "-r", commit, "--",
                ).split("\0") if item
            }
            relevant = changed & expected
            if not relevant:
                continue
            shown = subprocess.run(
                ["git", "show", "-s", "--format=%s", commit],
                cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            if (
                shown.returncode != 0
                or shown.stdout.strip() != repair_subject
                or changed != relevant
            ):
                raise DeliveryError("classified path changed outside its exact repair chain")
            result.append({
                "commit": commit,
                "subject": repair_subject,
                "paths": sorted(relevant),
                "repair": True,
            })
            latest_by_path.update({path: commit for path in relevant})
        return result, latest_by_path

    def _audit_partial_classified_commits(
        self,
        *,
        repository_head_before: str,
        repository_head_after: str,
        exact_paths: Sequence[str],
        category: str,
        path_categories: Mapping[str, str] | None,
        message: str,
    ) -> list[dict[str, Any]]:
        """Prove every commit created after an attempt is one exact classified group."""
        if repository_head_after == repository_head_before:
            return []
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", repository_head_before, repository_head_after],
            cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        if ancestor.returncode != 0:
            raise DeliveryError("partial delivery HEAD is not a descendant of the attempt baseline")
        categories_input = dict(path_categories or {})
        expected_by_subject: dict[str, set[str]] = defaultdict(set)
        for path in exact_paths:
            selected = categories_input.get(path, category)
            if not SAFE_CATEGORY_RE.fullmatch(selected):
                raise DeliveryError(f"invalid commit category for path: {path}")
            expected_by_subject[f"xirang({selected}): {message.strip()}"].add(path)
            expected_by_subject[f"xirang(repair): {selected}: {message.strip()}"].add(path)
        listed = subprocess.run(
            ["git", "rev-list", "--reverse", f"{repository_head_before}..{repository_head_after}"],
            cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False,
        )
        commits = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if listed.returncode != 0 or not commits or commits[-1] != repository_head_after:
            raise DeliveryError("partial delivery commit chain cannot be enumerated exactly")
        if len(commits) > len(expected_by_subject):
            raise DeliveryError("partial delivery created more commits than classified groups")
        result: list[dict[str, Any]] = []
        used_subjects: set[str] = set()
        used_paths: set[str] = set()
        for commit in commits:
            shown = subprocess.run(
                ["git", "show", "-s", "--format=%s", commit],
                cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, check=False,
            )
            subject = shown.stdout.strip()
            changed = {
                item for item in self._git(
                    "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-z",
                    "-r", commit, "--",
                ).split("\0") if item
            }
            expected = expected_by_subject.get(subject)
            if (
                shown.returncode != 0
                or expected is None
                or subject in used_subjects
                or not changed
                or not changed <= expected
                or changed & used_paths
            ):
                raise DeliveryError("partial delivery commit is outside the exact classified request")
            used_subjects.add(subject)
            used_paths.update(changed)
            result.append({"commit": commit, "subject": subject, "paths": sorted(changed)})
        return result

    def _exact_path_commit(
        self, *, paths: Sequence[str], subject: str, baseline_staged: set[str],
    ) -> str:
        group = set(paths)
        try:
            self._git("add", "--", *paths)
            if self._staged_paths() != baseline_staged | group:
                raise DeliveryError("staged path set drifted while adding the exact commit group")
            if baseline_staged:
                self._git("commit", "--only", "-m", subject, "--", *paths)
            else:
                self._git("commit", "-m", subject)
        except Exception:
            rollback = subprocess.run(
                ["git", "reset", "-q", "HEAD", "--", *paths],
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if rollback.returncode != 0 or self._staged_paths() != baseline_staged:
                raise DeliveryError(
                    "failed exact-path commit could not restore the staged baseline"
                )
            raise
        commit = self._git("rev-parse", "HEAD").strip()
        self._verify_exact_commit(commit, group)
        if self._staged_paths() != baseline_staged:
            raise DeliveryError("unrelated staged baseline changed during exact-path commit")
        return commit

    def _project_task(self, task_id: str) -> tuple[str, str]:
        task = self.store.get_task(task_id) or {}
        relative = self._task_card_relative(task)
        write_task_card_projection(self.store, workspace_root=self.repo, task_id=task_id)
        absolute = (self.repo / relative).resolve()
        digest = _sha256(absolute)
        return relative, digest

    def _ensure_task_record(
        self, *, task_id: str, delivery_id: str, implementation_commit: str,
        baseline_dirty: set[str], baseline_staged: set[str],
    ) -> dict[str, Any]:
        task = self.store.get_task(task_id) or {}
        relative = self._task_card_relative(task)
        absolute = (self.repo / relative).resolve()
        tag_name = f"xirang/task-record/{delivery_id}"
        recorded = self.store.get_delivery_task_record(delivery_id)
        if recorded is not None:
            commit = str(recorded.get("task_record_commit") or "")
            tag_object = self._git("rev-parse", f"refs/tags/{tag_name}^{{tag}}").strip()
            if tag_object != recorded.get("task_record_tag_object"):
                raise DeliveryError("authority-committed task-record tag drifted")
            if self._git("cat-file", "-t", tag_object).strip() != "tag":
                raise DeliveryError("task-record tag is not annotated")
            if self._git("rev-parse", f"{tag_object}^{{commit}}").strip() != commit:
                raise DeliveryError("task-record tag no longer identifies its commit")
            if self._git("rev-parse", f"{commit}^").strip() != implementation_commit:
                raise DeliveryError("task-record commit is not directly after the implementation commit")
            self._verify_exact_commit(commit, {relative})
            if self._commit_blob_sha256(commit, relative) != recorded.get("sha256"):
                raise DeliveryError("task-record commit blob drifted")
            result = dict(recorded)
            result["tag"] = tag_name
            result["idempotent"] = True
        else:
            orphan = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/{tag_name}^{{tag}}"],
                cwd=self.repo, capture_output=True, text=True, check=False,
            )
            commit = ""
            tag_object = ""
            if orphan.returncode == 0:
                tag_object = orphan.stdout.strip()
                if self._git("cat-file", "-t", tag_object).strip() != "tag":
                    raise DeliveryError("orphan task-record recovery requires an annotated tag")
                tag_text = self._git("cat-file", "-p", tag_object)
                if not tag_text.endswith(f"\n\nXirang task record {delivery_id}\n"):
                    raise DeliveryError("orphan task-record tag message does not match the controlled operation")
                commit = self._git("rev-parse", f"{tag_object}^{{commit}}").strip()
            else:
                head = self._git("rev-parse", "HEAD").strip()
                if head != implementation_commit:
                    try:
                        parent = self._git("rev-parse", f"{head}^").strip()
                        self._verify_exact_commit(head, {relative})
                    except DeliveryError as exc:
                        raise DeliveryError("repository advanced beyond the implementation commit") from exc
                    if parent != implementation_commit:
                        raise DeliveryError("repository advanced beyond the implementation commit")
                    commit = head
                else:
                    relative, _ = self._project_task(task_id)
                    commit = self._exact_path_commit(
                        paths=[relative],
                        subject=f"xirang(task_record): submitted {task_id}",
                        baseline_staged=baseline_staged,
                    )
                self._git("tag", "-a", tag_name, "-m", f"Xirang task record {delivery_id}", commit)
                tag_object = self._git("rev-parse", f"refs/tags/{tag_name}^{{tag}}").strip()
            if self._git("rev-parse", f"{commit}^").strip() != implementation_commit:
                raise DeliveryError("task-record commit is not directly after the implementation commit")
            self._verify_exact_commit(commit, {relative})
            digest = self._commit_blob_sha256(commit, relative)
            if not absolute.exists() or _sha256(absolute) != digest:
                raise DeliveryError("task-record worktree and committed projection differ")
            projection = self.store.get_projection_manifest(task_id)
            if (projection is None or projection.get("status") != "projected"
                    or projection.get("path") != str(absolute) or projection.get("sha256") != digest):
                refreshed = write_task_card_projection(
                    self.store, workspace_root=self.repo, task_id=task_id,
                )
                if refreshed["sha256"] != digest:
                    raise DeliveryError("committed task record is stale against current authority")
            result = self.store.record_delivery_task_record(
                delivery_id=delivery_id, task_id=task_id, path=str(absolute), sha256=digest,
                commit=commit, tree=self._git("rev-parse", f"{commit}^{{tree}}").strip(),
                tag_object=tag_object,
            )
            result["tag"] = tag_name
        if self._dirty_paths() != baseline_dirty or self._staged_paths() != baseline_staged:
            raise DeliveryError("unrelated dirty/staged baseline changed during task-record commit")
        return result

    def _deliver_files_no_git(
        self,
        *,
        task: Mapping[str, Any],
        access: Mapping[str, Any],
        task_id: str,
        exact_paths: Sequence[str],
        category: str,
        categories_input: Mapping[str, str],
        recovery_input: Mapping[str, str],
        initial_dirty: set[str],
        initial_staged: set[str],
        baseline_dirty: set[str],
        baseline_staged: set[str],
        message: str,
        delivery_id: str | None,
        validation_summary: str,
        adversarial_review_summary: str,
    ) -> dict[str, Any]:
        requested = set(exact_paths)
        if not requested or set(recovery_input) != requested:
            raise DeliveryError("files_no_git requires one pre-write recovery manifest per exact path")
        if not requested <= initial_dirty:
            raise DeliveryError("files_no_git paths must remain uncommitted worktree changes")
        if requested & initial_staged:
            raise DeliveryError("files_no_git paths must never be staged")
        roots = list(access.get("allowed_write_roots") or task.get("allowed_write_roots") or [])
        receipts = {item["path"]: item for item in self.store.list_effective_write_receipts(task_id)}
        manifest: list[dict[str, Any]] = []
        for path in exact_paths:
            if not any(_under(path, root) for root in roots):
                raise DeliveryError(f"files_no_git path is outside the current task scope: {path}")
            if not self._is_git_tracked(path):
                raise DeliveryError(f"files_no_git only supports tracked repository paths: {path}")
            receipt = receipts.get(path)
            if receipt is None or receipt.get("evidence_kind"):
                raise DeliveryError(f"files_no_git requires a direct effective write receipt: {path}")
            absolute = self.repo / path
            exists_after = bool(receipt.get("exists_after"))
            if exists_after:
                if not absolute.is_file() or absolute.is_symlink() or not receipt.get("sha256"):
                    raise DeliveryError(f"files_no_git current file is invalid: {path}")
                current_sha = _sha256(absolute)
            else:
                if absolute.exists() or absolute.is_symlink() or receipt.get("sha256") is not None:
                    raise DeliveryError(f"files_no_git deletion receipt drifted: {path}")
                current_sha = None
            if current_sha != receipt.get("sha256"):
                raise DeliveryError(f"files_no_git write receipt is stale: {path}")
            recovery_path = Path(recovery_input[path]).expanduser().resolve()
            try:
                registry = load_registry(self.repo / ".xirang/contract/recovery-roots.yaml")
                require_registered(recovery_path, registry, kind="manifests")
                recovery_bytes = recovery_path.read_bytes()
                recovery = verify_file_preimage(recovery_path)
                require_registered(Path(str(recovery.get("object") or "")), registry, kind="objects")
                captured_at = datetime.fromisoformat(
                    str(recovery.get("captured_at") or "").replace("Z", "+00:00")
                )
                receipt_at = datetime.fromisoformat(
                    str(receipt.get("created_at") or "").replace("Z", "+00:00")
                )
            except (Exception, RecoveryRootError) as exc:
                raise DeliveryError(f"files_no_git pre-image is invalid: {path}: {exc}") from exc
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=timezone.utc)
            if receipt_at.tzinfo is None:
                receipt_at = receipt_at.replace(tzinfo=timezone.utc)
            if recovery.get("logical_path") != path or captured_at > receipt_at:
                raise DeliveryError(f"files_no_git pre-image does not prove a write-before boundary: {path}")
            selected = categories_input.get(path, category)
            if not SAFE_CATEGORY_RE.fullmatch(selected):
                raise DeliveryError(f"invalid no-Git delivery category for path: {path}")
            manifest.append({
                "path": path,
                "sha256": current_sha,
                "receipt_id": receipt["receipt_id"],
                "category": selected,
                "exists_after": exists_after,
                "git_effect": False,
                "evidence_only": False,
                "delivery_mode": "no_git",
                "recovery_manifest": str(recovery_path),
                "recovery_manifest_sha256": hashlib.sha256(recovery_bytes).hexdigest(),
                "preimage_sha256": recovery["sha256"],
                "git_recovery_available": False,
            })
        manifest_sha = self._no_git_manifest_digest(manifest)
        manifest = [{**item, "no_git_manifest_sha256": manifest_sha} for item in manifest]
        self._verify_no_git_manifest(manifest)
        head_before = self._git("rev-parse", "HEAD").strip()
        recorded_id = self.store.create_delivery(
            task_id=task_id,
            delivery_id=delivery_id,
            manifest=manifest,
            submitted_at=_now(),
            implementation_commit=None,
            implementation_tree=None,
            tag_object=None,
            validation_summary=validation_summary,
            adversarial_review_summary=adversarial_review_summary,
        )
        if (
            self._git("rev-parse", "HEAD").strip() != head_before
            or self._staged_paths() != baseline_staged
            or self._dirty_paths() != initial_dirty
            or baseline_dirty != initial_dirty - requested - {self._task_card_relative(task)}
        ):
            raise DeliveryError("files_no_git delivery unexpectedly changed Git/worktree state")
        self.store.set_task_metadata(task_id, {
            "submitted_at": _now(),
            "delivery_id": recorded_id,
            "verification_summary": validation_summary,
            "submission_summary": message,
        })
        self.store.transition_stage(
            task_id=task_id,
            to_stage="submitted",
            details={
                "delivery_id": recorded_id,
                "delivery_mode": "files_no_git",
                "no_git_manifest_sha256": manifest_sha,
                "git_recovery_available": False,
                "classified_commits": [],
            },
        )
        return {
            "delivery_id": recorded_id,
            "commit": None,
            "tree": None,
            "tag": None,
            "tag_object": None,
            "manifest": manifest,
            "no_git_manifest_sha256": manifest_sha,
            "classified_commits": [],
            "task_record": None,
            "task_record_commit": None,
            "git_recovery_available": False,
        }

    def deliver(
        self,
        *,
        task_id: str,
        session_id: str,
        paths: Sequence[str],
        evidence_only_paths: Sequence[str] = (),
        category: str = "implementation",
        path_categories: Mapping[str, str] | None = None,
        recovery_manifests: Mapping[str, str] | None = None,
        message: str = "controlled delivery",
        delivery_id: str | None = None,
        validation_summary: str = "",
        adversarial_review_summary: str = "",
    ) -> dict[str, Any]:
        self.task_id = task_id
        task = self.store.get_task(task_id)
        if task is None:
            raise DeliveryError(f"unknown task: {task_id}")
        task_delivery_mode = str(task.get("delivery_mode") or "files")
        if task_delivery_mode not in {"chat", "files", "files_no_git"}:
            raise DeliveryError(f"unknown task delivery mode: {task_delivery_mode}")
        delivery_id = delivery_id or (
            "D-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:10]
        )
        existing = self.store.get_delivery(delivery_id)
        if existing is not None:
            return self._deliver_impl(
                task_id=task_id,
                session_id=session_id,
                paths=paths,
                evidence_only_paths=evidence_only_paths,
                category=category,
                path_categories=path_categories,
                recovery_manifests=recovery_manifests,
                message=message,
                delivery_id=delivery_id,
                validation_summary=validation_summary,
                adversarial_review_summary=adversarial_review_summary,
            )
        evidence_paths = self._normalize_evidence_only_paths(evidence_only_paths)
        if task_delivery_mode == "files_no_git" and evidence_paths:
            raise DeliveryError("files_no_git tracked delivery cannot mix evidence-only paths")
        exact_paths = self._normalize_paths(
            paths,
            allow_empty=task_delivery_mode == "chat" or bool(evidence_paths),
        )
        if set(exact_paths) & set(evidence_paths):
            raise DeliveryError("a delivery path cannot be both Git and evidence-only")
        if self._stage() != "committing":
            raise DeliveryError("task must be in committing stage")
        request_payload = {
            "task_id": task_id,
            "delivery_id": delivery_id,
            "git_paths": exact_paths,
            "evidence_only_paths": evidence_paths,
            "default_category": category,
            "path_categories": dict(sorted((path_categories or {}).items())),
            "recovery_manifests": dict(sorted((recovery_manifests or {}).items())),
            "message": message.strip(),
        }
        request_digest = hashlib.sha256(_json(request_payload).encode("utf-8")).hexdigest()
        repository_head = self._git("rev-parse", "HEAD").strip()
        staged_before = self._staged_paths()
        try:
            attempt = self.store.begin_controlled_delivery_attempt(
                attempt_id="CDA-" + uuid.uuid4().hex[:24],
                task_id=task_id,
                session_id=session_id,
                delivery_id=delivery_id,
                request_digest=request_digest,
                repository_head=repository_head,
            )
        except StateConflict as exc:
            raise DeliveryError("session has no delivery-capable task lease") from exc
        try:
            return self._deliver_impl(
                task_id=task_id,
                session_id=session_id,
                paths=exact_paths,
                evidence_only_paths=evidence_paths,
                category=category,
                path_categories=path_categories,
                recovery_manifests=recovery_manifests,
                message=message,
                delivery_id=delivery_id,
                validation_summary=validation_summary,
                adversarial_review_summary=adversarial_review_summary,
            )
        except Exception as exc:
            delivery_created = self.store.get_delivery(delivery_id) is not None
            repository_head_after = self._git("rev-parse", "HEAD").strip()
            staged_unchanged = self._staged_paths() == staged_before
            tag_exists = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/xirang/submitted/{delivery_id}^{{tag}}"],
                cwd=self.repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if not delivery_created and not tag_exists and staged_unchanged:
                try:
                    partial_commits = self._audit_partial_classified_commits(
                        repository_head_before=repository_head,
                        repository_head_after=repository_head_after,
                        exact_paths=exact_paths,
                        category=category,
                        path_categories=path_categories,
                        message=message,
                    )
                    self.store.fail_controlled_delivery_attempt(
                        attempt_event_id=attempt["event_id"],
                        task_id=task_id,
                        session_id=session_id,
                        repository_head_after=repository_head_after,
                        partial_commits=partial_commits,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                    )
                except Exception as audit_exc:
                    raise DeliveryError(
                        "controlled delivery failure could not be atomically recorded"
                    ) from audit_exc
            raise

    def _deliver_impl(
        self,
        *,
        task_id: str,
        session_id: str,
        paths: Sequence[str],
        evidence_only_paths: Sequence[str] = (),
        category: str = "implementation",
        path_categories: Mapping[str, str] | None = None,
        recovery_manifests: Mapping[str, str] | None = None,
        message: str = "controlled delivery",
        delivery_id: str | None = None,
        validation_summary: str = "",
        adversarial_review_summary: str = "",
    ) -> dict[str, Any]:
        self.task_id = task_id
        task = self.store.get_task(task_id)
        if task is None:
            raise DeliveryError(f"unknown task: {task_id}")
        task_delivery_mode = str(task.get("delivery_mode") or "files")
        if task_delivery_mode not in {"chat", "files", "files_no_git"}:
            raise DeliveryError(f"unknown task delivery mode: {task_delivery_mode}")
        retry_delivery = self.store.get_delivery(delivery_id) if delivery_id else None
        if (retry_delivery is not None and retry_delivery.get("task_id") == task_id
                and task.get("session_id") == session_id):
            access = {"kind": "owner_retry", "task": task,
                      "allowed_write_roots": task.get("allowed_write_roots") or []}
        else:
            access = self.store.resolve_task_access(
                session_id=session_id, task_id=task_id, capability="delivery")
        if access is None:
            raise DeliveryError("session has no delivery-capable task lease")

        evidence_paths = self._normalize_evidence_only_paths(evidence_only_paths)
        exact_paths = self._normalize_paths(
            paths,
            allow_empty=task.get("delivery_mode") == "chat" or bool(evidence_paths),
        )
        requested = set(exact_paths)
        evidence_requested = set(evidence_paths)
        if requested & evidence_requested:
            raise DeliveryError("a delivery path cannot be both Git and evidence-only")
        all_requested = requested | evidence_requested
        requested_modes = self._requested_path_modes(
            exact_paths, evidence_paths,
            exact_mode="no_git" if task_delivery_mode == "files_no_git" else "git",
        )
        categories_input = dict(path_categories or {})
        recovery_input = dict(recovery_manifests or {})
        unknown_categories = set(categories_input) - all_requested
        if unknown_categories:
            raise DeliveryError(f"path category keys are not exact delivery paths: {sorted(unknown_categories)}")
        if set(recovery_input) - all_requested:
            raise DeliveryError("recovery manifest key must exactly match one delivery path")
        task_card = self._task_card_relative(task)
        if task_card in requested:
            raise DeliveryError("current task card is committed only by the task_record phase")
        initial_dirty = self._dirty_paths()
        initial_staged = self._staged_paths()
        if requested & initial_staged:
            raise DeliveryError("requested delivery paths must not be pre-staged")
        if task_card in initial_staged:
            raise DeliveryError("current task card must not be pre-staged")
        baseline_dirty = initial_dirty - requested - {task_card}
        baseline_staged = initial_staged - requested - {task_card}
        if delivery_id:
            existing = self.store.get_delivery(delivery_id)
            if existing is not None:
                if existing["task_id"] != task_id:
                    raise DeliveryError("delivery_id belongs to another task")
                manifest = existing.get("manifest") or []
                if {item.get("path") for item in manifest} != all_requested:
                    raise DeliveryError("retry paths differ from the authority-committed delivery")
                if self._manifest_path_modes(manifest) != requested_modes:
                    raise DeliveryError("retry Git/evidence path modes differ from the committed delivery")
                if categories_input and any(
                    categories_input.get(str(item.get("path")), item.get("category")) != item.get("category")
                    for item in manifest
                ):
                    raise DeliveryError("retry path categories differ from the authority-committed delivery")
                if task_delivery_mode == "files_no_git":
                    if any(existing.get(key) for key in (
                        "implementation_commit", "implementation_tree", "tag_object",
                    )):
                        raise DeliveryError("files_no_git authority record falsely contains Git identity")
                    manifest_sha = self._verify_no_git_manifest(manifest)
                    receipts = {
                        item["path"]: item for item in self.store.list_effective_write_receipts(task_id)
                    }
                    for item in manifest:
                        receipt = receipts.get(str(item.get("path") or ""))
                        if receipt is None:
                            raise DeliveryError(
                                f"authority-committed no-Git path lost effective evidence: {item.get('path')}"
                            )
                        self._verify_manifest_against_effective_receipt(item, receipt)
                    if set(recovery_input) != all_requested:
                        raise DeliveryError("files_no_git retry requires every bound pre-image manifest")
                    for path, supplied in recovery_input.items():
                        bound = next(
                            (item.get("recovery_manifest") for item in manifest if item["path"] == path),
                            None,
                        )
                        if not bound or Path(str(bound)).expanduser().resolve() != Path(supplied).expanduser().resolve():
                            raise DeliveryError(f"files_no_git retry pre-image binding differs: {path}")
                    self.store.set_task_metadata(task_id, {
                        "submitted_at": existing.get("submitted_at") or _now(),
                        "delivery_id": delivery_id,
                        "verification_summary": existing.get("validation_summary") or validation_summary,
                        "submission_summary": message,
                    })
                    if self._stage() == "committing":
                        self.store.transition_stage(
                            task_id=task_id, to_stage="submitted",
                            details={
                                "delivery_id": delivery_id,
                                "delivery_mode": "files_no_git",
                                "no_git_manifest_sha256": manifest_sha,
                                "git_recovery_available": False,
                            },
                        )
                    return {
                        "delivery_id": delivery_id,
                        "commit": None,
                        "tree": None,
                        "tag": None,
                        "tag_object": None,
                        "manifest": manifest,
                        "no_git_manifest_sha256": manifest_sha,
                        "classified_commits": [],
                        "task_record": None,
                        "task_record_commit": None,
                        "git_recovery_available": False,
                        "idempotent_retry": True,
                    }
                commit = str(existing.get("implementation_commit") or "")
                tree = str(existing.get("implementation_tree") or "")
                tag_name = f"xirang/submitted/{delivery_id}"
                if not commit or self._git("rev-parse", f"{commit}^{{tree}}").strip() != tree:
                    raise DeliveryError("authority-committed delivery tree is unavailable or drifted")
                tag_object = self._git("rev-parse", f"refs/tags/{tag_name}^{{tag}}").strip()
                if tag_object != str(existing.get("tag_object") or ""):
                    raise DeliveryError("authority-committed delivery tag drifted")
                if self._git("cat-file", "-t", tag_object).strip() != "tag":
                    raise DeliveryError("authority-committed tag object is not annotated")
                if self._git("rev-parse", f"{tag_object}^{{commit}}").strip() != commit:
                    raise DeliveryError("authority-committed tag no longer identifies the delivery commit")
                tag_payload = self._read_tag_payload(tag_object)
                expected_payload = self._tag_payload(
                    delivery_id=delivery_id, task_id=task_id, commit=commit, tree=tree,
                    manifest=manifest,
                )
                if tag_payload != expected_payload:
                    raise DeliveryError("authority-committed tag manifest differs from StateStore")
                receipts = {
                    item["path"]: item for item in self.store.list_effective_write_receipts(task_id)
                }
                for item in manifest:
                    receipt = receipts.get(str(item.get("path") or ""))
                    if receipt is None:
                        raise DeliveryError(f"authority-committed path lost effective evidence: {item.get('path')}")
                    self._verify_manifest_against_effective_receipt(item, receipt)
                    if item.get("git_effect"):
                        if self._commit_path_sha256(commit, str(item["path"])) != item.get("sha256"):
                            raise DeliveryError(f"authority-committed path drifted: {item['path']}")
                    else:
                        self._verify_non_git_manifest_item(item)
                for path, supplied in recovery_input.items():
                    bound = next(
                        (item.get("recovery_manifest") for item in manifest if item["path"] == path),
                        None,
                    )
                    if not bound or Path(str(bound)).expanduser().resolve() != Path(supplied).expanduser().resolve():
                        raise DeliveryError(f"retry recovery manifest differs from committed binding: {path}")
                self.store.set_task_metadata(task_id, {
                    "submitted_at": existing.get("submitted_at") or _now(),
                    "delivery_id": delivery_id,
                    "verification_summary": existing.get("validation_summary") or validation_summary,
                    "submission_summary": message,
                })
                if self._stage() == "committing":
                    self.store.transition_stage(
                        task_id=task_id, to_stage="submitted",
                        details={"delivery_id": delivery_id, "commit": commit, "tree": tree,
                                 "tag": tag_name, "tag_object": tag_object,
                                 "classified_commits": []},
                    )
                task_record = self._ensure_task_record(
                    task_id=task_id, delivery_id=delivery_id, implementation_commit=commit,
                    baseline_dirty=baseline_dirty, baseline_staged=baseline_staged,
                )
                return {"delivery_id": delivery_id, "commit": commit, "tree": tree,
                        "tag": tag_name, "tag_object": tag_object, "manifest": manifest,
                        "classified_commits": [], "task_record": task_record,
                        "task_record_commit": task_record["task_record_commit"],
                        "idempotent_retry": True}
        if self._stage() != "committing":
            raise DeliveryError("task must be in committing stage")
        if task_delivery_mode == "files_no_git":
            return self._deliver_files_no_git(
                task=task,
                access=access,
                task_id=task_id,
                exact_paths=exact_paths,
                category=category,
                categories_input=categories_input,
                recovery_input=recovery_input,
                initial_dirty=initial_dirty,
                initial_staged=initial_staged,
                baseline_dirty=baseline_dirty,
                baseline_staged=baseline_staged,
                message=message,
                delivery_id=delivery_id,
                validation_summary=validation_summary,
                adversarial_review_summary=adversarial_review_summary,
            )
        if delivery_id:
            tag_name = f"xirang/submitted/{delivery_id}"
            orphan = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/{tag_name}^{{tag}}"],
                cwd=self.repo, capture_output=True, text=True, check=False,
            )
            if orphan.returncode == 0:
                tag_object = orphan.stdout.strip()
                if self._git("cat-file", "-t", tag_object).strip() != "tag":
                    raise DeliveryError("orphan recovery requires an annotated delivery tag")
                commit = self._git("rev-parse", f"{tag_object}^{{commit}}").strip()
                tree = self._git("rev-parse", f"{commit}^{{tree}}").strip()
                tag_payload = self._read_tag_payload(tag_object)
                if (
                    tag_payload.get("delivery_id") != delivery_id
                    or tag_payload.get("task_id") != task_id
                    or tag_payload.get("implementation_commit") != commit
                    or tag_payload.get("implementation_tree") != tree
                ):
                    raise DeliveryError("orphan delivery tag authority binding is invalid")
                recovered_manifest = tag_payload["manifest"]
                if {item.get("path") for item in recovered_manifest} != all_requested:
                    raise DeliveryError("orphan retry paths differ from the tag-bound delivery")
                if self._manifest_path_modes(recovered_manifest) != requested_modes:
                    raise DeliveryError("orphan retry Git/evidence modes differ from the tag-bound delivery")
                if categories_input and any(
                    categories_input.get(str(item.get("path")), item.get("category"))
                    != item.get("category") for item in recovered_manifest
                ):
                    raise DeliveryError("orphan retry categories differ from the tag-bound delivery")
                roots = list(access.get("allowed_write_roots") or task.get("allowed_write_roots") or [])
                receipts = {item["path"]: item for item in self.store.list_effective_write_receipts(task_id)}
                for item in recovered_manifest:
                    path = str(item["path"])
                    if not any(_under(path, root) for root in roots):
                        raise DeliveryError(f"orphan recovery path is outside task scope: {path}")
                    evidence_only = item["delivery_mode"] == "evidence_only"
                    if evidence_only:
                        self._require_evidence_only_path_kind(path)
                    absolute = self._evidence_path(path) if evidence_only else self.repo / path
                    receipt = receipts.get(path)
                    if receipt is None:
                        raise DeliveryError(f"orphan recovery lacks an effective receipt: {path}")
                    self._verify_manifest_against_effective_receipt(item, receipt)
                    exists_after = bool(receipt.get("exists_after"))
                    if exists_after:
                        if not absolute.is_file() or absolute.is_symlink() or not receipt.get("sha256"):
                            raise DeliveryError(f"orphan recovery lacks an effective file receipt: {path}")
                        digest = _sha256(absolute)
                    else:
                        if absolute.exists() or absolute.is_symlink() or receipt.get("sha256") is not None:
                            raise DeliveryError(f"orphan recovery deletion receipt drifted: {path}")
                        digest = None
                    if digest != receipt.get("sha256"):
                        raise DeliveryError(f"orphan recovery receipt mismatch: {path}")
                    if item.get("sha256") != digest or bool(item.get("exists_after")) != exists_after:
                        raise DeliveryError(f"orphan tag manifest differs from effective receipt: {path}")
                    if item.get("git_effect") and self._commit_path_sha256(commit, path) != digest:
                        raise DeliveryError(f"orphan recovery commit/receipt mismatch: {path}")
                    elif not item.get("git_effect"):
                        self._verify_non_git_manifest_item(item)
                for path, supplied in recovery_input.items():
                    bound = next(
                        (item.get("recovery_manifest") for item in recovered_manifest if item["path"] == path),
                        None,
                    )
                    if not bound or Path(str(bound)).expanduser().resolve() != Path(supplied).expanduser().resolve():
                        raise DeliveryError(f"orphan retry recovery manifest differs from tag binding: {path}")
                recorded_id = self.store.create_delivery(
                    task_id=task_id, delivery_id=delivery_id, manifest=recovered_manifest,
                    submitted_at=_now(), implementation_commit=commit,
                    implementation_tree=tree, tag_object=tag_object,
                    validation_summary=validation_summary,
                    adversarial_review_summary=adversarial_review_summary,
                )
                self.store.set_task_metadata(task_id, {
                    "submitted_at": _now(), "delivery_id": recorded_id,
                    "verification_summary": validation_summary, "submission_summary": message,
                })
                self.store.transition_stage(
                    task_id=task_id, to_stage="submitted",
                    details={"delivery_id": recorded_id, "commit": commit, "tree": tree,
                             "tag": tag_name, "tag_object": tag_object,
                             "classified_commits": []},
                )
                task_record = self._ensure_task_record(
                    task_id=task_id, delivery_id=recorded_id, implementation_commit=commit,
                    baseline_dirty=baseline_dirty, baseline_staged=baseline_staged,
                )
                return {"delivery_id": recorded_id, "commit": commit, "tree": tree,
                        "tag": tag_name, "tag_object": tag_object,
                        "manifest": recovered_manifest, "classified_commits": [],
                        "task_record": task_record,
                        "task_record_commit": task_record["task_record_commit"],
                        "idempotent_retry": True, "recovered_orphan": True}
        roots = list(access.get("allowed_write_roots") or task.get("allowed_write_roots") or [])
        receipts = {item["path"]: item for item in self.store.list_effective_write_receipts(task_id)}
        categories: dict[str, str] = {}
        manifest: list[dict[str, Any]] = []
        for path in exact_paths:
            if not any(_under(path, root) for root in roots):
                raise DeliveryError(f"path is outside the current task scope: {path}")
            absolute = self.repo / path
            receipt = receipts.get(path)
            if receipt is None:
                raise DeliveryError(f"missing effective write receipt: {path}")
            evidence_binding = self._receipt_evidence_binding(receipt)
            exists_after = bool(receipt.get("exists_after"))
            if exists_after:
                if not absolute.exists() or absolute.is_symlink() or not absolute.is_file() or not receipt.get("sha256"):
                    raise DeliveryError(f"delivery path must be an existing regular file: {path}")
                actual_sha = _sha256(absolute)
                if actual_sha != receipt["sha256"]:
                    raise DeliveryError(f"stale write receipt: {path}")
                git_effect = True
            else:
                if absolute.exists() or absolute.is_symlink() or receipt.get("sha256") is not None:
                    raise DeliveryError(f"stale deletion receipt: {path}")
                tracked = self._is_git_tracked(path)
                recovery = None
                if not tracked:
                    recovery_path = recovery_input.get(path)
                    if not recovery_path:
                        raise DeliveryError(
                            f"untracked deletion has no recoverable pre-image; snapshot receipt required: {path}"
                        )
                    try:
                        recovery = verify_file_preimage(Path(recovery_path))
                    except Exception as exc:
                        raise DeliveryError(f"invalid pre-image recovery manifest for {path}: {exc}") from exc
                    if recovery.get("logical_path") != path:
                        raise DeliveryError(f"pre-image manifest path mismatch: {path}")
                actual_sha = None
                git_effect = tracked
            selected = categories_input.get(path, category)
            if not SAFE_CATEGORY_RE.fullmatch(selected):
                raise DeliveryError(f"invalid commit category for {path}: {selected}")
            categories[path] = selected
            manifest.append({
                "path": path,
                "sha256": actual_sha,
                "receipt_id": receipt["receipt_id"],
                "category": selected,
                "exists_after": exists_after,
                "git_effect": git_effect,
                "delivery_mode": "git",
                "evidence_only": False,
                **evidence_binding,
                **({
                    "recovery_manifest": recovery["manifest"],
                    "preimage_sha256": recovery["sha256"],
                } if not exists_after and not tracked and recovery is not None else {}),
            })

        for path in evidence_paths:
            if not any(_under(path, root) for root in roots):
                raise DeliveryError(f"evidence-only path is outside the current task scope: {path}")
            self._require_evidence_only_path_kind(path)
            absolute = self._evidence_path(path)
            receipt = receipts.get(path)
            if receipt is None:
                raise DeliveryError(f"missing effective evidence-only write receipt: {path}")
            evidence_binding = self._receipt_evidence_binding(receipt)
            exists_after = bool(receipt.get("exists_after"))
            recovery = None
            if exists_after:
                if not absolute.is_file() or absolute.is_symlink() or not receipt.get("sha256"):
                    raise DeliveryError(f"evidence-only path must be an existing regular file: {path}")
                actual_sha = _sha256(absolute)
                if actual_sha != receipt["sha256"]:
                    raise DeliveryError(f"stale evidence-only write receipt: {path}")
            else:
                if absolute.exists() or absolute.is_symlink() or receipt.get("sha256") is not None:
                    raise DeliveryError(f"stale evidence-only deletion receipt: {path}")
                recovery_path = recovery_input.get(path)
                if not recovery_path:
                    raise DeliveryError(
                        f"evidence-only deletion has no recoverable pre-image: {path}"
                    )
                try:
                    recovery = verify_file_preimage(Path(recovery_path))
                except Exception as exc:
                    raise DeliveryError(f"invalid evidence-only pre-image for {path}: {exc}") from exc
                if recovery.get("logical_path") != path:
                    raise DeliveryError(f"evidence-only pre-image path mismatch: {path}")
                actual_sha = None
            selected = categories_input.get(path, category)
            if not SAFE_CATEGORY_RE.fullmatch(selected):
                raise DeliveryError(f"invalid commit category for {path}: {selected}")
            manifest.append({
                "path": path,
                "sha256": actual_sha,
                "receipt_id": receipt["receipt_id"],
                "category": selected,
                "exists_after": exists_after,
                "git_effect": False,
                "evidence_only": True,
                "delivery_mode": "evidence_only",
                **evidence_binding,
                **({
                    "recovery_manifest": recovery["manifest"],
                    "preimage_sha256": recovery["sha256"],
                } if recovery is not None else {}),
            })

        grouped: dict[str, list[str]] = defaultdict(list)
        for item in manifest:
            if item["git_effect"]:
                grouped[categories[item["path"]]].append(item["path"])
        commits: list[dict[str, Any]] = []
        for selected, group in grouped.items():
            subject = f"xirang({selected}): {message.strip()}"
            repair_subject = f"xirang(repair): {selected}: {message.strip()}"
            dirty_group = set(group) & initial_dirty
            if dirty_group == set(group):
                commit = self._exact_path_commit(
                    paths=group, subject=subject, baseline_staged=baseline_staged,
                )
                chain = [{
                    "commit": commit, "subject": subject, "paths": list(group),
                    "repair": False, "recovered_partial": False,
                }]
                latest_by_path = {path: commit for path in group}
            else:
                recovered_chain, latest_by_path = self._recover_classified_commit_chain(
                    paths=group, subject=subject, repair_subject=repair_subject,
                )
                chain = [{**row, "recovered_partial": True} for row in recovered_chain]
                clean_group = set(group) - dirty_group
                for item in manifest:
                    if item["path"] in clean_group and (
                        self._commit_path_sha256(latest_by_path[item["path"]], item["path"])
                        != item["sha256"]
                    ):
                        raise DeliveryError(
                            f"classified commit chain differs from clean receipt: {item['path']}"
                        )
                if dirty_group:
                    repair_commit = self._exact_path_commit(
                        paths=sorted(dirty_group),
                        subject=repair_subject,
                        baseline_staged=baseline_staged,
                    )
                    chain.append({
                        "commit": repair_commit,
                        "subject": repair_subject,
                        "paths": sorted(dirty_group),
                        "repair": True,
                        "recovered_partial": False,
                    })
                    latest_by_path.update({path: repair_commit for path in dirty_group})
            commits.extend({"category": selected, **row} for row in chain)
            for item in manifest:
                if item["path"] in group:
                    item_commit = latest_by_path[item["path"]]
                    if self._commit_path_sha256(item_commit, item["path"]) != item["sha256"]:
                        raise DeliveryError(f"classified commit path differs from receipt: {item['path']}")
                    item["commit"] = item_commit

        remaining_dirty = self._dirty_paths()
        if requested & remaining_dirty:
            raise DeliveryError("requested paths remain dirty after classified commits")
        if remaining_dirty - {task_card} != baseline_dirty or self._staged_paths() != baseline_staged:
            raise DeliveryError("unrelated dirty/staged baseline changed during classified commits")
        final_commit = self._git("rev-parse", "HEAD").strip()
        final_tree = self._git("rev-parse", "HEAD^{tree}").strip()
        for item in manifest:
            if item.get("git_effect") and self._commit_path_sha256(final_commit, item["path"]) != item["sha256"]:
                raise DeliveryError(f"delivery commit path differs from receipt: {item['path']}")
            if not item.get("git_effect"):
                self._verify_non_git_manifest_item(item)
        delivery_id = delivery_id or "D-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:10]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", delivery_id):
            raise DeliveryError("delivery_id contains unsafe tag characters")
        tag_name = f"xirang/submitted/{delivery_id}"
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag_name}"],
            cwd=self.repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if exists:
            raise DeliveryError(f"delivery tag already exists: {tag_name}")
        tag_payload = self._tag_payload(
            delivery_id=delivery_id, task_id=task_id, commit=final_commit,
            tree=final_tree, manifest=manifest,
        )
        self._git("tag", "-a", tag_name, "-m", _json(tag_payload), final_commit)
        tag_object = self._git("rev-parse", f"refs/tags/{tag_name}^{{tag}}").strip()
        if self._git("cat-file", "-t", tag_object).strip() != "tag":
            raise DeliveryError("created tag is not annotated")
        if self._read_tag_payload(tag_object) != tag_payload:
            raise DeliveryError("created tag did not preserve the immutable delivery manifest")

        recorded_id = self.store.create_delivery(
            task_id=task_id,
            delivery_id=delivery_id,
            manifest=manifest,
            submitted_at=_now(),
            implementation_commit=final_commit,
            implementation_tree=final_tree,
            tag_object=tag_object,
            validation_summary=validation_summary,
            adversarial_review_summary=adversarial_review_summary,
        )
        task = self.store.get_task(task_id) or {}
        preference = task.get("interaction_preference_snapshot") or {}
        metadata = {
            "submitted_at": _now(), "delivery_id": recorded_id,
            "verification_summary": validation_summary,
            "submission_summary": message,
        }
        if preference.get("review_prompt_policy") == "report_once_no_prompt":
            metadata["review_prompt_consumed_at"] = _now()
        self.store.set_task_metadata(task_id, metadata)
        self.store.transition_stage(
            task_id=task_id,
            to_stage="submitted",
            details={
                "delivery_id": recorded_id,
                "commit": final_commit,
                "tree": final_tree,
                "tag": tag_name,
                "tag_object": tag_object,
                "classified_commits": commits,
            },
        )
        task_record = self._ensure_task_record(
            task_id=task_id, delivery_id=recorded_id, implementation_commit=final_commit,
            baseline_dirty=baseline_dirty, baseline_staged=baseline_staged,
        )
        return {
            "delivery_id": recorded_id,
            "commit": final_commit,
            "tree": final_tree,
            "tag": tag_name,
            "tag_object": tag_object,
            "manifest": manifest,
            "classified_commits": commits,
            "task_record": task_record,
            "task_record_commit": task_record["task_record_commit"],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument(
        "--evidence-only-path", action="append", default=[],
        help="exact runtime/config path to bind into the delivery manifest without Git staging",
    )
    parser.add_argument(
        "--path-category", action="append", default=[], metavar="EXACT_PATH=CATEGORY",
        help="repeatable exact delivery path to commit category mapping",
    )
    parser.add_argument(
        "--recovery-manifest", action="append", default=[], metavar="EXACT_PATH=MANIFEST",
        help="content-addressed pre-image manifest for an untracked deletion",
    )
    parser.add_argument("--category", default="implementation")
    parser.add_argument("--message", default="controlled delivery")
    parser.add_argument("--delivery-id")
    parser.add_argument("--validation-summary", default="")
    parser.add_argument("--adversarial-review-summary", default="")
    parser.add_argument("--events-output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    path_categories: dict[str, str] = {}
    recovery_manifests: dict[str, str] = {}
    exact_cli_paths = set(args.path) | set(args.evidence_only_path)
    if not exact_cli_paths:
        parser.error("at least one --path or --evidence-only-path is required")
    for item in args.path_category:
        if "=" not in item:
            parser.error("--path-category must be EXACT_PATH=CATEGORY")
        exact_path, selected = item.rsplit("=", 1)
        if exact_path not in exact_cli_paths:
            parser.error("--path-category key must exactly match one --path value")
        if exact_path in path_categories and path_categories[exact_path] != selected:
            parser.error("conflicting --path-category values for the same path")
        path_categories[exact_path] = selected
    for item in args.recovery_manifest:
        if "=" not in item:
            parser.error("--recovery-manifest must be EXACT_PATH=MANIFEST")
        exact_path, manifest_path = item.split("=", 1)
        if exact_path not in exact_cli_paths:
            parser.error("--recovery-manifest key must exactly match one --path value")
        if exact_path in recovery_manifests and recovery_manifests[exact_path] != manifest_path:
            parser.error("conflicting --recovery-manifest values for the same path")
        recovery_manifests[exact_path] = manifest_path
    try:
        result = ControlledDelivery(args.db, args.repo).deliver(
            task_id=args.task_id,
            session_id=args.session_id,
            paths=args.path,
            evidence_only_paths=args.evidence_only_path,
            category=args.category,
            path_categories=path_categories,
            recovery_manifests=recovery_manifests,
            message=args.message,
            delivery_id=args.delivery_id,
            validation_summary=args.validation_summary,
            adversarial_review_summary=args.adversarial_review_summary,
        )
        output = (
            Path(args.events_output).expanduser().resolve()
            if args.events_output
            else Path(args.db).expanduser().resolve().parent.parent / "events/events.jsonl"
        )
        refresh_events_projection(
            StateStore(args.db), workspace_root=Path(args.repo), output=output
        )
    except Exception as exc:
        print(_json({"ok": False, "error": str(exc)}))
        return 2
    print(_json({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
