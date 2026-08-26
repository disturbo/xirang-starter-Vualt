#!/usr/bin/env python3
"""Audit frontend files for visual consistency drift.

This script is intentionally read-only. It reports likely sources of visual
entropy: raw colors, font declarations, icon-library mixing, type sizes, and
border-radius values.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable


TEXT_EXTENSIONS = {
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".astro",
    ".mdx",
}

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "coverage",
    ".turbo",
    ".cache",
    "vendor",
}

COLOR_PATTERNS = [
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch)\([^;\n)]*\)", re.IGNORECASE),
]

CSS_VAR_COLOR_RE = re.compile(r"--[a-zA-Z0-9_-]*(?:color|colour|bg|background|surface|border|text|accent|primary|secondary|success|warning|danger|error)[a-zA-Z0-9_-]*")
FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;\n}]+)", re.IGNORECASE)
FONT_IMPORT_RE = re.compile(r"@import\s+url\(([^)]*fonts[^)]*)\)", re.IGNORECASE)
FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([^;\n}]+)", re.IGNORECASE)
RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;\n}]+)", re.IGNORECASE)

IMPORT_RE = re.compile(r"from\s+['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]\s*\)")
ICON_SOURCES = [
    "lucide",
    "lucide-react",
    "react-icons",
    "@heroicons/react",
    "@mui/icons-material",
    "@material-ui/icons",
    "antd",
    "@ant-design/icons",
    "phosphor-react",
    "@phosphor-icons/react",
    "@tabler/icons-react",
    "@fortawesome",
    "bootstrap-icons",
    "remixicon",
]


def iter_files(paths: Iterable[Path], max_files: int | None) -> Iterable[Path]:
    seen = 0
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            if path.suffix.lower() in TEXT_EXTENSIONS:
                yield path
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for name in files:
                file_path = Path(root) / name
                if file_path.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                yield file_path
                seen += 1
                if max_files is not None and seen >= max_files:
                    return


def safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def sample_path(counter: dict[str, list[str]], token: str, path: Path, limit: int) -> None:
    samples = counter.setdefault(token, [])
    text = str(path)
    if len(samples) < limit and text not in samples:
        samples.append(text)


def top(counter: collections.Counter[str], limit: int) -> list[dict[str, object]]:
    return [{"value": value, "count": count} for value, count in counter.most_common(limit)]


def detect_icon_source(module: str) -> str | None:
    for source in ICON_SOURCES:
        if module == source or module.startswith(source + "/"):
            return source
    if "/icons" in module or module.endswith("-icons"):
        return module
    return None


def audit(paths: list[Path], max_files: int | None, sample_limit: int) -> dict[str, object]:
    files = list(iter_files(paths, max_files))
    colors: collections.Counter[str] = collections.Counter()
    color_vars: collections.Counter[str] = collections.Counter()
    font_families: collections.Counter[str] = collections.Counter()
    font_imports: collections.Counter[str] = collections.Counter()
    font_sizes: collections.Counter[str] = collections.Counter()
    radii: collections.Counter[str] = collections.Counter()
    icon_sources: collections.Counter[str] = collections.Counter()

    samples: dict[str, dict[str, list[str]]] = {
        "colors": {},
        "font_families": {},
        "font_sizes": {},
        "radii": {},
        "icon_sources": {},
    }

    for file_path in files:
        text = safe_read(file_path)
        if not text:
            continue

        for pattern in COLOR_PATTERNS:
            for match in pattern.findall(text):
                token = match.strip()
                colors[token] += 1
                sample_path(samples["colors"], token, file_path, sample_limit)

        for match in CSS_VAR_COLOR_RE.findall(text):
            color_vars[match] += 1

        for match in FONT_FAMILY_RE.findall(text):
            token = " ".join(match.strip().split())
            font_families[token] += 1
            sample_path(samples["font_families"], token, file_path, sample_limit)

        for match in FONT_IMPORT_RE.findall(text):
            token = match.strip("'\" ")
            font_imports[token] += 1

        for match in FONT_SIZE_RE.findall(text):
            token = " ".join(match.strip().split())
            font_sizes[token] += 1
            sample_path(samples["font_sizes"], token, file_path, sample_limit)

        for match in RADIUS_RE.findall(text):
            token = " ".join(match.strip().split())
            radii[token] += 1
            sample_path(samples["radii"], token, file_path, sample_limit)

        for import_match in IMPORT_RE.findall(text):
            module = import_match[0] or import_match[1]
            source = detect_icon_source(module)
            if source:
                icon_sources[source] += 1
                sample_path(samples["icon_sources"], source, file_path, sample_limit)

    return {
        "files_scanned": len(files),
        "raw_colors": {
            "unique": len(colors),
            "total": sum(colors.values()),
            "top": top(colors, 30),
            "samples": samples["colors"],
        },
        "color_variables": {
            "unique": len(color_vars),
            "top": top(color_vars, 30),
        },
        "font_families": {
            "unique": len(font_families),
            "top": top(font_families, 30),
            "samples": samples["font_families"],
        },
        "font_imports": {
            "unique": len(font_imports),
            "top": top(font_imports, 30),
        },
        "font_sizes": {
            "unique": len(font_sizes),
            "top": top(font_sizes, 30),
            "samples": samples["font_sizes"],
        },
        "border_radii": {
            "unique": len(radii),
            "top": top(radii, 30),
            "samples": samples["radii"],
        },
        "icon_sources": {
            "unique": len(icon_sources),
            "top": top(icon_sources, 30),
            "samples": samples["icon_sources"],
        },
    }


def print_text_report(result: dict[str, object]) -> None:
    print(f"Files scanned: {result['files_scanned']}")
    print()

    sections = [
        ("Raw colors", "raw_colors"),
        ("Color variables", "color_variables"),
        ("Font families", "font_families"),
        ("Font imports", "font_imports"),
        ("Font sizes", "font_sizes"),
        ("Border radii", "border_radii"),
        ("Icon sources", "icon_sources"),
    ]
    for title, key in sections:
        data = result[key]
        print(f"{title}: {data['unique']} unique")
        for item in data["top"][:12]:
            print(f"  {item['count']:>4}  {item['value']}")
        print()

    notes = []
    if result["raw_colors"]["unique"] > 20:
        notes.append("Many raw colors detected. Prefer semantic tokens or CSS variables.")
    if result["font_families"]["unique"] > 2:
        notes.append("Multiple font-family declarations detected. Check for accidental font sprawl.")
    if result["icon_sources"]["unique"] > 1:
        notes.append("Multiple icon sources detected. Consolidate within the touched surface.")
    if result["border_radii"]["unique"] > 5:
        notes.append("Many border-radius values detected. Normalize to the project scale.")

    if notes:
        print("Review notes:")
        for note in notes:
            print(f"  - {note}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit frontend files for visual consistency drift.")
    parser.add_argument("paths", nargs="+", type=Path, help="Files or directories to scan")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--max-files", type=int, default=None, help="Maximum number of files to scan")
    parser.add_argument("--sample-limit", type=int, default=3, help="Maximum sample paths per token")
    args = parser.parse_args()

    result = audit(args.paths, args.max_files, args.sample_limit)
    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        print_text_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
