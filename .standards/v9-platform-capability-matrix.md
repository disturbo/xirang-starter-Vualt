---
type: 规范
domain: 多智能体治理
version: "1.0.0"
created: 2026-05-31
updated: 2026-07-19
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
| **pre-write 硬拦截** | ✅ 原生 hook | ❌ 无平台 hook | ❌ 无平台 hook | ⚠️ `apply_patch` 硬拦；`exec_command` 仍有 shell 写入盲区 |
| **post-write 审计** | ✅ 原生 hook | ❌ 手动脚本 | ❌ 无 | ⚠️ `apply_patch` 自动审计；shell 写入不自动逐文件记账 |
| **session 状态检测** | ✅ session-guard.sh | ❌ 手动 | ❌ 无 | ✅ Desktop session hook |
| **gate-enforce 集成** | ✅ hook 自动调用 | ⚠️ 需手动调用 | ❌ 无 | ⚠️ Desktop adapter 自动调用；shell 写入依赖事后 scope 检查 |
| **事件流自动写入** | ✅ hook 自动 | ⚠️ 脚本手动 | ❌ 依赖 SOUL 合规 | ✅ 生命周期与 `apply_patch` 写入事件 |
| **心跳更新** | ✅ heartbeat-update.sh (V9.2) | ⚠️ heartbeat.sh 需手动调 | ❌ 无 | ⚠️ 有生命周期状态，无独立长任务 heartbeat |
| **成本追踪** | 已退役 | 已退役 | 已退役 | 已退役 |
| **任务卡授权** | ✅ task-card.yaml | ⚠️ 脚本创建 | ❌ 无 | ✅ handshake + task card |
| **合规执行方式** | 自动 (hook 强制) | 半自动 (脚本辅助) | 信任制 (SOUL 契约) | 半自动（Desktop hook + shell 写入事后检查） |

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

### Codex (红霉素) — Desktop hook 已接入，仍有 shell 写入盲区

**配置位置**: Vault `.codex/hooks.json` + `.standards/hooks/codex-hook-adapter.py`

**治理机制**:
- Desktop 的 `SessionStart / PreToolUse / PostToolUse / Stop` 已映射到现有 V9 门禁与生命周期协议。
- `apply_patch` 真实触发 pre-write 与 post-write；拒绝 canary 已证明阻断路径可达。
- Codex 身份固定记录为 `agent=hongmeisu, platform=codex`，不再误记为 claudian。
- hook 固定使用 `/usr/bin/python3` 与系统 PATH，避免长会话中的 Homebrew Python 动态加载卡死。

**已知限制**:
- `exec_command` 可以通过 shell 间接写文件，当前只有 session guard 与事后 scope-tamper 检查，不能声明逐文件硬拦截。
- 仅以 hook 配置存在不足以验收；必须保留 write/deny canary 与生命周期事件证据。

---

## 推广可行性评估

| 平台 | 可推广程度 | 推广策略 | 优先级 |
|------|-----------|---------|--------|
| **Claude Code** | ✅ 已完成参数化 (V9.2) | V8_AGENT_ID 环境变量 | Done |
| **OpenClaw** | 高 — 脚本体系已有 | 在现有脚本中嵌入 gate-enforce 调用 | P0.1 |
| **Hermes** | 中 — 需验证 hook 支持 | 优先验证 hooks/ 目录；不支持则标记 manual | P0.1 (conditional) |
| **Codex** | 高 — Desktop 已适配 `apply_patch` / `exec_command` | `.codex/hooks.json` + `codex-hook-adapter.py` | Done |

## 全平台治理覆盖率

| 指标 | V9.2 当前值 | 备注 |
|------|--------|-------------|
| hook 硬拦截覆盖 | 1 个完整 + 1 个部分 / 4 平台 | Claudian 完整；Codex `apply_patch` 已覆盖但 shell 写入仍是盲区 |
| 事件流自动写入 | 2/4 平台有自动生命周期/写入事件 | Claudian 完整；Codex 部分；成本事件流已退役 |
| 心跳可信度 | 1/4 完整 + Codex 生命周期状态 | Codex 尚无独立长任务 heartbeat |
| 合规执行方式分布 | 自动:1，半自动:2，信任:1 | Claudian 自动；OpenClaw/Codex 半自动；Hermes 信任制 |

## 相关文档

- [[agents-registry]] — Agent 登记表
- [[agent-paths]] — 工具路径登记
- v8-handshake.sh — 生命周期协议
- gate-enforce.py — 统一门禁
