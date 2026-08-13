#!/usr/bin/env python3
"""Regression tests for V9 Phase E knowledge/governance truth chains."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTROPY = ROOT / "02-项目管理/脚本/v9-entropy-governance.py"
LLM_WIKI = ROOT / ".standards/scripts/llm_wiki_check.py"
GBRAIN_VERIFY = Path.home() / ".gbrain/verify-runtime-contract.py"
SEMANTIC_RECALL = ROOT / ".standards/semantic-recall.py"
SCOPE_TAMPER = ROOT / "02-项目管理/脚本/v9-scope-tamper-check.py"
REFLEX = ROOT / "02-项目管理/脚本/v9-reflex-check.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class PhaseETests(unittest.TestCase):
    def test_idle_stale_scope_is_not_an_active_authorization_debt(self) -> None:
        module = load(SCOPE_TAMPER, "scope_tamper_idle")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / "02-项目管理/智能体状态/Claudian.md"
            status.parent.mkdir(parents=True)
            status.write_text(
                '---\nagent_id: claudian\nstatus: idle\ncurrent_task_id: null\n'
                'write_scope: "10-项目/迭代/260828迭代/"\n---\n',
                encoding="utf-8",
            )
            self.assertEqual([], module.check_status_file(status, root / "_temp"))

    def test_scope_checker_falls_back_to_formal_task_card(self) -> None:
        module = load(SCOPE_TAMPER, "scope_tamper_formal_card")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            card = root / "02-项目管理/任务卡/2026-08/T-TEST.md"
            card.parent.mkdir(parents=True)
            card.write_text(
                '---\ntask_id: T-TEST\npaths:\n  allowed_write_roots:\n'
                '    - "02-项目管理/"\n---\n',
                encoding="utf-8",
            )
            status = root / "02-项目管理/智能体状态/红霉素.md"
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text(
                '---\nagent_id: hongmeisu\nstatus: busy\ncurrent_task_id: T-TEST\n'
                'write_scope: "02-项目管理/脚本/"\n---\n',
                encoding="utf-8",
            )
            self.assertEqual([], module.check_status_file(status, root / "_temp"))

    def test_normal_hitl_queue_is_not_labeled_governance_debt(self) -> None:
        module = load(REFLEX, "reflex_review_queue")
        with tempfile.TemporaryDirectory() as raw:
            scripts = Path(raw)
            fake = scripts / "v9-task-state-check.py"
            fake.write_text(
                'import json\nprint(json.dumps({"findings": [], "summary": '
                '{"done_missing_review_status": 0, "awaiting_review": 53}}))\n',
                encoding="utf-8",
            )
            original = module.SCRIPT_DIR
            try:
                module.SCRIPT_DIR = scripts
                self.assertEqual([], module.collect_task_state())
            finally:
                module.SCRIPT_DIR = original

    def test_semantic_recall_consumes_results_without_logging_raw_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            fake = temp / "gbrain"
            fake.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                "'[0.9910] 50-经验/agent协作方法论/息壤v9-运行时契约卡 -- current contract'\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            result = subprocess.run([
                "/usr/bin/python3", str(SEMANTIC_RECALL), "--query", "private task title",
                "--source", "task_start", "--task-id", "T-TEST", "--vault", str(temp),
                "--gbrain", str(fake),
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual("success", report["status"])
            self.assertTrue(report["contract_hit"])
            event_file = temp / "02-项目管理/智能体状态/智能体事件.jsonl"
            event = json.loads(event_file.read_text(encoding="utf-8"))
            self.assertEqual("semantic_recall", event["event"])
            self.assertEqual("T-TEST", event["task_id"])
            self.assertNotIn("query", event)
            self.assertEqual(64, len(event["query_sha256"]))

    def test_entropy_queue_is_idempotent_human_gated_and_tracks_net_backlog(self) -> None:
        module = load(ENTROPY, "entropy_governance")
        detector = {
            "detector_version": "2.0.0", "mode": "shadow",
            "findings": [
                {"category": "broken_link", "confidence": "confirmed", "source": "A.md", "target": "Missing/X", "reason": "missing"},
                {"category": "orphan", "confidence": "needs_review", "source": "B.md", "target": "", "reason": "review"},
            ],
        }
        first = module.ingest({}, detector, "2026-07-18T20:00:00+08:00")
        self.assertEqual(1, first["metrics"]["pending_confirmation"])
        self.assertEqual(1, first["metrics"]["new_since_previous"])
        self.assertEqual(1, first["metrics"]["net_backlog_delta"])
        item_id = first["items"][0]["id"]
        decided = module.decide(first, item_id, "confirm", "human-reviewer", "verified", "2026-07-18T20:01:00+08:00")
        self.assertEqual("confirmed_for_action", decided["items"][0]["status"])
        second = module.ingest(decided, detector, "2026-07-18T20:02:00+08:00")
        self.assertEqual("confirmed_for_action", second["items"][0]["status"])
        self.assertEqual(0, second["metrics"]["new_since_previous"])
        self.assertEqual(0, second["metrics"]["net_backlog_delta"])
        third = module.ingest(second, {**detector, "findings": []}, "2026-07-18T20:03:00+08:00")
        self.assertEqual("resolved", third["items"][0]["status"])
        self.assertEqual(1, third["metrics"]["resolved_since_previous"])
        self.assertEqual(-1, third["metrics"]["net_backlog_delta"])

    def test_entropy_unclaimed_items_converge_and_changed_evidence_reopens(self) -> None:
        module = load(ENTROPY, "entropy_governance_default_disposition")
        finding = {
            "category": "broken_link", "confidence": "confirmed", "source": "A.md",
            "target": "Missing/X", "reason": "missing",
        }
        detector = {"detector_version": "2.0.0", "mode": "shadow", "findings": [finding]}
        queue = module.ingest({}, detector, "2026-07-01T09:00:00+08:00")
        self.assertEqual("pending_confirmation", queue["items"][0]["status"])
        same_week = module.ingest(queue, detector, "2026-07-02T09:00:00+08:00")
        self.assertEqual("pending_confirmation", same_week["items"][0]["status"])
        self.assertEqual(1, same_week["items"][0]["unclaimed_cycles"])
        queue = same_week
        queue = module.ingest(queue, detector, "2026-07-08T09:00:00+08:00")
        self.assertEqual("deferred", queue["items"][0]["status"])
        self.assertEqual(1, queue["metrics"]["current_open"])
        queue = module.ingest(queue, detector, "2026-07-15T09:00:00+08:00")
        queue = module.ingest(queue, detector, "2026-07-22T09:00:00+08:00")
        self.assertEqual("deferred", queue["items"][0]["status"])
        self.assertEqual(1, queue["metrics"]["current_open"])
        changed = {**finding, "reason": "target removed from current prototype"}
        queue = module.ingest(queue, {**detector, "findings": [changed]}, "2026-07-29T09:00:00+08:00")
        self.assertEqual("pending_confirmation", queue["items"][0]["status"])
        self.assertEqual(1, queue["items"][0]["unclaimed_cycles"])
        self.assertIsNone(queue["items"][0]["decision"])

    @unittest.skip("production-only prototype path is intentionally absent from starter")
    def test_llm_wiki_uses_only_exact_725_relative_paths(self) -> None:
        module = load(LLM_WIKI, "llm_wiki_check")
        self.assertEqual(Path("/Users/yudongbo/Desktop/沙箱/奕境项目/奕境DMS/725"), module.PROTOTYPE_ROOT)
        self.assertTrue(module.prototype_exists("pc/pages/report/repair-daily-report.html"))
        self.assertFalse(module.prototype_exists("report/repair-daily-report.html"))

    def test_gbrain_verifier_requires_body_identity_and_semantic_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            source = temp / "contract.md"
            source.write_text("""---
updated: 2026-07-18
---
# 息壤 V9 运行时契约卡
## 铁律 10：运行时不能假绿
`~/.xirang/v9-runtime/巡检/status-latest.json`
自动召回留下 `semantic_recall` 消费事件。
""", encoding="utf-8")
            fake = temp / "gbrain"
            fake.write_text("""#!/bin/sh
if [ "$1" = get ]; then
  printf '%s\\n' '---' "updated: '2026-07-18T00:00:00.000Z'" '---' '# 息壤 V9 运行时契约卡' '## 铁律 10：运行时不能假绿' '`~/.xirang/v9-runtime/巡检/status-latest.json`' '自动召回留下 `semantic_recall` 消费事件。'
else
  printf '%s\\n' '[0.99] 50-经验/agent协作方法论/息壤v9-运行时契约卡 -- current'
fi
""", encoding="utf-8")
            fake.chmod(0o755)
            result = subprocess.run([
                "/usr/bin/python3", str(GBRAIN_VERIFY), "--source", str(source),
                "--gbrain", str(fake), "--json",
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("success", json.loads(result.stdout)["status"])
            source.write_text(source.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
            drift = subprocess.run([
                "/usr/bin/python3", str(GBRAIN_VERIFY), "--source", str(source),
                "--gbrain", str(fake), "--json",
            ], capture_output=True, text=True)
            self.assertNotEqual(0, drift.returncode)
            self.assertIn("indexed_body_mismatch", json.loads(drift.stdout)["failures"])

    def test_gbrain_verifier_reports_semantic_query_timeout_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            source = temp / "contract.md"
            source.write_text("""---
updated: 2026-07-18
---
# 息壤 V9 运行时契约卡
## 铁律 10：运行时不能假绿
`~/.xirang/v9-runtime/巡检/status-latest.json`
自动召回留下 `semantic_recall` 消费事件。
""", encoding="utf-8")
            fake = temp / "gbrain"
            fake.write_text("""#!/bin/sh
if [ "$1" = get ]; then
  printf '%s\n' '---' "updated: '2026-07-18T00:00:00.000Z'" '---' '# 息壤 V9 运行时契约卡' '## 铁律 10：运行时不能假绿' '`~/.xirang/v9-runtime/巡检/status-latest.json`' '自动召回留下 `semantic_recall` 消费事件。'
else
  sleep 2
fi
""", encoding="utf-8")
            fake.chmod(0o755)
            result = subprocess.run([
                "/usr/bin/python3", str(GBRAIN_VERIFY), "--source", str(source),
                "--gbrain", str(fake), "--query-timeout", "1", "--json",
            ], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            report = json.loads(result.stdout)
            self.assertEqual("failed", report["status"])
            self.assertIsNone(report["query_exit"])
            self.assertEqual(1, report["query_timeout_seconds"])
            self.assertEqual(["semantic_query_timeout"], report["failures"])
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
