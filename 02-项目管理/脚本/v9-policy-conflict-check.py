#!/usr/bin/env python3
"""
v9-policy-conflict-check.py — V9 规范管辖权索引与冲突扫描

职责：
  1. 读取 V9 规范管辖权索引中的机器可读 JSON；
  2. 检查 primary/supporting/inactive 路径与 frontmatter 状态；
  3. 扫描现行规范目录，发现 active/正式 但未纳入索引的规范文件。

本脚本只读文件，不写看板/日志/规范正文。输出统一 findings schema，
供 v9-reflex-check.py 聚合。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(".")
INDEX_PATH = ROOT / "50-经验" / "Agent协作方法论" / "V9-规范管辖权索引-2026-06-25.md"
CHECK_NAME = "v9-policy-conflict-check"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}

ACTIVE_STATUSES = {
    "active",
    "正式",
    "maturity:正式",
    "已发布",
    "生效中",
    "accepted",
    "accepted_with_changes",
    "accepted-with-changes",
}
DRAFT_STATUSES = {"draft", "草稿", "待评审", "wip", "submitted", "pending"}
INACTIVE_STATUSES = {"archived", "archive", "归档", "deprecated", "retired", "已废弃", "废弃", "下线"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_finding(severity: str, rule_id: str, obj: str, message: str, detail: dict | None = None) -> dict:
    finding = {
        "severity": severity,
        "rule_id": rule_id,
        "object": obj,
        "message": message,
        "source": "policy-conflict",
    }
    if detail:
        finding["detail"] = detail
    return finding


def normalize_path(value: str) -> str:
    return value.strip().strip('"').strip("'").lstrip("./")


def normalize_status(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().strip('"').strip("'").lower().replace("-", "_")


def parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}

    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "-")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def path_exists_or_dir(path_text: str) -> bool:
    return (ROOT / normalize_path(path_text)).exists()


def iter_markdown_under(path_text: str) -> list[Path]:
    path = ROOT / normalize_path(path_text)
    if path.is_dir():
        return sorted(p for p in path.rglob("*.md") if p.is_file())
    if path.is_file() and path.suffix == ".md":
        return [path]
    return []


def load_index() -> tuple[list[dict[str, Any]], list[dict]]:
    if not INDEX_PATH.exists():
        return [], [
            make_finding("p1", "POLICY_INDEX_MISSING", str(INDEX_PATH), f"规范管辖权索引缺失：{INDEX_PATH}")
        ]

    text = INDEX_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- v9-jurisdiction-index:start -->\s*```json\s*(.*?)\s*```\s*<!-- v9-jurisdiction-index:end -->",
        text,
        re.DOTALL,
    )
    if not match:
        return [], [
            make_finding("p1", "POLICY_INDEX_BLOCK_MISSING", str(INDEX_PATH), "规范管辖权索引缺少机器可读 JSON 块。")
        ]
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return [], [
            make_finding("p1", "POLICY_INDEX_JSON_INVALID", str(INDEX_PATH), f"规范管辖权索引 JSON 无法解析：{exc}")
        ]
    if not isinstance(data, list):
        return [], [
            make_finding("p1", "POLICY_INDEX_JSON_INVALID", str(INDEX_PATH), "规范管辖权索引 JSON 顶层必须是数组。")
        ]
    return data, []


def status_bucket(fm: dict[str, str]) -> str:
    status = normalize_status(fm.get("status"))
    maturity = normalize_status(fm.get("maturity"))
    if status in {s.lower().replace("-", "_") for s in INACTIVE_STATUSES}:
        return "inactive"
    if status in {s.lower().replace("-", "_") for s in DRAFT_STATUSES}:
        return "draft"
    if status in {s.lower().replace("-", "_") for s in ACTIVE_STATUSES}:
        return "active"
    if maturity in {"正式", "active"}:
        return "active"
    return "unknown"


def check_primary(domain: str, path_text: str) -> list[dict]:
    path = ROOT / normalize_path(path_text)
    if not path.exists():
        return [
            make_finding("p1", "POLICY_PRIMARY_MISSING", domain, f"{domain}: primary 文件缺失：{path_text}")
        ]
    if path.is_dir():
        return [
            make_finding("p1", "POLICY_PRIMARY_IS_DIR", domain, f"{domain}: primary 必须是文件，当前是目录：{path_text}")
        ]
    fm = parse_frontmatter(path)
    bucket = status_bucket(fm)
    if bucket in {"inactive", "draft"}:
        return [
            make_finding(
                "p1",
                "POLICY_PRIMARY_NOT_ACTIVE",
                path_text,
                f"{domain}: primary 状态不是 active/正式：{path_text}",
                {"status": fm.get("status"), "maturity": fm.get("maturity")},
            )
        ]
    if bucket == "unknown":
        return [
            make_finding(
                "advisory",
                "POLICY_PRIMARY_STATUS_UNKNOWN",
                path_text,
                f"{domain}: primary 缺少可识别 status/maturity：{path_text}",
                {"status": fm.get("status"), "maturity": fm.get("maturity")},
            )
        ]
    return []


def check_supporting(domain: str, path_text: str) -> list[dict]:
    if not path_exists_or_dir(path_text):
        return [
            make_finding("advisory", "POLICY_SUPPORTING_MISSING", path_text, f"{domain}: supporting 路径缺失：{path_text}")
        ]
    return []


def check_inactive(domain: str, path_text: str) -> list[dict]:
    findings: list[dict] = []
    for path in iter_markdown_under(path_text):
        fm = parse_frontmatter(path)
        if status_bucket(fm) != "active":
            continue
        rel = str(path.relative_to(ROOT))
        severity = "advisory" if "_archive" in path.parts else "p1"
        findings.append(
            make_finding(
                severity,
                "POLICY_INACTIVE_STILL_ACTIVE",
                rel,
                f"{domain}: inactive 路径下文件仍标为 active/正式：{rel}",
                {"status": fm.get("status"), "maturity": fm.get("maturity")},
            )
        )
    return findings


def indexed_prefixes(index: list[dict[str, Any]]) -> set[str]:
    prefixes: set[str] = set()
    for entry in index:
        for key in ("primary", "supporting"):
            value = entry.get(key, [])
            values = value if isinstance(value, list) else [value]
            for raw in values:
                if not isinstance(raw, str):
                    continue
                norm = normalize_path(raw)
                prefixes.add(norm)
    return prefixes


def is_indexed(path: Path, prefixes: set[str]) -> bool:
    rel = str(path.relative_to(ROOT))
    for prefix in prefixes:
        if prefix.endswith("/"):
            if rel.startswith(prefix):
                return True
        elif rel == prefix:
            return True
    return False


def iter_current_policy_docs() -> list[Path]:
    candidates: list[Path] = []
    for base in [ROOT / "30-规范", ROOT / "50-经验" / "Agent协作方法论"]:
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            if "_archive" in path.parts:
                continue
            if path.name.startswith("."):
                continue
            candidates.append(path)
    return sorted(candidates)


def check_unindexed_active(index: list[dict[str, Any]]) -> list[dict]:
    prefixes = indexed_prefixes(index)
    findings: list[dict] = []
    for path in iter_current_policy_docs():
        if is_indexed(path, prefixes):
            continue
        fm = parse_frontmatter(path)
        bucket = status_bucket(fm)
        if bucket != "active":
            continue
        doc_type = normalize_status(fm.get("type"))
        if doc_type in {"proposal", "cost_report"}:
            continue
        rel = str(path.relative_to(ROOT))
        findings.append(
            make_finding(
                "advisory",
                "POLICY_ACTIVE_UNINDEXED",
                rel,
                f"active/正式 规范文件未纳入管辖权索引：{rel}",
                {"status": fm.get("status"), "maturity": fm.get("maturity"), "type": fm.get("type")},
            )
        )
    return findings


def collect_findings() -> tuple[list[dict], list[dict[str, Any]]]:
    index, findings = load_index()
    if findings:
        return findings, index

    domains: set[str] = set()
    for entry in index:
        domain = str(entry.get("domain") or "").strip()
        primary = str(entry.get("primary") or "").strip()
        if not domain:
            findings.append(make_finding("p1", "POLICY_DOMAIN_MISSING", "<index>", "索引条目缺少 domain。"))
            continue
        if domain in domains:
            findings.append(make_finding("p1", "POLICY_DOMAIN_DUPLICATE", domain, f"规范管辖域重复：{domain}"))
        domains.add(domain)
        if not primary:
            findings.append(make_finding("p1", "POLICY_PRIMARY_EMPTY", domain, f"{domain}: 缺少 primary。"))
            continue

        findings.extend(check_primary(domain, primary))
        for supporting in entry.get("supporting") or []:
            findings.extend(check_supporting(domain, str(supporting)))
        for inactive in entry.get("inactive") or []:
            findings.extend(check_inactive(domain, str(inactive)))

    findings.extend(check_unindexed_active(index))
    return dedupe_findings(findings), index


def dedupe_findings(findings: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for finding in findings:
        key = (finding["rule_id"], finding["object"])
        if key not in merged:
            merged[key] = finding
            continue

        current = merged[key]
        if SEVERITY_ORDER.get(finding["severity"], 9) < SEVERITY_ORDER.get(current["severity"], 9):
            current["severity"] = finding["severity"]

        detail = current.setdefault("detail", {})
        domains = detail.setdefault("domains", [])
        current_message = current.get("message", "")
        if ":" in current_message:
            current_domain = current_message.split(":", 1)[0]
            if current_domain not in domains:
                domains.append(current_domain)
        message = finding.get("message", "")
        if ":" in message:
            domain = message.split(":", 1)[0]
            if domain not in domains:
                domains.append(domain)

    for finding in merged.values():
        domains = finding.get("detail", {}).get("domains")
        if domains:
            finding["message"] = f"{finding['message']}（同类信号域: {', '.join(domains)}）"
    return list(merged.values())


def build_report(findings: list[dict], index: list[dict[str, Any]]) -> dict:
    def count(sev: str) -> int:
        return sum(1 for f in findings if f["severity"] == sev)

    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "index": str(INDEX_PATH),
        "summary": {
            "domains": len(index),
            "total": len(findings),
            "p0": count("p0"),
            "p1": count("p1"),
            "advisory": count("advisory"),
            "worst": worst,
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    findings, index = collect_findings()
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule_id"], f["object"]))
    report = build_report(findings, index)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"# V9 规范冲突扫描")
        print(f"索引: {INDEX_PATH}")
        print(f"汇总: domains={s['domains']} total={s['total']} p1={s['p1']} advisory={s['advisory']}")
        for f in findings:
            print(f"- [{f['severity']}] {f['rule_id']} | {f['message']}")

    return 1 if any(f["severity"] == "p0" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
