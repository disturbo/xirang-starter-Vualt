# Skill Resolver

> 用途：让 agent 不再读完整协议猜流程，而是按任务类型选最小工作流。

## 启动顺序

1. 先读 `00-MOC/LLM-Wiki-MOC.md`。
2. 再读 `00-MOC/多智能体协作看板.md`。
3. 按任务类型选择下面一个或多个 skill。
4. 只读取目标模块和必要规范，避免全库漫游。

## 路由

| 任务 | 使用 skill |
|---|---|
| 开始任意任务、确认 owner/blocked/产物路径 | `start-task/SKILL.md` |
| 读取飞书、docx、xlsx、截图、会议纪要并沉淀 | `ingest-source/SKILL.md` |
| 联网搜集行业信息、技术文档、论文、博客 | `web-research/SKILL.md` |
| 写 PRD、设计方案、规范、模块摘要 | `publish-wiki/SKILL.md` |
| 任务完成、暂停、转交给其它 agent | `handoff-task/SKILL.md` |
| 生成图表（架构图/流程图/关系图/信息图） | `generate-diagram/SKILL.md` |
| 发现资料冲突、编号冲突、路径冲突、需求冲突 | `review-contradictions/SKILL.md` |

## 禁止

- 不要跳过 MOC 和看板直接改 PRD。
- 不要把 source 原文直接复制到发布层。
- 不要在没有产物路径的情况下声称任务完成。
- 不要改 `00-MOC/` 和 `30-规范/` 的规则性文件，除非任务明确要求。
