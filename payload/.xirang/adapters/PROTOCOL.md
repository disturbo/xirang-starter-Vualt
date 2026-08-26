# 息壤 V3 Agent 写入协议

本文件是所有 Agent 使用当前 Vault 的共享协议。平台专属入口只能引用或忠实投影本协议，不能降低约束，也不能因文件已生成就声称协议已实际应用。

## 用户授权

1. 用户在任意 Agent 对话中的自然语言决定，是息壤范围内的最高授权来源。
2. 授权绑定已展示的目标、写入范围、排除项和影响边界形成的执行包络，不绑定 Agent、模型或平台实例。
3. Agent 只解释意图并提出受约束动作，不能填写用户身份、验证标志、内部状态或授权结果。
4. 用户不需要固定短语、任务号、提案号、租约号或底层脚本。
5. 用户对一次完整范围展示表达执行意图后，同一包络不得再次询问确认、授权、同意或继续。

## 写入边界

- 写入前一次性用人话展示目标、写入范围、排除项、不可逆和外部影响、验收方。
- 进入新的实质阶段时通知目标、范围和授权沿用情况，但既有包络内不阻塞。
- 只有范围扩大、不可逆操作、外部发送或发布、必须由用户选择的重大分歧、缺少恢复路径时暂停。
- 只读任务直接执行。文件写入必须存在当前会话主任务或有效 worker 租约。
- 有效 worker 写入范围是执行包络与租约范围的交集。
- 有效权限同时取决于路径和操作类型：`权限 = 路径范围 ∩ 操作类型`。每个路径必须有逐项 `grants`，目录可写不代表允许移动、删除、发布或控制面修复。
- `--scope` 等入口始终以数组传递；含分号、换行或未经解析逗号串的单个范围直接拒绝，禁止先拼接后在下游猜测拆分。

## 一包络一主任务

- 一个执行包络只有一个主任务。
- 接手 Agent 和子 Agent 是同一任务的 worker，不创建替代业务任务。
- worker 租约包含来源会话、目标会话、角色、只读标志、范围、有效期和状态。
- `trusted` 模式可由宿主强制租约；`manual_guard` 下租约只提供可追踪分权记录，`enforcement_verified=false`。
- 换 Agent、压缩上下文或恢复执行不触发原范围重新授权。

## 跨会话交接

- 实质阶段变化、上下文压缩、会话结束和提交时，把任务摘要、已完成项、当前状态、下一步和踩坑记录写入 SQLite handoff 检查点。
- 接手者只读取用户明确指向任务的最新有效检查点，并登记消费；禁止用 Workspace 唯一候选猜测任务。
- 检查点绑定 `task_id`、`envelope_id`、权威状态版本和交付版本。任务或交付推进后旧检查点自动失效。
- handoff 只传上下文，不授予权限；写入仍要求同一执行包络下的有效 worker 租约。

## 三种平台模式

### Trusted

宿主必须证明用户事件、展示顺序、单次消费、worker 身份和不可绕过写入门禁。缺少任何当前证据时不得显示 `trusted` 或 `connected`。

### Manual Guard

平台可以提供自然授权、连续执行、显式路径检查、外置证据和过程追踪，但不提供不可抵赖性。每个相关记录必须保持：

```yaml
actor_verified: false
disclosure_verified: false
sequence_verified: false
enforcement_verified: false
```

内部 handle、环境变量、HMAC、本地密钥、SQLite、日志、Tag 和租约都不是宿主身份凭证。当前用户原文最多转交一次，不得改写、重放、刷新原事件时间或扩大范围。

### Contract Only

平台只加载共享协议并提供只读诊断，不允许控制面授权或写入。固定短语和模型动作不能绕过该限制。

## 连续执行

主流程固定为：

```text
authorized
→ preparing
→ implementing
→ validating
→ discovery_review
→ repairing
→ revalidating
→ confirmation_review
→ committing
→ submitted
```

- `discovery_review` 发现问题，`confirmation_review` 确认修复，每轮记录 `review_round`。
- 修复后必须重新验证并重新审核；仍有阻断项时不得提交。
- 预算、Agent 数、嵌套深度、运行时间和修复轮次必须受限。
- 连续两轮没有新证据或重复同一阻断时进入 `blocked_nonconvergent`。
- `blocked_budget`、`blocked_external_dependency` 和 `suspended_lease_expired` 在系统条件恢复后续接，不重新索要原范围授权。
- 只有 `awaiting_material_user_choice` 需要用户新决定。

## 写入证据

- `pre-write` 校验任务或租约、阶段、规范化路径和包络范围。
- `post-write` 重新校验实际落盘路径、任务或租约并记录最终哈希。
- `submit` 校验全部变更都有有效收据且属于同一包络。
- `present-review` 绑定具体交付版本。
- `accept` 校验用户事件、当前会话焦点或明确跨会话指向、交付版本和最终证据。
- 任何后一层都不能把前一层遗漏的越权路径合法化。
- 证据写入失败时保留文件，但交付不得提交或验收。

## 基建 Tag 发布血缘

- 息壤基建发布只认当前真源仓库中的 annotated Tag、完整 Commit、提交内 artifact spec 和受保护运行根的 `production-baseline.json`；版本号或工作树声明不能替代这些事实。
- 首次启用只允许对当前用户拥有的空 `0700` 运行根执行 bootstrap。输入包取得目录锁后以 `O_NOFOLLOW` 冻结一次，后续验包、解压与安装只读冻结副本；持久 journal 以 `current` 为最终提交点，崩溃恢复后只能是完整闭包或空根。成功同时固化可信 annotated Tag object、包 SHA、生产树 SHA、基线 revision 和待验收 receipt。
- 后续发布必须经过任务启动锁、干净构建、annotated Tag object/Commit/祖先校验、清单闭包、Zip Slip 防护、包 SHA、基线 CAS、纯静态且不执行候选代码的健康检查和失败零变化恢复；运行根独立校验，不信任本地构建参数。
- `status` 必须在共享发布锁内复核当前指针、未完成 journal、Tag object、receipt、包 manifest/spec/SHA、包解压闭包与生产树等价关系；任一残留或混配都不得报告 active。
- “源码已实现”“Tag 存在”或“wrapper 已安装”均不等于能力已启用。只有 `~/.xirang/bin/xirang-release-lineage status` 当前验证通过，才能报告本机 `manual_guard` 已激活。
- 同用户权限无法形成强隔离或不可抵赖边界；`status` 必须返回 `strongIsolation=false`，不得把本地权限、Git Tag 或 SQLite 冒充独立安全域。

## 验收关联

- 当前会话有有效展示焦点时，决定只绑定该交付版本。
- 没有焦点时，只能自动关联当前会话唯一待验收交付。
- 当前会话存在多个候选时不改变状态，并用任务名称澄清。
- 跨会话验收必须由用户明确指向任务名或交付内容，并绑定具体交付版本。
- 禁止 Workspace 唯一候选 fallback，禁止把全局状态页或“最新提交”当验收上下文。
- 执行者不能验收自己的交付，`submitted` 不等于 `accepted`。
- 控制面故障不取消已存在的用户授权：有授权且逐路径外置 pre-image 快照、带外审计均成功时，可保留本地工作树改动；没有授权或恢复证据时仍阻断编辑。
- 写后证据、交付或验收链故障分别只阻断提交或验收。人工降级只能形成精确路径的 `committed_pending_reconciliation`，不得冒充 `submitted` 或 `accepted`。
- 原子接管必须在同一 SQLite 事务内锁定 blocked 前任、撤销其租约、创建唯一后继；任一步失败全部回滚。所有终态转换必须同事务撤销活动租约。
- 用户明确要求不再催验收时，保持 `submitted` 并报告一次。

## 用户交互偏好

- “同一包络不重复确认”是系统规则。
- “提交后不催验收”是用户交互偏好，不是治理变更，不扩大文件权限。
- 设置或撤销偏好必须绑定用户事件；Agent 不能自行修改。
- 新任务快照当前偏好，跨 Agent 接手后继续生效。

## 状态真源

- V3 要求 SQLite 是唯一权威运行状态。
- 任务卡、状态页、`events.jsonl`、receipt 文件和提案文件只能是单向投影、导出或历史归档。
- 所有生产读写者必须先经过统一 StateStore，完成 shadow 导入、全量对账、排他锁和最终增量导入后才能切换。
- 禁止长期双写；投影漂移时失败关闭，禁止从 Markdown 或 JSONL 反向猜测状态。
- V3 runtime 未切换前必须标记 `unverified` 或 `migration_legacy`，不能把本协议存在当作 SQLite、租约或协调器已经接通。

## 维护与恢复

- 控制面维护不能由 Agent、环境变量或 `--maintenance` 自授。
- HMAC 只校验内容完整性，不证明用户身份。
- 授权、任务创建、用户事件消费和 outbox 必须可幂等恢复；内部失败不得再次要求用户授权原范围。
- SQLite 使用 WAL、在线一致性快照、schema 版本和恢复后投影重建。Manual Guard 下这些能力不提供同权限进程级防篡改保证。
- 救援入口独立于普通任务门禁，只允许检查 SQLite、创建快照、撤销租约、修复机器可证明的错误包络和重建投影；禁止修改业务笔记、验收、删除用户内容、推送或发布。
- 错误包络修复必须在一个事务内验证原授权链和范围不扩大、标记旧包络 `invalid_envelope`、撤销旧租约、创建逐路径披露/提案/任务/租约、建立审计映射；带外审计写失败则数据库事务回滚。
- 恢复位置只从 `.xirang/contract/recovery-roots.yaml` 解析。未登记目录即使可写也拒绝；快照缺 manifest、Hash 不符或恢复目标已存在时拒绝。
- 用户明确排除 Git commit 时不得擅自提交，也不得声称已获得 Git 可恢复版本。

## 真实性与退役能力

- 能力只有具备真实调度、输入、输出、消费者、行为效果和当前证据时才算接通。
- 外部入口、人格镜像、Skill 或配置未实际应用并完成当前 canary 时必须标记 `unverified`。
- 旧 V8、M3/M4/M5、二元触发器、pre-flight、亮灯、旧心跳、A2A Mock 和退役成本熔断只能作为历史或迁移输入，不得作为现行能力或验收证据。
- `not_enabled` 表示可选能力未启用；`unverified` 表示缺少当前应用或行为证据；两者都不能显示绿色。
