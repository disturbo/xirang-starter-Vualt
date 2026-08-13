#!/usr/bin/env python3
"""Phoenix v1 — bounded self-heal executor and evolution candidate generator.

Phoenix consumes the latest reflex findings. It may execute only fixed, audited
actions for known runtime-liveness failures. It never edits source notes,
accepts tasks, weakens gates, changes the release manifest, or activates its own
upgrade candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


CHECK_NAME = "v9-phoenix"
SCHEMA_VERSION = "v1"
OBSERVATION_SCHEMA_VERSION = "v2"
ROOT = Path(__file__).resolve().parents[2]
UPGRADE_THRESHOLD = 3
TRUSTED_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
TRUSTED_PYTHON = Path(sys.executable).resolve()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def runtime_root() -> Path:
    return Path(os.environ.get("XIRANG_V9_RUNTIME_DIR", "~/.xirang/v9-runtime")).expanduser()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execution_policy() -> dict:
    catalog = {}
    for action_id, contract in action_catalog().items():
        artifacts = []
        for part in contract["command"]:
            path = Path(part)
            if not path.is_absolute() or not path.is_file():
                continue
            resolved = path.resolve()
            artifacts.append({
                "path": str(resolved),
                "sha256": sha256_file(resolved),
                "executable": os.access(resolved, os.X_OK),
            })
        catalog[action_id] = {
            "command": contract["command"],
            "rules": sorted(contract["rules"]),
            "artifacts": artifacts,
        }
    return {
        "environment_overrides_allowed": False,
        "trusted_home": str(TRUSTED_HOME),
        "catalog": catalog,
    }


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def action_catalog() -> dict[str, dict]:
    return {
        "refresh_entropy": {
            "rules": {"ENTROPY_JOB_STATE_INVALID", "ENTROPY_SHADOW_MISSING", "ENTROPY_SHADOW_STALE"},
            "command": [
                str(TRUSTED_PYTHON),
                str(TRUSTED_HOME / ".hermes/scripts/v9-entropy-shadow.py"),
            ],
            "timeout": 1800,
        },
        "refresh_gbrain": {
            "rules": {
                "GBRAIN_SYNC_NEVER_SUCCEEDED",
                "GBRAIN_SYNC_STALE",
                "GBRAIN_CURRENT_REVISION_NOT_CONSUMED",
            },
            "command": [
                str(TRUSTED_HOME / ".gbrain/maintenance-run.sh"),
                "sync",
            ],
            "timeout": 300,
        },
    }


def repair_actions(findings: list[dict]) -> list[dict]:
    rule_ids = {str(item.get("rule_id", "")) for item in findings}
    actions = []
    for action_id, contract in action_catalog().items():
        matched = sorted(rule_ids & contract["rules"])
        if matched:
            actions.append({"action_id": action_id, "matched_rules": matched, **contract})
    return actions


def update_observations(
    previous: dict, findings: list[dict], observed_at: str, health_generated_at: str = "",
) -> tuple[dict, list[dict]]:
    observations = previous.get("observations") if isinstance(previous.get("observations"), dict) else {}
    seen_this_run: set[str] = set()
    for finding in findings:
        rule_id = str(finding.get("rule_id", "")).strip()
        if not rule_id or rule_id in seen_this_run:
            continue
        seen_this_run.add(rule_id)
        current = observations.get(rule_id) if isinstance(observations.get(rule_id), dict) else {}
        # Count distinct failure episodes, not scheduler polls of one unresolved
        # snapshot. A rule becomes a new episode only after it disappeared.
        legacy_observation = bool(current) and "active" not in current
        was_active = current.get("active") is True
        count = 1 if legacy_observation else int(current.get("count", 0) or 0) + (0 if was_active else 1)
        observations[rule_id] = {
            "rule_id": rule_id,
            "count": count,
            "first_seen": current.get("first_seen") or observed_at,
            "last_seen": observed_at,
            "last_health_generated_at": health_generated_at or None,
            "active": True,
            "severity": finding.get("severity"),
            "source": finding.get("source"),
            "object": finding.get("object"),
        }

    for rule_id, current in observations.items():
        if rule_id not in seen_this_run and isinstance(current, dict):
            current["active"] = False
            current["cleared_at"] = observed_at

    candidates = []
    auto_rules = {rule for action in action_catalog().values() for rule in action["rules"]}
    for rule_id, item in sorted(observations.items()):
        if (
            item.get("active") is not True
            or int(item.get("count", 0) or 0) < UPGRADE_THRESHOLD
            or rule_id in auto_rules
        ):
            continue
        candidate_id = "phoenix-upgrade-" + hashlib.sha256(rule_id.encode()).hexdigest()[:12]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "rule_id": rule_id,
                "status": "proposed",
                "requires_human_review": True,
                "activation": "forbidden_without_external_acceptance",
                "evidence": item,
                "proposal": "为该重复故障设计确定性检测、白名单修复动作与负向回归；通过独立验收后方可进入抗体库。",
            }
        )
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "updated_at": observed_at,
        "observations": observations,
    }, candidates


def run_action(action: dict, apply_safe: bool) -> dict:
    base = {
        "action_id": action["action_id"],
        "matched_rules": action["matched_rules"],
        "command": action["command"],
    }
    if not apply_safe:
        return {**base, "status": "planned", "returncode": None}
    try:
        proc = subprocess.run(
            action["command"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=action["timeout"],
            env={
                "HOME": str(TRUSTED_HOME),
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
            },
        )
        return {
            **base,
            "status": "applied" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-1000:],
            "stderr_tail": proc.stderr[-1000:],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {**base, "status": "failed", "returncode": None, "error": f"{type(exc).__name__}: {exc}"}


def build_report(health_path: Path, apply_safe: bool) -> dict:
    generated_at = now_iso()
    health = read_json(health_path)
    findings = health.get("findings") if isinstance(health.get("findings"), list) else []
    actions = [run_action(action, apply_safe) for action in repair_actions(findings)]
    runtime = runtime_root()
    observation_path = runtime / "治理/phoenix-observations.json"
    previous_observations = read_json(observation_path)
    if previous_observations.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        previous_observations = {}
    observation_state, candidates = update_observations(
        previous_observations, findings, generated_at, str(health.get("generated_at", "")),
    )
    atomic_write_json(observation_path, observation_state)
    atomic_write_json(
        runtime / "治理/phoenix-upgrade-candidates.json",
        {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "updated_at": generated_at,
            "policy": "proposal_only; human_acceptance_required; phoenix_cannot_activate_own_upgrade",
            "threshold": UPGRADE_THRESHOLD,
            "candidates": candidates,
        },
    )
    applied = sum(item["status"] == "applied" for item in actions)
    failed = sum(item["status"] == "failed" for item in actions)
    return {
        "schema_version": SCHEMA_VERSION,
        "check": CHECK_NAME,
        "generated_at": generated_at,
        "status": "degraded" if failed else "success",
        "mode": "apply_safe" if apply_safe else "plan",
        "health_path": str(health_path),
        "health_generated_at": health.get("generated_at"),
        "findings_observed": len(findings),
        "repair_actions": actions,
        "repairs_applied": applied,
        "repairs_failed": failed,
        "upgrade_candidates": len(candidates),
        "execution_policy": execution_policy(),
        "safety": {
            "source_note_edits": False,
            "gate_changes": False,
            "self_acceptance": False,
            "manifest_changes": False,
        },
    }


def main() -> int:
    runtime = runtime_root()
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", type=Path, default=runtime / "巡检/health-latest.json")
    parser.add_argument("--apply-safe", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(args.health.expanduser(), args.apply_safe)
    latest = runtime / "巡检/phoenix-latest.json"
    atomic_write_json(latest, report)
    append_jsonl(runtime / "治理/phoenix-events.jsonl", report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Phoenix {report['status']}: applied={report['repairs_applied']} "
            f"failed={report['repairs_failed']} candidates={report['upgrade_candidates']}"
        )
    return 1 if report["status"] == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
