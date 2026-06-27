#!/usr/bin/env python3
"""v9-accept.py — V9.4.1 task acceptance command.

Implements the safe path for review_status=submitted -> accepted:
  1. read the current task card;
  2. build an accepted candidate in a temporary file;
  3. run gate-enforce pre-accept on the candidate;
  4. atomically replace the task card only after the gate passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
TASKS_DIR = VAULT_ROOT / "02-项目管理" / "任务卡"
GATE_ENFORCE = VAULT_ROOT / ".standards" / "gate-enforce.py"
LATEST_EVAL = VAULT_ROOT / "02-项目管理" / "巡检" / "harness-eval-latest.json"
TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("task card missing frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("task card frontmatter not closed")
    return text[4:end], text[end:]


def fm_value(fm: str, key: str) -> str:
    prefix = f"{key}:"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return clean_value(line[len(prefix):])
    return ""


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_frontmatter_fields(text: str, updates: dict[str, str]) -> str:
    fm, rest = split_frontmatter(text)
    seen: set[str] = set()
    lines: list[str] = []
    for line in fm.splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in updates and line.startswith(f"{key}:"):
            lines.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + rest


def find_task_card(task_id: str) -> Path:
    if not TASKS_DIR.exists():
        raise FileNotFoundError(f"task card directory not found: {TASKS_DIR}")
    matches = sorted(TASKS_DIR.rglob(f"{task_id}.md"))
    if not matches:
        raise FileNotFoundError(f"task card not found: {task_id}")
    if len(matches) > 1:
        raise RuntimeError(f"multiple task cards found for {task_id}: {[str(p) for p in matches[:3]]}")
    return matches[0]


def requires_fresh_eval(text: str) -> bool:
    return ".standards/" in text or "02-项目管理/脚本/" in text


def run_pre_accept(candidate: Path, task_id: str, require_fresh_eval: bool) -> tuple[int, dict | None, str]:
    cmd = [sys.executable, str(GATE_ENFORCE), "pre-accept", "--candidate", str(candidate), "--task-id", task_id, "--json"]
    if require_fresh_eval:
        cmd.extend(["--require-fresh-eval", "--eval-report", str(LATEST_EVAL)])
    proc = subprocess.run(cmd, cwd=str(VAULT_ROOT), capture_output=True, text=True, timeout=60)
    data = None
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None
    return proc.returncode, data, proc.stderr


def atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def build_candidate(text: str, accepted_by: str, reviewer: str | None) -> str:
    fm, _ = split_frontmatter(text)
    reviewer_value = fm_value(fm, "reviewer")
    updates = {
        "review_status": "accepted",
        "accepted_by": yaml_scalar(accepted_by),
        "accepted_at": yaml_scalar(now_iso()),
        "acceptance_result": "accepted",
        "updated_at": yaml_scalar(now_iso()),
    }
    if not reviewer_value or reviewer_value in {"null", "none", "None", "~"}:
        updates["reviewer"] = yaml_scalar(reviewer or accepted_by)
    return set_frontmatter_fields(text, updates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely accept a V9 task card.")
    parser.add_argument("task_id")
    parser.add_argument("accepted_by")
    parser.add_argument("--reviewer", help="填补缺失 reviewer；默认使用 accepted_by")
    parser.add_argument("--require-fresh-eval", action="store_true", help="强制要求 harness eval 最新")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        card = find_task_card(args.task_id)
        old_text = card.read_text(encoding="utf-8")
        candidate_text = build_candidate(old_text, args.accepted_by, args.reviewer)
        need_eval = args.require_fresh_eval or requires_fresh_eval(old_text)

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(candidate_text)
            candidate = Path(tf.name)
        try:
            gate_code, gate_report, gate_stderr = run_pre_accept(candidate, args.task_id, need_eval)
        finally:
            candidate.unlink(missing_ok=True)

        if gate_code != 0:
            result = {
                "ok": False,
                "task_id": args.task_id,
                "card": str(card),
                "reason": "pre_accept_failed",
                "gate": gate_report,
                "stderr": gate_stderr,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"[v9_accept] blocked: {result['reason']}")
            return 1

        if not args.dry_run:
            atomic_write(card, candidate_text)
        result = {
            "ok": True,
            "task_id": args.task_id,
            "card": str(card),
            "accepted_by": args.accepted_by,
            "dry_run": args.dry_run,
            "fresh_eval_checked": need_eval,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"[v9_accept] accepted: {args.task_id} by {args.accepted_by}")
        return 0
    except Exception as exc:
        result = {"ok": False, "task_id": args.task_id, "reason": type(exc).__name__, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"[v9_accept] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
