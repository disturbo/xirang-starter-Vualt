---
title: "息壤V9-多Agent对抗性审查-M0试点协议"
version: 0.1
status: 已完成试点
maturity: 已验证
type: 方法论试点协议
created: 2026-08-08
updated: 2026-08-10
author: Codex
tags: [方法论, 息壤, 多Agent, 对抗性审查, M0]
superseded_by: "[[息壤V9-多Agent对抗性审查协议]]"
---

# 息壤 V9 多 Agent 对抗性审查 M0 试点协议

> M0 已完成回放验证并于 2026-08-10 晋级为正式可选模式。当前运行规则以《[[息壤V9-多Agent对抗性审查协议]]》为准；本文保留为试点基线。

## 1. 定位与边界

多 Agent 对抗性审查是息壤评审闭环的一种风险触发型审查模式，不是新的生命周期阶段。M0 只验证一个假设：独立 Challenger 盲审是否能相对普通评审产生稳定的增量有效 finding。

M0 不引入 Defender、机器主审、交叉质询、finding 状态机、自动 Gate 或自动验收。任务级 `reviewer`、`review_status` 与 `v9_accept` 语义保持不变。

## 2. 角色

| 角色 | 职责 | 禁止事项 |
|---|---|---|
| 产出者 | 提交候选产物；审查后逐条回应 | 删除、合并或改写 Challenger 原始 finding |
| Challenger | 在独立上下文中证伪；提交结构化 finding | 写入项目产物；横向 spawn；代替人类验收 |
| 验收方 | 逐条裁定 finding；执行任务级验收 | 以 Agent 数量或重复证据提高置信度 |

## 3. 启动条件

1. 父 Agent 已确认当前平台能创建独立上下文；无法确认则不宣称盲审，可降级为普通评审。
2. Challenger 只接收审查包，不继承作者聊天、作者自评、既有 Reviewer finding 或未确认的倾向性意见。
3. 首轮试验优先让 Challenger 与产出者使用不同模型；这是试验控制条件，不是永久铁律。
4. Challenger 只读且不得继续 spawn。

## 4. 盲审包

盲审包必须包含：候选产物全文、正式引用资料、适用规范、验收标准、已知约束与不做范围、攻击面清单。

正式产物中的设计理由、方案对比和取舍说明属于审查对象，不得剔除。只排除生成过程中的非正式引导信息：作者聊天、作者自评、其他 Reviewer 意见和未确认的倾向性意见。

## 5. 执行流程

```text
候选产物
  → 组装盲审包
  → 独立 Challenger 一次审查
  → 固化原始 findings
  → 产出者逐条回应一次
  → 具名人类逐条裁决
  → 现有 v9_accept 决定任务是否 accepted
```

产出者回应只能选择 `agree`、`disagree` 或 `needs_evidence`，不得开启第二轮 Agent 质询。Challenger 的 `blocking_recommendation` 只是建议，不改变任务状态，也不阻断 `v9_accept`。

## 6. 报告格式

报告使用 JSON，遵循 `.standards/schemas/adversarial-review-m0.schema.json`。每条 finding 必须包含：ID、严重度、主张、证据、影响、复现或反例、建议动作、是否建议阻断。

作者回应和人类裁决可在 Challenger 输出固化后追加，但原始字段不得修改。任务级 `acceptance_note` 只记录汇总和报告引用，不承载逐条裁决。

校验命令：

```bash
python3 .standards/adversarial-review-check.py path/to/report.json --json
```

校验器只读，不写 `review_status`，不调用 Gate。重复证据按来源、定位与观察内容的规范化指纹识别；重复引用不增加置信度。

## 7. 回放试验

选择 5 至 10 个具有既知验收结果的历史交付物，同批执行：

- A 组：现行普通评审；
- B 组：独立 Challenger 盲审；
- Gold：人类确认的真实缺陷集。

记录增量有效 finding、误报率、P0/P1 缺陷召回、审查耗时、token、有效修订数和与普通评审的重复率。试验前固定晋级阈值；未达阈值则保持 M0 或停止，不扩建基建。

## 8. 演进闸口

只有 M0 证明存在稳定增量收益后，才依次考虑：结构化作者回应工具、Defender 或有限质询、finding 治理状态机、Gate 联动。不得跳级建设。

M0 报告通过机械校验，只表示结构合规，不表示 finding 成立，更不表示任务验收通过。
