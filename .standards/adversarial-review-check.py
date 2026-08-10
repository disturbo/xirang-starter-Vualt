#!/usr/bin/env python3
"""Read-only validator for Xi Rang adversarial-review JSON reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SUPPORTED_SCHEMA_VERSIONS = {"0.1.0", "1.0.0"}
FORMAL_SCHEMA_VERSION = "1.0.0"
SEVERITIES = {"P0", "P1", "P2", "P3"}
EXCLUDED_CONTEXT = {"author_chat_history", "author_self_assessment", "prior_review_findings", "unconfirmed_directional_opinions"}
AUTHOR_POSITIONS = {"agree", "disagree", "needs_evidence"}
HUMAN_RESULTS = {"accepted_finding", "rejected_finding", "accepted_risk", "needs_verification"}
RISK_TRIGGERS = {"payment_settlement_refund", "authorization_identity_data", "state_machine_cross_system", "concurrency_idempotency_retry", "high_risk_automation", "xirang_governance_infrastructure", "explicit_human_request"}


def finding(severity: str, rule_id: str, message: str, obj: str = "report") -> dict:
    return {"severity": severity, "rule_id": rule_id, "object": obj, "message": message}


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def evidence_fingerprint(item: dict) -> str:
    normalized = "\n".join(str(item.get(key, "")).strip().casefold() for key in ("source", "locator", "excerpt_or_observation"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_report(payload: object) -> list[dict]:
    issues: list[dict] = []
    if not isinstance(payload, dict):
        return [finding("p0", "REPORT_NOT_OBJECT", "报告根节点必须是 JSON object。")]
    required = {"schema_version", "review_id", "task_id", "review_mode", "context_mode", "reviewer", "review_package", "findings"}
    for key in sorted(required - payload.keys()):
        issues.append(finding("p1", "FIELD_MISSING", f"缺少必填字段：{key}。", key))
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        issues.append(finding("p1", "SCHEMA_VERSION", f"schema_version 必须为 {sorted(SUPPORTED_SCHEMA_VERSIONS)} 之一。"))
    if payload.get("review_mode") != "single_challenger":
        issues.append(finding("p1", "REVIEW_MODE", "当前仅允许 single_challenger。"))
    if payload.get("context_mode") != "blind_package":
        issues.append(finding("p1", "CONTEXT_MODE", "必须声明 context_mode=blind_package。"))
    if not nonempty(payload.get("review_id")) or not str(payload.get("review_id", "")).startswith("AR-"):
        issues.append(finding("p1", "REVIEW_ID", "review_id 必须以 AR- 开头且非空。"))
    if not nonempty(payload.get("task_id")):
        issues.append(finding("p1", "TASK_ID", "task_id 必须非空。"))

    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, dict):
        issues.append(finding("p1", "REVIEWER_INVALID", "reviewer 必须是 object。"))
    else:
        for key in ("agent_id", "model", "platform"):
            if not nonempty(reviewer.get(key)):
                issues.append(finding("p1", "REVIEWER_FIELD", f"reviewer.{key} 必须非空。", f"reviewer.{key}"))

    package = payload.get("review_package")
    if not isinstance(package, dict):
        issues.append(finding("p1", "PACKAGE_INVALID", "review_package 必须是 object。"))
    else:
        if not nonempty(package.get("artifact")):
            issues.append(finding("p1", "ARTIFACT_MISSING", "review_package.artifact 必须非空。"))
        criteria = package.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or not all(nonempty(x) for x in criteria):
            issues.append(finding("p1", "CRITERIA_MISSING", "acceptance_criteria 必须包含至少一项非空标准。"))
        if schema_version == FORMAL_SCHEMA_VERSION:
            triggers = package.get("risk_triggers")
            if not isinstance(triggers, list) or not triggers:
                issues.append(finding("p1", "RISK_TRIGGERS_MISSING", "正式报告必须记录至少一个 risk_trigger。"))
            else:
                unknown_triggers = sorted(set(triggers) - RISK_TRIGGERS)
                if unknown_triggers:
                    issues.append(finding("p1", "RISK_TRIGGERS_INVALID", f"未知 risk_triggers：{unknown_triggers}。"))
        excluded = package.get("excluded_context")
        if not isinstance(excluded, list):
            issues.append(finding("p1", "EXCLUDED_CONTEXT_MISSING", "excluded_context 必须显式声明。"))
        else:
            unknown = sorted(set(excluded) - EXCLUDED_CONTEXT)
            if unknown:
                issues.append(finding("p1", "EXCLUDED_CONTEXT_INVALID", f"未知 excluded_context：{unknown}。"))
            missing = sorted(EXCLUDED_CONTEXT - set(excluded))
            if missing:
                issues.append(finding("advisory", "BLIND_PACKAGE_INCOMPLETE", f"盲审包未排除：{missing}。"))

    items = payload.get("findings")
    if not isinstance(items, list):
        issues.append(finding("p1", "FINDINGS_INVALID", "findings 必须是 array。"))
        return issues
    ids: set[str] = set()
    evidence_seen: dict[str, str] = {}
    for index, item in enumerate(items):
        obj = f"findings[{index}]"
        if not isinstance(item, dict):
            issues.append(finding("p1", "FINDING_INVALID", "finding 必须是 object。", obj))
            continue
        fid = item.get("id")
        if not nonempty(fid):
            issues.append(finding("p1", "FINDING_ID_MISSING", "finding.id 必须非空。", obj))
        elif fid in ids:
            issues.append(finding("p1", "FINDING_ID_DUPLICATE", f"重复 finding id：{fid}。", obj))
        else:
            ids.add(fid)
        if item.get("severity") not in SEVERITIES:
            issues.append(finding("p1", "SEVERITY_INVALID", "severity 必须为 P0/P1/P2/P3。", obj))
        for key in ("claim", "impact", "reproduction_or_counterexample", "recommendation"):
            if not nonempty(item.get(key)):
                issues.append(finding("p1", "FINDING_FIELD_MISSING", f"{key} 必须非空。", obj))
        if not isinstance(item.get("blocking_recommendation"), bool):
            issues.append(finding("p1", "BLOCKING_RECOMMENDATION_INVALID", "blocking_recommendation 必须是 boolean，且仅表示建议。", obj))
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(finding("p1", "EVIDENCE_MISSING", "每条 finding 至少需要一项证据。", obj))
        else:
            for evidence_index, evidence_item in enumerate(evidence):
                evidence_obj = f"{obj}.evidence[{evidence_index}]"
                if not isinstance(evidence_item, dict) or not all(nonempty(evidence_item.get(key)) for key in ("source", "locator", "excerpt_or_observation")):
                    issues.append(finding("p1", "EVIDENCE_INVALID", "证据必须包含非空 source/locator/excerpt_or_observation。", evidence_obj))
                    continue
                digest = evidence_fingerprint(evidence_item)
                if digest in evidence_seen:
                    issues.append(finding("advisory", "EVIDENCE_DUPLICATE", f"证据与 {evidence_seen[digest]} 内容重复；重复不增加置信度。", evidence_obj))
                else:
                    evidence_seen[digest] = evidence_obj
        response = item.get("author_response")
        if response is not None and (not isinstance(response, dict) or response.get("position") not in AUTHOR_POSITIONS or not nonempty(response.get("note"))):
            issues.append(finding("p1", "AUTHOR_RESPONSE_INVALID", "author_response 必须包含合法 position 和非空 note。", obj))
        decision = item.get("human_decision")
        if decision is not None and (not isinstance(decision, dict) or decision.get("result") not in HUMAN_RESULTS or not nonempty(decision.get("decided_by")) or not nonempty(decision.get("note"))):
            issues.append(finding("p1", "HUMAN_DECISION_INVALID", "human_decision 必须包含合法 result、decided_by 和 note。", obj))
    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, dict) or not isinstance(summary.get("finding_count"), int):
            issues.append(finding("p1", "SUMMARY_INVALID", "summary.finding_count 必须是 integer。", "summary"))
        elif summary["finding_count"] != len(items):
            issues.append(finding("p1", "FINDING_COUNT_MISMATCH", f"summary.finding_count={summary['finding_count']}，实际 findings={len(items)}。", "summary.finding_count"))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    try:
        payload = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = {"valid": False, "issues": [finding("p0", "REPORT_READ_FAILED", str(exc))]}
    else:
        issues = validate_report(payload)
        result = {"valid": not any(item["severity"] in {"p0", "p1"} for item in issues), "issues": issues, "summary": {"p0": sum(item["severity"] == "p0" for item in issues), "p1": sum(item["severity"] == "p1" for item in issues), "advisory": sum(item["severity"] == "advisory" for item in issues)}}
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("valid" if result["valid"] else "invalid")
        for item in result["issues"]:
            print(f"[{item['severity']}] {item['rule_id']} | {item['message']}")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
