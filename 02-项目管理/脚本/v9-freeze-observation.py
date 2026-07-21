#!/usr/bin/env python3
"""Measure the V9 stabilization freeze with reproducible runtime evidence.

The probe is read-only for Vault/distribution inputs. Its only write is the
daily observation history under ~/.xirang/v9-runtime/治理/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = Path.home() / ".xirang/v9-runtime"
DEFAULT_MANIFEST = ROOT / "02-项目管理/巡检/v9-release-manifest.json"
DEFAULT_OUTPUT = DEFAULT_RUNTIME / "治理/freeze-observation.json"


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return observed if observed.tzinfo else observed.astimezone()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".freeze-observation-", dir=path.parent, text=True)
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


def metric(passed: bool, detail: object) -> dict:
    return {"status": "pass" if passed else "fail", "detail": detail}


def read_events(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except OSError:
        pass
    return rows


def check_identity(events: list[dict], freeze_start: datetime | None) -> dict:
    relevant = []
    for row in events:
        observed = parse_iso(row.get("ts"))
        if row.get("platform") != "codex" or (freeze_start and observed and observed < freeze_start):
            continue
        relevant.append(row)
    mismatches = [row for row in relevant if row.get("agent") != "hongmeisu"]
    return metric(not mismatches, {"codex_events": len(relevant), "mismatches": len(mismatches)})


def check_harness(root: Path, report: Path) -> dict:
    verifier = root / ".standards/harness-eval-verify.py"
    try:
        proc = subprocess.run(
            ["/usr/bin/python3", str(verifier), "--report", str(report), "--root", str(root), "--json"],
            capture_output=True, text=True, timeout=90, check=False,
        )
        payload = json.loads(proc.stdout) if proc.stdout else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return metric(False, {"error": str(exc)})
    return metric(proc.returncode == 0 and payload.get("valid") is True, {
        "valid": payload.get("valid"), "verified_files": payload.get("verified_files"),
        "reasons": payload.get("reasons", []),
    })


def check_hook_evidence(events: list[dict], hooks: dict, manifest: dict) -> dict:
    config_text = json.dumps(hooks, ensure_ascii=False)
    configured = all(value in config_text for value in ("apply_patch", "exec_command", "pre-exec", "post-exec"))
    evidence = manifest.get("hook_evidence") if isinstance(manifest.get("hook_evidence"), dict) else {}
    freeze_start = parse_iso(manifest.get("freeze_started_at"))

    def evidence_floor(value: object) -> datetime | None:
        explicit = parse_iso(value)
        candidates = [item for item in (explicit, freeze_start) if item is not None]
        return max(candidates) if candidates else None

    file_after = evidence_floor(evidence.get("file_write_after"))
    deny_after = evidence_floor(evidence.get("shell_denied_after"))

    def has_event(name: str, after: datetime | None) -> bool:
        return any(
            row.get("event") == name and row.get("platform") == "codex" and row.get("agent") == "hongmeisu"
            and (after is None or (parse_iso(row.get("ts")) is not None and parse_iso(row.get("ts")) >= after))
            for row in events
        )

    file_write = has_event("file_write", file_after)
    denied = has_event("shell_command_denied", deny_after)
    return metric(configured and file_write and denied, {
        "configured": configured, "file_write_observed": file_write, "deny_observed": denied,
        "file_write_after": file_after.isoformat() if file_after else None,
        "shell_denied_after": deny_after.isoformat() if deny_after else None,
    })


def check_consumption(runtime: Path, now: datetime) -> dict:
    inspect = runtime / "巡检"
    queue = load_json(runtime / "治理/entropy-governance-queue.json")
    health = load_json(inspect / "health-latest.json")
    status = load_json(inspect / "status-latest.json")
    report = load_json(inspect / "harness-eval-latest.json")
    timestamps = {
        "entropy_queue": parse_iso(queue.get("updated_at")),
        "health": parse_iso(health.get("generated_at")),
        "status": parse_iso(status.get("generated_at")),
        "harness": parse_iso(report.get("generated_at")),
    }
    missing = [key for key, value in timestamps.items() if value is None]
    ordered = not missing and timestamps["entropy_queue"] <= timestamps["health"] <= timestamps["status"]
    harness_consumed = not missing and timestamps["harness"] <= timestamps["status"]
    max_ages = {
        "entropy_queue": timedelta(days=8),
        "health": timedelta(hours=24),
        "status": timedelta(hours=24),
        "harness": timedelta(hours=24),
    }
    freshness = {}
    for key, value in timestamps.items():
        if value is None:
            freshness[key] = {"fresh": False, "age_seconds": None, "max_age_seconds": int(max_ages[key].total_seconds())}
            continue
        age = now - value
        freshness[key] = {
            "fresh": -timedelta(minutes=5) <= age <= max_ages[key],
            "age_seconds": int(age.total_seconds()),
            "max_age_seconds": int(max_ages[key].total_seconds()),
        }
    fresh = not missing and all(item["fresh"] for item in freshness.values())
    verification = ((status.get("parts") or {}).get("harness_eval") or {}).get("verification") or {}
    verified = verification.get("valid") is True
    return metric(bool(ordered and harness_consumed and fresh and verified), {
        "missing": missing, "ordered": bool(ordered), "harness_consumed": bool(harness_consumed),
        "fresh": bool(fresh), "freshness": freshness, "harness_verified": verified,
    })


def git_head(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["/usr/bin/git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def check_distribution(manifest: dict) -> dict:
    roots = manifest.get("roots") if isinstance(manifest.get("roots"), dict) else {}
    releases = manifest.get("releases") if isinstance(manifest.get("releases"), list) else []
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    issues: list[str] = []
    for release in releases:
        if not isinstance(release, dict):
            issues.append("invalid_release")
            continue
        root = Path(str(roots.get(release.get("root"), ""))).expanduser()
        if git_head(root) != release.get("commit"):
            issues.append(f"commit:{release.get('name', release.get('root'))}")
    checked = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            issues.append("invalid_artifact")
            continue
        root = Path(str(roots.get(artifact.get("root"), ""))).expanduser()
        path = root / str(artifact.get("path", ""))
        checked += 1
        if not path.is_file():
            issues.append(f"missing:{artifact.get('root')}:{artifact.get('path')}")
        elif sha256(path) != artifact.get("sha256"):
            issues.append(f"hash:{artifact.get('root')}:{artifact.get('path')}")
    valid_shape = bool(releases and artifacts)
    return metric(valid_shape and not issues, {"releases": len(releases), "artifacts": checked, "issues": issues})


def consecutive_pass_days(observations: dict, today: datetime) -> int:
    count = 0
    cursor = today.date()
    while True:
        row = observations.get(cursor.isoformat())
        if not isinstance(row, dict) or row.get("daily_status") != "pass":
            return count
        count += 1
        cursor -= timedelta(days=1)


def observe(root: Path, runtime: Path, manifest_path: Path, output: Path, now: datetime) -> dict:
    manifest = load_json(manifest_path)
    events = read_events(root / "02-项目管理/智能体状态/智能体事件.jsonl")
    metrics = {
        "identity_misrecord": check_identity(events, parse_iso(manifest.get("freeze_started_at"))),
        "stale_report_green": check_harness(root, runtime / "巡检/harness-eval-latest.json"),
        "codex_hook_firing": check_hook_evidence(events, load_json(root / ".codex/hooks.json"), manifest),
        "output_consumption": check_consumption(runtime, now),
        "distribution_drift": check_distribution(manifest),
    }
    daily_pass = all(item["status"] == "pass" for item in metrics.values())
    history = load_json(output)
    observations = history.get("observations") if isinstance(history.get("observations"), dict) else {}
    observations[now.date().isoformat()] = {
        "observed_at": now.isoformat(timespec="seconds"),
        "daily_status": "pass" if daily_pass else "fail",
        "metrics": metrics,
    }
    required = int(manifest.get("required_consecutive_days", 14))
    streak = consecutive_pass_days(observations, now)
    payload = {
        "schema_version": 1,
        "check": "v9-freeze-observation",
        "updated_at": now.isoformat(timespec="seconds"),
        "freeze_started_at": manifest.get("freeze_started_at"),
        "required_consecutive_days": required,
        "consecutive_pass_days": streak,
        "unlock_allowed": daily_pass and streak >= required,
        "status": "eligible_to_unfreeze" if daily_pass and streak >= required else "observing" if daily_pass else "blocked",
        "today": observations[now.date().isoformat()],
        "observations": observations,
    }
    atomic_write(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    now = parse_iso(args.now) if args.now else datetime.now().astimezone()
    if now is None:
        parser.error("--now must be a valid ISO timestamp")
    payload = observe(args.root.resolve(), args.runtime.expanduser(), args.manifest, args.output, now)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"freeze observation: {payload['status']} {payload['consecutive_pass_days']}/{payload['required_consecutive_days']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
