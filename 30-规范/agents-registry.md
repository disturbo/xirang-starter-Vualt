---
type: 规范
domain: 多智能体协作
maintainer: {你的名字}
created: 2026-05-13
status: active
---

# Agents Registry — 跨智能体登记表

> 每个 AI 助手必须登记：叫啥、住哪、能干啥、会改啥。
> 任何 AI **改 vault 规范前**，必须先看本表，确认不会冒犯其他智能体的领地。

---

## 🐛 阿莫西林（OpenClaw · Main Session）

| 字段 | 内容 |
|------|------|
| **承载** | OpenClaw v?.?.? · 主会话 |
| **路径** | `~/.openclaw/workspace/` |
| **守则** | `~/.openclaw/workspace/AGENTS.md` |
| **身份** | `~/.openclaw/workspace/IDENTITY.md` / `SOUL.md` |
| **职责** | {项目名} 项目监督者 + 主理；vault 知识图谱维护；产出物质量把关 |
| **可改文件** | OpenClaw workspace、vault 全部、~/wiki/ |
| **不可改** | Hermes 配置（`~/.hermes/`）、其他 AI 的 SOUL/AGENTS |
| **沟通入口** | vault `02-项目管理/智能体协作看板.md` |
| **最近上线** | 2026-05-16（升级计划登记到看板） |

---

## 💧 头孢（Hermes Agent · CLI）

| 字段 | 内容 |
|------|------|
| **承载** | Hermes Agent v0.13.0 · 终端 CLI |
| **路径** | `~/.hermes/` |
| **守则** | `~/.hermes/SOUL.md` |
| **职责** | 多 skill 协同（GBrain、apple、creative 等）；终端命令执行；本地 LLM 调用 |
| **可改文件** | Hermes 自身配置、可读 vault、可读 ~/wiki/ |
| **不可改** | OpenClaw workspace（`~/.openclaw/workspace/`）、阿莫西林的 SOUL/AGENTS/IDENTITY |
| **沟通入口** | vault `02-项目管理/智能体协作看板.md` |
| **最近活跃** | 2026-05-12 升级方法论 v4.0（5/13 由阿莫西林订正为 v4.1）；memory.md 最后更新 2026-04-26 |

---

## ⚡Claudian / Claudian（常驻主力）

| 字段 | 内容 |
|------|------|
| **承载** | Claudian（Obsidian 嵌入 Claude, CC Switcher 转发） |
| **路径** | `.claude/CLAUDE.md` |
| **职责** | Vault 操作、脚本基建、PRD/方案设计、原型 HTML、协调调度 |
| **可改文件** | vault 全部、沙箱目录、`.standards/`、配置文件 |
| **不可改** | 其他 agent 的 SOUL/AGENTS（需通过看板协商） |
| **沟通入口** | vault `00-MOC/多智能体协作看板.md` |
| **最近上线** | 2026-05-24（承接青霉素PRD/方案职责） |

---

## 🐕 红霉素 / Codex（按需启用）

| 字段 | 内容 |
|------|------|
| **承载** | OpenAI Codex CLI |
| **路径** | `.codex/instructions.md` |
| **职责** | 代码审核、批量生成、规范检核、结构化治理 |
| **可改文件** | `10-项目/{项目名}/`、`02-项目管理/脚本/`、`~/wiki/`、`_temp/` |
| **不可改** | vault 规范（除非阿莫西林授权）、AI 配置文件 |
| **沟通入口** | vault `00-MOC/多智能体协作看板.md` |
| **最近上线** | 2026-05-23（V8 握手格式升级） |

---

## 🔕 青霉素 / Claude Desktop（已下线 deprecated）

| 字段 | 内容 |
|------|------|
| **承载** | Claude Desktop（桌面端，订阅制） |
| **状态** | deprecated - 订阅到期不再续费（2026-05-24） |
| **原职责** | 大块方案/PRD/原型设计文档（已移交Claudian） |
| **历史产出** | V5/V7 方法论、架构图、LLM Wiki 核实等（看板中标 🐾 青霉素 的 done 任务） |

---

## 🔧 共享基础设施

| 设施 | 路径 | 说明 |
|------|------|------|
| **共享 Skill 库** | `~/skills/` | 所有 agent 共享的能力文件。通用 skill 只装一份，各平台 symlink 指向。 |
| **ClawHub** | `npx clawhub` v0.11.0 | Skill 注册表。`clawhub install --dir ~/skills <slug>` 装到共享库。 |
| **obsidian-markdown** | `~/skills/obsidian-markdown/` | 已装。所有 agent 写出 Obsidian Markdown 时参照此规范。 |

## 📋 跨智能体协作铁律

> 🚨 以下规则违反 = 红线，会写进教训库

1. **不跨界改配置**
   - 阿莫西林不能直接编辑 `~/.hermes/SOUL.md`
   - 头孢不能直接编辑 `~/.openclaw/workspace/AGENTS.md`
   - 跨界沟通必须通过 vault 看板

2. **不假装代表对方**
   - 阿莫西林不能说"头孢同意了 XXX"，除非看板有记录
   - 改方法论前必须在看板留 RFC 等待 ≥ 24h

3. **不重复升级**
   - 任何方法论 / 规范升级前，必须 ls 验证现状
   - 不能把"计划"写成"已完成"
   - 升级后必须在本表"最近活跃"字段标注变更

4. **改本表**
   - 新增智能体上线 → 主动添加条目
   - 智能体下线 → 标注 deprecated 但保留历史
   - 路径变更 → 同时更新 [[agent-paths]]

---

## 看板模板（每次跨智能体沟通必填）

详见 [[多智能体协作看板]]，标准格式：

```
## 2026-05-XX HH:MM | 发起人 → 接收人

**主题**: 一句话说清要做啥
**背景**: 为什么需要协作（不超过 3 行）
**请求**: 具体要对方做什么 / 决策什么
**截止**: YYYY-MM-DD（无截止写"任意"）
**状态**: 待回复 / 已回复 / 已完成 / 已搁置
```

---

## 相关文档

- [[多智能体协作机制]]
- [[agent-paths]]
- [[多智能体协作看板]]
- [[教训库]]
