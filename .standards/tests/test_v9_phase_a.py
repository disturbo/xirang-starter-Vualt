#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


VAULT = Path(__file__).resolve().parents[2]
STATUS_SUMMARY = VAULT / "02-项目管理/脚本/v9-status-summary.py"
RUNNER = VAULT / ".standards/v9-reflex-run.sh"
WORKBENCH = Path.home() / "Desktop/xirang-workbench/main.js"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class PhaseATests(unittest.TestCase):
    def test_status_summary_rejects_future_clock_as_fresh(self) -> None:
        spec = importlib.util.spec_from_file_location("v9_status_summary", STATUS_SUMMARY)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        now = datetime.now(timezone.utc).astimezone()
        report = {"generated_at": (now + timedelta(minutes=10)).isoformat()}
        observed = module.freshness(report, now, 24)
        self.assertEqual("clock_skew", observed["state"])
        self.assertEqual("yellow", observed["status"])

    def test_runner_publishes_status_in_same_transaction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v9-phase-a-runner-") as raw:
            root = Path(raw)
            runtime = root / "runtime"
            reflex = root / "reflex.py"
            summary = root / "summary.py"
            harness_runner = root / "harness-runner.py"
            harness_verifier = root / "harness-verifier.py"
            write(harness_runner, "raise SystemExit(99)\n")
            write(harness_verifier, "raise SystemExit(0)\n")
            write(reflex, """import json, os
from datetime import datetime
from pathlib import Path
p = Path(os.environ['XIRANG_V9_RUNTIME_DIR']) / '巡检/health-latest.json'
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({'generated_at': datetime.now().astimezone().isoformat()}))
""")
            write(summary, """import json, os
from datetime import datetime
from pathlib import Path
i = Path(os.environ['XIRANG_V9_RUNTIME_DIR']) / '巡检'
h = json.loads((i / 'health-latest.json').read_text())
now = datetime.now().astimezone().isoformat()
parts = {'health': {'status': 'red', 'generated_at': h['generated_at']}, 'harness_eval': {'status': 'green'}, 'iteration_ops': {'status': 'green'}}
r = {'schema_version':'v1','generated_at':now,'status':'red','paths':{'runtime_dir':str(i),'status_latest':str(i/'status-latest.json')},'parts':parts}
(i / 'status-latest.json').write_text(json.dumps(r))
raise SystemExit(1)
""")
            result = subprocess.run(
                ["bash", str(RUNNER)], text=True, capture_output=True, check=False,
                env={
                    **os.environ,
                    "XIRANG_V9_VAULT_DIR": str(root),
                    "XIRANG_V9_RUNTIME_DIR": str(runtime),
                    "XIRANG_V9_PYTHON": os.sys.executable,
                    "XIRANG_V9_REFLEX_SCRIPT": str(reflex),
                    "XIRANG_V9_STATUS_SCRIPT": str(summary),
                    "XIRANG_V9_HARNESS_SCRIPT": str(harness_runner),
                    "XIRANG_V9_HARNESS_VERIFY_SCRIPT": str(harness_verifier),
                },
            )
            self.assertEqual(0, result.returncode, result.stderr)
            state = json.loads((runtime / "巡检/reflex-scheduler-health.json").read_text())
            self.assertEqual("success", state["status"])
            self.assertEqual("completed_status_red", state["reason"])

    def test_workbench_rejects_status_older_than_health(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v9-phase-a-workbench-") as raw:
            inspect = Path(raw) / "巡检"
            inspect.mkdir(parents=True)
            now = datetime.now(timezone.utc).astimezone().isoformat()
            health = inspect / "health-latest.json"
            harness = inspect / "harness-eval-latest.json"
            status = inspect / "status-latest.json"
            write(health, "{}\n")
            write(harness, "{}\n")
            report = {
                "schema_version": "v1",
                "generated_at": now,
                "max_age_hours": 24,
                "status": "green",
                "paths": {
                    "runtime_dir": str(inspect),
                    "status_latest": str(status),
                    "health_latest": str(health),
                    "harness_eval_latest": str(harness),
                },
                "parts": {
                    "health": {"status": "green", "freshness": {"state": "fresh"}},
                    "harness_eval": {"status": "green", "freshness": {"state": "fresh"}},
                    "iteration_ops": {"status": "green"},
                },
                "ui": {"badges": [], "actions": []},
            }
            write(status, json.dumps(report))
            newer = status.stat().st_mtime + 5
            os.utime(health, (newer, newer))
            node = r"""
const Module = require('module');
const original = Module._load;
class Base {}
Module._load = function(request, parent, isMain) {
  if (request === 'obsidian') return {Plugin:Base, ItemView:Base, PluginSettingTab:Base, Notice:Base, Setting:Base, TFile:Base};
  return original.apply(this, arguments);
};
global.window = {};
const Plugin = require(process.argv[1]).default;
const plugin = Object.create(Plugin.prototype);
plugin.settings = {inspectDir: process.argv[2]};
plugin.readStatusReport().then(() => process.exit(9)).catch((error) => {
  if (!String(error.message).includes('落后于来源')) { console.error(error); process.exit(8); }
});
"""
            result = subprocess.run(
                ["node", "-e", node, str(WORKBENCH), str(inspect)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
