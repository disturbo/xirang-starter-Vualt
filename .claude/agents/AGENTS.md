# AGENTS.md — Agent 入口索引（P2 重构后）

> 本索引指向各 Agent 的实际运行入口。P2 之后架构为：
> - Claudian agents（claudian/qingmeisu/workbuddy）：由 `prompt-build.py` 生成
> - Codex agent（hongmeisu）：由 `prompt-build.py` 生成到 `.codex/instructions.md`
> - OpenClaw agent（xiaochong）：`~/.openclaw/workspace/AGENTS.md` 手动维护
> - Hermes agent（toubao）：`~/.hermes/SOUL.md` 手动维护

## 生成式管理（prompt-build.py）

修改规则：编辑 `.prompt-src/prompt-base.md`（全局）或 `.prompt-src/agents/{id}.delta.md`（个体）→ 运行 `python3 .prompt-src/prompt-build.py --apply`

| Agent | 平台 | 入口文件 | 维护方式 |
|-------|------|----------|----------|
| Claudian | Claudian | `.claude/agents/claudian.md` | 自动生成 |
| 青霉素 | Claudian | `.claude/agents/qingmeisu.md` | 自动生成 |
| WorkBuddy | Claudian | `.claude/agents/workbuddy.md` | 自动生成 |
| 红霉素 | Codex | `.codex/instructions.md` | 自动生成 |
| 阿莫西林 | OpenClaw | `~/.openclaw/workspace/AGENTS.md` | 手动（P2 skip） |
| 头孢 | Hermes | `~/.hermes/SOUL.md` | 手动（P2 skip） |

## 源文件结构

```
.prompt-src/
  prompt-base.md          ← 全局基类（103 行）
  prompt-build.py         ← 构建脚本
  agents/
    claudian.delta.md     ← Claudian 特化
    qingmeisu.delta.md    ← 青霉素特化
    workbuddy.delta.md    ← WorkBuddy 特化
    hongmeisu.delta.md    ← 红霉素特化
    xiaochong.delta.md    ← 阿莫西林特化（仅 build 预览用，不 apply）
    toubao.delta.md       ← 头孢特化（仅 build 预览用，不 apply）
  _build/                 ← 生成产物暂存
```
