#!/usr/bin/env python3
"""Build the deterministic XiRang V9.7 universal release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path


VERSION = "9.7.0"
TAG = f"v{VERSION}"
ARCHIVE_ROOT = f"xi-rang-v{VERSION}"
ASSET = f"xi-rang-v{VERSION}-universal.zip"
FIXED_DATE = (2026, 8, 26, 12, 0, 0)
TOP_FILES = (
    "START-HERE.md",
    "AGENT-INSTALL.md",
    "README.md",
    "GOVERNANCE.md",
    "RELEASE-NOTES.md",
    "VERSION",
    "setup.sh",
)
SOURCE_DIRS = ("installer", "baselines", "payload", "templates")
BLOCKED_PARTS = {".git", ".DS_Store", "__pycache__", ".pytest_cache"}
BLOCKED_TEXT = (
    "/Users/",
    "C:\\Users\\",
    "yudongbo",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "ghp_",
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


def source_files(root: Path) -> list[tuple[Path, Path]]:
    result: list[tuple[Path, Path]] = []
    for logical in TOP_FILES:
        source = root / logical
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"missing release source: {logical}")
        result.append((source, Path(logical)))
    for directory in SOURCE_DIRS:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise SystemExit(f"missing release source directory: {directory}")
        for source in sorted(base.rglob("*")):
            if source.is_symlink():
                raise SystemExit(f"symlink forbidden: {source}")
            if not source.is_file():
                continue
            relative = source.relative_to(root)
            if BLOCKED_PARTS.intersection(relative.parts):
                continue
            result.append((source, relative))
    return result


def scan_leaks(staging: Path) -> None:
    findings: list[str] = []
    for path in sorted(staging.rglob("*")):
        relative = path.relative_to(staging)
        if path.is_symlink() or BLOCKED_PARTS.intersection(relative.parts):
            findings.append(f"path:{relative.as_posix()}")
            continue
        if not path.is_file():
            continue
        if relative.as_posix() in {
            "payload/.xirang/local-config.json",
            "payload/.xirang/contract/recovery-roots.yaml",
        } or path.name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
            findings.append(f"runtime:{relative.as_posix()}")
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in BLOCKED_TEXT:
            if needle in text:
                findings.append(f"text:{relative.as_posix()}:{needle}")
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
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative}", date_time=FIXED_DATE)
            mode = 0o755 if relative in {"setup.sh", "installer/xirang_install.py"} else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for name in (ASSET, "release-manifest.json", "SHA256SUMS"):
        (output / name).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="xirang-v97-build-") as raw:
        staging = Path(raw) / ARCHIVE_ROOT
        staging.mkdir()
        for source, relative in source_files(root):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o755 if relative.as_posix() in {"setup.sh", "installer/xirang_install.py"} else 0o644)
        manifests = staging / "manifests"
        manifests.mkdir()
        core = {
            "schema_version": 1,
            "version": VERSION,
            "kind": "managed_payload",
            "files": file_rows(staging, prefix=Path("payload")),
        }
        atomic_json(manifests / "core-manifest.json", core)
        scan_leaks(staging)
        package = {
            "schema_version": 1,
            "version": VERSION,
            "tag": TAG,
            "platform": "macOS",
            "python": ">=3.11",
            "archive_root": ARCHIVE_ROOT,
            "files": file_rows(staging, exclude={"manifests/package-manifest.json"}),
        }
        atomic_json(manifests / "package-manifest.json", package)
        destination = output / ASSET
        make_zip(staging, destination)
        zip_sha = sha256(destination)
        release = {
            "schema_version": 1,
            "product": "XiRang universal starter",
            "version": VERSION,
            "tag": TAG,
            "released_at": "2026-08-26",
            "support": {"platform": "macOS", "python": ">=3.11"},
            "asset": {
                "name": ASSET,
                "sha256": zip_sha,
                "size": destination.stat().st_size,
                "download_url": f"https://github.com/disturbo/xirang-starter-Vualt/releases/download/{TAG}/{ASSET}",
            },
            "package_manifest_sha256": sha256(manifests / "package-manifest.json"),
            "core_manifest_sha256": sha256(manifests / "core-manifest.json"),
            "interaction": "one package; Agent auto-detects fresh install, supported upgrade, current repair, or assistance required",
        }
        atomic_json(output / "release-manifest.json", release)
        (output / "SHA256SUMS").write_text(f"{zip_sha}  {ASSET}\n", encoding="utf-8")
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
