#!/usr/bin/env python3
"""
v9-status-summary.py — V9.5 UI-facing status aggregator.

Scope:
  - Read health-latest.json and harness-eval-latest.json from the runtime dir.
  - Run v9-iteration-ops-check.py in read-only JSON mode for the current iteration.
  - Emit one stable status JSON for future Obsidian plugin / desktop UI.
  - Writes only to the Vault-external runtime dir when --write-latest is used.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CHECK_NAME = "v9-status-summary"
SCHEMA_VERSION = "v1"
STATUS_LATEST_NAME = "status-latest.json"
STATUS_LABELS = {
    "green": "正常",
    "yellow": "关注",
    "red": "异常",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def runtime_inspect_dir() -> Path:
    explicit = os.environ.get("XIRANG_V9_INSPECT_DIR")
    if explicit:
        return Path(explicit).expanduser()
    runtime_root = os.environ.get("XIRANG_V9_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root).expanduser() / "巡检"
    return Path.home() / ".xirang" / "v9-runtime" / "巡检"


def read_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def freshness(report: dict | None, now: datetime, max_age_hours: float) -> dict:
    generated_at = report.get("generated_at") if report else None
    parsed = parse_time(generated_at)
    if not parsed:
        return {
            "state": "unknown",
            "status": "yellow",
            "generated_at": generated_at,
            "reason": "generated_at_missing_or_invalid",
        }
    signed_age_seconds = int((now - parsed).total_seconds())
    max_age_seconds = int(timedelta(hours=max_age_hours).total_seconds())
    if signed_age_seconds < -300:
        return {
            "state": "clock_skew",
            "status": "yellow",
            "generated_at": generated_at,
            "age_seconds": signed_age_seconds,
            "max_future_skew_seconds": 300,
        }
    age_seconds = max(0, signed_age_seconds)
    if age_seconds > max_age_seconds:
        return {
            "state": "stale",
            "status": "yellow",
            "generated_at": generated_at,
            "age_seconds": age_seconds,
            "max_age_seconds": max_age_seconds,
        }
    return {
        "state": "fresh",
        "status": "green",
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
    }


def combine_status(primary: str, freshness_status: str) -> str:
    if primary == "red":
        return "red"
    if freshness_status == "yellow":
        return "yellow"
    return primary


def run_iteration_ops(script: Path, project_root: Path) -> tuple[dict | None, str | None]:
    if not script.exists():
        return None, f"script_missing: {script}"
    proc = subprocess.run(
        [sys.executable, str(script), "--project-root", str(project_root), "--json"],
        cwd=project_root.parent,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None, f"exit_{proc.returncode}: {proc.stderr.strip()}"
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"


def health_status(health: dict | None, error: str | None, now: datetime, max_age_hours: float) -> dict:
    if error:
        return {"available": False, "status": "red", "error": error}
    summary = health.get("summary", {}) if health else {}
    sources_failed = health.get("sources_failed", []) if health else []
    # cooldown controls notification repetition, not health truth. An unresolved
    # suppressed P1 must therefore keep the health badge red.
    red = bool(sources_failed) or int(summary.get("p0", 0) or 0) > 0 or int(summary.get("p1", 0) or 0) > 0
    yellow = int(summary.get("advisory", 0) or 0) > 0
    fresh = freshness(health, now, max_age_hours)
    status = "red" if red else "yellow" if yellow else "green"
    return {
        "available": True,
        "status": combine_status(status, fresh["status"]),
        "generated_at": health.get("generated_at") if health else None,
        "freshness": fresh,
        "sources_ok": health.get("sources_ok") if health else None,
        "sources_failed": sources_failed,
        "summary": summary,
    }


def verify_harness_report(report: dict | None, verifier: Path, repo_root: Path, max_age_hours: float, now: datetime) -> tuple[dict | None, str | None]:
    if report is None:
        return None, "report_missing"
    if not verifier.is_file():
        return None, f"verifier_missing: {verifier}"
    try:
        spec = importlib.util.spec_from_file_location("v9_harness_eval_verify", verifier)
        if spec is None or spec.loader is None:
            return None, "verifier_import_failed"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.verify_report(report, repo_root, max_age_hours, now), None
    except Exception as exc:
        return None, f"verifier_failed: {type(exc).__name__}: {exc}"


def eval_status(
    report: dict | None,
    error: str | None,
    now: datetime,
    max_age_hours: float,
    verification: dict | None = None,
    verification_error: str | None = None,
) -> dict:
    if error:
        return {"available": False, "status": "yellow", "error": error}
    summary = report.get("summary", {}) if report else {}
    failed = int(summary.get("failed", 0) or 0)
    missed_negative = int(summary.get("missed_negative", 0) or 0)
    meta_failed = int(summary.get("meta_failed", 0) or 0)
    red = failed > 0 or missed_negative > 0 or meta_failed > 0 or verification_error is not None or not (verification or {}).get("valid", False)
    fresh = freshness(report, now, max_age_hours)
    status = "red" if red else "green"
    return {
        "available": True,
        "status": combine_status(status, fresh["status"]),
        "generated_at": report.get("generated_at") if report else None,
        "freshness": fresh,
        "summary": summary,
        "verification": verification,
        "verification_error": verification_error,
    }


def iteration_status(report: dict | None, error: str | None) -> dict:
    if error:
        return {"available": False, "status": "red", "error": error}
    summary = report.get("summary", {}) if report else {}
    p0 = int(summary.get("p0", 0) or 0)
    p1 = int(summary.get("p1", 0) or 0)
    advisory = int(summary.get("advisory", 0) or 0)
    return {
        "available": True,
        "status": "red" if p0 or p1 else "yellow" if advisory else "green",
        "generated_at": report.get("generated_at") if report else None,
        "current_iteration": report.get("current_iteration") if report else None,
        "iteration_root": report.get("iteration_root") if report else None,
        "management_root": report.get("management_root") if report else None,
        "summary": summary,
        "findings": report.get("findings", []) if report else [],
    }


def overall_status(parts: list[dict]) -> str:
    statuses = {part.get("status") for part in parts}
    if "red" in statuses:
        return "red"
    if "yellow" in statuses:
        return "yellow"
    return "green"


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_report(args: argparse.Namespace) -> dict:
    inspect_dir = runtime_inspect_dir()
    status_latest = inspect_dir / STATUS_LATEST_NAME
    health_latest = inspect_dir / "health-latest.json"
    eval_latest = inspect_dir / "harness-eval-latest.json"
    now = datetime.now(timezone.utc).astimezone()
    health, health_error = read_json(health_latest)
    eval_report, eval_error = read_json(eval_latest)
    iteration_report, iteration_error = run_iteration_ops(args.iteration_check_script, args.project_root)
    eval_verification, eval_verification_error = verify_harness_report(
        eval_report, args.harness_verify_script, args.repo_root, args.max_age_hours, now
    )

    health_part = health_status(health, health_error, now, args.max_age_hours)
    eval_part = eval_status(
        eval_report, eval_error, now, args.max_age_hours, eval_verification, eval_verification_error
    )
    iteration_part = iteration_status(iteration_report, iteration_error)
    status = overall_status([health_part, eval_part, iteration_part])
    iteration_root = iteration_part.get("iteration_root")
    management_root = iteration_part.get("management_root")
    workbench_path = str(Path(management_root) / "README.md") if management_root else None
    paths = {
        "runtime_dir": str(inspect_dir),
        "status_latest": str(status_latest),
        "health_latest": str(health_latest),
        "harness_eval_latest": str(eval_latest),
        "repo_root": str(args.repo_root),
        "project_root": str(args.project_root),
        "iteration_root": iteration_root,
        "management_root": management_root,
        "iteration_workbench": workbench_path,
    }
    badges = [
        {
            "id": "health",
            "label": "反射器",
            "status": health_part.get("status"),
            "target": "health_latest",
            "detail_path": "parts.health",
        },
        {
            "id": "harness_eval",
            "label": "回归测试",
            "status": eval_part.get("status"),
            "target": "harness_eval_latest",
            "detail_path": "parts.harness_eval",
        },
        {
            "id": "iteration_ops",
            "label": "迭代治理",
            "status": iteration_part.get("status"),
            "target": "iteration_workbench",
            "detail_path": "parts.iteration_ops",
        },
    ]
    actions = [
        {"id": "open_iteration_workbench", "label": "打开迭代工作台", "kind": "open_file", "target": "iteration_workbench"},
        {"id": "open_health_latest", "label": "打开反射器状态", "kind": "open_file", "target": "health_latest"},
        {"id": "open_harness_eval_latest", "label": "打开回归测试", "kind": "open_file", "target": "harness_eval_latest"},
        {"id": "open_status_latest", "label": "打开状态文件", "kind": "open_file", "target": "status_latest"},
    ]
    status_label = STATUS_LABELS.get(status, status)

    return {
        "schema_version": SCHEMA_VERSION,
        "check": CHECK_NAME,
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "runtime_dir": str(inspect_dir),
        "max_age_hours": args.max_age_hours,
        "current_iteration": iteration_part.get("current_iteration"),
        "project_root": str(args.project_root),
        "paths": paths,
        "parts": {
            "health": health_part,
            "harness_eval": eval_part,
            "iteration_ops": iteration_part,
        },
        "ui": {
            "headline": f"V9 {status_label}",
            "summary": f"{status_label} · 迭代 {iteration_part.get('current_iteration') or '-'}",
            "badges": badges,
            "actions": actions,
        },
    }


def print_text(report: dict) -> None:
    print(f"# {CHECK_NAME}")
    print(f"状态: {STATUS_LABELS.get(report['status'], report['status'])} 迭代={report.get('current_iteration') or '-'}")
    for badge in report["ui"]["badges"]:
        print(f"- {badge['label']}: {STATUS_LABELS.get(badge['status'], badge['status'])}")


def main() -> int:
    repo_root = Path(".").resolve()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--project-root", type=Path, default=repo_root / "10-项目")
    parser.add_argument(
        "--iteration-check-script",
        type=Path,
        default=repo_root / "02-项目管理" / "脚本" / "v9-iteration-ops-check.py",
    )
    parser.add_argument(
        "--harness-verify-script",
        type=Path,
        default=repo_root / ".standards" / "harness-eval-verify.py",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--write-latest", action="store_true", help="写入运行态 status-latest.json")
    parser.add_argument("--max-age-hours", type=float, default=24.0, help="latest 超过该小时数后降为 yellow")
    args = parser.parse_args()

    args.repo_root = args.repo_root.resolve()
    args.project_root = args.project_root.resolve()
    args.iteration_check_script = args.iteration_check_script.resolve()
    args.harness_verify_script = args.harness_verify_script.resolve()

    report = build_report(args)
    if args.write_latest:
        atomic_write_json(runtime_inspect_dir() / STATUS_LATEST_NAME, report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)

    return 1 if report["status"] == "red" else 0


if __name__ == "__main__":
    sys.exit(main())
