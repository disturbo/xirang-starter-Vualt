#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


VAULT = Path(__file__).resolve().parents[2]
VERIFY = VAULT / ".standards/harness-eval-verify.py"
GATE = VAULT / ".standards/gate-enforce.py"
ACCEPT = VAULT / ".standards/v9-accept.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PhaseCTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="v9-phase-c-")
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        standards = self.root / ".standards"
        standards.mkdir(parents=True)
        (standards / "harness-eval-verify.py").symlink_to(VERIFY)
        (standards / "harness-tested-files.txt").write_text("subject.py\n", encoding="utf-8")
        (self.root / "subject.py").write_text("stable\n", encoding="utf-8")
        self.report_path = self.root / "harness-eval-latest.json"
        self.write_report()

    def write_report(self, generated_at: str | None = None) -> None:
        digest = hashlib.sha256((self.root / "subject.py").read_bytes()).hexdigest()[:16]
        report = {
            "check": "v9-harness-eval-runner",
            "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": {"total": 1, "passed": 1, "failed": 0, "missed_negative": 0, "meta_failed": 0},
            "tested_hashes": {"subject.py": digest},
        }
        self.report_path.write_text(json.dumps(report), encoding="utf-8")

    def test_verifier_detects_hash_change_after_green_report(self) -> None:
        module = load(VERIFY, "phase_c_verify")
        self.assertTrue(module.verify_path(self.report_path, self.root, 24)["valid"])
        (self.root / "subject.py").write_text("changed\n", encoding="utf-8")
        observed = module.verify_path(self.report_path, self.root, 24)
        self.assertFalse(observed["valid"])
        self.assertIn("HARNESS_HASH_STALE", {r["rule_id"] for r in observed["reasons"]})

    def test_gate_rejects_future_report_even_when_hashes_match(self) -> None:
        self.write_report((datetime.now().astimezone() + timedelta(minutes=10)).isoformat())
        result = subprocess.run(
            [
                "python3", str(GATE), "pre-accept", "--task-id", "T-CANARY",
                "--require-fresh-eval", "--eval-report", str(self.report_path), "--json",
            ],
            cwd=self.root,
            env={"VAULT_ROOT": str(self.root)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("STALE_EVAL", {v["rule_id"] for v in report["violations"]})

    def test_v9_accept_rejects_draft_to_accepted_transition(self) -> None:
        module = load(ACCEPT, "phase_c_accept")
        card = "---\nreview_status: draft\nreviewer: 波波\n---\n"
        with self.assertRaisesRegex(ValueError, "invalid acceptance transition"):
            module.build_candidate(card, "波波", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
