#!/usr/bin/env python3
"""Regression tests for V9 Phase H long-session runtime stability."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / ".standards/hooks/codex-hook-adapter.py"
HANDSHAKE = ROOT / ".standards/v8-handshake.sh"
REFLEX = ROOT / "02-项目管理/脚本/v9-reflex-check.py"
COST_HOOK = ROOT / ".standards/hooks/cost-event.sh"
COST_FUSE = ROOT / ".standards/cost-fuse.py"
SPAWN_BUDGET = ROOT / ".standards/spawn-budget-check.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class PhaseHTests(unittest.TestCase):
    def test_codex_adapter_pins_system_python(self) -> None:
        adapter = load(ADAPTER, "phase_h_adapter")
        env = adapter.hook_env(ROOT)
        self.assertTrue(env["PATH"].startswith("/usr/bin:/bin:/usr/sbin:/sbin:"))
        self.assertEqual("/usr/bin/python3", env["XIRANG_PYTHON_BIN"])
        self.assertNotIn("codex-cost-import.py", ADAPTER.read_text(encoding="utf-8"))

    def test_handshake_uses_pinned_python(self) -> None:
        lines = HANDSHAKE.read_text(encoding="utf-8").splitlines()
        self.assertIn('V8_PYTHON="${XIRANG_PYTHON_BIN:-/usr/bin/python3}"', lines)
        executable_bare = [
            line for line in lines
            if "python3" in line and not line.lstrip().startswith("#") and not line.startswith("V8_PYTHON=")
        ]
        self.assertEqual([], executable_bare)
        self.assertNotIn("cost-event.sh", "\n".join(lines))

    def test_cost_pipeline_is_explicitly_retired(self) -> None:
        self.assertNotIn("codex_cost_telemetry", REFLEX.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as raw:
            env = {**os.environ, "VAULT_ROOT": raw}
            proc = subprocess.run(
                ["/bin/bash", str(COST_HOOK), "start", "T-RETIRED", "hongmeisu"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(3, proc.returncode)
            self.assertIn("V9-RETIRED", proc.stderr)
            self.assertFalse((Path(raw) / "02-项目管理/智能体状态/智能体事件.jsonl").exists())
        for tool in (COST_FUSE, SPAWN_BUDGET):
            proc = subprocess.run(["/usr/bin/python3", str(tool)], capture_output=True, text=True)
            self.assertEqual(3, proc.returncode)
            self.assertIn('"status": "retired"', proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
