---
title: "Agent Paths — 工具路径统一登记"
type: 规范
domain: 多智能体协作
maintainer: 用户
created: 2026-05-13
last_verified: 2026-06-10
status: active
tags: [规范]
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
| **Agent-Reach** | `~/.local/bin/agent-reach` | `~/.agent-reach/` | 1.4.0 | `agent-reach --version` |
| **PaddleOCR** | `~/.local/bin/paddleocr` | `~/.paddlex/official_models/` | paddleocr 3.6.0 / paddlepaddle 3.3.1 | `paddleocr --version` |
| **yt-dlp** | `~/.local/bin/yt-dlp` | `~/Library/Application Support/yt-dlp/config` | 2026.03.17 | `yt-dlp --version` |
| **mcporter** | `~/.npm-global/bin/mcporter` | `~/.mcporter/mcporter.json` | (查看 `mcporter --version`) | `mcporter config list` |
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
| **共享 Skill 库** | `~/.skills-manager/skills/` | 本机多智能体共享 skill 主目录（单一真相源） |
| **ClawHub** | `npx clawhub`（v0.11.0） | Skill 注册表管理（搜索/安装/更新） |
| **Codex Skills** | `~/.codex/skills/` | Codex 的 skill 入口（共享 skill 使用 symlink） |
| **Claude Code Skills** | `~/.claude/skills/` | Claude Code / Claudian skill 入口（共享 skill 使用 symlink） |
| **Agents Skills** | `~/.agents/skills/` | 通用 Agent Skills 入口（共享 skill 使用 symlink） |
| **Hermes Skills** | `~/.hermes/skills/` | 头孢的 skill 库（平台私有 + 共享 symlink） |
| **Hermes Desktop Skills** | `~/Library/Application Support/hermes-desktop/skills/` | Hermes Desktop 的共享 skill 入口（当前用于 PaddleOCR 识图补盲） |
| **Reasonix Global Skills** | `~/Library/Application Support/reasonix/global-workspace/.agents/skills/` 与 `.claude/skills/` | Reasonix Desktop 全局 workspace 的双 skill 入口 |
| **OpenClaw Skills** | `~/.openclaw/skills/` | 阿莫西林的 skill 库（平台私有 + 共享 symlink） |
| **WorkBuddy Skills** | `~/.workbuddy/skills/` | Claudian/WorkBuddy 的 skill 库（平台私有 + 共享 symlink） |
| **Vault Skills** | `~/Desktop/obsidianVault/.skills/` | Vault 工作流 skill 与共享 skill 路由入口 |

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
  ~/.local/bin/agent-reach \
  ~/.local/bin/paddleocr \
  ~/.local/bin/yt-dlp \
  ~/.npm-global/bin/mcporter \
  ~/.mcporter/mcporter.json \
  /opt/homebrew/bin/ollama \
  ~/Desktop/obsidianVault \
  ~/wiki \
  ~/.gbrain/brain.pglite \
  ~/.openclaw/workspace \
  ~/.skills-manager/skills \
  ~/.skills-manager/skills/agent-reach/SKILL.md \
  ~/.skills-manager/skills/feishu-collection/SKILL.md \
  ~/.skills-manager/skills/last30days/SKILL.md \
  ~/.skills-manager/skills/markitdown/SKILL.md \
  ~/.skills-manager/skills/paddleocr-text-recognition/SKILL.md \
  ~/.skills-manager/skills/paddleocr-doc-parsing/SKILL.md \
  ~/.skills-manager/skills/yijing-prd-spec/SKILL.md \
  ~/.skills-manager/skills/taste-skill/SKILL.md \
  ~/.skills-manager/skills/flowforge/SKILL.md \
  ~/.codex/skills \
  ~/.claude/skills \
  ~/.agents/skills \
  ~/.hermes/skills \
  ~/Library/Application\ Support/hermes-desktop/skills \
  ~/Library/Application\ Support/reasonix/global-workspace/.agents/skills \
  ~/Library/Application\ Support/reasonix/global-workspace/.claude/skills \
  ~/.openclaw/skills \
  ~/.workbuddy/skills \
  ~/Desktop/obsidianVault/.skills \
  ~/.openclaw/installs/gbrain-master ; do
  if [ -e "$p" ]; then echo "✅ $p"; else echo "❌ $p (缺失)"; fi
done
```

### 共享 Skill 库 symlink 校验
```bash
# 确保共享 skill 的平台入口指向同一主目录
for slug in agent-reach feishu-collection last30days markitdown paddleocr-text-recognition paddleocr-doc-parsing yijing-prd-spec taste-skill flowforge; do
  target="$HOME/.skills-manager/skills/$slug"
  for p in "$HOME/.codex/skills/$slug" \
           "$HOME/.openclaw/skills/$slug" \
           "$HOME/.workbuddy/skills/$slug"; do
    if [ -L "$p" ] && [ "$(readlink "$p")" = "$target" ]; then
      echo "✅ $p"
    else
      echo "❌ $p (symlink 断链或指向错误)"
    fi
  done
done

# Hermes / Vault 按能力挂在不同子目录，需逐项校验
test "$(readlink "$HOME/.hermes/skills/yijing-dms/feishu-collection")" = "$HOME/.skills-manager/skills/feishu-collection" && echo "✅ Hermes feishu-collection"
test "$(readlink "$HOME/.hermes/skills/research/agent-reach")" = "$HOME/.skills-manager/skills/agent-reach" && echo "✅ Hermes agent-reach"
test "$(readlink "$HOME/.hermes/skills/research/last30days")" = "$HOME/.skills-manager/skills/last30days" && echo "✅ Hermes last30days"
test "$(readlink "$HOME/.hermes/skills/productivity/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Hermes paddleocr-text-recognition"
test "$(readlink "$HOME/.hermes/skills/productivity/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Hermes paddleocr-doc-parsing"
test "$(readlink "$HOME/.hermes/skills/software-development/markitdown")" = "$HOME/.skills-manager/skills/markitdown" && echo "✅ Hermes markitdown"
test "$(readlink "$HOME/.hermes/skills/yijing-dms/yijing-prd-spec")" = "$HOME/.skills-manager/skills/yijing-prd-spec" && echo "✅ Hermes yijing-prd-spec"
test "$(readlink "$HOME/.hermes/skills/software-development/taste-skill")" = "$HOME/.skills-manager/skills/taste-skill" && echo "✅ Hermes taste-skill"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/feishu-collection")" = "$HOME/.skills-manager/skills/feishu-collection" && echo "✅ Vault feishu-collection"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/agent-reach")" = "$HOME/.skills-manager/skills/agent-reach" && echo "✅ Vault agent-reach"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/last30days")" = "$HOME/.skills-manager/skills/last30days" && echo "✅ Vault last30days"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Vault paddleocr-text-recognition"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Vault paddleocr-doc-parsing"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/markitdown")" = "$HOME/.skills-manager/skills/markitdown" && echo "✅ Vault markitdown"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/yijing-prd-spec")" = "$HOME/.skills-manager/skills/yijing-prd-spec" && echo "✅ Vault yijing-prd-spec"
test "$(readlink "$HOME/Desktop/obsidianVault/.skills/taste-skill")" = "$HOME/.skills-manager/skills/taste-skill" && echo "✅ Vault taste-skill"
test "$(readlink "$HOME/.claude/skills/agent-reach")" = "$HOME/.skills-manager/skills/agent-reach" && echo "✅ Claude agent-reach"
test "$(readlink "$HOME/.claude/skills/last30days")" = "$HOME/.skills-manager/skills/last30days" && echo "✅ Claude last30days"
test "$(readlink "$HOME/.claude/skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Claude paddleocr-text-recognition"
test "$(readlink "$HOME/.claude/skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Claude paddleocr-doc-parsing"
test "$(readlink "$HOME/.agents/skills/agent-reach")" = "$HOME/.skills-manager/skills/agent-reach" && echo "✅ Agents agent-reach"
test "$(readlink "$HOME/.agents/skills/last30days")" = "$HOME/.skills-manager/skills/last30days" && echo "✅ Agents last30days"
test "$(readlink "$HOME/.agents/skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Agents paddleocr-text-recognition"
test "$(readlink "$HOME/.agents/skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Agents paddleocr-doc-parsing"
test "$(readlink "$HOME/Library/Application Support/hermes-desktop/skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Hermes Desktop paddleocr-text-recognition"
test "$(readlink "$HOME/Library/Application Support/hermes-desktop/skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Hermes Desktop paddleocr-doc-parsing"
test "$(readlink "$HOME/Library/Application Support/reasonix/global-workspace/.agents/skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Reasonix .agents paddleocr-text-recognition"
test "$(readlink "$HOME/Library/Application Support/reasonix/global-workspace/.agents/skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Reasonix .agents paddleocr-doc-parsing"
test "$(readlink "$HOME/Library/Application Support/reasonix/global-workspace/.claude/skills/paddleocr-text-recognition")" = "$HOME/.skills-manager/skills/paddleocr-text-recognition" && echo "✅ Reasonix .claude paddleocr-text-recognition"
test "$(readlink "$HOME/Library/Application Support/reasonix/global-workspace/.claude/skills/paddleocr-doc-parsing")" = "$HOME/.skills-manager/skills/paddleocr-doc-parsing" && echo "✅ Reasonix .claude paddleocr-doc-parsing"
```

---

## Skill 安装与管理

| 工具 | 路径/命令 | 用途 |
|------|---------|------|
| **Codex skill-installer/skill-creator** | `~/.codex/skills/.system/` | 安装或创建 Codex 兼容 skill |
| **clawhub** | `npx clawhub`（全局 v0.11.0） | Skill 注册表搜索、安装、更新（旧共享路径需手动迁移） |
| **共享 Skill 库** | `~/.skills-manager/skills/` | 所有 agent 共享的能力源文件（单一真相源） |
| **Skill Inventory** | `~/Desktop/obsidianVault/00-MOC/Skill-Inventory.md` | 跨平台 skill 索引、入口、生命周期 |

**跨平台共享规则**：
- 通用 skill（feishu-collection、markitdown、yijing-prd-spec、taste-skill、flowforge 等）→ 先落到 `~/.skills-manager/skills/{slug}`
- 各平台通过 symlink 指向 `~/.skills-manager/skills/{slug}`：
  - `~/.codex/skills/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/.openclaw/skills/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/.workbuddy/skills/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/.hermes/skills/{分类}/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/Desktop/obsidianVault/.skills/{slug}` → `~/.skills-manager/skills/{slug}`（需要 Vault 路由时）
- Claude Code / Agent Skills 自动入口：
  - `~/.claude/skills/{slug}` → `~/.skills-manager/skills/{slug}`（需要 Claude Code 识别时）
  - `~/.agents/skills/{slug}` → `~/.skills-manager/skills/{slug}`（需要 Agent Skills 通用入口时）
- Reasonix / Hermes Desktop 识图补盲入口：
  - `~/Library/Application Support/reasonix/global-workspace/.agents/skills/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/Library/Application Support/reasonix/global-workspace/.claude/skills/{slug}` → `~/.skills-manager/skills/{slug}`
  - `~/Library/Application Support/hermes-desktop/skills/{slug}` → `~/.skills-manager/skills/{slug}`
- 平台专属 skill 留在各平台自己的 skills 目录，不放入共享库
- 同名 skill 如必须保留多份实体目录，必须在 Skill Inventory 标注“平台私有”或“分叉版”
- 一台机器只装一份共享实体；CLI 已存在时，skill 只写路由、约束和踩坑，不复制执行器逻辑

## 维护节奏

- **每月一次**：手动跑校验脚本，更新本表"last_verified"字段
- **变更时**：任何 AI 装新工具或换路径，必须立即更新本表
- **崩溃恢复时**：本表是恢复路径的权威依据

---

## 相关文档

- [[50-经验/Agent协作方法论/息壤V9-运行时契约卡|息壤V9-运行时契约卡]] — 当前运行规则
- [[agents-registry]] — 跨智能体登记
- [[多智能体协作看板]] — 跨智能体沟通入口
