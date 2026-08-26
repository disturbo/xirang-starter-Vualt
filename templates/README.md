# Agent 底层约束模板

本目录给安装器和执行安装的 Agent 使用，普通用户不需要选择文件。

| 宿主 | 原生入口模板 | 默认目标 |
|---|---|---|
| Codex | `codex-AGENTS.md` | 工作区根 `AGENTS.md` 受管区块 |
| Claude Code | `claude-CLAUDE.md` | 工作区 `.claude/CLAUDE.md` |
| OpenClaw | `openclaw-AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| Hermes | `hermes-SOUL.md` | `~/.hermes/SOUL.md` |
| DeepSeek Harness | `deepseek-harness-AGENTS.md` | `~/.dsh/AGENTS.md` |
| WorkBuddy / CodeBuddy | `workbuddy-CODEBUDDY.md` | `~/.codebuddy/CODEBUDDY.md` |
| 未登记 Agent | `generic-AGENTS.md` | 工作区根通用约束，不写未知外部路径 |

模板是分发源，不是应用证据。安装器只把当前调用 Agent 对应的模板合并到原生入口，并在写入前把该外部路径列进确认卡和恢复快照。其他模板保持未应用。平台清单由 `platforms.json` 开放登记，不是固定角色表；未登记平台安全降级到工作区通用入口。

所有模板共同遵守 `agent-root-spec.md`，并引用目标工作区的 `.xirang/adapters/PROTOCOL.md` 和机器契约。宿主入口加载和当前会话 canary 完成前，只能报告“入口已应用，待验证”。
