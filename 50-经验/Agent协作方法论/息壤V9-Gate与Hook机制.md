---
title: "息壤V9-Gate与Hook机制"
version: 9.2.0
status: 正式
type: 技术文档
created: 2026-05-28
updated: 2026-05-31
author: claudian
tags: [方法论, 息壤, V9, Gate, Hook]
supersedes:
  - "[[_archive/V8.5/息壤V8.5-Gate强制机制]]"
  - "[[_archive/V8.5/息壤V8.5-Hook强制门禁设计]]"
---

# 息壤 V9 Gate 与 Hook 机制

> 强制执行层：即使 Agent 忘记合规流程，硬件门禁也会拦截。

## 1. 架构概览

```
Agent 写文件操作
  │
  ▼
┌────────────────────────────────────────┐
│  pre-write-hook.sh v1.1                │  ← PreToolUse 自动触发
│  (支持 V8_AGENT_ID 多 Agent 参数化)     │
├────────────────────────────────────────┤
│  Layer 0: 不可篡改路径(task-card.yaml)  │
│  Layer 1A: 禁止路径无条件拦截           │  ← V9.2: busy 时也需 write_scope
│  Layer 1B: 任务卡授权 + gate-enforce    │
└────────────────────────────────────────┘
  │
  ▼ 通过 → 允许写入
  ✗ 阻断 → 返回错误信息给 Agent
```

### 1.1 V9.2 多 Agent 参数化

pre-write-hook.sh v1.1 通过环境变量支持多 Agent 共用同一 hook：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `V8_AGENT_ID` | claudian | Agent 标识符，决定读取哪个状态文件 |
| `V8_PLATFORM` | claude-code | 平台标识 |

配置示例（.claude/settings.json）：
```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Write|Edit|NotebookEdit",
      "hook": "bash .standards/hooks/pre-write-hook.sh"
    }]
  }
}
```

参见 `.standards/agent-contract.yaml` 获取各 Agent 的完整配置。

## 2. gate-enforce.py v1.1.0

### 2.1 四个子命令

| 子命令 | 调用时机 | 检查内容 |
|--------|---------|---------|
| `pre-start` | M4/M5 handshake | Agent 是否已 busy / 任务是否被阻塞 |
| `pre-spawn` | 创建子任务 | 无 task_id / 成本熔断 / scope 越权 |
| `pre-write` | 文件写入前 | V9 声明缺失 / 路径越权 / 禁止目录 |
| `pre-end` | 任务关闭 | 重复关闭 / 未收集子任务 / 收工不完整 |

### 2.2 规则清单

| 规则 ID | 优先级 | 触发条件 |
|---------|:------:|---------|
| AGENT_BUSY | P0 | Agent 已 busy 时再次激活 |
| TASK_BLOCKED | P0 | 任务 blocked_by 非空 |
| TASK_CANCELLED | P0 | 任务状态为 done/cancelled |
| NO_TASK_ID | P0 | spawn 时无 task_id |
| FUSE_BLOWN | P0 | 成本达上限 |
| SCOPE_VIOLATION | P0 | 子任务 scope 含禁止路径 |
| SCOPE_EXCEEDED | P0 | 文件路径不在 write_scope 内 |
| PATH_FORBIDDEN | P0 | 写入禁止目录（未在 scope 声明）|
| ALREADY_IDLE | P0 | 重复关闭（已 idle）|
| BUDGET_EXHAUSTED | P0 | 所有模型均超预算 |
| **WRITE_WITHOUT_V9_PREFIX** | **P1** | **无 task_id 写非豁免路径（V9 新增）** |
| BUDGET_WARNING | P1 | 预算已用 60%+ |
| UNCOLLECTED_SUBTASKS | P1 | 关闭时有未收集子任务 |
| MISSING_DELIVERABLES | P0 | 收工缺产物 |
| KANBAN_NOT_UPDATED | P0 | 收工未更新看板 |
| NO_RUN_LOG | P0 | 收工无运行日志 |
| MODEL_DOWNGRADE | P2 | 建议降级模型 |
| FRONTMATTER_MISSING | P1 | 产物缺 frontmatter |
| EMOJI_DETECTED | P2 | 检测到装饰性 emoji |
| BRAND_COLOR | P3 | 品牌色不合规 |

### 2.3 V9 豁免路径

以下路径在无 task_id 时不触发 WRITE_WITHOUT_V9_PREFIX：
- `02-项目管理/运行日志/` — M3 收工日志
- `02-项目管理/智能体状态/` — heartbeat 更新

### 2.4 调用方式

```bash
# 手动检查
python3 .standards/gate-enforce.py pre-write \
  --file "10-项目/{项目名}/file.md" \
  --task-id "T-20260528-01" \
  --write-scope "10-项目/{项目名}/" \
  --json

# 退出码
# 0 = 通过（P1-P3 仅 advisory）
# 1 = P0 硬阻断
# 2 = 参数错误
```

## 3. pre-write-hook.sh

### 3.1 层级结构（V9.2 修订）

```
Layer 0: 不可篡改路径（task-card.yaml 只允许 Bash 创建）
Layer 1A: 禁止路径无条件拦截
  ├─ 非 busy → 直接阻断
  └─ busy → 检查 write_scope 是否包含该路径（V9.2 关键修复）
Layer 1A.5: 交付物路径（未激活任务时阻断）
Layer 1B-1: 任务卡授权验证（核心目录需 task_card + scope_source=task_card）
Layer 1B-2: gate-enforce pre-write（busy 时的完整门禁检查）
```

**V9.2 关键变更**：Layer 1A 禁止路径检查改为无条件执行，不再依赖 gate-enforce.py 是否存在。即使 Agent 处于 busy 状态，禁止路径（`00-MOC/`, `.standards/`, `30-规范/`, `40-决策/`, `02-项目管理/智能体状态/`）也必须在 write_scope 中明确声明才能写入。这防止了 gate 缺失时的安全漏洞。

### 3.2 白名单（永远放行）

```
_temp/                    — 临时文件
02-项目管理/运行日志/      — M3 日志
.obsidian/                — Obsidian 配置
```

### 3.3 核心目录（需任务卡授权）

```
10-项目/
20-知识/
50-经验/
02-项目管理/交付物/
```

写入这些路径要求：
1. 有效 task_id
2. task-card.yaml 存在
3. scope_source = task_card
4. 路径在 task card 的 authorized_paths 中

## 4. 工作流示意

```
M3 操作:
  Agent 写文件 → hook 检查白名单 → 放行（运行日志/临时）
  Agent 写非白名单 → hook 检查 status → idle → 禁止路径拦截

M4/M5 操作:
  Agent handshake → status=busy, task_card 创建
  Agent 写文件 → hook 检查 status=busy → 读 task_card
    → 验证 authorized_paths → 调 gate-enforce → 通过/阻断
```

## 5. 心跳与成本自动化（V9.2）

### 5.1 心跳脚本

| 脚本 | 用途 | 调用方式 |
|------|------|---------|
| `heartbeat-update.sh` | 更新 last_heartbeat + 写入 PID | 需设置 `V8_AGENT_PID` 环境变量 |
| `heartbeat-check.sh` | 三级超时检测 | cron 或手动调用 |

超时分级：
- 15min 无心跳 → `warn`（仅警告）
- 30min 无心跳 → `stale`（标记过期）
- 60min 无心跳 → `dead`（自动回 idle + 追加事件）

### 5.2 成本事件集成

v8_handshake 自动写入 `cost_start`，v8_end 自动写入 `cost_finalize`。
中间阶段可手动调用：

```bash
bash .standards/hooks/cost-event.sh checkpoint <task_id> <agent_id> [tokens] [cost_cny] [description]
```

口径规则：每次写入的 tokens/cost_cny 是增量(delta)，不是累计。cost-fuse.py 汇总时只加 checkpoint + finalize，忽略 start。

### 5.3 模型降级（cost-fuse v1.1）

当 cost-fuse 检测到预算告警时，读取 `agent-contract.yaml` 中的 `fallback_model` 配置：
- 60% → 输出降级链信息（advisory）
- 100% → 输出降级建议；若 `auto_fallback: true` 则标记为强制

## 6. 故障排除

| 症状 | 原因 | 修复 |
|------|------|------|
| `[V8-HOOK-BLOCK] 路径在禁止目录中` | 未 handshake 就写禁止路径 | 先执行 v8_handshake |
| `[V8-HOOK-BLOCK] gate-enforce 拒绝写入` | scope 不包含目标路径 | 检查 task-card authorized_paths |
| `WRITE_WITHOUT_V9_PREFIX` advisory | 无 task_id 写非豁免文件 | M4+ 需 handshake; M3 可忽略 advisory |
| `task-card.yaml 只允许 Bash 创建` | Agent 试图 Edit task-card | 只有 v8_handshake (Bash) 可创建任务卡 |
