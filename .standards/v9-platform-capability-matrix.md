---
type: 规范
domain: 多智能体治理
version: "1.0.0"
created: 2026-05-31
maintainer: Claudian
task_id: T-20260531-18
tags: [规范, 治理, hook]
---

# V9 平台 Hook 能力矩阵

> 本文件是 V9 治理能力全平台闭环的基础数据。
> 每个平台的 hook 能力以实际验证为准，不以文档或推测为准。

## 能力矩阵

| 能力维度 | Claude Code (Claudian) | OpenClaw (阿莫西林) | Hermes (头孢) | Codex (红霉素) |
|----------|:---------------------:|:-------------------:|:-------------:|:--------------:|
| **pre-write 硬拦截** | ✅ 原生 hook | ❌ 无平台 hook | ❌ 无平台 hook | ❌ 无平台 hook |
| **post-write 审计** | ✅ 原生 hook | ❌ 手动脚本 | ❌ 无 | ❌ 无 |
| **session 状态检测** | ✅ session-guard.sh | ❌ 手动 | ❌ 无 | ❌ 无 |
| **gate-enforce 集成** | ✅ hook 自动调用 | ⚠️ 需手动调用 | ❌ 无 | ❌ 无 |
| **事件流自动写入** | ✅ hook 自动 | ⚠️ 脚本手动 | ❌ 依赖 SOUL 合规 | ❌ 无 |
| **心跳更新** | ✅ heartbeat-update.sh (V9.2) | ⚠️ heartbeat.sh 需手动调 | ❌ 无 | ❌ 无 |
| **成本追踪** | ✅ 三阶段自动(start/checkpoint/finalize) | ⚠️ task-end.sh 可选参数 | ❌ 无 | ❌ 无 |
| **任务卡授权** | ✅ task-card.yaml | ⚠️ 脚本创建 | ❌ 无 | ❌ 无 |
| **合规执行方式** | 自动 (hook 强制) | 半自动 (脚本辅助) | 信任制 (SOUL 契约) | 无 (静态权限) |

> 图例：✅ 已实现且自动 | ⚠️ 部分实现或需手动 | ❌ 无

## 各平台详细分析

### Claude Code (Claudian) — 治理最完整

**hook 配置位置**: `.claude/settings.json` → `hooks` 字段

**三层 hook 架构**:

| 层 | 脚本 | 触发点 | 模式 | 退出码含义 |
|----|------|--------|------|-----------|
| Layer 0+1 | `pre-write-hook.sh` | PreToolUse (Write/Edit) | blocking | 0=放行, 2=阻断 |
| Layer 2 | `session-guard.sh` | PreToolUse (首次操作) | advisory | 总是 0 |
| Layer 3 | `post-write-hook.sh` | PostToolUse (Write/Edit) | audit | 总是 0 |

**stdin 格式**: `{"tool_name":"Write","tool_input":{"file_path":"...","content":"..."}}`

**V9.2 更新**:
- ~~hook 脚本中 agent_id 硬编码为 Claudian~~ → 已通过 `V8_AGENT_ID` 环境变量参数化 (v1.1)
- VAULT_ROOT 硬编码为绝对路径（保留，因为 hook 需要确定性路径）
- 只对 Write/Edit/NotebookEdit 工具生效，Bash 写文件不受控
- 禁止路径检查已改为无条件执行（不依赖 gate-enforce.py 是否存在）

---

### OpenClaw (阿莫西林) — 脚本辅助治理

**配置位置**: `~/.openclaw/workspace/`

**v8-runtime 生命周期脚本**:

| 脚本 | 对应 hook 阶段 | 调用方式 | 自动化程度 |
|------|--------------|---------|-----------|
| `task-start.sh` | pre-write 等价 | 手动 `bash` 调用 | 需 agent 主动调用 |
| `task-end.sh` | task-end 等价 | 手动 `bash` 调用 | 需 agent 主动调用 |
| `heartbeat.sh` | 心跳 | 手动或 cron | cron 已配置 |
| `v8-watchdog.sh` | 超时检测 | cron */5 | 自动 |
| `v8-validate.sh` | 收工检查 | 手动 | 需 agent 主动调用 |

**合规执行**: AGENTS.md + SOUL.md 中注入了 V9 二元触发器规则。
agent 必须自觉调用脚本，平台不会自动拦截违规写入。

**已知限制**:
- 无 blocking gate — agent 可以绕过所有脚本直接写文件
- 事件流写入依赖 agent 调用 task-start/task-end
- gate-enforce.py 未被自动调用

---

### Hermes (头孢) — 纯信任制

**配置位置**: `~/.hermes/`

**治理机制**:
- `SOUL.md`: 注入了 V9 二元触发器规则（与 OpenClaw 相同的文本）
- `config.yaml`: 平台配置（model、delegation 限制等）
- `~/.hermes/hooks/`: 目录存在但为空 — **无任何 hook 安装**

**已知限制**:
- 完全依赖 agent 自觉遵守 SOUL.md
- 无拦截、无审计、无事件流自动写入
- Hermes 平台是否支持自定义 hook 尚未验证（hooks 目录存在暗示可能支持）

**待验证项**:
- [ ] `~/.hermes/hooks/` 是否是 Hermes 平台原生 hook 目录
- [ ] Hermes 是否支持 pre/post tool use hook 配置
- [ ] 如果支持，hook stdin 格式是什么

---

### Codex (红霉素) — 无治理

**配置位置**: `~/.codex/`

**治理机制**:
- `config.toml`: 只有 trust_level 和 sandbox_permissions（静态）
- `instructions.md`: 生成式指令（如存在），不包含 hook 配置
- 无 hook 目录、无 hook 配置、无生命周期脚本

**已知限制**:
- Codex 平台不支持自定义 hook
- 唯一的"门控"是 sandbox_permissions（disk-read/disk-write），粒度太粗
- 无法拦截特定路径写入
- 合规只能靠 instructions.md 中注入规则 + agent 自觉

---

## 推广可行性评估

| 平台 | 可推广程度 | 推广策略 | 优先级 |
|------|-----------|---------|--------|
| **Claude Code** | ✅ 已完成参数化 (V9.2) | V8_AGENT_ID 环境变量 | Done |
| **OpenClaw** | 高 — 脚本体系已有 | 在现有脚本中嵌入 gate-enforce 调用 | P0.1 |
| **Hermes** | 中 — 需验证 hook 支持 | 优先验证 hooks/ 目录；不支持则标记 manual | P0.1 (conditional) |
| **Codex** | 低 — 平台不支持 hook | 只能加强 instructions.md 规则注入 | 标记 manual_compliance_required |

## 全平台治理覆盖率

| 指标 | V9.2 当前值 | 备注 |
|------|--------|-------------|
| hook 硬拦截覆盖 | 1/5 (20%) — Claudian 参数化完成 | OpenClaw 脚本辅助, Hermes/Codex manual |
| 事件流自动写入 | 2/5 (40%) — Claudian(自动) + OpenClaw(脚本) | 三阶段 cost-event.sh 已集成 v8_handshake/v8_end |
| 心跳可信度 | 1/5 (20%) — Claudian heartbeat-update.sh | 需 V8_AGENT_PID 环境变量 |
| 合规执行方式分布 | 自动:1, 半自动:1, 信任:1, manual标记:2 | agent-contract.yaml 统一注册 |

## 相关文档

- [[agents-registry]] — Agent 登记表
- [[agent-paths]] — 工具路径登记
- v8-handshake.sh — 生命周期协议
- gate-enforce.py — 统一门禁
