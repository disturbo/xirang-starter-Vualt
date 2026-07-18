#!/usr/bin/env python3
"""Verify that a persisted Harness report proves the current trust set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_ROOT = Path(os.environ.get(
    "VAULT_ROOT", str(Path(__file__).resolve().parent.parent),
)).expanduser().resolve()
MANIFEST_REL = Path(".standards/harness-tested-files.txt")
CHECK_NAME = "v9-harness-eval-runner"


def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_manifest(root: Path) -> list[str]:
    manifest = root / MANIFEST_REL
    lines = manifest.read_text(encoding="utf-8").splitlines()
    paths = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Harness trust manifest is empty or contains duplicates")
    return paths


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def verify_report(report: dict, root: Path, max_age_hours: float, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    reasons: list[dict[str, object]] = []

    if report.get("check") != CHECK_NAME:
        reasons.append({"rule_id": "HARNESS_REPORT_KIND", "message": "报告 check 字段不匹配"})

    generated_at = parse_time(report.get("generated_at"))
    if generated_at is None:
        reasons.append({"rule_id": "HARNESS_REPORT_TIME", "message": "generated_at 缺失、无时区或不可解析"})
    else:
        age = now.astimezone(generated_at.tzinfo) - generated_at
        if age < -timedelta(minutes=5):
            reasons.append({"rule_id": "HARNESS_REPORT_CLOCK_SKEW", "message": "报告时间位于未来", "age_seconds": age.total_seconds()})
        elif age > timedelta(hours=max_age_hours):
            reasons.append({"rule_id": "HARNESS_REPORT_STALE", "message": f"报告超过 {max_age_hours:g} 小时", "age_seconds": age.total_seconds()})

    summary = report.get("summary")
    if not isinstance(summary, dict):
        reasons.append({"rule_id": "HARNESS_SUMMARY_MISSING", "message": "summary 缺失"})
    else:
        total = summary.get("total")
        if not isinstance(total, int) or total <= 0 or summary.get("passed") != total or summary.get("failed") != 0:
            reasons.append({"rule_id": "HARNESS_CASES_NOT_GREEN", "message": "用例汇总不是全量通过", "summary": summary})
        if summary.get("missed_negative", 0) != 0 or summary.get("meta_failed", 0) != 0:
            reasons.append({"rule_id": "HARNESS_NEGATIVE_MISSED", "message": "存在漏拦 negative 或 meta 失败", "summary": summary})

    try:
        expected = load_manifest(root)
    except (OSError, ValueError) as exc:
        expected = []
        reasons.append({"rule_id": "HARNESS_MANIFEST_INVALID", "message": str(exc)})

    tested = report.get("tested_hashes")
    if not isinstance(tested, dict):
        tested = {}
        reasons.append({"rule_id": "HARNESS_HASHES_MISSING", "message": "tested_hashes 缺失"})

    missing = sorted(set(expected) - set(tested))
    extra = sorted(set(tested) - set(expected))
    if missing or extra:
        reasons.append({"rule_id": "HARNESS_TRUST_SET_MISMATCH", "message": "报告 trust set 与当前 manifest 不一致", "missing": missing, "extra": extra})

    mismatched: list[str] = []
    invalid_hashes: list[str] = []
    missing_files: list[str] = []
    for rel in expected:
        path = root / rel
        value = tested.get(rel)
        if not path.is_file():
            missing_files.append(rel)
        elif not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value):
            invalid_hashes.append(rel)
        elif sha16(path) != value:
            mismatched.append(rel)
    if missing_files:
        reasons.append({"rule_id": "HARNESS_TESTED_FILE_MISSING", "message": "trust set 文件不存在", "paths": missing_files})
    if invalid_hashes:
        reasons.append({"rule_id": "HARNESS_HASH_INVALID", "message": "hash 格式无效", "paths": invalid_hashes})
    if mismatched:
        reasons.append({"rule_id": "HARNESS_HASH_STALE", "message": "tested_hashes 与当前文件不一致", "paths": mismatched})

    return {
        "check": "v9-harness-eval-verify",
        "verified_at": now.isoformat(timespec="seconds"),
        "report_generated_at": report.get("generated_at"),
        "valid": not reasons,
        "expected_files": len(expected),
        "verified_files": len(expected) - len(missing_files) - len(invalid_hashes) - len(mismatched),
        "reasons": reasons,
    }


def verify_path(report_path: Path, root: Path, max_age_hours: float) -> dict:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"check": "v9-harness-eval-verify", "valid": False, "reasons": [{"rule_id": "HARNESS_REPORT_MISSING", "message": str(report_path)}]}
    except (OSError, json.JSONDecodeError) as exc:
        return {"check": "v9-harness-eval-verify", "valid": False, "reasons": [{"rule_id": "HARNESS_REPORT_INVALID", "message": str(exc)}]}
    if not isinstance(report, dict):
        return {"check": "v9-harness-eval-verify", "valid": False, "reasons": [{"rule_id": "HARNESS_REPORT_INVALID", "message": "report root is not an object"}]}
    return verify_report(report, root, max_age_hours)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify_path(Path(args.report), Path(args.root).resolve(), args.max_age_hours)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["valid"] else "FAIL")
        for reason in result.get("reasons", []):
            print(f"[{reason.get('rule_id')}] {reason.get('message')}")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
