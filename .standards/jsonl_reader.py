#!/usr/bin/env python3
"""Shared tolerant JSONL reader for V9 observability consumers.

Malformed historical rows are never rewritten here. Consumers receive valid
object rows plus explicit diagnostics, so degraded input cannot look complete.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JsonlDiagnostics:
    path: str
    physical_lines: int = 0
    blank_lines: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    invalid_line_samples: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_jsonl(
    path: Path,
    *,
    warn: bool = False,
    sample_limit: int = 10,
) -> tuple[list[dict[str, Any]], JsonlDiagnostics]:
    """Return valid JSON object rows and diagnostics without mutating *path*."""
    rows: list[dict[str, Any]] = []
    physical = blank = invalid = 0
    samples: list[int] = []

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                physical += 1
                text = line.strip()
                if not text:
                    blank += 1
                    continue
                try:
                    value = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    value = None
                if not isinstance(value, dict):
                    invalid += 1
                    if len(samples) < sample_limit:
                        samples.append(line_no)
                    continue
                rows.append(value)

    diagnostics = JsonlDiagnostics(
        path=str(path),
        physical_lines=physical,
        blank_lines=blank,
        valid_rows=len(rows),
        invalid_rows=invalid,
        invalid_line_samples=tuple(samples),
    )
    if warn and invalid:
        print(
            f"[JSONL-WARN] {path}: skipped {invalid} invalid rows "
            f"(valid={len(rows)}, samples={samples})",
            file=sys.stderr,
        )
    return rows, diagnostics
