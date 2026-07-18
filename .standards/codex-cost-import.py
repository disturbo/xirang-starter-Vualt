#!/usr/bin/env python3
"""Incrementally import Codex Desktop rollout token/model metadata into V9."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


HOME = Path.home()
CODEX_SESSIONS = HOME / ".codex/sessions"
DEFAULT_STATE = HOME / ".xirang/v9-runtime/成本/codex-cost-import-state.json"
VAULT = Path(os.environ.get(
    "VAULT_ROOT", str(Path(__file__).resolve().parent.parent),
)).expanduser().resolve()
COST_TOOL = VAULT / ".standards/agent-cost-events.py"
STATUS = VAULT / "02-项目管理/智能体状态/红霉素.md"
USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".codex-cost-", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def find_rollout(session_id: str, sessions_root: Path) -> Path | None:
    candidates = list(sessions_root.glob(f"**/*{session_id}.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def rollout_metadata(path: Path) -> tuple[str, dict[str, int], str]:
    model = "unknown"
    latest: dict[str, int] = {}
    usage_timestamp = ""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if row.get("type") == "turn_context" and payload.get("model"):
                model = str(payload["model"])
            if row.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                if usage:
                    latest = {key: int(usage.get(key) or 0) for key in USAGE_KEYS}
                    usage_timestamp = str(row.get("timestamp") or "")
    return model, latest, usage_timestamp


def active_task_id(status_path: Path) -> str:
    try:
        text = status_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    status = re.search(r"(?m)^status:\s*['\"]?([^'\"\n]+)", text)
    task = re.search(r"(?m)^current_task_id:\s*['\"]?([^'\"\n]+)", text)
    if status and status.group(1).strip() == "busy" and task:
        value = task.group(1).strip()
        return "" if value in {"", "null", "none"} else value
    return ""


def delta_usage(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    delta: dict[str, int] = {}
    reset = current.get("total_tokens", 0) < previous.get("total_tokens", 0)
    for key in USAGE_KEYS:
        delta[key] = current.get(key, 0) if reset else max(0, current.get(key, 0) - previous.get(key, 0))
    return delta


def append_event(task_id: str, model: str, delta: dict[str, int], session_id: str, timestamp: str) -> subprocess.CompletedProcess[str]:
    command = [
        "/usr/bin/python3", str(COST_TOOL), "append",
        "--task-id", task_id, "--agent", "hongmeisu", "--model", model,
        "--input-tokens", str(delta["input_tokens"]),
        "--output-tokens", str(delta["output_tokens"]),
        "--cached-input-tokens", str(delta["cached_input_tokens"]),
        "--reasoning-output-tokens", str(delta["reasoning_output_tokens"]),
        "--cost-cny", "0", "--phase", "execution",
        "--source", "codex_rollout_token_count", "--usage-source", "codex_rollout",
        "--billing-status", "usage_only", "--note", f"session_id={session_id}", "--json",
    ]
    if timestamp:
        command.extend(["--ts", timestamp])
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--sessions-root", type=Path, default=CODEX_SESSIONS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--status", type=Path, default=STATUS)
    parser.add_argument("--task-id")
    parser.add_argument("--initialize", action="store_true", help="Record current cumulative usage without appending.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rollout = find_rollout(args.session_id, args.sessions_root)
    if rollout is None:
        result = {"status": "failed", "reason": "rollout_missing", "session_id": args.session_id}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    model, current, timestamp = rollout_metadata(rollout)
    if not current or model == "unknown":
        result = {"status": "failed", "reason": "usage_or_model_missing", "session_id": args.session_id}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    args.state.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(args.state)
        sessions = state.setdefault("sessions", {})
        previous = sessions.get(args.session_id, {}) if isinstance(sessions.get(args.session_id), dict) else {}
        prior_usage = previous.get("usage", {}) if isinstance(previous.get("usage"), dict) else {}
        task_id = args.task_id or active_task_id(args.status) or str(previous.get("last_task_id") or "unknown")
        delta = delta_usage(current, prior_usage)
        appended = False
        if not args.initialize and delta.get("total_tokens", 0) > 0:
            proc = append_event(task_id, model, delta, args.session_id, timestamp)
            if proc.returncode != 0:
                result = {"status": "failed", "reason": "append_failed", "detail": (proc.stderr or proc.stdout)[-1000:]}
                print(json.dumps(result, ensure_ascii=False))
                return 1
            appended = True
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        sessions[args.session_id] = {
            "rollout": str(rollout), "model": model, "usage": current,
            "usage_timestamp": timestamp, "last_task_id": task_id, "updated_at": now,
        }
        state.update({"schema_version": 1, "updated_at": now, "source": "codex_rollout_token_count"})
        atomic_write(args.state, state)

    result = {
        "status": "success", "session_id": args.session_id, "rollout": str(rollout),
        "task_id": task_id, "model": model, "initialized": args.initialize,
        "appended": appended, "delta": delta, "state": str(args.state),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
