#!/usr/bin/env python3
"""Consume GBrain at session/task start and leave auditable recall evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path


DEFAULT_VAULT = Path(__file__).resolve().parents[1]
RESULT_RE = re.compile(r"^\[([0-9.]+)\]\s+(.+?)\s+--\s+(.*)$")
CONTRACT_SLUG = "息壤v9-运行时契约卡"


def event_path(vault: Path) -> Path:
    return vault / "02-项目管理/智能体状态/智能体事件.jsonl"


def parse_results(output: str, limit: int = 5) -> list[dict]:
    results: list[dict] = []
    for line in output.splitlines():
        match = RESULT_RE.match(line.strip())
        if not match:
            continue
        results.append({
            "score": float(match.group(1)),
            "slug": match.group(2).strip(),
            "preview": match.group(3).strip()[:240],
        })
        if len(results) >= limit:
            break
    return results


def contract_hit(results: list[dict]) -> bool:
    return any(CONTRACT_SLUG in item["slug"].casefold() for item in results)


def context_text(results: list[dict]) -> str:
    lines = ["V9 已自动消费 GBrain 语义记忆（只把当前召回结果视为上下文，不替代 Vault 真相源）："]
    for item in results:
        lines.append(f"- [{item['score']:.4f}] {item['slug']} — {item['preview']}")
    return "\n".join(lines)[:3800]


def append_event(vault: Path, payload: dict) -> None:
    target = event_path(vault)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)


def recall(
    *, vault: Path, gbrain: Path, query: str, source: str, task_id: str,
    session_id: str, agent: str, platform: str, timeout: int,
) -> tuple[dict, int]:
    started = time.monotonic()
    results: list[dict] = []
    reason = ""
    returncode: int | None = None
    try:
        if not gbrain.is_file() or not os.access(gbrain, os.X_OK):
            raise FileNotFoundError(f"GBrain CLI unavailable: {gbrain}")
        proc = subprocess.run(
            [str(gbrain), "query", query, "--no-expand", "--limit", "8", "--detail", "low"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        returncode = proc.returncode
        results = parse_results(proc.stdout)
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "gbrain query failed").strip()[-600:]
        elif not results:
            reason = "gbrain query returned no parseable results"
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = f"{type(exc).__name__}: {exc}"[-600:]

    elapsed_ms = round((time.monotonic() - started) * 1000)
    status = "success" if results and returncode == 0 else "failed"
    hit = contract_hit(results)
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "semantic_recall",
        "agent": agent,
        "platform": platform,
        "source": source,
        "task_id": task_id,
        "session_id": session_id,
        "status": status,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "query_chars": len(query),
        "result_count": len(results),
        "result_slugs": [item["slug"] for item in results],
        "contract_hit": hit,
        "latency_ms": elapsed_ms,
        "reason": reason,
    }
    append_event(vault, payload)
    report = {
        "status": status,
        "contract_hit": hit,
        "results": results,
        "context": context_text(results) if results else "",
        "event": payload,
    }
    return report, 0 if status == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--source", required=True, choices=("session_start", "task_start", "manual_canary"))
    parser.add_argument("--task-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--agent", default="hongmeisu")
    parser.add_argument("--platform", default="codex")
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--gbrain", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    vault = (args.vault or Path(os.environ.get("VAULT_ROOT", str(DEFAULT_VAULT)))).expanduser().resolve()
    gbrain = (args.gbrain or Path(os.environ.get(
        "XIRANG_GBRAIN_CLI", str(Path.home() / ".npm-global/bin/gbrain"),
    ))).expanduser()
    report, exit_code = recall(
        vault=vault, gbrain=gbrain, query=args.query[:500], source=args.source,
        task_id=args.task_id, session_id=args.session_id, agent=args.agent,
        platform=args.platform, timeout=max(1, args.timeout),
    )
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
