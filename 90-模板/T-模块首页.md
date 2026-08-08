---
title: "{{module_name}}"
module_id: "{{module_id}}"
module_name: "{{module_name}}"
module_no: "{{module_no}}"
status: "pending"
prd_status: "pending"
prototype_status: "pending"
design_status: "pending"
maturity: "骨架占位"
updated: "{{date}}"
last-edited-by: "{{agent_name}}"
tags: [模块入口, "{{module_name}}", LLM-Wiki]
created: 2026-05-16
---

# {{module_name}}

> **模块编号**: {{module_no}} | **原始ID**: {{module_id}} | **状态**: `待启动`
> **所属域**：{{domain}}
> **业务编码**：{{business_code}}
> **优先级**：{{priority}}
> **负责人**：用户

---

## Agent 启动顺序

1. 读本 README → 确认模块范围和当前状态
2. 读 `资料摘要.md` → 掌握业务背景和现有资料
3. 读 `设计方案.md`（若有）→ 确认已确定的方案
4. 读 [[5步工作流]] → 确认当前在哪一步，下一步产出什么

---

## 当前状态

| 项 | 状态 |
|---|---|
| 资料摘要 | ⏳ 待编写 |
| 启动问卷 | ⏳ 待执行 |
| 设计方案 | ⏳ 待编写 |
| 逻辑确认稿 | ⏳ 待编写 |
| 原型 | ⏳ 待制作 |
| PRD | ⏳ 待编写 |

---

## 交付物清单

| 类型 | Vault 路径 | 说明 |
|---|---|---|
| 资料摘要 | [[知识库工程化/01-个人库团队库联动/资料摘要]] | Step 1 · RAG 检索产出 |
| 设计方案 | [[10-项目/基线/01-PDI管理/设计方案]] | Step 2 · 业务流程 + 角色权限 + 状态机 + 字段 |
| 逻辑确认稿 | [[逻辑确认稿]] | Step 3.5 · 页面×字段×操作映射表 |
| 原型 | 见下方原型映射 | Step 4 · HTML 原型页面 |
| PRD | [[{{module_no}}-{{module_name}}-PRD]] | Step 5 · 完整 PRD 文档 |

---

## 原型映射

> 原型根路径：`~/Desktop/沙箱/示例项目项目/示例项目EXAMPLE/prototype/v3.2/pages/{{module_abbr}}/`

| 原型页面 | 沙箱路径 | 对应说明 |
|---|---|---|
| {页面1} | `{module_abbr}-{page1}.html` | {说明} |
| {页面2} | `{module_abbr}-{page2}.html` | {说明} |

---

## 边界与注意

- {本模块与其他模块的边界说明}
- {需要注意的业务约束}
- {已知的技术限制或依赖}

---

## 关联模块

- [[{关联模块1}]]
- [[{关联模块2}]]

---

## 待办

- [ ] Step 1 · 资料摘要
- [ ] Step 1.5 · 启动问卷
- [ ] Step 2 · 设计方案
- [ ] Step 3 · 方案确认
- [ ] Step 3.5 · 逻辑确认稿
- [ ] Step 4 · 原型产出
- [ ] Step 5 · PRD 产出

---

*使用 Templater 模板生成 · frontmatter 供 Dataview 自动聚合*
*maturity 五档枚举：骨架占位 / 业务理解已完成 / 业务理解+流程已完成 / 已设计方案 / 已发布 PRD*
