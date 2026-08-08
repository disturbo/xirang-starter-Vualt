#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "02-项目管理" / "脚本" / "v9-task-review-queue.py"


def load_module():
    spec = importlib.util.spec_from_file_location("v9_task_review_queue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TaskReviewQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_submitted_card_reports_existing_and_missing_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            card = root / "02-项目管理/任务卡/2026-07/T-1.md"
            card.parent.mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "docs/present.md").write_text("ok", encoding="utf-8")
            card.write_text(
                "---\ntask_id: T-1\ntitle: Fixture\nreview_status: submitted\n"
                "deliverables:\n  - path: docs/present.md\n  - path: docs/missing.md\n---\n"
                "## 3. Handoff\n",
                encoding="utf-8",
            )
            payload = self.module.build_queue(root)
            self.assertEqual(1, payload["summary"]["awaiting_review"])
            item = payload["items"][0]
            self.assertEqual("missing_deliverables", item["evidence_state"])
            self.assertEqual(["docs/missing.md"], item["missing_deliverables"])
            self.assertTrue(item["has_handoff_section"])

    def test_non_review_state_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            card = root / "02-项目管理/任务卡/2026-07/T-2.md"
            card.parent.mkdir(parents=True)
            card.write_text("---\ntask_id: T-2\nreview_status: accepted\n---\n", encoding="utf-8")
            self.assertEqual(0, self.module.build_queue(root)["summary"]["awaiting_review"])

    def test_runtime_snapshot_migration_mapping(self) -> None:
        candidate = self.module.relocated_runtime_path(
            "02-项目管理/巡检/harness-eval-latest.json"
        )
        self.assertEqual(
            Path.home() / ".xirang/v9-runtime/巡检/harness-eval-latest.json",
            candidate,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
