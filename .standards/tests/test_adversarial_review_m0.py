#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "adversarial-review-check.py"


def load_module():
    spec = importlib.util.spec_from_file_location("adversarial_review_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_report() -> dict:
    return {"schema_version": "0.1.0", "review_id": "AR-test-001", "task_id": "T-test-001", "review_mode": "single_challenger", "context_mode": "blind_package", "reviewer": {"agent_id": "challenger", "model": "deepseek", "platform": "test"}, "review_package": {"artifact": "candidate.md", "references": ["spec.md"], "acceptance_criteria": ["核心异常路径完整"], "attack_surfaces": ["状态机"], "excluded_context": ["author_chat_history", "author_self_assessment", "prior_review_findings", "unconfirmed_directional_opinions"]}, "findings": [{"id": "F-001", "severity": "P1", "claim": "缺少失败状态。", "evidence": [{"source": "candidate.md", "locator": "状态表", "excerpt_or_observation": "仅定义成功状态。"}], "impact": "失败后无法恢复。", "reproduction_or_counterexample": "接口超时时无合法转移。", "recommendation": "增加 failed 和 retry。", "blocking_recommendation": True}]}


def valid_formal_report() -> dict:
    payload = valid_report()
    payload["schema_version"] = "1.0.0"
    payload["review_package"]["risk_triggers"] = ["state_machine_cross_system"]
    payload["summary"] = {"finding_count": 1}
    return payload


class AdversarialReviewM0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_valid_minimal_report(self) -> None:
        self.assertEqual([], self.module.validate_report(valid_report()))

    def test_missing_blind_context_is_advisory(self) -> None:
        payload = valid_report()
        payload["review_package"]["excluded_context"].remove("prior_review_findings")
        issues = self.module.validate_report(payload)
        self.assertIn("BLIND_PACKAGE_INCOMPLETE", {item["rule_id"] for item in issues})

    def test_duplicate_evidence_is_reported_without_blocking(self) -> None:
        payload = valid_report()
        duplicate = dict(payload["findings"][0])
        duplicate["id"] = "F-002"
        duplicate["evidence"] = [dict(payload["findings"][0]["evidence"][0])]
        payload["findings"].append(duplicate)
        issues = self.module.validate_report(payload)
        duplicate_issues = [item for item in issues if item["rule_id"] == "EVIDENCE_DUPLICATE"]
        self.assertEqual(1, len(duplicate_issues))
        self.assertEqual("advisory", duplicate_issues[0]["severity"])

    def test_author_response_and_human_decision_are_optional_but_validated(self) -> None:
        payload = valid_report()
        item = payload["findings"][0]
        item["author_response"] = {"position": "agree", "note": "补充失败路径。"}
        item["human_decision"] = {"result": "accepted_finding", "decided_by": "波波", "note": "纳入修订。"}
        self.assertEqual([], self.module.validate_report(payload))
        item["human_decision"]["decided_by"] = ""
        issues = self.module.validate_report(payload)
        self.assertIn("HUMAN_DECISION_INVALID", {issue["rule_id"] for issue in issues})

    def test_formal_report_requires_risk_trigger_and_validates_count(self) -> None:
        payload = valid_formal_report()
        self.assertEqual([], self.module.validate_report(payload))
        payload["review_package"]["risk_triggers"] = []
        payload["summary"]["finding_count"] = 2
        rule_ids = {issue["rule_id"] for issue in self.module.validate_report(payload)}
        self.assertIn("RISK_TRIGGERS_MISSING", rule_ids)
        self.assertIn("FINDING_COUNT_MISMATCH", rule_ids)

    def test_blocking_recommendation_rejects_explanatory_string(self) -> None:
        payload = valid_formal_report()
        payload["findings"][0]["blocking_recommendation"] = "true，状态机缺失"
        issues = self.module.validate_report(payload)
        self.assertIn("BLOCKING_RECOMMENDATION_INVALID", {issue["rule_id"] for issue in issues})


if __name__ == "__main__":
    unittest.main(verbosity=2)
