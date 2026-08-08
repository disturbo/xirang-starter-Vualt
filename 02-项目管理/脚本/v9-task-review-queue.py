#!/usr/bin/env python3
"""Build a read-only evidence queue for submitted V9 task cards."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path


DEFAULT_ROOT = Path.home() / "Desktop" / "obsidianVault"
DEFAULT_OUTPUT = Path.home() / ".xirang" / "v9-runtime" / "治理" / "task-review-queue.json"
REVIEW_STATES = {"submitted", "reviewing"}


def clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    return text[4:end], text[end + 4:]


def top_level_fields(frontmatter: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match:
            fields[match.group(1)] = clean(match.group(2))
    return fields


def deliverable_paths(frontmatter: str) -> list[str]:
    paths: list[str] = []
    in_deliverables = False
    for line in frontmatter.splitlines():
        if line == "deliverables:":
            in_deliverables = True
            continue
        if in_deliverables and line and not line[:1].isspace():
            break
        if in_deliverables:
            match = re.match(r"^\s+-\s+path:\s*(.+?)\s*$", line)
            if match:
                paths.append(clean(match.group(1)))
    return paths


def resolve_deliverable(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path


def relocated_runtime_path(raw: str) -> Path | None:
    prefix = "02-项目管理/巡检/"
    if raw.startswith(prefix):
        return Path.home() / ".xirang" / "v9-runtime" / "巡检" / Path(raw).name
    return None


def inspect_card(root: Path, path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    fields = top_level_fields(fm)
    review_status = fields.get("review_status", "")
    if review_status not in REVIEW_STATES:
        return None

    declared = deliverable_paths(fm)
    existing: list[str] = []
    missing: list[str] = []
    external: list[str] = []
    relocated: list[dict[str, str]] = []
    for raw in declared:
        resolved = resolve_deliverable(root, raw)
        if resolved.is_absolute() and not resolved.is_relative_to(root):
            external.append(raw)
        if resolved.exists():
            existing.append(raw)
            continue
        runtime_path = relocated_runtime_path(raw)
        if runtime_path and runtime_path.exists():
            existing.append(raw)
            relocated.append({"declared": raw, "current": str(runtime_path)})
            continue
        missing.append(raw)

    if not declared:
        evidence_state = "no_declared_deliverables"
    elif missing:
        evidence_state = "missing_deliverables"
    else:
        evidence_state = "declared_deliverables_present"

    return {
        "task_id": fields.get("task_id", path.stem),
        "title": fields.get("title", path.stem),
        "owner": fields.get("owner", ""),
        "reviewer": fields.get("reviewer", ""),
        "review_status": review_status,
        "submitted_at": fields.get("submitted_at", ""),
        "completed_at": fields.get("completed_at", ""),
        "card": path.relative_to(root).as_posix(),
        "declared_deliverables": declared,
        "existing_deliverables": existing,
        "missing_deliverables": missing,
        "external_deliverables": external,
        "relocated_deliverables": relocated,
        "has_handoff_section": bool(re.search(r"^##\s+.*Handoff", body, re.MULTILINE | re.IGNORECASE)),
        "has_acceptance_section": bool(re.search(r"^##\s+.*(验收|Acceptance)", body, re.MULTILINE | re.IGNORECASE)),
        "evidence_state": evidence_state,
    }


def build_queue(root: Path) -> dict:
    cards = []
    for path in sorted((root / "02-项目管理" / "任务卡").glob("20??-??/T-*.md")):
        item = inspect_card(root, path)
        if item:
            cards.append(item)
    counts: dict[str, int] = {}
    for item in cards:
        state = item["evidence_state"]
        counts[state] = counts.get(state, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": {
            "awaiting_review": len(cards),
            "by_evidence_state": counts,
            "missing_deliverables": sum(len(item["missing_deliverables"]) for item in cards),
            "external_deliverables": sum(len(item["external_deliverables"]) for item in cards),
            "relocated_deliverables": sum(len(item["relocated_deliverables"]) for item in cards),
        },
        "disclaimer": "交付物存在不等于验收通过；本队列不写 accepted 状态。",
        "items": cards,
    }


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write-latest", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_queue(args.root.resolve())
    if args.write_latest:
        atomic_write_json(args.output.expanduser(), payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
