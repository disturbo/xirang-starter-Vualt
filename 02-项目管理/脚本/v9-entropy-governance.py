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
UNRESOLVED = {"pending_confirmation", "confirmed_for_action", "deferred"}
AUTO_DEFER_CYCLE = 2
POLICY = "human_confirmation_with_default_defer; unresolved_findings_remain_backlog; source_notes_never_auto_modified"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def observation_period(timestamp: str) -> str:
    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    year, week, _ = observed.isocalendar()
    return f"{year}-W{week:02d}"


def finding_id(item: dict) -> str:
    signature = "\0".join(str(item.get(key, "")) for key in ("category", "source", "target"))
    return "entropy-" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def evidence_sha(item: dict) -> str:
    evidence = "\0".join(str(item.get(key, "")) for key in ("category", "source", "target", "reason"))
    return hashlib.sha256(evidence.encode()).hexdigest()


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
        "pending_confirmation", "confirmed_for_action", "deferred", "archived", "rejected", "resolved",
    )}
    # Deferred means "not claimed yet", not "resolved".  Keep it in the
    # visible backlog until the detector stops observing it or a human rejects it.
    current_open = counts["pending_confirmation"] + counts["confirmed_for_action"] + counts["deferred"]
    return {
        **counts,
        "previous_open": previous_open,
        "current_open": current_open,
        "new_since_previous": new,
        "resolved_since_previous": resolved,
        "net_backlog_delta": current_open - previous_open,
    }


def ingest(queue: dict, detector: dict, timestamp: str) -> dict:
    period = observation_period(timestamp)
    old_items = {
        item.get("id"): dict(item)
        for item in queue.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    previous_open_ids = {key for key, item in old_items.items() if item.get("status") in UNRESOLVED}
    current: dict[str, dict] = {}
    for raw in detector.get("findings", []):
        if not isinstance(raw, dict) or raw.get("confidence") != "confirmed":
            continue
        key = finding_id(raw)
        prior = old_items.get(key, {})
        fingerprint = evidence_sha(raw)
        same_evidence = prior.get("evidence_sha256") in {None, fingerprint}
        decision = prior.get("decision") if same_evidence and isinstance(prior.get("decision"), dict) else None
        if not same_evidence:
            cycle = 1
        elif prior.get("last_observation_period") == period:
            cycle = max(1, int(prior.get("unclaimed_cycles", 0)))
        else:
            cycle = int(prior.get("unclaimed_cycles", 0)) + 1
        if decision and decision.get("action") == "confirm":
            status = "confirmed_for_action"
        elif decision and decision.get("action") == "reject":
            status = "rejected"
        elif cycle >= AUTO_DEFER_CYCLE:
            status = "deferred"
        else:
            status = "pending_confirmation"
        current[key] = {
            "id": key,
            "category": str(raw.get("category", "")),
            "source": str(raw.get("source", "")),
            "target": str(raw.get("target", "")),
            "reason": str(raw.get("reason", "")),
            "evidence_sha256": fingerprint,
            "status": status,
            "first_seen_at": prior.get("first_seen_at", timestamp) if same_evidence else timestamp,
            "last_seen_at": timestamp,
            "last_observation_period": period,
            "unclaimed_cycles": cycle,
            "decision": decision,
        }
        if not same_evidence and prior:
            current[key]["reopened_at"] = timestamp
            current[key]["previous_evidence_sha256"] = prior.get("evidence_sha256")

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

    active_ids = {key for key, item in current.items() if item.get("status") in UNRESOLVED}
    new_ids = active_ids - previous_open_ids
    ordered = sorted(current.values(), key=lambda item: (item.get("status", ""), item.get("source", ""), item["id"]))
    return {
        "schema_version": 2,
        "updated_at": timestamp,
        "detector_version": detector.get("detector_version"),
        "detector_mode": detector.get("mode"),
        "source_report": detector.get("report_path"),
        "source_summary": detector.get("summary", {}),
        "policy": POLICY,
        "default_disposition": {
            "defer_after_unchanged_cycles": AUTO_DEFER_CYCLE,
            "archive_automatically": False,
            "reopen_when_evidence_changes": True,
        },
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
    action.add_argument("--status", action="store_true", help="Read queue metrics without modifying state.")
    parser.add_argument("--owner", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    timestamp = now_iso()
    queue = load_json(args.queue)
    if args.status:
        if not queue:
            raise SystemExit(f"queue not found or invalid: {args.queue}")
        payload = {"status": "success", "queue": str(args.queue), "metrics": queue.get("metrics", {})}
        print(json.dumps(payload, ensure_ascii=False) if args.json else json.dumps(payload["metrics"], ensure_ascii=False))
        return 0
    if args.input:
        queue = ingest(queue, read_input(args.input), timestamp)
    elif args.confirm or args.reject:
        if not args.owner.strip():
            parser.error("--owner is required for human decisions")
        queue = decide(queue, args.confirm or args.reject, "confirm" if args.confirm else "reject", args.owner, args.note, timestamp)
    else:
        parser.error("use --input, --confirm, --reject, or --status")

    atomic_write(args.queue, queue)
    if args.json:
        print(json.dumps({"status": "success", "queue": str(args.queue), "metrics": queue["metrics"]}, ensure_ascii=False))
    else:
        print(f"entropy governance queue: {args.queue}")
        print(json.dumps(queue["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
