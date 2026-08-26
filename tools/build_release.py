#!/usr/bin/env python3
"""Build deterministic XiRang V9.7 starter-Vault and Agent-upgrade bundles."""

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


VERSION = "9.7.0"
STARTER_ROOT = f"xi-rang-v{VERSION}-starter-vault"
UPGRADE_ROOT = f"xi-rang-v{VERSION}-upgrade"
STARTER_ASSET = f"{STARTER_ROOT}.zip"
UPGRADE_ASSET = f"{UPGRADE_ROOT}.zip"
FIXED_DATE = (2026, 8, 26, 12, 0, 0)
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
BLOCKED_PARTS = {
    ".git",
    ".DS_Store",
    ".idea",
    ".private",
    ".pytest_cache",
    ".wrangler",
    "__pycache__",
    "node_modules",
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
    "/Users/",
    "C:\\Users\\",
    "yudongbo",
    "余东波",
    "波波",
    "联友",
    "奕境",
    "thisbo",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "github_pat_",
    "ghp_",
)
IDENTITY_PATTERN = re.compile(r"\bou_[A-Za-z0-9]{8,}\b")
API_KEY_PATTERN = re.compile(r"\b(?:sk-|AKIA)[A-Za-z0-9_-]{16,}\b")
SECRET_PATTERN = re.compile(
    r"(?i)(?:app[_-]?secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|password|cookie)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-/.+=]{8,}"
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
        mode = 0o755 if relative.as_posix() in {"setup.sh", "installer/xirang_install.py"} else 0o644
        os.chmod(destination, mode)


def core_source_files(root: Path) -> list[tuple[Path, Path]]:
    payload = root / "payload"
    return [
        (source, logical.relative_to("payload"))
        for source, logical in collect_files(payload, relative_to=root)
    ]


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


def starter_overlay_files(root: Path) -> list[tuple[Path, Path]]:
    return collect_files(root / "starter-vault", relative_to=root / "starter-vault")


def scan_leaks(staging: Path, *, kind: str) -> None:
    findings: list[str] = []
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
        if kind == "starter_vault" and logical.startswith(".obsidian/plugins/"):
            findings.append(f"plugin:{logical}")
            continue
        if logical in {
            ".xirang/local-config.json",
            ".xirang/contract/recovery-roots.yaml",
            "payload/.xirang/local-config.json",
            "payload/.xirang/contract/recovery-roots.yaml",
        }:
            findings.append(f"runtime:{logical}")
            continue
        if any(path.name.lower().endswith(suffix) for suffix in BLOCKED_SUFFIXES):
            findings.append(f"blocked-file:{logical}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in BLOCKED_TEXT:
            if needle in text:
                findings.append(f"text:{logical}:{needle}")
        if IDENTITY_PATTERN.search(text):
            findings.append(f"identity:{logical}")
        if API_KEY_PATTERN.search(text):
            findings.append(f"api-key:{logical}")
        if SECRET_PATTERN.search(text):
            findings.append(f"secret:{logical}")
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


def make_zip(staging: Path, destination: Path, *, archive_root: str, executables: set[str]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(f"{archive_root}/{relative}", date_time=FIXED_DATE)
            mode = 0o755 if relative in executables else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for name in (STARTER_ASSET, UPGRADE_ASSET, "release-manifest.json", "SHA256SUMS"):
        (output / name).unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="xirang-v97-build-") as raw:
        temporary = Path(raw)
        starter = temporary / STARTER_ROOT
        upgrade = temporary / UPGRADE_ROOT
        starter.mkdir()
        upgrade.mkdir()

        core_pairs = core_source_files(root)
        overlay_pairs = starter_overlay_files(root)
        core_paths = {logical for _, logical in core_pairs}
        overlay_paths = {logical for _, logical in overlay_pairs}
        collisions = sorted(path.as_posix() for path in core_paths.intersection(overlay_paths))
        if collisions:
            raise SystemExit("starter overlay conflicts with Core:\n" + "\n".join(collisions))
        copy_pairs(core_pairs, starter)
        copy_pairs([(source, Path("payload") / logical) for source, logical in core_pairs], upgrade)
        copy_pairs(overlay_pairs, starter)
        copy_pairs(upgrade_source_files(root), upgrade)

        core = {
            "schema_version": 1,
            "version": VERSION,
            "kind": "managed_payload",
            "files": file_rows(upgrade, prefix=Path("payload")),
        }
        starter_core_manifest = starter / ".xirang/distribution/core-manifest.json"
        upgrade_core_manifest = upgrade / "manifests/core-manifest.json"
        atomic_json(starter_core_manifest, core)
        atomic_json(upgrade_core_manifest, core)

        starter_package_manifest = starter / ".xirang/distribution/package-manifest.json"
        upgrade_package_manifest = upgrade / "manifests/package-manifest.json"
        atomic_json(
            starter_package_manifest,
            {
                "schema_version": 1,
                "version": VERSION,
                "kind": "starter_vault",
                "archive_root": STARTER_ROOT,
                "files": file_rows(
                    starter,
                    exclude={".xirang/distribution/package-manifest.json"},
                ),
            },
        )
        atomic_json(
            upgrade_package_manifest,
            {
                "schema_version": 1,
                "version": VERSION,
                "kind": "agent_upgrade",
                "platform": "macOS",
                "python": ">=3.11",
                "archive_root": UPGRADE_ROOT,
                "files": file_rows(upgrade, exclude={"manifests/package-manifest.json"}),
            },
        )

        scan_leaks(starter, kind="starter_vault")
        scan_leaks(upgrade, kind="agent_upgrade")

        starter_destination = output / STARTER_ASSET
        upgrade_destination = output / UPGRADE_ASSET
        make_zip(starter, starter_destination, archive_root=STARTER_ROOT, executables=set())
        make_zip(
            upgrade,
            upgrade_destination,
            archive_root=UPGRADE_ROOT,
            executables={"setup.sh", "installer/xirang_install.py"},
        )

        assets = []
        for kind, destination in (
            ("starter_vault", starter_destination),
            ("agent_upgrade", upgrade_destination),
        ):
            assets.append(
                {
                    "kind": kind,
                    "name": destination.name,
                    "sha256": sha256(destination),
                    "size": destination.stat().st_size,
                }
            )
        release = {
            "schema_version": 1,
            "product": "XiRang dual delivery package",
            "version": VERSION,
            "status": "candidate",
            "built_at": "2026-08-26",
            "support": {"platform": "macOS", "python": ">=3.11"},
            "assets": assets,
            "core_manifest_sha256": sha256(upgrade_core_manifest),
            "package_manifest_sha256": {
                "starter_vault": sha256(starter_package_manifest),
                "agent_upgrade": sha256(upgrade_package_manifest),
            },
            "interaction": {
                "starter_vault": "解压后直接用 Obsidian 打开顶层文件夹",
                "agent_upgrade": "把整个升级包交给当前可用的智能体，并让它先 plan 再 apply",
            },
        }
        atomic_json(output / "release-manifest.json", release)
        checksum_lines = [f"{asset['sha256']}  {asset['name']}" for asset in assets]
        (output / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
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
