#!/usr/bin/env python3
"""Regression tests for V9 Phase H long-session runtime stability."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IMPORTER = ROOT / ".standards/codex-cost-import.py"
ADAPTER = ROOT / ".standards/hooks/codex-hook-adapter.py"
HANDSHAKE = ROOT / ".standards/v8-handshake.sh"
REFLEX = ROOT / "02-项目管理/脚本/v9-reflex-check.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def row(kind: str, payload: dict, timestamp: str = "2026-07-18T20:00:00+08:00") -> bytes:
    return (json.dumps({"timestamp": timestamp, "type": kind, "payload": payload}) + "\n").encode()


def token_row(total: int, timestamp: str = "2026-07-18T20:00:00+08:00") -> bytes:
    usage = {
        "input_tokens": total - 20,
        "cached_input_tokens": 10,
        "cache_write_input_tokens": 0,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": total,
    }
    return row("event_msg", {"type": "token_count", "info": {"total_token_usage": usage}}, timestamp)


def prior(path: Path, model: str, usage: dict, timestamp: str, cursor: dict) -> dict:
    return {
        "rollout": str(path),
        "model": model,
        "usage": usage,
        "usage_timestamp": timestamp,
        "offset": cursor["offset"],
        "device": cursor["device"],
        "inode": cursor["inode"],
    }


class PhaseHTests(unittest.TestCase):
    def test_incremental_cursor_reads_only_tail_and_preserves_partial_line(self) -> None:
        module = load(IMPORTER, "phase_h_importer_incremental")
        with tempfile.TemporaryDirectory() as raw:
            rollout = Path(raw) / "rollout.jsonl"
            with rollout.open("wb") as handle:
                handle.write(row("turn_context", {"model": "gpt-5.6-sol"}))
                for index in range(10_000):
                    handle.write(row("event_msg", {"type": "noise", "index": index}))
                handle.write(token_row(100))

            model, usage, timestamp, first = module.rollout_metadata_incremental(rollout, {})
            self.assertTrue(first["reset"])
            self.assertEqual(rollout.stat().st_size, first["offset"])
            self.assertEqual("gpt-5.6-sol", model)
            self.assertEqual(100, usage["total_tokens"])

            with rollout.open("ab") as handle:
                handle.write(token_row(140, "2026-07-18T20:01:00+08:00"))
            model, usage, timestamp, second = module.rollout_metadata_incremental(
                rollout, prior(rollout, model, usage, timestamp, first),
            )
            self.assertFalse(second["reset"])
            self.assertLess(second["bytes_read"], 1024)
            self.assertEqual(140, usage["total_tokens"])

            complete_offset = second["offset"]
            partial = token_row(180, "2026-07-18T20:02:00+08:00").rstrip(b"\n")
            with rollout.open("ab") as handle:
                handle.write(partial)
            model2, usage2, timestamp2, third = module.rollout_metadata_incremental(
                rollout, prior(rollout, model, usage, timestamp, second),
            )
            self.assertEqual(complete_offset, third["offset"])
            self.assertEqual(0, third["bytes_read"])
            self.assertEqual(140, usage2["total_tokens"])

            with rollout.open("ab") as handle:
                handle.write(b"\n")
            _model3, usage3, _timestamp3, fourth = module.rollout_metadata_incremental(
                rollout, prior(rollout, model2, usage2, timestamp2, third),
            )
            self.assertGreater(fourth["bytes_read"], 0)
            self.assertEqual(180, usage3["total_tokens"])

    def test_truncation_resets_cursor_and_drops_stale_metadata(self) -> None:
        module = load(IMPORTER, "phase_h_importer_truncate")
        with tempfile.TemporaryDirectory() as raw:
            rollout = Path(raw) / "rollout.jsonl"
            rollout.write_bytes(row("turn_context", {"model": "old-model"}) + token_row(999))
            model, usage, timestamp, cursor = module.rollout_metadata_incremental(rollout, {})
            previous = prior(rollout, model, usage, timestamp, cursor)
            rollout.write_bytes(row("turn_context", {"model": "new-model"}) + token_row(12))

            model, usage, _timestamp, cursor = module.rollout_metadata_incremental(rollout, previous)
            self.assertTrue(cursor["reset"])
            self.assertEqual("new-model", model)
            self.assertEqual(12, usage["total_tokens"])

    def test_codex_hooks_pin_system_python(self) -> None:
        adapter = load(ADAPTER, "phase_h_adapter")
        reflex = load(REFLEX, "phase_h_reflex")
        env = adapter.hook_env(ROOT)
        self.assertTrue(env["PATH"].startswith("/usr/bin:/bin:/usr/sbin:/sbin:"))
        self.assertEqual("/usr/bin/python3", env["XIRANG_PYTHON_BIN"])

        lines = HANDSHAKE.read_text(encoding="utf-8").splitlines()
        self.assertIn('V8_PYTHON="${XIRANG_PYTHON_BIN:-/usr/bin/python3}"', lines)
        executable_bare = [
            line for line in lines
            if "python3" in line and not line.lstrip().startswith("#") and not line.startswith("V8_PYTHON=")
        ]
        self.assertEqual([], executable_bare)
        parsed = reflex.parse_iso("2026-07-18T15:07:37.303Z")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
