# Publish Wiki

## When

将资料摘要、原型评审、业务规则沉淀为 PRD、设计方案、规范、模块 README 时使用。

## Check

- 是否已读 source 或 summary。
- 是否已有旧版 PRD/设计方案。
- 是否需要同步规范或模块进度。
- 是否涉及编号、路径、状态、权限这类全局口径。

## Steps

1. 从 summary 层抽取，不直接搬 source 原文。
2. 发布层文件必须有 frontmatter：version、status、last-edited-by、last-edited-at。
3. 新增或大改时更新模块 README/模块笔记的交付物清单。
4. 涉及进度时更新 `00-MOC/{项目名}-MOC.md`。
5. 涉及规则时更新 `30-规范/`，并在决策日志或 changelog 说明影响。

## Quality Bar

- 发布层可被其它 agent 直接执行。
- 来源、状态、待确认事项清楚。
- 不制造孤立文件：必须能从 MOC 或模块入口找到。
