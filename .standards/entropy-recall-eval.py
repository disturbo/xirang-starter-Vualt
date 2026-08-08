#!/usr/bin/env python3
"""Evaluate doc-gardening v2 recall on an isolated current-structure copy.

The live Vault is read-only. Known broken links and known orphan notes are
seeded only under a disposable work directory (default: /tmp).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_VAULT = Path.home() / "Desktop" / "obsidianVault"
DEFAULT_DETECTOR = (
    Path.home()
    / ".hermes/skills/yijing-dms/spec-auto-fusion/scripts/doc-gardening.py"
)
DEFAULT_WORK_DIR = Path("/tmp/v9-entropy-recall-baseline")
CURRENT_STRUCTURE_PATHS = (
    Path("10-项目/迭代/260725迭代"),
    Path("20-资料/参考系统/源码参考/code0711"),
)


@dataclass(frozen=True)
class Seed:
    seed_id: str
    category: str
    confidence: str
    source: str
    target: str = ""


def build_seeds() -> list[Seed]:
    seeds: list[Seed] = []
    for index in range(1, 11):
        branch = "10-项目/迭代/260725迭代" if index <= 5 else "20-资料/参考系统/源码参考/code0711"
        seeds.append(Seed(
            seed_id=f"broken-{index:02d}",
            category="broken_link",
            confidence="confirmed",
            source=f"{branch}/_v9-recall-seeds/broken-{index:02d}.md",
            target=f"{branch}/不存在-召回种子-{index:02d}",
        ))
    for index in range(1, 6):
        branch = "10-项目/迭代/260725迭代" if index <= 3 else "20-资料/参考系统/源码参考/code0711"
        seeds.append(Seed(
            seed_id=f"orphan-{index:02d}",
            category="orphan",
            confidence="needs_review",
            source=f"{branch}/_v9-recall-seeds/orphan-{index:02d}.md",
        ))
    return seeds


def prepare_copy(vault: Path, work_dir: Path, seeds: list[Seed]) -> Path:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_vault = work_dir / "vault"
    work_vault.mkdir(parents=True)
    for relative in CURRENT_STRUCTURE_PATHS:
        source = vault / relative
        if not source.is_dir():
            raise FileNotFoundError(f"current-structure path missing: {source}")
        shutil.copytree(source, work_vault / relative)

    for seed in seeds:
        path = work_vault / seed.source
        path.parent.mkdir(parents=True, exist_ok=True)
        if seed.category == "broken_link":
            path.write_text(
                f"---\nv9_recall_seed: {seed.seed_id}\n---\n"
                f"# V9 recall seed {seed.seed_id}\n\n[[{seed.target}]]\n",
                encoding="utf-8",
            )
        else:
            path.write_text(
                f"---\nv9_recall_seed: {seed.seed_id}\n---\n"
                f"# V9 isolated orphan seed {seed.seed_id}\n\n"
                "This note intentionally has no inbound semantic reference.\n",
                encoding="utf-8",
            )
    return work_vault


def evaluate(detector: Path, work_vault: Path, seeds: list[Seed]) -> dict:
    result = subprocess.run(
        [sys.executable, str(detector), "--vault", str(work_vault), "--no-write", "--json"],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    actual = {
        (
            item.get("category", ""),
            item.get("confidence", ""),
            item.get("source", ""),
            item.get("target", ""),
        )
        for item in payload["findings"]
    }
    checks = []
    for seed in seeds:
        signature = (seed.category, seed.confidence, seed.source, seed.target)
        checks.append({**asdict(seed), "detected": signature in actual})
    detected = sum(1 for check in checks if check["detected"])
    by_category = {}
    for category in sorted({seed.category for seed in seeds}):
        selected = [check for check in checks if check["category"] == category]
        hit = sum(1 for check in selected if check["detected"])
        by_category[category] = {
            "detected": hit,
            "seeded": len(selected),
            "recall": hit / len(selected),
        }
    return {
        "detector_version": payload.get("detector_version"),
        "mode": "isolated_recall_evaluation",
        "live_vault_mutated": False,
        "work_vault": str(work_vault),
        "current_structure_paths": [str(path) for path in CURRENT_STRUCTURE_PATHS],
        "seeded": len(seeds),
        "detected": detected,
        "recall": detected / len(seeds),
        "by_category": by_category,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--detector", type=Path, default=DEFAULT_DETECTOR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    seeds = build_seeds()
    work_vault = prepare_copy(args.vault.resolve(), args.work_dir.resolve(), seeds)
    result = evaluate(args.detector.resolve(), work_vault, seeds)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"recall={result['detected']}/{result['seeded']} "
            f"({result['recall']:.1%}); live_vault_mutated=false"
        )
    return 0 if result["detected"] == result["seeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
