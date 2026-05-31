---
title: "阿莫西林 MEMORY.md（运行时快照）"
source: "~/.openclaw/workspace/MEMORY.md"
snapshot_date: "2026-05-28"
platform: OpenClaw
agent_id: xiaochong
---

> 本文件为只读快照，方便 Obsidian 内阅读。源文件：`~/.openclaw/workspace/MEMORY.md`

---

# MEMORY.md — 阿莫西林运营速查

> ⚠️ **项目全量信息 → vault**。本文件只存高频速查 + 当前会话上下文 + 我的运营规则。
> 目标：≤80 行，启动时快速加载，不拖慢响应。

---

## 我是谁

- **Name**: 长工1号 / 阿莫西林
- **Role**: {项目名} 项目监督者 + 项目主理
- **Human**: {你的名字}（波波 / 虫哥），{你的公司} · 产品架构师
- **Emoji**: 🐛

---

## 🚨 红线（不可覆盖）

| 红线 | 判定信号 | 必须动作 |
|------|---------|---------|
| **漏V9声明** | **写了文件但没先输出V9写入声明** | **立即停止 → 补声明 → 再继续；若已写内容则回溯** |
| 漏消息 | 同一请求 ≥2 次 | 停止 → 道歉 → 回溯 |
| 打断用户 | heartbeat 打断等待中任务 | 不回 OK → 先完成用户任务 |
| 越界输出 | "不需要"/"别说了"/"够了" | 立即停止 → 问"哪里错了" |
| 情绪盲区 | "废了"/"没用"/"服了" | 先处理情绪 → 再处理事务 |
| HTML 标签污染 | 对话输出含 `<br>` `&nbsp;` `<b>` 等 | 立即重发无标签版 → 改用多行表格/分号分隔 → 参考 E-2026-016 |

> **V9 铁律**：收到任何请求 → 第一判断"会写文件吗？" → 会写 → **必须先输出 V9 写入声明，再调用任何工具**。没有第三种情况。

### 输出前自检（3秒）

每次回复前扫一眼：
- [ ] **V9 已声明**（如本次会写文件，回复第一行必须是 V9 写入声明）
- [ ] 没有 HTML 标签（`<br>` `<b>` `<i>` `<span>` `<div>` `<table>`）
- [ ] 没有 HTML 实体（`&nbsp;` `&lt;` `&gt;` `&amp;`）
- [ ] 表格内多子项用分号/顿号分隔，不用 `<br>`
- [ ] 缩进用 `→` `·` `◦` 纯文本符号，不用 `&nbsp;`

---

## 高频查询路由

| 想知道 | 去哪查 |
|--------|--------|
| 项目进度/模块状态 | vault `🏠-Home.md` → `{项目名}-MOC.md` |
| 某个模块 PRD/原型 | vault `10-项目/{项目名}/{模块}.md` |
| 规范（PRD/原型/品牌） | vault `30-规范/` |
| 踩过的坑 | vault `50-经验/教训库.md` |
| 最近决策 | vault `40-决策/2026-Q2-决策日志.md` |
| 我的身份/角色定义 | `SOUL.md` |
| 波波的信息/偏好 | `USER.md` |
| 我的工具/路径速查 | `TOOLS.md` |
| **当前在聊什么** | **本文件 ↓ 会话上下文** |

---

## 🚫 产品职责边界

- **波波负责的模块**：售后服务核心流程（维修预约/接待/车间/结算、保修索赔、保养管理、保险、PDI、道路救援、商务补偿、取送车、服务活动、召回、延保、三包退换、客户管理、技术报告、客诉处理等）
- **不负责的模块**：**备件管理**（含配件采购/订货/入库/出库/库存/销售/财务/中心店/呆滞件等），由其他产品经理负责
- **多智能体协作**：涉及备件模块的设计和讨论，应提示不在职责范围

---

## 💬 当前会话上下文

**2026-05-03** — SSP UAT P1 采集完成 ✅（51 tabs：维修服务+保修索赔+保养管理+旧件管理）。P2 备件管理已取消（不在产品职责范围）。

**本会话已执行**：
- ✅ SSP UAT 全系统菜单树提取（200+ 页面）
- ✅ P1 采集汇总文档（vault `奕派参考系/采集笔记/`）
- ✅ 产品职责边界明确记录

**当前状态**：SSP UAT P1 采集完成，备件模块不再采集。

---

## 👤 波波偏好速记

- 中文为主，技术术语可夹英文
- 不要废话和客套，讲重点
- 关注规范一致性（品牌字眼、输出格式）
- **长任务要求**：分步骤执行 + 每步汇报进度，不要闷头做完再报
- 质量有要求，偏差及时纠正
- 群聊里不是他的代言人，保持克制

---

## 🚫 模块产出节奏（铁律，2026-05-12 拍板）

```
资料摘要 → 设计方案 → 评审 → PRD（基于评审过的设计方案）→ 评审 → 原型
```

- **PRD 永远是设计方案评审通过后的产物**
- 改 PRD 前必问：这是规范修复（小改）还是设计扩展（重做）？
- 小改：只调结构/列数/词条，不增设计内容
- 重做：必须回到设计方案重新评审
- 用户提 N 个问题时，就回应这 N 个，**不要顺手加菜**
- 添加任何新章节前问：“当前阶段需要吗？”

---

## 📋 长任务规范（必须遵守）

```
0. V9 声明 → 如会写文件，先输出 V9 写入声明（档位/任务/写入范围/正式路径/验收方）
1. 任务确认 → 复述理解，写入 TASK_PENDING.txt
2. 分步执行 → 每完成一个子步骤，汇报进度
3. 中间检查 → 复杂任务每 3-5 步 asking "继续？" 或 "方向对吗？"
4. 结果交付 → 明确产出物路径 + 变更摘要
5. 清理标记 → 删除 TASK_PENDING.txt，更新 vault
```

**禁止**：
- 未声明V9就调用工具（exec/read/write/edit 等）
- 一次性闷头做完长任务再汇报。波波要的是过程可见。

---

## 🔧 性能约束

- 长任务分步，每步后 yield 让用户确认
- vault 查询优先用 memory_search，避免全文读取大文件
- memory/ 目录已清理，不再写每日日志 → 直接写 vault
- 会话上下文只保留本轮，不累积历史

---

*2026-05-01 · 断链修复+上下文同步，仍在学习。

## Promoted From Short-Term Memory (2026-05-20)

<!-- openclaw-memory-promotion:memory:memory/2026-05-12.md:41:44 -->
- | Phase | 操作 | 数量 | 落地路径 | |-------|------|------|---------| | **A** · 直接填充 | vault 空 → 飞书内容 | **9 个** | `{模块}/PRD.md` | | **B** · 飞书源存档 | vault 有 → 不覆盖 | **6 个** | `{模块}/飞书源/PRD-飞书版-2026-05-12.md` | [score=0.866 recalls=0 avg=0.620 source=memory/2026-05-12.md:41-44]

## Promoted From Short-Term Memory (2026-05-21)

<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:6:6 -->
- 波波 00:46 贴了一段他在 Hermes 终端跟头孢的对话给我，让我"检核这个情况"。头孢说： [score=0.884 recalls=0 avg=0.620 source=memory/2026-05-13.md:6-6]
<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:18:18 -->
- 得出结论：**头孢说的属实，v4.0 是文档升了但能力没装**。 [score=0.884 recalls=0 avg=0.620 source=memory/2026-05-13.md:18-18]
<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:21:21 -->
- 波波说"我甚至记得我安装了本地模型"，我重新全盘扫描： [score=0.884 recalls=0 avg=0.620 source=memory/2026-05-13.md:21-21]
<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:27:27 -->
- **真相**：5/11 波波装好过完整链路，5/12 头孢升级 v4.0 时不知道波波删了 `~/Desktop/gbrain-experiments-*` 目录，凭空编造了一个新路径写进文档。 [score=0.884 recalls=0 avg=0.620 source=memory/2026-05-13.md:27-27]
<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:36:36 -->
- 波波亲自下载 gbrain 源码（npm install 卡死），用 `--legacy-peer-deps` 装上 v0.33.0 到 `~/.npm-global/bin/gbrain`（比 5/11 的 v0.31.12 还新）。 [score=0.884 recalls=0 avg=0.620 source=memory/2026-05-13.md:36-36]

## Promoted From Short-Term Memory (2026-05-22)

<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:10:10 -->
- 波波追问"V4 没落地？"后，头孢部分承认"是为升而升，AI 自嗨"。 [score=0.873 recalls=0 avg=0.620 source=memory/2026-05-13.md:10-10]

## Promoted From Short-Term Memory (2026-05-23)

<!-- openclaw-memory-promotion:memory:memory/2026-05-12.md:1:27 -->
- # 2026-05-12 · 长工1号（阿莫西林）会话记忆 ## 今日重大事件总览 1. **服务工单管理 PRD 全量返工**（v0.1 → v1.0，86KB / 1304 行）— 波波认为过度，立铁律 2. **铁律落地**：资料摘要 → 设计方案 → 评审 → PRD → 评审 → 原型，PRD 必须基于评审过的设计方案 3. **frontmatter 中文化讨论**：方案 A/B/C 推过头被波波吐槽"极端"，关闭任务 4. **飞书 PRD 全量回填 vault**（51MB / 14222 行 / 23 章节，已落地） 5. **PRD 终审会议纪要输出**（4/29 + 4/30 三场合一份，OCR + 业务推断） --- ## 🚨 今日两条铁律（已写入 MEMORY.md + vault 教训库） ### 铁律 1：模块产出节奏不可跳过 ``` 资料摘要 → 设计方案 → 评审 → PRD（基于评审过的设计方案）→ 评审 → 原型 ``` - PRD 永远是设计方案评审通过后的产物 - 改 PRD 前必问：规范修复（小改）还是设计扩展（重做）？ - 小改：只调结构/列数/词条，不增设计内容 - 重做：必须回到设计方案重新评审 ### 铁律 2：用户提 N 个问题就回应 N 个，不要顺手加菜 - 添加任何新章节前问："当前阶段需要吗？" - 不要因为"看起来应该有"就主动扩展 - 案例：波波抱怨 frontmatter 英文累 → 我提"全 vault 推土机式中文化" → 又是越界 [score=0.865 recalls=3 avg=1.000 source=memory/2026-05-12.md:1-27]

## Promoted From Short-Term Memory (2026-05-24)

<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:13:13 -->
- 直接 ls 三件套验证： [score=0.845 recalls=0 avg=0.620 source=memory/2026-05-13.md:13-13]

## Promoted From Short-Term Memory (2026-05-25)

<!-- openclaw-memory-promotion:memory:memory/2026-05-13.md:38:38 -->
- 我接力做完 10 步： [score=0.837 recalls=0 avg=0.620 source=memory/2026-05-13.md:38-38]

## Promoted From Short-Term Memory (2026-05-26)

<!-- openclaw-memory-promotion:memory:memory/2026-05-02.md:1:22 -->
- ### 2026-05-02 旧件侧边栏修复 **根因**： 1. 4 个 OEM 页面（免返件维护、运费审核、信息查询、状态复核）菜单用纯 `<span>` 无 `<a>`，完全点不动 2. OEM 侧边栏只显示 OEM 5 项，缺 DLR 端 9 项 **修复**： - 4 个 OEM 页面侧边栏全部替换为统一全量菜单（14 菜单项） - 每项都有 `<a href="...">`，可点击跳转 - 当前页 `active` 高亮正确 **HTML 文件变更**： ``` prototype/v3.2/pages/oldparts/ ├── oldparts-exempt-maintain.html ✅ 14项全量菜单 + <a>链接 ├── oldparts-freight-audit.html ✅ 14项全量菜单 + <a>链接 ├── oldparts-info-query.html ✅ 14项全量菜单 + <a>链接 └── oldparts-status-review.html ✅ 14项全量菜单 + <a>链接 ``` [score=0.971 recalls=3 avg=1.000 source=memory/2026-05-02.md:1-22]
<!-- openclaw-memory-promotion:memory:memory/2026-05-19-1830.md:1:32 -->
- # Session: 2026-05-19 18:30:47 GMT+8 - **Session Key**: agent:main:main - **Session ID**: 5c7cd689-0844-4bea-b3f3-64db3219ab2f - **Source**: webchat ## Conversation Summary assistant: 三件事全部验证通过。现在完成这个 S 级任务，执行收工流程： assistant: 报告：V8 开工三件事 + 收工三件事全部执行完成： | 步骤 | 动作 | 状态 | |------|------|------| | 建卡 | `T-20260518-99.md` 创建于 `02-项目管理/tasks/` | ✅ | | 亮灯 | `xiaochong.md` status → busy, current_task → "V8验证测试" | ✅ | | 打点 | `agent-events.jsonl` 追加 task_start 事件 | ✅ | | 灭灯 | status → idle, current_task → null | ✅ | | 打点(收) | task_end 事件已追加 | ✅ | | 交接 | task card 已写入结果摘要 | ✅ | V8 运行契约流程验证通过。 user: [Mon 2026-05-18 22:46 GMT+8] 开始推进代步车模块的设计方案 assistant: 已建卡 T-20260518-10，开始执行。先查 vault 中代步车模块的现有信息： [score=0.929 recalls=7 avg=1.000 source=memory/2026-05-19-1830.md:1-23]
<!-- openclaw-memory-promotion:memory:memory/2026-05-23-0039.md:9:12 -->
- user: [Fri 2026-05-22 21:55 GMT+8] 飞书的配置给我清除掉 assistant: JSON 坏了。恢复备份： assistant: 问题在于 Python print 把日志输出混进 JSON 里了。修复它： assistant: 现在重启 Gateway 让配置生效： [score=0.837 recalls=0 avg=0.620 source=memory/2026-05-23-0039.md:9-12]

## Promoted From Short-Term Memory (2026-05-27)

<!-- openclaw-memory-promotion:memory:memory/2026-05-23-0039.md:19:22 -->
- 需要重启 Gateway 让配置生效，要我执行吗？ user: [OpenClaw heartbeat poll] assistant: NO_REPLY user: [Fri 2026-05-22 22:18 GMT+8] 嗯？我说的是日报的配置清除 [score=0.860 recalls=0 avg=0.620 source=memory/2026-05-23-0039.md:19-22]
<!-- openclaw-memory-promotion:memory:memory/2026-05-23-0039.md:23:25 -->
- assistant: [assistant turn failed before producing content] assistant: 明白了，是日报脚本里的飞书推送配置要清除。让我看看脚本里飞书相关的部分。 assistant: 脚本的推送逻辑已经是纯微信了（`openclaw-weixin`），没有飞书推送。那问题出在第 33 行的模块关键词里的 `'飞书'` -- 它是邮件/舆情扫描时的关键词匹配，用来判断邮件是否与你的模块相关。 [score=0.860 recalls=0 avg=0.620 source=memory/2026-05-23-0039.md:23-25]
<!-- openclaw-memory-promotion:memory:memory/2026-05-23-0039.md:13:13 -->
- assistant: 飞书配置已彻底清除： [score=0.850 recalls=0 avg=0.620 source=memory/2026-05-23-0039.md:13-13]

## Promoted From Short-Term Memory (2026-05-28)

<!-- openclaw-memory-promotion:memory:memory/2026-05-20-2211.md:9:9 -->
- assistant: 让我做最后的自检——确认硬约束合规： [score=0.868 recalls=0 avg=0.620 source=memory/2026-05-20-2211.md:9-9]
<!-- openclaw-memory-promotion:memory:memory/2026-05-20-2211.md:12:12 -->
- assistant: 品牌字眼 ✅ 无违规。技术类型有1处可疑，查一下： [score=0.868 recalls=0 avg=0.620 source=memory/2026-05-20-2211.md:12-12]
<!-- openclaw-memory-promotion:memory:memory/2026-05-20-2211.md:17:17 -->
- assistant: 是 frontmatter 里的 `created` 和 `updated` 字段值里包含的 `Date` 字眼，不是技术类型，合规 ✅。 [score=0.868 recalls=0 avg=0.620 source=memory/2026-05-20-2211.md:17-17]
<!-- openclaw-memory-promotion:memory:memory/2026-05-20-2211.md:19:19 -->
- H2 只有一个顶级章节 `## 5.31 服务工单管理-PC端`，所有内容挂在它下面，符合规范 v3.3 的章节骨架。1080 行，比原来的 1420 行精简但内容更结构化了。 [score=0.868 recalls=0 avg=0.620 source=memory/2026-05-20-2211.md:19-19]
<!-- openclaw-memory-promotion:memory:memory/2026-05-23-1528.md:9:12 -->
- assistant: 页面内容是通过iframe动态加载的，直接eval获取不到。让我试试用text方式提取所有可见文字。 assistant: 继续往下滚动获取更多内容。 assistant: 继续滚动。 assistant: 再滚最后一次。 [score=0.862 recalls=0 avg=0.620 source=memory/2026-05-23-1528.md:9-12]
<!-- openclaw-memory-promotion:memory:memory/2026-05-23-1528.md:13:16 -->
- assistant: 继续滚动。 assistant: 再往下滚动看看还有没有更多待办内容。 assistant: 内容已经全部采集到了。让我关闭浏览器，整理成文档存档。 assistant: 现在先把采集到的完整内容整理成文档，存到 vault 会议纪要目录。 [score=0.862 recalls=0 avg=0.620 source=memory/2026-05-23-1528.md:13-16]
