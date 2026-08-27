# Skill Resolver

用户只需要描述目标，不需要记住 Skill 名称。Agent 先读当前工作区规则，再从下表选择最小充分的 Skill；目录存在只表示可发现，不表示外部账号、CLI 或网络已经接通。

| 目标 | 首选 Skill |
|---|---|
| 开始新的工作区任务 | `start-task` |
| 整理本地文件、截图或外部资料 | `ingest-source` |
| 把资料写成稳定知识、PRD 或规范 | `publish-wiki` |
| 任务暂停、接力或提交 | `handoff-task` |
| 发现来源、文档或规则冲突 | `review-contradictions` |
| 联网查资料 | `web-research`；跨平台采集可用 `agent-reach` |
| 飞书/Lark 文档、Wiki、表格或白板 | `feishu-collection` |
| 本地/URL 文件转 Markdown | `markitdown` |
| 图片、截图、扫描件文字识别 | `paddleocr-text-recognition` |
| 扫描文档结构化解析 | `paddleocr-doc-parsing` |
| 最近 30 天社区趋势研究 | `last30days` |
| 图表、架构图或流程图 | `generate-diagram` |
| 前端视觉一致性 | `taste-skill` |
| 动效命名、设计、机会、审计或评审 | 按目标选择对应 animation Skill |

## 真实性规则

1. 先确认当前 Agent 是否会自动发现工作区 `.skills/`；不会时，显式读取对应 `SKILL.md`。
2. 依赖 CLI、登录、Token、Cookie、浏览器会话或网络的 Skill，先做无副作用状态检查。
3. 未验证的能力只报告 `installed_unverified` 或 `conditional`，不能报告为已接通。
4. Skill 只提供工作方法，不扩大当前任务的写入、外发、发布或账号权限。

## 规范路由

- 新建、移动、归档或打包知识文件：先读 `30-规范/知识库信息架构与生命周期规范.md` 与 `Markdown知识库写作规范.md`。
- 会议材料：先读 `会议纪要整理规范.md`；涉及飞书时再调用 `feishu-collection`。
- 流程图或架构图：`generate-diagram` 必须先读对应流程图/SVG规范；正式飞书流程图同时执行会议来源与原生画板门禁。
- PRD：先读 `通用PRD输出规范.md`，若项目有更具体规范则采用项目上位规范。
