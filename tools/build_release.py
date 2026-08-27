#!/usr/bin/env python3
"""Build the deterministic XiRang V9 Starter package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path


VERSION = "9.7.2"
PACKAGE_ROOT = f"xi-rang-v{VERSION}-starter"
PACKAGE_ASSET = f"{PACKAGE_ROOT}.zip"
RELEASE_TAG = f"v{VERSION}"
RELEASE_URL = f"https://github.com/disturbo/xirang-starter-Vualt/releases/tag/{RELEASE_TAG}"
DOWNLOAD_URL = f"https://github.com/disturbo/xirang-starter-Vualt/releases/download/{RELEASE_TAG}/{PACKAGE_ASSET}"
FIXED_DATE = (2026, 8, 27, 16, 0, 0)
UPGRADE_PATH = Path(".xirang/distribution/upgrade")
UPGRADE_TOP_FILES = (
    "START-HERE.md",
    "AGENT-INSTALL.md",
    "README.md",
    "GOVERNANCE.md",
    "RELEASE-NOTES.md",
    "VERSION",
    "setup.sh",
)
UPGRADE_SOURCE_DIRS = ("installer", "baselines", "templates")
PAYLOAD_LIFECYCLE = Path("starter-vault/.xirang/distribution/payload-lifecycle.json")
MANAGED_LIFECYCLES = {"managed_core", "merge"}
KNOWN_LIFECYCLES = MANAGED_LIFECYCLES | {"seed_if_absent", "user_mutable"}
OBSOLETE_ASSETS = (
    "xi-rang-v9.7.0-complete-vault.zip",
    "xi-rang-v9.7.0-starter-vault.zip",
    "xi-rang-v9.7.0-upgrade.zip",
    "xi-rang-v9.7.0-universal.zip",
)
BLOCKED_PARTS = {
    ".git",
    ".DS_Store",
    ".idea",
    ".private",
    ".pytest_cache",
    ".wrangler",
    "__pycache__",
    "node_modules",
    "site-packages",
}
BLOCKED_SUFFIXES = {
    ".db",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".jsonl",
    ".key",
    ".log",
    ".mobileprovision",
    ".p12",
    ".pem",
    ".png",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".webp",
    ".zip",
}
BLOCKED_TEXT = (
    "yudongbo",
    "余东波",
    "波波",
    "联友",
    "奕境",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "github_pat_",
    "ghp_",
)
SENSITIVE_NAMES = {
    ".npmrc",
    ".pypirc",
    "auth.json",
    "cookies.json",
    "credentials.json",
    "data.json",
    "secrets.json",
}
MAC_USER_PATH = re.compile(r"/Users/(?!Shared(?:/|$))[A-Za-z0-9._-]+/")
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:\\Users\\[^\\/]+\\", re.IGNORECASE)
IDENTITY_PATTERN = re.compile(r"\bou_[A-Za-z0-9]{8,}\b")
API_KEY_PATTERN = re.compile(r"\b(?:sk-|AKIA)[A-Za-z0-9_-]{16,}\b")
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(raw, encoding="utf-8")
    os.replace(temporary, path)


def collect_files(base: Path, *, relative_to: Path) -> list[tuple[Path, Path]]:
    if not base.is_dir() or base.is_symlink():
        raise SystemExit(f"missing release source directory: {base.relative_to(relative_to)}")
    result: list[tuple[Path, Path]] = []
    for source in sorted(base.rglob("*")):
        logical = source.relative_to(relative_to)
        if BLOCKED_PARTS.intersection(logical.parts):
            continue
        if source.is_symlink():
            raise SystemExit(f"symlink forbidden: {logical.as_posix()}")
        if source.is_file():
            result.append((source, logical))
    return result


def copy_pairs(pairs: list[tuple[Path, Path]], staging: Path) -> None:
    for source, relative in pairs:
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        executable = bool(source.stat().st_mode & 0o111) or relative.as_posix() in {
            "setup.sh",
            "installer/xirang_install.py",
        }
        os.chmod(destination, 0o755 if executable else 0o644)


def payload_source_files(root: Path) -> list[tuple[Path, Path]]:
    payload = root / "payload"
    return [
        (source, logical.relative_to("payload"))
        for source, logical in collect_files(payload, relative_to=root)
    ]


def load_payload_lifecycle(root: Path, payload_paths: set[str]) -> tuple[dict, dict[str, str]]:
    source = root / PAYLOAD_LIFECYCLE
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid payload lifecycle manifest: {exc}") from exc
    if document.get("schema_version") != 1 or not isinstance(document.get("files"), list):
        raise SystemExit("invalid payload lifecycle manifest schema")
    lifecycle_by_path: dict[str, str] = {}
    for row in document["files"]:
        if not isinstance(row, dict):
            raise SystemExit("payload lifecycle row must be an object")
        path = str(row.get("path") or "")
        lifecycle = str(row.get("lifecycle") or "")
        if not path or path in lifecycle_by_path or lifecycle not in KNOWN_LIFECYCLES:
            raise SystemExit(f"invalid payload lifecycle row: {row!r}")
        lifecycle_by_path[path] = lifecycle
    missing = sorted(payload_paths - set(lifecycle_by_path))
    extra = sorted(set(lifecycle_by_path) - payload_paths)
    if missing or extra:
        raise SystemExit(
            "payload lifecycle does not close over payload:\n"
            + f"missing={missing}\nextra={extra}"
        )
    return document, lifecycle_by_path


def upgrade_source_files(root: Path) -> list[tuple[Path, Path]]:
    result: list[tuple[Path, Path]] = []
    for logical in UPGRADE_TOP_FILES:
        source = root / logical
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"missing release source: {logical}")
        result.append((source, Path(logical)))
    for directory in UPGRADE_SOURCE_DIRS:
        result.extend(collect_files(root / directory, relative_to=root))
    return result


def overlay_source_files(root: Path) -> list[tuple[Path, Path]]:
    return collect_files(root / "starter-vault", relative_to=root / "starter-vault")


def scan_leaks(staging: Path) -> None:
    findings: list[str] = []
    runtime_paths = {
        ".xirang/local-config.json",
        ".xirang/contract/recovery-roots.yaml",
        f"{UPGRADE_PATH.as_posix()}/payload/.xirang/local-config.json",
        f"{UPGRADE_PATH.as_posix()}/payload/.xirang/contract/recovery-roots.yaml",
    }
    for path in sorted(staging.rglob("*")):
        relative = path.relative_to(staging)
        logical = relative.as_posix()
        if path.is_symlink() or BLOCKED_PARTS.intersection(relative.parts):
            findings.append(f"path:{logical}")
            continue
        if not path.is_file():
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            findings.append(f"environment:{logical}")
            continue
        if path.name.lower() in SENSITIVE_NAMES:
            findings.append(f"sensitive-file:{logical}")
            continue
        if logical in runtime_paths:
            findings.append(f"runtime:{logical}")
            continue
        if any(path.name.lower().endswith(suffix) for suffix in BLOCKED_SUFFIXES):
            findings.append(f"blocked-file:{logical}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in BLOCKED_TEXT:
            if needle in text:
                findings.append(f"text:{logical}:{needle}")
        if MAC_USER_PATH.search(text) or WINDOWS_USER_PATH.search(text):
            findings.append(f"user-path:{logical}")
        if IDENTITY_PATTERN.search(text):
            findings.append(f"identity:{logical}")
        if API_KEY_PATTERN.search(text):
            findings.append(f"api-key:{logical}")
        if JWT_PATTERN.search(text):
            findings.append(f"jwt:{logical}")
    if findings:
        raise SystemExit("release leakage scan failed:\n" + "\n".join(findings))


def file_rows(staging: Path, *, prefix: Path | None = None, exclude: set[str] | None = None) -> list[dict]:
    base = staging / prefix if prefix else staging
    excluded = exclude or set()
    rows = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        logical = path.relative_to(base).as_posix()
        if logical in excluded:
            continue
        rows.append({"path": logical, "sha256": sha256(path), "size": path.stat().st_size})
    return rows


def make_zip(staging: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(f"{PACKAGE_ROOT}/{relative}", date_time=FIXED_DATE)
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for name in (PACKAGE_ASSET, *OBSOLETE_ASSETS, "release-manifest.json", "SHA256SUMS"):
        (output / name).unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="xirang-v97-complete-") as raw:
        staging = Path(raw) / PACKAGE_ROOT
        staging.mkdir()

        payload_pairs = payload_source_files(root)
        overlay_pairs = overlay_source_files(root)
        payload_paths = {logical for _, logical in payload_pairs}
        overlay_paths = {logical for _, logical in overlay_pairs}
        collisions = sorted(path.as_posix() for path in payload_paths.intersection(overlay_paths))
        if collisions:
            raise SystemExit("starter overlay conflicts with Core:\n" + "\n".join(collisions))
        lifecycle_document, lifecycle_by_path = load_payload_lifecycle(
            root, {path.as_posix() for path in payload_paths}
        )
        copy_pairs(payload_pairs, staging)
        copy_pairs(overlay_pairs, staging)

        upgrade = staging / UPGRADE_PATH
        upgrade.mkdir(parents=True)
        copy_pairs([(source, Path("payload") / logical) for source, logical in payload_pairs], upgrade)
        copy_pairs(upgrade_source_files(root), upgrade)
        atomic_json(upgrade / "manifests/payload-lifecycle.json", lifecycle_document)

        distribution_sources = (
            (root / "tools/verify_complete.py", Path(".xirang/distribution/verify_complete.py")),
            (root / "tools/install_extras.py", Path(".xirang/distribution/install_extras.py")),
        )
        copy_pairs(list(distribution_sources), staging)

        payload_rows = file_rows(upgrade, prefix=Path("payload"))
        core = {
            "schema_version": 1,
            "version": VERSION,
            "kind": "managed_payload",
            "files": [
                row for row in payload_rows
                if lifecycle_by_path[row["path"]] in MANAGED_LIFECYCLES
            ],
        }
        root_core_manifest = staging / ".xirang/distribution/core-manifest.json"
        upgrade_core_manifest = upgrade / "manifests/core-manifest.json"
        atomic_json(root_core_manifest, core)
        atomic_json(upgrade_core_manifest, core)

        upgrade_package_manifest = upgrade / "manifests/package-manifest.json"
        atomic_json(
            upgrade_package_manifest,
            {
                "schema_version": 1,
                "version": VERSION,
                "kind": "agent_upgrade",
                "platform": "macOS",
                "python": ">=3.11",
                "archive_root": UPGRADE_PATH.as_posix(),
                "files": file_rows(upgrade, exclude={"manifests/package-manifest.json"}),
            },
        )

        package_manifest = staging / ".xirang/distribution/package-manifest.json"
        atomic_json(
            package_manifest,
            {
                "schema_version": 1,
                "version": VERSION,
                "kind": "starter_vault",
                "archive_root": PACKAGE_ROOT,
                "files": file_rows(
                    staging,
                    exclude={".xirang/distribution/package-manifest.json"},
                ),
            },
        )
        scan_leaks(staging)

        destination = output / PACKAGE_ASSET
        make_zip(staging, destination)
        asset_sha = sha256(destination)
        skill_count = sum(1 for path in (staging / ".skills").glob("*/SKILL.md") if path.is_file())
        plugin_count = sum(1 for path in (staging / ".obsidian/plugins").glob("*/manifest.json") if path.is_file())
        release = {
            "schema_version": 1,
            "product": "XiRang V9 Starter",
            "version": VERSION,
            "tag": RELEASE_TAG,
            "released_at": "2026-08-27",
            "release_url": RELEASE_URL,
            "support": {"platform": "macOS", "python": ">=3.11"},
            "asset": {
                "kind": "starter_vault",
                "name": PACKAGE_ASSET,
                "download_url": DOWNLOAD_URL,
                "sha256": asset_sha,
                "size": destination.stat().st_size,
            },
            "contents": {
                "portable_skills": skill_count,
                "obsidian_plugins": plugin_count,
                "theme": "Things 2.2.3",
                "css_snippets": sum(1 for path in (staging / ".obsidian/snippets").glob("*.css") if path.is_file()),
            },
            "core_manifest_sha256": sha256(upgrade_core_manifest),
            "package_manifest_sha256": sha256(package_manifest),
            "interaction": "新人直接用 Obsidian 打开；旧用户把同一包交给当前 Agent 执行 AGENT-SETUP.md",
        }
        atomic_json(output / "release-manifest.json", release)
        (output / "SHA256SUMS").write_text(f"{asset_sha}  {PACKAGE_ASSET}\n", encoding="utf-8")
        return release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.expanduser().resolve() if args.output_dir else root / "dist"
    print(json.dumps(build(root, output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
