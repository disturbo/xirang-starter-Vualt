---
task_id: T-YYYYMMDD-NN
title: "{任务名}"
module: ""
min_level: M4
task_size: S
owner: ""
author: ""
participants: []
status: ready
review_status: draft
reviewer: "用户"
submitted_at: null
accepted_by: null
accepted_at: null
acceptance_result: null
acceptance_note: ""
priority: P1
created_at: YYYY-MM-DDT00:00:00+08:00
updated_at: YYYY-MM-DDT00:00:00+08:00
completed_at: null
sla:
  target_hours: 2
  hard_deadline: null
budget:
  max_total_tokens: 500000
  max_subagent_tokens: 50000
  cost_ceiling_cny: 5.0
  on_exceed: alert_openclaw
actual_cost:
  tokens: 0
  cost_cny: 0.0
  model_used: ""
paths:
  allowed_write_roots: []
  temp_root: _temp/{task-id}/
parent_task: null
subtasks: []
blocked_by: []
deliverables:
  - path: ""
    type: ""
    state: pending
handoff_to: null
resubmit_count: 0
retrospective_required: false
gates:
  pre_start: pending
  pre_write: pending
  cost_fuse: pending
  handoff: pending
tags: [模板]
---

# {任务名}

## 1. 上下文

- 背景：
- 目标：
- 不做范围：
- 关联文档：

## 2. 执行计划

| 步骤 | 执行方 | 产物 | 状态 |
|------|--------|------|:---:|
| 1 |  |  | pending |

## 3. 门禁记录

| 门禁 | 检查项 | 结果 | 备注 |
|------|--------|------|------|
| pre-start-check | task card / owner / budget / deliverables | pending |  |
| pre-write-check | 路径 / frontmatter / 品牌色 / 引用链 | pending |  |
| cost-fuse | 当前累计成本是否超阈值 | pending |  |
| handoff-check | 产物路径 / 验证记录 / next action | pending |  |

## 4. 中间产物

| 时间 | 产物 | 路径 | 说明 |
|------|------|------|------|

## 5. Handoff 记录

```markdown
任务：
- owner:
- status:
- 产物路径:
- 来源/依据:
- 验证结果:
- next action:
- blocked by:
- 关键发现:
```

## 6. Retrospective 记录

- 是否需要 retrospective：
- 漂移点：
- 修订建议：
