# Skill Resolver

> 用途：让 agent 不再读完整协议猜流程，而是按任务类型选最小工作流。

## 启动顺序

1. 先读 `00-MOC/LLM-Wiki-MOC.md`。
2. 再读 `00-MOC/多智能体协作看板.md`。
3. 按任务类型选择下面一个或多个 skill。
4. 只读取目标模块和必要规范，避免全库漫游。

## 同名 skill 解析

- 默认只接受同一真实目录的软链入口；不同实体的同名 skill 由 `02-项目管理/脚本/v9-skill-shadow-check.py` 检测。
- 允许的平台变体必须同时声明同一 `version`、同一 `x-v9-shadow-group`、不同 `x-v9-variant`；调用方按自身平台选择变体，不跨平台回退。
- Reasonix 的 `impeccable`：Agents/Codex 入口选 `codex-agents`，Claude 入口选 `claude`，二者当前均为 v3.5.0。

## 路由

| 任务 | 使用 skill |
|---|---|
| 开始任意任务、确认 owner/blocked/产物路径 | `start-task/SKILL.md` |
| 读取/编辑飞书或 Lark 文档、知识库、表格、会议纪要并沉淀 | `feishu-collection/SKILL.md` + `ingest-source/SKILL.md` |
| 转换/抽取本地文件或 URL 为 Markdown（PDF/DOCX/PPTX/XLSX/HTML/CSV/JSON/XML/ZIP/EPUB/图片/音频） | `markitdown/SKILL.md` + `ingest-source/SKILL.md` |
| 图片、截图、扫描件、无文字层 PDF 的纯文字识别 | `paddleocr-text-recognition/SKILL.md` + `ingest-source/SKILL.md` |
| 扫描 PDF、图片文档、表格/公式/印章/复杂版面的结构化解析 | `paddleocr-doc-parsing/SKILL.md` + `ingest-source/SKILL.md` |
| 搜索/阅读外部平台内容（网页、GitHub、YouTube/B站、RSS、V2EX、公众号、社媒） | `agent-reach/SKILL.md` + `ingest-source/SKILL.md` |
| 研究任意主题最近 30 天的社区热度、舆情、GitHub/Reddit/HN/YouTube 等趋势 | `last30days/SKILL.md` |
| 联网搜集行业信息、技术文档、论文、博客 | `web-research/SKILL.md` |
| 写 PRD、评审 PRD、生成 PRD 骨架或校验 PRD | `yijing-prd-spec/SKILL.md` + `publish-wiki/SKILL.md` |
| 写设计方案、规范、模块摘要 | `publish-wiki/SKILL.md` |
| 任务完成、暂停、转交给其它 agent | `handoff-task/SKILL.md` |
| 生成图表（架构图/流程图/关系图/信息图） | `generate-diagram/SKILL.md` |
| 前端/HTML 原型/应用 UI 的颜色、字体、图标、视觉一致性审查 | `taste-skill/SKILL.md` |
| 发现资料冲突、编号冲突、路径冲突、需求冲突 | `review-contradictions/SKILL.md` |
| 不知道某个动效/交互效果叫什么、想反查准确术语 | `animation-vocabulary/SKILL.md` |
| 构建或评审手势/弹簧/惯性/材质等 Apple 风格流体交互 | `apple-design/SKILL.md` |
| UI 打磨、组件设计、动效决策的品味/哲学咨询 | `emil-design-eng/SKILL.md` |
| 扫描 UI/代码库找"该动却没动"的动效机会（只读，不改码） | `find-animation-opportunities/SKILL.md` |
| 通读代码库动效产出分级审计 + 自包含实施计划（只读，不改码） | `improve-animations/SKILL.md` |
| 按高标准评审动效代码（需显式调用） | `review-animations/SKILL.md` |

## 图片/OCR 来源标注铁律

凡是从图片、截图、扫描件、PDF 图片页得到的信息，输出必须标注来源：

- `[OCR:PaddleOCR-local]`：本地 PaddleOCR 或 helper 抽出的文字
- `[OCR:PaddleOCR-api]`：PaddleOCR 云 API 抽出的文字/结构
- `[Vision:multimodal]`：多模态模型对画面、布局、交互、图标、设计的理解
- `[Hybrid]`：OCR 事实 + 视觉/业务推理后的结论
- `[Unverified]`：未被 OCR 或视觉直接确认的信息

字段名、金额、编号、日期、状态、按钮文案以 OCR 为准；布局、图标意图、视觉层级、交互关系以多模态视觉或人工复核为准。Reasonix / Hermes Desktop 只有 PaddleOCR 时，只能声明“文字识别结果”，不能声明“已完整看懂图片”。

## 禁止

- 不要跳过 MOC 和看板直接改 PRD。
- 不要把 source 原文直接复制到发布层。
- 不要在没有产物路径的情况下声称任务完成。
- 不要改 `00-MOC/` 和 `30-规范/` 的规则性文件，除非任务明确要求。
