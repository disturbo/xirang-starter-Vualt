---
title: "LLM Wiki MOC"
version: "1.2"
status: active
type: MOC
tags: [MOC, LLM-Wiki, 知识管理, 多智能体]
created: 2026-01-01
---

# LLM Wiki MOC

> 目标：把 vault 从"资料堆 + 协作记录"收束成 agent 可执行的工作知识库。

## 核心原则

| 原则 | 落地方式 |
|---|---|
| 单一可信源 | vault 是协作黑板；外部只放运行产物 |
| 源/摘要/发布三层 | 原始资料不直接变 PRD，先沉淀摘要，再发布正式文档 |
| 任务可交接 | 任务必须带 owner、next action、blocked by、产物路径 |
| 冲突显式化 | 矛盾、疑点先记录，不悄悄抹平 |
| skill 化 | 常见工作流拆成 `.skills/`，agent 按任务调用 |

## 入口

| 场景 | 先读 |
|---|---|
| 开始任务 | [[多智能体协作看板]] + `.skills/start-task/SKILL.md` |
| 写 PRD / 设计方案 | `.skills/publish-wiki/SKILL.md` |
| 处理新资料 | `.skills/ingest-source/SKILL.md` |
| 交接任务 | `.skills/handoff-task/SKILL.md` |
| 发现冲突 | `.skills/review-contradictions/SKILL.md` |

## 三层资料结构

| 层级 | 路径 | 作用 | 可直接引用到 PRD |
|---|---|---|:---:|
| Source 原始源 | `20-资料/` | 原始文件、会议纪要、截图 | 否 |
| Summary 摘要层 | `10-项目/{项目名}/{模块}/资料摘要.md` | 结构化提炼、去重、标注来源 | 是 |
| Published 发布层 | `10-项目/{项目名}/{模块}/PRD.md` | 已评审或正在交付的正式文档 | 是 |

## Skills 一览

| Skill | 触发条件 | 状态 |
|---|---|---|
| `.skills/RESOLVER.md` | 不知道该读哪个 skill | 已建立 |
| `.skills/start-task/SKILL.md` | 任一 agent 开始任务 | 已建立 |
| `.skills/ingest-source/SKILL.md` | 新增业务文件、文档 | 已建立 |
| `.skills/publish-wiki/SKILL.md` | 摘要进入 PRD/设计方案/规范 | 已建立 |
| `.skills/handoff-task/SKILL.md` | 任务完成、暂停、转交 | 已建立 |
| `.skills/review-contradictions/SKILL.md` | 发现口径冲突 | 已建立 |

## 关联

- [[多智能体协作看板]]
- [[知识管理规范]]
