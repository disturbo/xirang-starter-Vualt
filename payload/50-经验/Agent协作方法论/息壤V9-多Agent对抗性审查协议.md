---
title: "息壤V9-多Agent对抗性审查协议"
version: 1.1.0
status: current
maturity: 正式
type: 方法论运行协议
created: 2026-08-10
updated: 2026-08-26
author: Codex
tags: [方法论, 息壤, 多Agent, 对抗性审查, 正式协议]
supersedes: "[[息壤V9-多Agent对抗性审查-M0试点协议]]"
runtime_alignment: V9.7
---

# 息壤 V9 多 Agent 对抗性审查协议

## 1. 定位

多 Agent 对抗性审查是息壤评审闭环中的**风险触发型正式可选模式**，不是新的生命周期阶段，也不是多数投票。它通过独立 Challenger 对候选产物进行证伪，补充普通审查不容易发现的动态缺陷。

普通审查仍负责字段、枚举、角色、格式、规范和静态完整性；对抗性审查重点检查并发、时序、重复回调、越权、跨系统一致性、版本漂移和职责分离。两者互补，不以 Agent 数量代表可信度。

当前正式版本只支持 `single_challenger + blind_package`。不引入 Defender、机器主审、自由辩论、独立 finding 状态机或自动 Gate；对抗报告不能自行改变 StateStore、任务阶段或验收状态。

## 2. 触发规则

默认执行普通审查。候选产物命中下列任一风险面时，可启用正式对抗性审查：

- 支付、结算、退款、余额、权益核销；
- 权限、身份、跨组织数据或敏感数据边界；
- 状态机、跨系统同步、异步回调或最终一致性；
- 并发、幂等、重试、补偿、重复提交或重复消费；
- 高风险告警、自动化决策或不可逆动作；
- 息壤自身规范、状态机、Gate、Hook、验收或分发基建变更。

触发判断由主任务 owner 记录在 `review_package.risk_triggers`，并在当前 `review_round` 或最终交付的 `adversarial_review_summary` 中留下摘要。没有命中项时仍可由具名验收方显式启用，报告使用 `explicit_human_request`；执行包络已经包含 `adversarial_review` 意图时，产出者不得自行取消或降级。

## 3. 角色与隔离契约

| 角色 | 职责 | 禁止事项 |
|------|------|----------|
| 产出者 / 主任务 owner | 提交候选产物；管理当前阶段；固化后逐条回应一次 | 删除、合并、改写原始 finding；自验收 |
| Challenger / 只读 worker | 在同一执行包络和只读 lease 内独立证伪并提交结构化 finding | 写项目产物；创建替代主任务；继续横向 spawn；读取被排除上下文；代替验收 |
| reviewer / 编排者 | 组装盲审包、记录触发原因、执行机械校验并把结论带回当前 review round | 用重复证据或 Agent 数量提高置信度；用报告直接改状态 |
| 具名人类验收方 | 裁定 finding 与剩余风险；执行现有验收 | 把“结构校验通过”等同于“finding 成立” |

Challenger 必须使用独立上下文，首轮输出前不得看到作者聊天、作者自评、既有 Reviewer finding 或未确认的倾向性意见。它继承的权限只能是执行包络与只读 worker lease 的交集，handoff 只传上下文，不授予权限。不同模型或平台是高风险任务的推荐增强项，不是隔离成立的替代条件；平台无法确认独立上下文时，必须降级为普通审查，且不得宣称盲审。

## 4. 正式运行流程

```text
候选产物进入 validating / discovery_review
  → 普通审查与风险触发判断
  → 在同一任务下发只读 worker lease，组装独立盲审包
  → Challenger 一次性证伪
  → 冻结原始 JSON，机械校验结构与计数
  → 主任务 owner 逐条回应，并记录当前 review_round 摘要
  → 存在成立或待补证的阻断项时进入 repairing → revalidating
  → confirmation_review 确认无阻断项
  → 精确交付并记录 adversarial_review_summary
  → submitted → present-review → 用户决定
```

Challenger 不得发起第二轮或继续 spawn。需要补证的 P0/P1 finding 直接升级人类，不开启自由辩论。`blocking_recommendation` 仅为布尔建议，解释写入 `blocking_reason`；它不改变任务状态。

## 5. 报告、固化与纠错

正式报告遵循 `.standards/schemas/adversarial-review.schema.json`。每条 finding 必须包含主张、可定位证据、影响、复现或反例、建议动作和是否建议阻断。

结构化 JSON 是任务证据产物，不是状态真源；StateStore 只管理权威任务、阶段、review round、交付摘要和验收关联。Challenger 原始 JSON 一经接收即冻结。机械校验失败时：

1. 保留原始文件，不静默改写；
2. 输出校验 finding；
3. 需要继续时另建修正版或规范化派生文件，并保留来源引用；
4. 只有结构合规的报告才进入作者回应和人工裁决。

报告只能写入当前任务明确授权的路径；没有固定的全局报告目录。需要作为交付证据保留时，将其纳入精确 manifest 并绑定最终 Hash；只用于本轮审查时，在摘要和必要证据完成绑定后按任务产物生命周期清理实时副本。

校验命令：

```bash
python3 .standards/adversarial-review-check.py path/to/report.json --json
```

报告中的汇总计数必须与 `findings` 实际数量一致。重复证据按来源、定位和观察内容生成规范化指纹；重复不增加置信度。

## 6. 与既有验收状态机的关系

- finding 的 `human_decision` 是逐条裁决，不是任务状态；
- `accepted_risk` 必须记录具名风险接受者；
- P0/P1 阻断 finding 未裁决、待补证或裁定成立时，主任务不得进入 `committing`；
- 所有 finding 已处理只表示具备验收条件，不自动产生 `accepted`；
- 任务提交后由 `present-review` 绑定具体 delivery；`accepted` 只能由具名用户事件经现行验收链产生，`.standards/v9-accept.py` 是内部后端，不是要求用户执行的命令；
- 结构化报告当前不由 Gate/Hook 自动消费，也不能直接写项目产物或 StateStore；主任务 owner 负责把有效结论写入当前阶段详情与交付摘要。

## 7. 运行上限与效果复核

单次正式审查默认上限：1 个 Challenger、1 轮输出、只读、不横向 spawn。Challenger 是当前主任务的 worker，不创建第二个业务任务。父任务按宿主可用能力限制 token、时间和修复轮次；超限停止对抗审查并升级人类或降级普通审查。

按月抽样比较普通审查与对抗审查的增量有效 finding、误报、P0/P1 召回、耗时和 token。若连续样本没有增量收益，收缩触发范围；若要引入 Defender、交叉质询、自动 Gate 或 finding 状态机，必须另行立项和验收，不得由本协议自动扩张。

## 8. 演进边界

引入 Defender、交叉质询、自动 Gate、独立 finding 状态机或 StateStore 结构化 finding 表，均属于新的控制面能力，必须同步修改机器契约、实现、正反测试和恢复方案，并重新验收。本协议的试点依据和具体案例见 [[../教训库#E-20260810-01 · 对抗审查回放同时暴露动态风险与报告结构缺陷|教训库 E-20260810-01]]，不在当前宪法中保留项目过程叙述。
