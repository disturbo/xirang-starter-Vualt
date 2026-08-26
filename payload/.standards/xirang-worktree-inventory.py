#!/usr/bin/env python3
"""Read-only Git worktree inventory. This command never repairs or removes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def git(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    allowed = {("rev-parse", "--show-toplevel"), ("rev-parse", "--git-common-dir"), ("worktree", "list", "--porcelain")}
    if tuple(args) not in allowed and tuple(args[:2]) != ("status", "--porcelain=v2"):
        raise ValueError(f"non-read-only git command rejected: {' '.join(args)}")
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=False,
        timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def parse_worktrees(raw: str) -> list[dict]:
    rows: list[dict] = []
    for block in raw.strip().split("\n\n") if raw.strip() else []:
        row: dict[str, object] = {"locked": False, "detached": False, "bare": False}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key in {"locked", "detached", "bare"}:
                row[key] = True
                if value:
                    row[f"{key}_reason"] = value
            elif key == "prunable":
                row["prunable_hint"] = True
                row["prunable_reason"] = value or None
            else:
                row[key] = value
        rows.append(row)
    return rows


def status_counts(raw: str) -> dict[str, object]:
    counts = {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0, "submodule_changes": 0}
    for line in raw.splitlines():
        if line.startswith("? "):
            counts["untracked"] += 1
        elif line.startswith("u "):
            counts["conflicted"] += 1
        elif line.startswith(("1 ", "2 ")):
            parts = line.split(" ", 3)
            xy = parts[1] if len(parts) > 1 else ".."
            counts["staged"] += int(xy[0] not in {".", " "})
            counts["unstaged"] += int(len(xy) > 1 and xy[1] not in {".", " "})
            counts["submodule_changes"] += int(len(parts) > 2 and parts[2] != "N...")
    counts["clean"] = not any(counts.values())
    return counts


def classify(item: dict, status: dict[str, object] | None, is_main: bool) -> tuple[str, list[str]]:
    if is_main:
        return "protected_main", ["main_worktree_never_cleanup_candidate"]
    if not item.get("path_exists"):
        return "registered_path_missing", ["registered_path_missing"]
    if item.get("locked"):
        return "locked", ["worktree_locked"]
    if status is None:
        return "status_unknown", ["status_not_observed"]
    dirty = [key for key in ("staged", "unstaged", "untracked", "conflicted", "submodule_changes") if status.get(key)]
    if not dirty:
        return "clean_idle_candidate_for_review", ["no_changes_observed", "release_not_authorized"]
    if "conflicted" in dirty:
        return "conflicted", [f"{key}_changes_present" for key in dirty]
    return (f"dirty_{dirty[0]}" if len(dirty) == 1 else "dirty_mixed", [f"{key}_changes_present" for key in dirty])


def inventory(repo: Path, timeout: int = 10) -> tuple[dict, int]:
    requested = repo.expanduser().resolve()
    top = git(requested, "rev-parse", "--show-toplevel", timeout=timeout)
    if top.returncode != 0:
        raise ValueError("目标不是可读取的 Git 仓库")
    root = Path(top.stdout.strip()).resolve()
    common = git(root, "rev-parse", "--git-common-dir", timeout=timeout)
    listed = git(root, "worktree", "list", "--porcelain", timeout=timeout)
    if common.returncode or listed.returncode:
        raise RuntimeError((common.stderr or listed.stderr).strip())
    common_path = (root / common.stdout.strip()).resolve()
    rows = parse_worktrees(listed.stdout)
    output: list[dict] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        path = Path(str(row.get("worktree", ""))).expanduser()
        canonical = path.resolve(strict=False)
        observed = None
        if path.exists():
            proc = git(path, "status", "--porcelain=v2", "--untracked-files=all", timeout=timeout)
            if proc.returncode == 0:
                observed = status_counts(proc.stdout)
            else:
                errors.append(f"status failed: {canonical}")
        item = {
            "resource_key": hashlib.sha256(f"{common_path}:{canonical}".encode()).hexdigest()[:20],
            "path": str(path), "canonical_path": str(canonical), "path_exists": path.exists(),
            "git_registered": True, "is_main_worktree": index == 0, "head": row.get("HEAD"),
            "branch_ref": row.get("branch"), "detached": bool(row.get("detached")),
            "bare": bool(row.get("bare")), "locked": bool(row.get("locked")),
            "lock_reason": row.get("locked_reason"), "prunable_hint": bool(row.get("prunable_hint")),
            "prunable_reason": row.get("prunable_reason"), "status_observed": observed is not None,
            "status": observed, "uncertainties": [] if observed is not None else ["status_unavailable"],
        }
        item["classification"], item["classification_reasons"] = classify(item, observed, index == 0)
        output.append(item)
    summary: dict[str, int] = {"total": len(output)}
    for item in output:
        key = str(item["classification"])
        summary[key] = summary.get(key, 0) + 1
    report = {
        "schema_version": 1, "inventory_id": hashlib.sha256(f"{root}:{datetime.now().timestamp()}".encode()).hexdigest()[:20],
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tool_version": "1.0", "mode": "read_only",
        "repository": {"requested_path": str(requested), "canonical_root": str(root), "common_git_dir": str(common_path), "main_worktree": str(root)},
        "summary": summary, "worktrees": output, "scan_errors": errors, "complete": not errors,
    }
    return report, 0 if not errors else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="只读盘点 Git worktree；不会清理或修复")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=10)
    args = parser.parse_args()
    try:
        report, code = inventory(args.repo, args.timeout_seconds)
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
        return code
    except Exception as exc:
        print(json.dumps({"ok": False, "mode": "read_only", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
