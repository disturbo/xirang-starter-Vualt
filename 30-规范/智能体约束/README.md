---
title: "智能体约束文件索引"
updated: 2026-06-10
created: 2026-05-28
type: index
---

# 智能体约束文件索引

> 本文件夹存放各 Agent 当前生效的运行时配置快照，方便在 Obsidian 内阅读和对比。
> 这些文件是**只读镜像**，编辑无效。修改请到源文件。

## 刷新方式

```bash
python3 .prompt-src/prompt-build.py --apply       # 重建自动平台
python3 .prompt-src/prompt-build.py --apply-block # 同步手动平台合规块
# 然后重新拷贝到本文件夹（或用下方一键脚本）
```

---

## 文件清单

| Agent | 平台 | 文件 | 源路径 | 维护方式 |
|-------|------|------|--------|---------|
| Claudian + WorkBuddy | Claudian（Obsidian嵌入） | [[Claudian-CLAUDE]] | `.claude/CLAUDE.md` | 手动 + apply-block |
| WorkBuddy | Claudian（Obsidian嵌入） | [[WorkBuddy-agent]] | `.claude/agents/workbuddy.md` | prompt-build --apply |
| 阿莫西林 | OpenClaw | [[阿莫西林-AGENTS]] | `~/.openclaw/workspace/AGENTS.md` | 手动 + apply-block |
| 阿莫西林 | OpenClaw | [[阿莫西林-MEMORY]] | `~/.openclaw/workspace/MEMORY.md` | 阿莫西林自维护 |
| 头孢 | Hermes | [[头孢-SOUL]] | `~/.hermes/SOUL.md` | 手动 + apply-block |
| 头孢 | Hermes | [[头孢-MEMORY]] | `~/.hermes/memories/MEMORY.md` | 头孢自维护 |
| 红霉素 | Codex | [[红霉素-instructions]] | `.codex/instructions.md` | prompt-build --apply |

---

## 架构说明

```
prompt-build.py 生成链：

  .prompt-src/v9-compliance-block.md   ← V9 合规块（唯一真理源）
  .prompt-src/preflight-auto-template.md ← 自动平台 pre-flight 模板
  .prompt-src/prompt-base.md           ← 全局基类
  .prompt-src/agents/{id}.delta.md     ← 各 Agent 特化配置
       ↓
  [自动平台] prompt-build --apply → .claude/agents/*.md, .codex/instructions.md
  [手动平台] prompt-build --apply-block → ~/.hermes/SOUL.md, ~/.openclaw/workspace/AGENTS.md
```

## 快照日期

最近一次全量刷新：**2026-06-11**
