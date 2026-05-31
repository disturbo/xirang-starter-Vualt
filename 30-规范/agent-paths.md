---
type: 规范
domain: 多智能体协作
maintainer: {你的名字}
created: 2026-05-13
last_verified: 2026-05-13
status: active
---

# Agent Paths — 工具路径统一登记

> 任何 AI 助手 / 文档 / 脚本提到工具路径时，**只信本表**。
> 本表是"事实源"，方法论文档只引用本表，不重复写路径。
>
> **修改本表前必须 ls 验证存在**。

---

## 核心工具

| 工具 | 命令位置 | 数据/配置位置 | 当前版本 | 验证命令 |
|------|---------|------------|---------|---------|
| **WorkBuddy** | Codebuddy Code 内置 | `~/.workbuddy/` | — | `open ~/.workbuddy/` |
| **OpenClaw** | `~/.npm-global/bin/openclaw` | `~/.openclaw/` | (查看 `openclaw --version`) | `openclaw --version` |
| **Hermes** | `~/.local/bin/hermes` | `~/.hermes/` | (查看 `hermes --version`) | `hermes --version` |
| **GBrain** | `~/.npm-global/bin/gbrain` | `~/.gbrain/brain.pglite/` | 0.33.0 | `gbrain --version` |
| **Ollama** | `/opt/homebrew/bin/ollama` | `~/.ollama/models/` | (查看 `ollama --version`) | `ollama --version` |

---

## 知识库路径

| 知识库 | 路径 | 用途 |
|-------|------|------|
| **WorkBuddy 记忆** | `~/.workbuddy/memory/` | 工作记忆（MEMORY.md + 日记账） |
| **Obsidian Vault** | `~/Desktop/obsidianVault/` | 项目知识中枢（动态、协作） |
| **LLM Wiki** | `~/wiki/` | 跨域长期知识库（稳定、引用） |
| **GBrain DB** | `~/.gbrain/brain.pglite/` | 向量索引数据库 |
| **OpenClaw Workspace** | `~/.openclaw/workspace/` | 阿莫西林的工作区（含 MEMORY.md, AGENTS.md, SOUL.md） |
| **共享 Skill 库** | `~/skills/` | 所有 agent 共享的能力文件（symlink 到各平台技能目录） |
| **ClawHub** | `npx clawhub`（v0.11.0） | Skill 注册表管理（搜索/安装/更新） |
| **Hermes Skills** | `~/.hermes/skills/` | 头孢的 skill 库（含到 ~/skills/ 的 symlink） |
| **OpenClaw Skills** | `~/.openclaw/workspace/skills/` | 阿莫西林的 skill 库（含到 ~/skills/ 的 symlink） |
| **WorkBuddy Skills** | `~/.workbuddy/skills/` | Claudian/WorkBuddy 的 skill 库（含到 ~/skills/ 的 symlink） |

---

## 安装路径（重要）

| 安装包 | 落地位置 | 备注 |
|-------|---------|------|
| **稳定安装目录** | `~/.openclaw/installs/` | ⚠️ 所有手动 git clone 的工具都装这里 |
| **gbrain 源码** | `~/.openclaw/installs/gbrain-master/` | 5/13 重装路径 |
| **❌ 禁止位置** | `~/Desktop/*-experiments-*/` | 5/11 教训：临时目录会被删，软链失效 |

---

## 路径有效性快速校验

```bash
# 一次性验证所有关键路径
for p in \
  ~/.workbuddy \
  ~/.npm-global/bin/openclaw \
  ~/.local/bin/hermes \
  ~/.npm-global/bin/gbrain \
  /opt/homebrew/bin/ollama \
  ~/Desktop/obsidianVault \
  ~/wiki \
  ~/.gbrain/brain.pglite \
  ~/.openclaw/workspace \
  ~/skills \
  ~/skills/obsidian-markdown/SKILL.md \
  ~/.hermes/skills \
  ~/.openclaw/installs/gbrain-master ; do
  if [ -e "$p" ]; then echo "✅ $p"; else echo "❌ $p (缺失)"; fi
done
```

### 共享 Skill 库 symlink 校验
```bash
# 确保三个平台的 symlink 都指向同一文件
for p in ~/.openclaw/workspace/skills/obsidian-markdown \
         ~/.hermes/skills/obsidian-markdown \
         ~/.workbuddy/skills/obsidian-markdown; do
  if [ -L "$p" ] && [ "$(readlink "$p")" = "$HOME/skills/obsidian-markdown" ]; then
    echo "✅ $p"
  else
    echo "❌ $p (symlink 断链或指向错误)"
  fi
done
```

---

## Skill 安装与管理

| 工具 | 路径/命令 | 用途 |
|------|---------|------|
| **clawhub** | `npx clawhub`（全局 v0.11.0） | Skill 注册表搜索、安装、更新 |
| **共享 Skill 库** | `~/skills/` | 所有 agent 共享的能力源文件（单一真相源） |

**跨平台共享规则**：
- 通用 skill（obsidian-markdown、yijing-prd-spec 等）→ `clawhub install --dir ~/skills`
- 各平台通过 symlink 指向 `~/skills/`：
  - `~/.openclaw/workspace/skills/{slug}` → `~/skills/{slug}`
  - `~/.hermes/skills/{slug}` → `~/skills/{slug}`
  - `~/.workbuddy/skills/{slug}` → `~/skills/{slug}`
- 平台专属 skill 留在各平台自己的 skills 目录，不放入共享库
- 一台机器只装一份，所有 agent 自动可见

## 维护节奏

- **每月一次**：手动跑校验脚本，更新本表"last_verified"字段
- **变更时**：任何 AI 装新工具或换路径，必须立即更新本表
- **崩溃恢复时**：本表是恢复路径的权威依据

---

## 相关文档

- [[多智能体协作机制]] — 方法论本体
- [[agents-registry]] — 跨智能体登记
- [[多智能体协作看板]] — 跨智能体沟通入口
