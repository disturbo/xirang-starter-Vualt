---
task_id: T-YYYYMMDD-NN
title: ""
module: ""
min_level: M4                   # 最低档位：M4/M5 才开卡（V8 §3 触发路由器）
task_size: S                    # S / M / L / XL
owner: ""                       # agent_id: xiaochong / claudian / qingmeisu / hongmeisu / toubao / cowork
participants: []
status: ready                   # ready / in_progress / submitted / reviewing / done / blocked / cancelled
priority: P1                    # P0 / P1 / P2 / P3
created_at: 2026-05-18T00:00:00+08:00
updated_at: 2026-05-18T00:00:00+08:00
completed_at: null              # ISO 8601，任务完成时填入
sla:
  target_hours: 2
  hard_deadline: null
budget:
  max_total_tokens: 500000
  max_subagent_tokens: 50000
  cost_ceiling_cny: 5.0
  on_exceed: alert_openclaw
actual_cost:                    # 任务完成时填入实际消耗
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
    state: pending              # pending / draft / submitted / verified / rejected
handoff_to: null
resubmit_count: 0               # 返工次数，每次重新提交 +1
retrospective_required: false
gates:
  pre_start: pending            # pending / passed / failed
  pre_write: pending
  cost_fuse: pending
  handoff: pending
---

# {任务名}

## 1 · 上下文

- 背景：
- 目标：
- 不做范围：
- 关联文档：

## 2 · 执行计划

| 步骤 | 执行方 | 产物 | 状态 |
|------|--------|------|:---:|
| 1 |  |  | pending |

## 3 · 门禁记录

| 门禁 | 检查项 | 结果 | 备注 |
|------|--------|------|------|
| pre-start-check | task card / owner / budget / deliverables | pending |  |
| pre-write-check | 路径 / emoji / frontmatter / 品牌色 | pending |  |
| cost-fuse | 当前累计成本是否超阈值 | pending |  |
| handoff-check | 产物路径 / 验证记录 / next action | pending |  |

## 4 · 中间产物

| 时间 | 产物 | 路径 | 说明 |
|------|------|------|------|

## 5 · Handoff 记录

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

## 6 · Retrospective 记录

- 是否需要 retrospective：
- 漂移点：
- 修订建议：

