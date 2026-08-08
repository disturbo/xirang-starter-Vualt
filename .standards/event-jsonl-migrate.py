#!/usr/bin/env python3
"""Deterministically repair known historical V9 event JSONL corruption.

Supported legacy forms:
1. ``<line-number>|<valid-json-object>`` prefixes.
2. A file_write object split across two physical lines by the exact historical
   ``agent == 'claudian\nclaudian'`` bug; the canonical agent becomes claudian.

Unknown malformed input aborts the whole migration. Apply mode writes an exact
backup plus manifest before atomically replacing the source file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


TZ = timezone(timedelta(hours=8))
DEFAULT_VAULT = Path.home() / "Desktop" / "obsidianVault"
DEFAULT_EVENT_FILE = DEFAULT_VAULT / "02-项目管理/智能体状态/智能体事件.jsonl"
DEFAULT_BACKUP_DIR = DEFAULT_VAULT / "02-项目管理/事件归档"
NUMBER_PIPE_RE = re.compile(r"^\s*\d+\|(.*)$")


@dataclass(frozen=True)
class MigrationStats:
    physical_lines_before: int
    semantic_events_after: int
    valid_unchanged: int
    number_pipe_repaired: int
    split_agent_pairs_repaired: int
    unknown_invalid_rows: int
    invalid_rows_after: int
    source_sha256: str
    output_sha256: str
    changed: bool


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_from_line(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def migrate_bytes(raw: bytes) -> tuple[bytes, MigrationStats]:
    text = raw.decode("utf-8")
    lines = text.splitlines()
    output: list[str] = []
    valid = prefixed = split_pairs = unknown = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        parsed = object_from_line(line)
        if parsed is not None:
            output.append(line)
            valid += 1
            i += 1
            continue

        match = NUMBER_PIPE_RE.match(line)
        if match:
            payload = match.group(1)
            if object_from_line(payload) is None:
                unknown += 1
                i += 1
                continue
            output.append(payload)
            prefixed += 1
            i += 1
            continue

        if i + 1 < len(lines):
            # Historical corruption inserted a physical newline inside a JSON
            # string. Reconstruct it as the JSON escape sequence before parse.
            joined = line + "\\n" + lines[i + 1]
            try:
                candidate = json.loads(joined)
            except (json.JSONDecodeError, ValueError):
                candidate = None
            if (
                isinstance(candidate, dict)
                and candidate.get("event") == "file_write"
                and candidate.get("agent") == "claudian\nclaudian"
            ):
                candidate["agent"] = "claudian"
                output.append(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
                split_pairs += 1
                i += 2
                continue

        unknown += 1
        i += 1

    if unknown:
        raise ValueError(f"migration aborted: {unknown} unknown invalid physical rows")

    output_bytes = (("\n".join(output) + "\n") if output else "").encode("utf-8")
    invalid_after = sum(1 for line in output if object_from_line(line) is None)
    stats = MigrationStats(
        physical_lines_before=len(lines),
        semantic_events_after=len(output),
        valid_unchanged=valid,
        number_pipe_repaired=prefixed,
        split_agent_pairs_repaired=split_pairs,
        unknown_invalid_rows=unknown,
        invalid_rows_after=invalid_after,
        source_sha256=sha256_bytes(raw),
        output_sha256=sha256_bytes(output_bytes),
        changed=raw != output_bytes,
    )
    return output_bytes, stats


def atomic_write(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=".event-migrate-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def apply_migration(path: Path, backup_dir: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    output, stats = migrate_bytes(raw)
    result: dict[str, Any] = {"mode": "apply", "stats": asdict(stats)}
    if not stats.changed:
        result.update({"applied": False, "reason": "already canonical"})
        return result

    timestamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S%z")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"智能体事件-pre-v9-migration-{timestamp}-{stats.source_sha256[:12]}.jsonl"
    manifest = backup.with_suffix(".manifest.json")
    with backup.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    manifest_payload = {
        "schema": "v9-event-migration-backup/v1",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source_path": str(path),
        "backup_path": str(backup),
        "source_sha256": stats.source_sha256,
        "output_sha256": stats.output_sha256,
        "stats": asdict(stats),
        "rollback_note": "Restore this exact backup only after verifying source_sha256.",
    }
    manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if sha256_bytes(backup.read_bytes()) != stats.source_sha256:
        raise RuntimeError("backup hash verification failed; source was not replaced")

    atomic_write(path, output)
    written = path.read_bytes()
    if sha256_bytes(written) != stats.output_sha256:
        raise RuntimeError("post-write hash verification failed")
    result.update({
        "applied": True,
        "backup_path": str(backup),
        "manifest_path": str(manifest),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_EVENT_FILE)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.apply:
        result = apply_migration(args.path.resolve(), args.backup_dir.resolve())
    else:
        _, stats = migrate_bytes(args.path.resolve().read_bytes())
        result = {"mode": "dry_run", "stats": asdict(stats), "applied": False}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"mode={result['mode']} applied={result['applied']} "
            f"stats={result['stats']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
