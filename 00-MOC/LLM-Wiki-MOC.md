---
title: "LLM Wiki MOC"
version: "1.4"
status: active
maturity: 正式
type: MOC
tags: [MOC, LLM-Wiki, 知识管理, 多智能体]
created: 2026-05-11
updated: 2026-07-21
owner: 红霉素
---

# LLM Wiki MOC

> 目标：把 vault 从“资料堆 + 协作记录”收束成 agent 可执行的工作知识库。

## 当前原则

| 原则 | 落地方式 |
|---|---|
| 单一可信源 | vault 是协作黑板；沙箱只放原型、业务文件和运行产物 |
| 源/摘要/发布三层 | 原始资料不直接变 PRD，先沉淀资料摘要，再发布 PRD/设计方案 |
| 任务可交接 | 任务必须带 owner、next action、blocked by、产物路径 |
| 冲突显式化 | 矛盾、疑点、口径冲突先记录，不在 PRD 里悄悄抹平 |
| skill 化 | 常见工作流拆成 `.skills/`，agent 按任务调用 |

## 入口

| 场景 | 先读 |
|---|---|
| 开始任务 | [[多智能体协作看板]] + `.skills/start-task/SKILL.md` |
| 写 PRD / 设计方案 | 目标模块 README/模块笔记 + `.skills/publish-wiki/SKILL.md` |
| 处理新资料 | `.skills/ingest-source/SKILL.md` |
| 交接任务 | `.skills/handoff-task/SKILL.md` |
| 发现冲突 | `.skills/review-contradictions/SKILL.md` |

## 三层资料结构

| 层级 | 路径 | 作用 | 可直接引用到 PRD |
|---|---|---|:---:|
| Source 原始源 | `20-资料/业务文件/`、沙箱业务文件 | docx/xlsx/飞书截图/会议纪要原文 | 否 |
| Summary 摘要层 | `10-项目/基线/{模块}/资料摘要.md` 或当前迭代工作稿 | 结构化提炼、去重、标注来源 | 是 |
| Published 发布层 | `10-项目/基线/{模块}/PRD.md`、`设计方案.md`、规范 | 已评审或当前稳定交付口径 | 是 |

> 当前迭代写入先进入 `10-项目/迭代/260725迭代/`，封版后再按归集清单吸收到基线。

## 首批 Skills

| Skill                                    | 触发条件               | 状态  |
| ---------------------------------------- | ------------------ | --- |
| `.skills/RESOLVER.md`                    | 不知道该读哪个 skill      | 已建立 |
| `.skills/start-task/SKILL.md`            | 任一 agent 开始任务      | 已建立 |
| `.skills/ingest-source/SKILL.md`         | 新增业务文件、飞书文档、调研纪要   | 已建立 |
| `.skills/publish-wiki/SKILL.md`          | 资料摘要进入 PRD/设计方案/规范 | 已建立 |
| `.skills/handoff-task/SKILL.md`          | 任务完成、暂停、转交         | 已建立 |
| `.skills/review-contradictions/SKILL.md` | 发现口径冲突、模块边界冲突、编号冲突 | 已建立 |

## 近期落地检查

- [x] 主 vault 已 git 初始化。
- [x] 首批 `.skills/` 建立。
- [x] 协作看板改为控制台结构。
- [x] 协作机制补入源/摘要/发布规则。
- [x] 纳入 LLM Wiki 管控的编号模块补 README：29 个编号模块（01-15、18-19、23-26、28-35）。
- [x] 每个模块 README 补”原型映射”。
- [x] 纳入 LLM Wiki 管控的编号模块补 `资料摘要.md`：29 个编号模块已补齐。
- [x] 活跃模块入口精修：01-PDI、03-保险、04-旧件、07-索赔、30-延保、31-服务工单。
- [x] 建立本地 README 检查脚本：`.standards/scripts/llm_wiki_check.py`。
- [x] 29 个模块 README 补 `maturity` 五档枚举字段。
- [x] 29 个模块 `module_id` 引号格式统一。
- [x] 检查脚本加 `--non-placeholder` 模式（maturity vs 实际内容校验）。
- [x] 项目首页加成熟度分布 Dataview 维度。
- [x] GBrain 已部署运行：v0.33.0 + PGLite + Ollama bge-m3（873 页、10,382 chunks，embedding 100%）。
- [x] SessionStart 与 M4/M5 任务握手已接自动语义召回；消费结果写入 `semantic_recall` 事件，失败 fail-open 但运行健康转红。
- [x] GBrain 遗留 frontmatter 清理（synced_at/keywords/type:md 共 7 个文件）。
- [x] `module-registry.json` 改为脚本派生：`.standards/scripts/generate_module_registry.py`。
- [x] 剩余骨架模块拉出业务确认清单：[[骨架模块业务确认清单-2026-06-10|骨架模块业务确认清单]]。

## 本地检查

**基础模式**（结构校验：README 存在 + 4 必备章节 + wikilink + 原型路径）：

```bash
python3 $HOME/Desktop/obsidianVault/.standards/scripts/llm_wiki_check.py
```

当前结果：2026-07-21 已对准 `725` 原型根，检查 29 个编号模块；errors = 0，warnings = 0。

**深度模式**（maturity vs 实际内容一致性校验）：

```bash
python3 $HOME/Desktop/obsidianVault/.standards/scripts/llm_wiki_check.py --non-placeholder
```

校验规则：
- `骨架占位`：跳过内容深度检查
- `业务理解已完成`：资料摘要.md 必须存在
- `业务理解+流程已完成`：摘要 ≥80 行 + 流程/字段/状态至少 2 个关键词
- `已设计方案`：设计方案.md / 设计方案*.md 或 PRD.md 存在
- `已发布PRD`：PRD.md 存在

当前结果：2026-06-10 基础/深度模式均通过；骨架占位剩余 4 个（18/19/23/26），均已补暂缓原因。

**严格模式**（warning 也视为失败）：

```bash
python3 $HOME/Desktop/obsidianVault/.standards/scripts/llm_wiki_check.py --strict --non-placeholder
```

**模块注册表生成/校验**（由 README frontmatter 派生 `module-registry.json`）：

```bash
python3 $HOME/Desktop/obsidianVault/.standards/scripts/generate_module_registry.py --write
python3 $HOME/Desktop/obsidianVault/.standards/scripts/generate_module_registry.py --check
```

## 模块成熟度分布

| 成熟度 | 数量 | 模块 |
|---|---|---|
| 已发布PRD | 17 | 01-PDI、03-保险、04-旧件、05-取送车、06-商务补偿、07-索赔、08-工单、09-维修接待与预检、10-维修结算、13-道路救援、14-线索任务、15-服务预约、24-市场处置、29-技术支持与品质报告、30-延保、33-代步车、34-服务包 |
| 已设计方案 | 3 | 02-保修、28-基础配置与系统管理、31-服务工单 |
| 业务理解+流程已完成 | 2 | 11-车间管理、12-质检管理 |
| 业务理解已完成 | 3 | 25-报表管理、32-服务助手手机端、35-善意保修 |
| 骨架占位 | 4 | 18-备件目录、19-库存管理、23-目标管理、26-门店检核 |

> 数据来源：各模块 README.md frontmatter `maturity` 字段。项目首页 Dataview 自动聚合。

## 资料摘要补齐进度

| 批次 | 模块 | 状态 |
|---|---|---|
| 第一批 | 01-PDI管理、02-保修管理、03-保险管理、04-旧件管理、07-索赔管理、30-延保销售 | 已完成（深度摘要） |
| 已存在 | 31-服务工单管理 | 已完成（深度摘要） |
| 第二批 | 05、06、08-29 | 已完成（骨架级摘要） |
| 补齐批 | 32-服务助手手机端、33-代步车服务、34-服务包、35-善意保修 | 已完成 |

## 下一步

| 优先级 | 任务 | 说明 |
|---|---|---|
| ~~P0~~ | ~~活跃模块原型映射复核~~ | ~~对照沙箱真实 HTML~~ [done] 已完成 2026-05-16（8模块交叉校验，修复06补偿+01 H5两处缺口） |
| P1 | 骨架模块业务确认 | 18/19/23/26 已拉清单，见 [[10-项目/基线/骨架模块业务确认清单-2026-06-10|骨架模块业务确认清单]] |
| P1 | 延保原型排期 | 30-延保销售已有 PRD，但暂无 v3.2 原型 |
| ~~P1~~ | ~~06-商务补偿资料摘要深化~~ | [done] 已完成 2026-05-16（52→117 行） |
| ~~P2~~ | ~~32-服务助手资料摘要新建~~ | [done] 已完成 2026-05-16（新建 99 行） |
| ~~P2~~ | ~~Mermaid→drawio P1 转换~~ | ~~PDI+保险 3 张~~ [done] 已完成 2026-05-16 |

## 已固化边界

| 边界 | 口径 |
|---|---|
| 保修/索赔 | 02-保修管理管”保修申请、保修鉴定、保修预结算”；07-索赔管理管”月度索赔结算、开票、SAP 索引单、挂账付款、索赔报表”。`warranty/` 六页当前全部归 02，07 未来新建 `claim/`。 |
| GBrain | **已部署运行**（2026-07-21 实测）：v0.33.0 + PGLite + Ollama bge-m3；873 页、10,382 chunks、10,382 embedded。cron 单次 sync/dream 保持新鲜，当前运行时契约可直接读取并被语义查询命中；SessionStart/任务握手已自动消费。详见 [[GBrain+LLM-Wiki-范式借鉴]]。 |

## 关联

- [[GBrain+LLM-Wiki-范式借鉴]]
- [[50-经验/Agent协作方法论/息壤V9-运行时契约卡|息壤V9-运行时契约卡]]
- [[多智能体协作看板]]
- [[示例项目EXAMPLE-MOC]]
- [[教训库]]
- [[LLM-Wiki-Karpathy原文与翻译]]
- [[骨架模块业务确认清单-2026-06-10|骨架模块业务确认清单]]
