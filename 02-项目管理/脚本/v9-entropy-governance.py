#!/usr/bin/env python3
"""Consume confirmed entropy findings into a human-confirmation queue.

This script only writes governance state. It never edits, moves, or deletes Vault notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


DEFAULT_QUEUE = Path.home() / ".xirang/v9-runtime/治理/entropy-governance-queue.json"
OPEN = {"pending_confirmation", "confirmed_for_action"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def finding_id(item: dict) -> str:
    signature = "\0".join(str(item.get(key, "")) for key in ("category", "source", "target"))
    return "entropy-" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".entropy-queue-", dir=path.parent, text=True)
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


def read_input(value: str) -> dict:
    if value == "-":
        return json.load(__import__("sys").stdin)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def metrics(items: list[dict], *, previous_open: int, new: int, resolved: int) -> dict:
    counts = {status: sum(item.get("status") == status for item in items) for status in (
        "pending_confirmation", "confirmed_for_action", "rejected", "resolved",
    )}
    current_open = counts["pending_confirmation"] + counts["confirmed_for_action"]
    return {
        **counts,
        "previous_open": previous_open,
        "current_open": current_open,
        "new_since_previous": new,
        "resolved_since_previous": resolved,
        "net_backlog_delta": current_open - previous_open,
    }


def ingest(queue: dict, detector: dict, timestamp: str) -> dict:
    old_items = {
        item.get("id"): dict(item)
        for item in queue.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    previous_open_ids = {key for key, item in old_items.items() if item.get("status") in OPEN}
    current: dict[str, dict] = {}
    for raw in detector.get("findings", []):
        if not isinstance(raw, dict) or raw.get("confidence") != "confirmed":
            continue
        key = finding_id(raw)
        prior = old_items.get(key, {})
        decision = prior.get("decision") if isinstance(prior.get("decision"), dict) else None
        if decision and decision.get("action") == "confirm":
            status = "confirmed_for_action"
        elif decision and decision.get("action") == "reject":
            status = "rejected"
        else:
            status = "pending_confirmation"
        current[key] = {
            "id": key,
            "category": str(raw.get("category", "")),
            "source": str(raw.get("source", "")),
            "target": str(raw.get("target", "")),
            "reason": str(raw.get("reason", "")),
            "status": status,
            "first_seen_at": prior.get("first_seen_at", timestamp),
            "last_seen_at": timestamp,
            "decision": decision,
        }

    resolved_ids: set[str] = set()
    for key, prior in old_items.items():
        if key in current:
            continue
        item = dict(prior)
        if item.get("status") != "resolved":
            item["previous_status"] = item.get("status")
            item["status"] = "resolved"
            item["resolved_at"] = timestamp
            resolved_ids.add(key)
        current[key] = item

    active_ids = {key for key, item in current.items() if item.get("status") in OPEN}
    new_ids = active_ids - previous_open_ids
    ordered = sorted(current.values(), key=lambda item: (item.get("status", ""), item.get("source", ""), item["id"]))
    return {
        "schema_version": 1,
        "updated_at": timestamp,
        "detector_version": detector.get("detector_version"),
        "detector_mode": detector.get("mode"),
        "source_report": detector.get("report_path"),
        "source_summary": detector.get("summary", {}),
        "policy": "human_confirmation_required; source_notes_never_auto_modified",
        "metrics": metrics(
            ordered,
            previous_open=len(previous_open_ids),
            new=len(new_ids),
            resolved=len(resolved_ids),
        ),
        "items": ordered,
    }


def decide(queue: dict, item_id: str, action: str, owner: str, note: str, timestamp: str) -> dict:
    found = False
    for item in queue.get("items", []):
        if item.get("id") != item_id:
            continue
        found = True
        item["decision"] = {"action": action, "owner": owner, "at": timestamp, "note": note}
        item["status"] = "confirmed_for_action" if action == "confirm" else "rejected"
    if not found:
        raise SystemExit(f"unknown queue item: {item_id}")
    items = queue.get("items", [])
    previous = int((queue.get("metrics") or {}).get("current_open", 0))
    queue["updated_at"] = timestamp
    queue["metrics"] = metrics(items, previous_open=previous, new=0, resolved=0)
    return queue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--input", help="Detector JSON file, or - for stdin.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--confirm", metavar="ITEM_ID")
    action.add_argument("--reject", metavar="ITEM_ID")
    parser.add_argument("--owner", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    timestamp = now_iso()
    queue = load_json(args.queue)
    if args.input:
        queue = ingest(queue, read_input(args.input), timestamp)
    elif args.confirm or args.reject:
        if not args.owner.strip():
            parser.error("--owner is required for human decisions")
        queue = decide(queue, args.confirm or args.reject, "confirm" if args.confirm else "reject", args.owner, args.note, timestamp)
    else:
        parser.error("use --input, --confirm, or --reject")

    atomic_write(args.queue, queue)
    if args.json:
        print(json.dumps({"status": "success", "queue": str(args.queue), "metrics": queue["metrics"]}, ensure_ascii=False))
    else:
        print(f"entropy governance queue: {args.queue}")
        print(json.dumps(queue["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
