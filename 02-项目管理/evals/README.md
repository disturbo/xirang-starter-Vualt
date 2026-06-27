---
tags: [V9, V9.4, evals, harness]
title: "V9 Harness Evals"
type: runbook
status: active
created: 2026-06-26
updated: 2026-06-27
owner: Codex
---

# V9 Harness Evals

> 定位：只测机械可断言的 harness 行为，不做 Agent 产出质量评分。

## 核心原则

| 原则 | 含义 |
|---|---|
| 先坏样本 | 每组 eval 必须有 known-bad fixture，并断言 harness 能拦住 |
| 正反分账 | 报告必须区分 `passed_positive` 与 `blocked_negative` |
| 临时造样本 | fixture 写入系统临时目录，跑完即删，不污染正式 Vault |
| 绿灯无感 | eval-runner 默认只手动运行；pre-commit 仅在 staged harness 文件触达 `.standards/` 或 `02-项目管理/脚本/` 时触发 |

## 当前 Runner

脚本：`02-项目管理/脚本/v9-harness-eval-runner.py`

常用命令：

```bash
python3 02-项目管理/脚本/v9-harness-eval-runner.py
python3 02-项目管理/脚本/v9-harness-eval-runner.py --json
python3 02-项目管理/脚本/v9-harness-eval-runner.py --write-latest
```

`--write-latest` 只写 `02-项目管理/巡检/harness-eval-latest.json`。

## 当前覆盖

| case | 类型 | 断言 |
|---|---|---|
| `project_ops_positive_clean` | positive | 干净任务卡 + 连续运行日志不产生 p0/p1 |
| `project_ops_negative_missing_frontmatter` | negative | 缺 frontmatter 的任务卡必须触发 `MISSING_FM` |
| `project_ops_negative_done_without_completed` | negative | `done` 但 `completed_at=null` 必须触发 `DONE_NO_COMPLETED` |
| `starter_leak_positive_clean` | positive | 干净 starter 不产生 p0/p1 |
| `starter_leak_negative_project_term` | negative | starter 中残留项目词必须触发 `PROJECT_TERM` |
| `starter_leak_negative_secret` | negative | starter 中疑似 secret 必须触发 `SECRET_JSON_APP_SECRET` |
| `cost_usage_positive_usage_only` | positive | `input_tokens/output_tokens` 可汇总为 usage-only token 事件 |
| `cost_usage_negative_connected_without_source` | negative | `billing_status=connected` 缺平台 `cost_source` 必须拒绝 |
| `task_state_positive_submitted` | positive | `done + review_status=submitted + accepted_by=null` 不产生 p1 |
| `task_state_negative_self_accept` | negative | `accepted_by == owner/author` 必须触发 `ACCEPTED_BY_SELF` |
| `task_state_negative_missing_acceptor` | negative | `review_status=accepted` 但缺 `accepted_by` 必须触发 `ACCEPTED_BY_MISSING` |
| `task_state_positive_reviewing` | positive | reviewing 状态有 reviewer/submitted_at 且未提前写验收字段时不产生 p1 |
| `task_state_negative_submitted_missing_submitted_at` | negative | 新 submitted 任务缺 `submitted_at` 必须触发 `SUBMITTED_AT_MISSING` |
| `task_state_negative_changes_requested_without_note` | negative | changes_requested 缺 `acceptance_note` 必须触发 `ACCEPTANCE_NOTE_MISSING` |
| `handoff_positive_actionable` | positive | `handoff_required=true` 的 done 任务含可接手 Handoff，不产生 p1 |
| `handoff_negative_missing` | negative | `handoff_required=true` 的 done 任务缺 Handoff 必须触发 `HANDOFF_MISSING` |
| `handoff_negative_incomplete` | negative | Handoff 缺产物/验证/next action 必须触发 `HANDOFF_INCOMPLETE` |
| `reflex_negative_missing_sources_visible` | negative | 空 fixture vault 中缺源必须进入 `sources_failed` |
| `reflex_negative_cooldown_escalation_visible` | negative | 冷却窗内严重度升级必须以 `escalated` active |
| `accept_gate_negative_edit_bare_accept` | negative | Edit 重建出的 self-accept 候选卡必须被 pre-accept 硬拦 |
| `accept_gate_negative_write_full_accept` | negative | Write 全量覆盖的 self-accept 候选卡必须被 pre-accept 硬拦 |
| `scope_tamper_negative_bash_writescope` | negative | Bash 直写状态文件扩权必须被事后检测为 `SCOPE_ESCALATION` |
| `eval_freshness_negative_stale_report` | negative | 缺新鲜度字段的旧 eval 报告必须被 `STALE_EVAL` 硬拦 |
| `accept_hook_negative_edit_payload` | negative | pre-write hook 必须解析 Edit payload 并拦 self-accept |
| `accept_hook_negative_write_payload` | negative | pre-write hook 必须解析 Write content 并拦 self-accept |
| `accept_command_positive_valid_reviewer` | positive | 合法 `v9_accept` 必须通过 pre-accept 并原子写回 |
| `eval_freshness_negative_stale_hash` | negative | hash/mtime 过期的 eval 报告必须被 `STALE_EVAL` 硬拦 |

## 下一批候选

| 候选 | 前置 |
|---|---|
| `review_status=submitted` 却文本声称已验收 | `v9-task-state-check.py` 增加低误报文本 overclaim 规则 |
| 工具注册表新鲜度检查 | 将 registry `last_verified` 与 `harness-eval-latest.json.tested_hashes` 对齐 |
