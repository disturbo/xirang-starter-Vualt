---
title: Skill Inventory
version: "1.0-portable"
status: current
type: MOC
tags: [Skill, Agent, 工具]
---

# Skill Inventory — 随包可移植清单

本清单只描述这个息壤完整包实际携带的 Skill。它不复制任何人的本机路径、账号状态或项目专用能力，也不把宿主内置 Skill 冒充成随包内容。

## 当前闭包

| 口径 | 数量 | 含义 |
|---|---:|---|
| 随包 Skill | 20 | `.skills/` 内存在完整 `SKILL.md` 和所需资源 |
| 工作区通用流程 | 7 | 适用于知识库采集、发布、交接、审查和图表 |
| 通用共享 Skill | 13 | 文件转换、OCR、飞书、研究、UI 与动效能力 |
| Obsidian 插件 | 16 | 通用插件程序与许可证随包提供；Floating TOC 与 Supercharged Links 已启用，个人运行配置不打包；不含息壤专用插件 |

目录存在只证明 `installed`。需要外部 CLI、网络、账号、Token、Cookie、浏览器或应用连接的能力，在当前调用成功前一律视为 `conditional` 或 `installed_unverified`。

## 工作区流程

| Skill | 何时使用 | 外部依赖 |
|---|---|---|
| `start-task` | 新任务启动、识别规则与执行边界 | 无；启用息壤时使用 StateStore |
| `ingest-source` | 原始文件、截图、导出或外部资料入库 | 按文件类型选择转换/OCR 工具 |
| `publish-wiki` | 把可靠摘要写成稳定知识、PRD、决策或规范 | 无 |
| `handoff-task` | 暂停、接力、压缩上下文或提交 | 无；handoff 不授予权限 |
| `review-contradictions` | 两个来源或产物互相冲突 | 无 |
| `web-research` | 联网查官方资料、论文、标准或时效信息 | 网络与当前 Agent 的浏览能力 |
| `generate-diagram` | 架构、流程、状态、层级或关系需要可视化 | 取决于选择的图表工具 |

## 通用共享 Skill

| Skill | 能力 | 当前随包状态 |
|---|---|---|
| `agent-reach` | 跨网页、代码、视频与社区的搜索/读取路由 | Skill 已携带；各 CLI 和登录现场检查 |
| `feishu-collection` | 飞书/Lark 对象类型识别与工具路由 | Skill 已携带；连接器与授权现场检查 |
| `last30days` | 最近 30 天社区与 Web 趋势研究 | 脚本已携带；部分来源需要账号或 Cookie |
| `markitdown` | PDF、Office、HTML、数据文件转 Markdown | Skill 已携带；需安装 `markitdown` CLI |
| `paddleocr-text-recognition` | 图片、截图和扫描件文字识别 | 本地 helper 已携带；需 PaddleOCR 依赖 |
| `paddleocr-doc-parsing` | 扫描文档、表格、公式和版面结构解析 | Skill 已携带；本地或云解析能力现场检查 |
| `taste-skill` | 前端视觉一致性与设计系统约束 | Skill 和本地审计脚本已携带 |
| `animation-vocabulary` | 把模糊动效描述反查为准确术语 | 已携带 |
| `apple-design` | Apple 风格交互、手势、弹簧与材质原则 | 已携带 |
| `emil-design-eng` | UI 打磨、组件和动效决策哲学 | 已携带 |
| `find-animation-opportunities` | 只读发现真正值得加动效的位置 | 已携带 |
| `improve-animations` | 只读审计动效并输出实施计划 | 已携带 |
| `review-animations` | 高标准评审动效代码 | 已携带；需要显式调用 |

## 没有随包复制的能力

- Codex、Claude、Hermes、OpenClaw、WorkBuddy、DeepSeek Harness 或其他宿主的内置 Skill、连接器和缓存。
- 具体业务项目、品牌、人员或账号专用 Skill。
- 需要账号轮换、现有登录态、个人 Cookie、私有 Token 或本机绝对路径才能工作的配置。
- 实验、备份、下载缓存、`node_modules`、`site-packages` 和同名影子副本。

这些能力可以由当前 Agent 依据自己的平台另行发现或安装，但不能因为它们出现在别人的清单里就宣称已经接通。

## Agent 如何路由

1. 先读 `.skills/RESOLVER.md`。
2. 根据自然语言目标选择最小充分 Skill，不要求用户报 Skill 名称。
3. 读取对应 `SKILL.md` 的完整内容；仅在需要时再读其 `references/`。
4. 依赖外部状态时先做无副作用检查。
5. Skill 不扩大任务授权；写入、外发、发布和账号操作仍受当前执行包络约束。

第三方来源和许可证见包根 `THIRD-PARTY-NOTICES.md`。
