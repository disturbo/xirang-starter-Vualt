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

`--write-latest` 只写 Vault 外运行态目录的 `harness-eval-latest.json`。默认位置：

```text
~/Desktop/沙箱/v9-runtime/巡检/harness-eval-latest.json
```

可用 `XIRANG_V9_RUNTIME_DIR` 或 `XIRANG_V9_INSPECT_DIR` 覆盖。

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
| `iteration_ops_positive_clean` | positive | 当前迭代管理文件齐备，不产生 p0/p1 |
| `iteration_ops_positive_absolute_project_root` | positive | checker 以绝对 `--project-root` 调用时，frontmatter 中 vault 相对 `iteration_root/baseline_root` 不误报 |
| `status_summary_positive_green` | positive | health/eval/iteration 三源干净时生成 green 的 UI 状态契约，并写入 runtime `status-latest.json` |
| `status_summary_positive_schema_contract` | positive | `status-latest.json` 暴露 `schema_version=v1`、`paths`、`ui.badges` 与 `ui.actions` |
| `status_summary_negative_red_health` | negative | 反射器 health 红灯时，状态聚合必须输出 red 且非零退出 |
| `status_summary_negative_stale_latest` | negative | health/eval latest 过期时，状态聚合必须降为 yellow 并输出 freshness 细节 |
| `iteration_ops_negative_workbench_double_time_missing` | negative | 当前迭代工作台缺 `observed_at/recorded_at` 时触发 `DOUBLE_TIME_MISSING` advisory |
| `iteration_ops_negative_metadata_time_order_invalid` | negative | `valid_from > valid_until` 等时间顺序异常时触发 `METADATA_TIME_ORDER_INVALID` advisory |
| `iteration_ops_negative_supersedes_target_missing` | negative | `supersedes` 指向不存在文件时触发 `SUPERSEDES_TARGET_MISSING` advisory |
| `iteration_ops_negative_agent_assignments_incomplete` | negative | 当前迭代工作台缺关键 `agent_assignments` 职责时触发 `AGENT_ASSIGNMENTS_INCOMPLETE` advisory |
| `iteration_ops_positive_agent_assignment_fallback_available` | positive | `agent_assignments` 中不可用角色已声明 `fallback` 时不产生 fallback finding |
| `iteration_ops_negative_agent_assignment_fallback_missing` | negative | `agent_assignments` 中不可用角色缺 `fallback` 时触发 `AGENT_ASSIGNMENT_FALLBACK_MISSING` advisory |
| `iteration_ops_negative_valid_for_mismatch` | negative | 当前迭代工作台 `valid_for` 指向非当前迭代时触发 `VALID_FOR_MISMATCH` p1 |
| `iteration_ops_positive_scope_candidates_before_freeze` | positive | 范围未冻结时，范围草案存在纳入候选项也不触发冻结规则 |
| `iteration_ops_negative_scope_added_after_freeze` | negative | 范围冻结后，纳入类范围项超过冻结基准时触发 `SCOPE_ADDED_AFTER_FREEZE` p1 |
| `iteration_ops_positive_scope_status_scoped_complete` | positive | `scope_status=scoped` 且具备 `scope_confirmed_at/scope_decision_ref` 时不触发状态转换提示 |
| `iteration_ops_negative_scope_status_transition_incomplete` | negative | `scope_status=scoped` 缺确认时间或决策引用时触发 `SCOPE_STATUS_TRANSITION_INCOMPLETE` advisory |
| `iteration_ops_negative_scope_status_advance_available_planning` | negative | planning 状态已具备 scoped 证据时触发 `SCOPE_STATUS_ADVANCE_AVAILABLE` advisory |
| `iteration_ops_negative_scope_status_advance_available_frozen` | negative | frozen 状态已具备 release 证据时触发 `SCOPE_STATUS_ADVANCE_AVAILABLE` advisory |
| `iteration_ops_positive_scope_status_released_complete` | positive | `scope_status=released` 且具备冻结与发布引用时不触发状态转换提示 |
| `iteration_ops_negative_scope_status_released_incomplete` | negative | `scope_status=released` 缺 `released_at/release_ref` 时触发 `SCOPE_STATUS_TRANSITION_INCOMPLETE` advisory |
| `iteration_ops_positive_scope_status_reviewed_complete` | positive | `scope_status=reviewed` 且具备冻结、发布、复盘引用时不触发状态转换提示 |
| `iteration_ops_negative_scope_status_reviewed_incomplete` | negative | `scope_status=reviewed` 缺 `reviewed_at/review_ref` 时触发 `SCOPE_STATUS_TRANSITION_INCOMPLETE` advisory |
| `iteration_ops_positive_authorized_baseline_write` | positive | 发布后且封版归集清单声明 `baseline_write_authorized=true` 时允许基线变更 |
| `iteration_ops_negative_baseline_write_without_release` | negative | 非发布/未授权状态下基线区有 git 变更时触发 `BASELINE_WRITE_WITHOUT_RELEASE` p1 |
| `iteration_ops_positive_carryover_within_limit` | positive | `carryover_count=2` 的遗留项不触发超期提示 |
| `iteration_ops_negative_carryover_too_long` | negative | `carryover_count>2` 且状态未关闭时触发 `CARRYOVER_TOO_LONG` advisory |
| `iteration_ops_negative_missing_workbench` | negative | 当前迭代缺 `迭代管理/README.md` 必须触发 `ITERATION_MANAGEMENT_DOC_MISSING` |
| `iteration_ops_negative_missing_write_boundary` | negative | 当前迭代缺智能体写入边界必须触发 `ITERATION_MANAGEMENT_DOC_MISSING` |
| `iteration_ops_negative_write_boundary_incomplete` | negative | 智能体写入边界缺默认迭代写区、基线归集例外、manifest 或封版保护时触发 `WRITE_BOUNDARY_CONTRACT_INCOMPLETE` advisory |
| `iteration_ops_negative_release_collection_incomplete` | negative | 封版归集清单缺归集目标、原则、台账列或完成条件时触发 `RELEASE_COLLECTION_CONTRACT_INCOMPLETE` advisory |
| `iteration_ops_negative_material_manifest_incomplete` | negative | 材料迁移 manifest 缺先登记、台账列、引用更新或搬迁后检查时触发 `MATERIAL_MANIFEST_CONTRACT_INCOMPLETE` advisory |
| `iteration_ops_negative_missing_carryover_ledger` | negative | 当前迭代缺遗留项台账时触发 `ITERATION_MANAGEMENT_DOC_MISSING` |
| `iteration_ops_negative_carryover_ledger_incomplete` | negative | 遗留项台账缺列、规则或 review 闭环时触发 `CARRYOVER_LEDGER_CONTRACT_INCOMPLETE` advisory |
| `iteration_ops_positive_visual_preview_checked` | positive | 迭代目录内 HTML 有 preview 记录时不触发缺预览提示 |
| `iteration_ops_negative_visual_preview_missing` | negative | 迭代目录内 HTML 缺 preview 记录时触发 `VISUAL_PREVIEW_MISSING` advisory |
| `iteration_ops_positive_declared_prototype_checked` | positive | 外部 `prototype_root` 入口存在、`preview_status=checked` 且覆盖审计为 42/42 时不触发提示 |
| `iteration_ops_negative_declared_prototype_requirement_gaps` | negative | 原型入口可达但覆盖为 22 完整 / 12 部分 / 8 缺失时，必须触发 `PROTOTYPE_REQUIREMENT_GAPS` P1 与 `PROTOTYPE_REQUIREMENT_PARTIAL` advisory |
| `iteration_ops_positive_fact_chain_consistent` | positive | 42/22/12/8 事实在工作台、范围核对、模块 README 和执行台账一致时，保留真实原型 P1 但不误报事实漂移 |
| `iteration_ops_negative_fact_chain_inconsistent` | negative | 范围核对中的原型缺口未回写模块 README 时，必须触发 `ITERATION_FACT_CHAIN_INCONSISTENT` P1 |
| `iteration_ops_positive_declared_prototype_script_shell` | positive | 外部原型入口是 JS shell 且有 script src 时不误判为空白页 |
| `iteration_ops_negative_declared_prototype_pending` | negative | 外部 `prototype_root` 声明的原型入口存在但 `preview_status=pending` 时触发 `VISUAL_PREVIEW_PENDING` advisory |
| `iteration_ops_negative_declared_prototype_blank_entry` | negative | 外部原型入口存在但缺可见文本、脚本或视觉元素时触发 `VISUAL_ARTIFACT_BLANK_OR_SHELL_EMPTY` advisory |
| `iteration_ops_positive_review_loop_complete` | positive | 发布后迭代存在完整 review 规则/eval/skill 闭环时不触发 review 提示 |
| `iteration_ops_negative_review_missing_after_release` | negative | 发布后缺 `review.md` 或 `{iteration}-review.md` 时触发 `REVIEW_MISSING_AFTER_RELEASE` advisory |
| `iteration_ops_negative_review_loop_incomplete` | negative | review 缺规则/eval/skill 晋升章节时触发 `ITERATION_REVIEW_PROMOTION_LOOP_INCOMPLETE` advisory |
| `iteration_ops_negative_review_carryover_loop_incomplete` | negative | review 的遗留项回填缺台账链接或 `close_decision` 动作时触发 `ITERATION_REVIEW_CARRYOVER_LOOP_INCOMPLETE` advisory |
| `iteration_ops_negative_review_scope_advance_loop_incomplete` | negative | review 的状态晋升建议回填缺规则、状态或决定字段时触发 `ITERATION_REVIEW_SCOPE_ADVANCE_LOOP_INCOMPLETE` advisory |
| `iteration_ops_negative_scope_status_accepted_but_not_applied` | negative | review 已接受状态晋升但工作台 `scope_status` 未更新时触发 `SCOPE_STATUS_ACCEPTED_BUT_NOT_APPLIED` advisory |
| `iteration_ops_negative_review_accepted_rule_without_eval` | negative | review 决定进入 checker/eval 但 `eval_status` 未完成或 Eval 回填缺 done 时触发 `REVIEW_ACCEPTED_RULE_WITHOUT_EVAL` advisory |
| `iteration_ops_positive_review_accepted_v9_body_done` | positive | review 决定进入 V9 正文且具备 eval done 与规则候选 done 时不触发提示 |
| `iteration_ops_negative_review_decision_matrix_inconsistent` | negative | review 决定进入 V9 正文但 checker/eval 未同步接受且 done 时触发 `REVIEW_DECISION_MATRIX_INCONSISTENT` advisory |
| `iteration_ops_negative_review_accepted_v9_body_without_evidence` | negative | review 决定进入 V9 正文但缺 eval done 或规则候选 done 时触发 `REVIEW_ACCEPTED_V9_BODY_WITHOUT_EVIDENCE` advisory |
| `iteration_ops_positive_review_accepted_skill_done` | positive | review 决定进入 skill/runbook 且 Skill 回填有新增项 done 时不触发提示 |
| `iteration_ops_negative_review_accepted_skill_without_writeback` | negative | review 决定进入 skill/runbook 但 Skill 回填缺新增项 done 时触发 `REVIEW_ACCEPTED_SKILL_WITHOUT_WRITEBACK` advisory |
| `reflex_negative_missing_sources_visible` | negative | 空 fixture vault 中缺源必须进入 `sources_failed`，包含 `iteration-ops` |
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
