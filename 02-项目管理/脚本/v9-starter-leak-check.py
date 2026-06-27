#!/usr/bin/env python3
"""
v9-starter-leak-check.py — V9 starter 分发泄漏扫描

职责：
  扫描可分发 starter Vault，发现个人路径、项目私货、旧 agent id、真实秘钥形态。
  本脚本只读 starter，不自动修改任何文件。

默认 root：
  1. --root 参数；
  2. XIRANG_STARTER_ROOT 环境变量；
  3. 当前 Vault 兄弟目录 ../xi-rang-v9-starter；
  4. ~/Desktop/xi-rang-v9-starter。

输出统一 findings schema，供 v9-reflex-check.py 聚合。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


CHECK_NAME = "v9-starter-leak-check"
SOURCE = "starter-leak"
SEVERITY_ORDER = {"p0": 0, "p1": 1, "advisory": 2}

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".obsidian/plugins",
    ".obsidian/themes",
    ".prompt-src/_build",
}

SKIP_FILENAMES = {
    ".DS_Store",
    "health-latest.json",
    "reflex-state.json",
    ".reflex.lock",
    Path(__file__).name,
}

LITERAL_PATTERNS = [
    ("PERSONAL_NAME", "波波"),
    ("PERSONAL_NAME", "余东波"),
    ("PERSONAL_PATH", "/Users/yudongbo"),
    ("PERSONAL_PATH", "yudongbo"),
    ("PROJECT_TERM", "奕境"),
    ("PROJECT_TERM", "东风"),
    ("PROJECT_TERM", "联友"),
    ("PROJECT_TERM", "花都"),
    ("PROJECT_TERM", "保险经纪"),
    ("PROJECT_TERM", "YJDMS"),
    ("PROJECT_TERM", "DFIB"),
    ("PROJECT_TERM", "DMS"),
    ("LEGACY_AGENT_ID", "dongfeng"),
    ("SESSION_LEAK", "openclaw-memory-promotion"),
    ("ACCOUNT_LEAK", "@im.wechat"),
]

REGEX_PATTERNS = [
    ("SECRET_CLI_TOKEN", re.compile(r"\bcli_[a-z0-9]{12,}\b")),
    ("SECRET_APP_SECRET", re.compile(r"\bAPP_SECRET\s*=\s*[\"'][A-Za-z0-9_-]{20,}[\"']")),
    ("SECRET_JSON_APP_SECRET", re.compile(r'"app_secret"\s*:\s*"[A-Za-z0-9_-]{20,}"')),
    ("SECRET_BEARER", re.compile(r"\bAuthorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}")),
    ("FEISHU_REAL_URL", re.compile(r"https://[a-z0-9]+\.feishu\.cn/(wiki|docx)/[A-Za-z0-9_-]{12,}")),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_finding(severity: str, rule_id: str, obj: str, message: str, detail: dict | None = None) -> dict:
    finding = {
        "severity": severity,
        "rule_id": rule_id,
        "object": obj,
        "message": message,
        "source": SOURCE,
    }
    if detail:
        finding["detail"] = detail
    return finding


def resolve_root(value: str | None) -> Path:
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value).expanduser())
    env_root = os.environ.get("XIRANG_STARTER_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.append((Path.cwd().parent / "xi-rang-v9-starter").expanduser())
    candidates.append((Path.home() / "Desktop" / "xi-rang-v9-starter").expanduser())
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else Path("xi-rang-v9-starter").resolve()


def should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    rel_text = rel.as_posix()
    if path.name in SKIP_FILENAMES:
        return True
    if path.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip"}:
        return True
    return any(rel_text == d or rel_text.startswith(f"{d}/") for d in SKIP_DIRS)


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not should_skip(path, root):
            yield path


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def is_detector_line(path: Path, line: str) -> bool:
    """避免扫描器/同步脚本的敏感词正则把自己报成泄漏。"""
    if path.name == "sync-to-dist.sh" and "SENSITIVE_PATTERN" in line:
        return True
    if "LITERAL_PATTERNS" in line or "REGEX_PATTERNS" in line:
        return True
    return False


def scan_file(path: Path, root: Path) -> list[dict]:
    text = read_text(path)
    if text is None:
        return []
    rel = path.relative_to(root).as_posix()
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if is_detector_line(path, line):
            continue
        for rule_id, literal in LITERAL_PATTERNS:
            if literal in line:
                findings.append(
                    make_finding(
                        "p1",
                        rule_id,
                        rel,
                        f"starter 疑似泄漏 {literal!r}: {rel}:{lineno}",
                        {"line": lineno, "match": literal},
                    )
                )
        for rule_id, pattern in REGEX_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    make_finding(
                        "p1",
                        rule_id,
                        rel,
                        f"starter 疑似秘钥/真实链接泄漏: {rel}:{lineno}",
                        {"line": lineno, "match": match.group(0)[:80]},
                    )
                )
    return findings


def summarize(findings: list[dict], files_scanned: int) -> dict:
    def count(sev: str) -> int:
        return sum(1 for f in findings if f["severity"] == sev)

    worst = min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 9)) if findings else None
    return {
        "total": len(findings),
        "p0": count("p0"),
        "p1": count("p1"),
        "advisory": count("advisory"),
        "worst": worst,
        "files_scanned": files_scanned,
    }


def build_report(root: Path) -> dict:
    findings: list[dict] = []
    files_scanned = 0

    if not root.exists():
        findings.append(
            make_finding(
                "advisory",
                "STARTER_ROOT_MISSING",
                str(root),
                f"starter 根目录不存在，跳过泄漏扫描：{root}",
            )
        )
    elif not root.is_dir():
        findings.append(
            make_finding("p1", "STARTER_ROOT_NOT_DIR", str(root), f"starter root 不是目录：{root}")
        )
    else:
        for path in iter_files(root):
            files_scanned += 1
            findings.extend(scan_file(path, root))

    return {
        "check": CHECK_NAME,
        "generated_at": now_iso(),
        "root": str(root),
        "summary": summarize(findings, files_scanned),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="starter Vault 根目录；默认用 XIRANG_STARTER_ROOT 或兄弟目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="发现 p0/p1 时退出码 1")
    args = parser.parse_args()

    root = resolve_root(args.root)
    report = build_report(root)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"# {CHECK_NAME}")
        print(f"root: {report['root']}")
        print(f"summary: total={s['total']} p0={s['p0']} p1={s['p1']} advisory={s['advisory']} files={s['files_scanned']}")
        for finding in report["findings"]:
            print(f"[{finding['severity']}] {finding['rule_id']} | {finding['message']}")

    if args.strict and (report["summary"]["p0"] > 0 or report["summary"]["p1"] > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
