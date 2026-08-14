#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "frontmatter-lint.py"


def load_linter():
    spec = importlib.util.spec_from_file_location("frontmatter_lint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FrontmatterLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.linter = load_linter()

    def test_nested_fields_do_not_override_top_level(self) -> None:
        fields, error = self.linter.parse_frontmatter(
            "---\nstatus: done\ntype: task_card\ndeliverables:\n  - type: run-log\n    status: generated\n---\n"
        )
        self.assertIsNone(error)
        self.assertEqual("done", fields["status"])
        self.assertEqual("task_card", fields["type"])

    def test_multiline_tags_are_present(self) -> None:
        fields, error = self.linter.parse_frontmatter(
            "---\ntitle: Fixture\ncreated: 2026-07-22\ntags:\n  - test\n---\n"
        )
        self.assertIsNone(error)
        self.assertTrue(fields["tags"].startswith("-"))

    def test_v_prefixed_version_is_valid(self) -> None:
        findings = self.linter.validate_field_values({"version": "v1.2.3"}, "规范")
        self.assertEqual([], findings)

    def test_v9_task_card_values_are_valid(self) -> None:
        fields = {
            "status": "done",
            "maturity": "implemented",
            "type": "task_card",
            "version": "v1.0",
        }
        self.assertEqual([], self.linter.validate_field_values(fields, "任务卡"))

    def test_v9_type_taxonomy_is_extensible(self) -> None:
        findings = self.linter.validate_field_values({"type": "runtime-authorization"}, "通用")
        self.assertEqual([], findings)

    def test_document_lifecycle_taxonomy_is_extensible(self) -> None:
        fields = {"status": "当前有效", "maturity": "workbench_mvp_verified"}
        self.assertEqual([], self.linter.validate_field_values(fields, "通用"))

    def test_task_status_remains_closed(self) -> None:
        findings = self.linter.validate_field_values({"status": "arbitrary"}, "任务卡")
        self.assertEqual(1, len(findings))
        self.assertEqual("enum_invalid", findings[0]["type"])

    def test_template_created_placeholder_is_valid(self) -> None:
        for value in ("{{date}}", "YYYY-MM-DD"):
            with self.subTest(value=value):
                self.assertEqual([], self.linter.validate_field_values({"created": value}, "通用"))

    def test_formal_task_card_schema_is_selected(self) -> None:
        name, _ = self.linter.detect_schema(
            "02-项目管理/任务卡/2026-07/T-20260722-01.md", {"task_id": "T-20260722-01"}
        )
        self.assertEqual("任务卡", name)

    def test_task_created_at_satisfies_created_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            card = root / "02-项目管理/任务卡/2026-07/T-20260722-01.md"
            card.parent.mkdir(parents=True)
            card.write_text(
                "---\ntask_id: T-20260722-01\ntitle: Fixture\nstatus: done\ncreated_at: 2026-07-22T10:00:00+08:00\n---\n",
                encoding="utf-8",
            )
            findings = self.linter.scan_file(str(card), str(root))
            self.assertFalse(any(item["message"].endswith("'created'") for item in findings))

    def test_document_date_satisfies_created_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            note = root / "notes/log.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                "---\ntitle: Fixture\ndate: 2026-07-22\ntags: [log]\n---\n",
                encoding="utf-8",
            )
            findings = self.linter.scan_file(str(note), str(root))
            self.assertFalse(any(item["message"].endswith("'created'") for item in findings))

    def test_parent_reference_keeps_existing_extension(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "docs/source.md"
            target = root / "docs/deck.html"
            source.parent.mkdir(parents=True)
            target.write_text("fixture", encoding="utf-8")
            findings = self.linter.check_parent_ref(
                {"parent": "deck.html"}, str(source), str(root)
            )
            self.assertEqual([], findings)

    def test_safe_fix_adds_path_tag_and_decision_type(self) -> None:
        violations = [
            {"type": "fm_field_missing", "message": "[决策] 缺少必填字段 'tags'"},
            {"type": "fm_field_missing", "message": "[决策] 缺少必填字段 'type'"},
        ]
        updates = self.linter.safe_fix_updates("40-决策/D-001.md", violations)
        self.assertEqual({"tags": "[决策]", "type": "决策"}, updates)
        content = "---\ntitle: Fixture\ncreated: 2026-07-22\n---\n"
        updated = self.linter.set_top_level_fields(content, updates)
        self.assertIn("tags: [决策]", updated)
        self.assertIn("type: 决策", updated)

    def test_safe_fix_never_touches_baseline(self) -> None:
        violations = [
            {"type": "fm_field_missing", "message": "[项目文档] 缺少必填字段 'tags'"},
        ]
        updates = self.linter.safe_fix_updates("10-项目/基线/PRD.md", violations)
        self.assertEqual({}, updates)

    def test_release_manifest_paths_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "02-项目管理/巡检/v9-release-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"artifacts":[{"path":"30-规范/protected.md"}]}', encoding="utf-8"
            )
            self.assertEqual(
                {"30-规范/protected.md"}, self.linter.release_protected_paths(root)
            )

    def test_clean_files_are_included_in_scan_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            note = root / "notes/clean.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                "---\ntitle: Clean\ncreated: 2026-08-14\ntags: [test]\n---\n\n# Clean\n",
                encoding="utf-8",
            )
            findings, files_scanned = self.linter.scan_vault(
                str(root), str(root), include_stats=True,
            )
            self.assertEqual([], findings)
            self.assertEqual(1, files_scanned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
