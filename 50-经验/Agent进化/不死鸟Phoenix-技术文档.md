---
title: "指北师Phoenix不死鸟 - 完整技术文档学习笔记"
updated: 2026-08-13
created: 2026-05-16
type: 技术参考
status: 参考资料（本地已部署有界子集）
runtime_status: active_bounded_subset
executor: v9-phoenix.py
scheduler: v9-reflex-run.sh
tags: ["经验"]
---

# 指北师Phoenix不死鸟 - 完整技术文档学习笔记

> **浴火不灭，迭代永生。**
> 本文档详细描述指北师Phoenix不死鸟的每一个板块、每一条路由逻辑、每一个执行流程。

> **V9 运行边界（2026-08-13）**：这是外部架构的学习笔记。本地只实现了有界白名单执行器、反射周期调度、运行留痕和升级候选；本文其余路由、模型切换、记忆与通用抗体流程不得解读为本地已生效能力。

---

## 目录
1. 架构总览
2. 11大板块详解
3. 核心模块详解
4. 路由系统详解
5. 信用监控+兜底模型
6. 记忆系统循环
7. 自查系统循环
8. Hermes融合机制
9. 完整执行流程
10. 配置参数速查
11. 文件结构

---

## 1. 架构总览

### 1.1 系统架构图

```
用户输入（飞书/微信/企业微信/CLI/TUI）
    ↓
① Core 配置中心
┌───────────────────────────────┐
│ PhoenixConfig │ AppState │ TaskManager │
├───────────────────────────────┤
│ 全局配置 │ 状态管理 │ 任务调度 │
└───────────────────────────────┘
    ↓
五档路由判断
┌───────────────────────────────┐
│ 日常40% │ 中等40% │ 深度15% │ 大神4% │ 真神1% │
│ 自动执行 │ 自动执行 │ 需确认 │ 必须确认 │ 必须确认 │
│ mimo-v2.5 │ mimo-pro │ sonnet-4.6 │ opus-4.7 │ opus+gpt │
└───────────────────────────────┘
    ↓
② Router 路由引擎（双维度：关键词 + LLM理解）
┌───────────────────────────────┐
│ IntentClf │ Complexity │ Gatekeeper │ LLMClf │
│ 意图分类 │ 复杂度评估 │ 准入控制 │ LLM分类 │
└───────────────────────────────┘
    ↓
chat │ code_medium │ reasoning │ vision
mimo-v2.5 │ mimo-v2.5 │ opus-4.7 │ mimo-v2-omni
    ↓
③ Executor 执行管道
┌───────────────────────────────┐
│ PreApprove │ MicroCompt │ DeepCompt │ SkillLdr │
│ 预审批 │ 微压缩 │ 深度压缩 │ 技能加载 │
├───────────────────────────────┤
│ CircuitBrk │ RespCache │ TaskDecom │ Parallel │
│ 熔断器 │ 响应缓存 │ 任务分解 │ 并行执行 │
└───────────────────────────────┘
    ↓
④ Memory 记忆系统（三层+结构化）
┌───────────────────────────────┐
│ 短期记忆 │ 事实记忆 │ 长期记忆 │ Structured │
│ 当前会话 │ 用户偏好 │ 跨会话 │ 结构化记忆 │
│ JSON │ JSON │ SQLite │ UserCtx │
└───────────────────────────────┘
```

**架构意义：** 整个系统从用户输入到最终输出，经过4大核心层的流水线处理。每一层职责明确，互相解耦，实现了"智能路由+按需执行+持久记忆"的完整闭环。

---

## 2. 11大板块详解

### 2.1 Core 配置中心

**作用：** 全局的配置管理和状态调度中心，所有模块的配置都在这里集中管理。

**包含组件：**
- `PhoenixConfig`：全局配置（模型、路由、安全等所有参数）
- `AppState`：状态管理（当前会话状态、运行状态）
- `TaskManager`：任务调度（管理任务队列、优先级）

---

### 2.2 ① Router 路由引擎

**文件路径：** `router/engine.py`，`router/intent_classifier.py`，`router/query_complexity.py`，`router/gatekeeper.py`，`router/llm_classifier.py`，`router/subagent_router.py`，`router/model_registry.py`

**作用：** 根据用户输入自动判断应该使用哪个模型处理。双维度路由：先用关键词快速匹配（<1ms），匹配不上再用LLM理解意图（~200ms）。

**双维度路由逻辑：**

```
输入消息
├─ 维度1：关键词匹配（<1ms）
│  ├─ "帮我写" → code_medium
│  ├─ "分析" → reasoning
│  ├─ "看这张图" → vision
│  └─ "你好" → chat
└─ 维度2：LLM意图分类（~200ms，仅关键词不明确时触发）
   ├─ 复杂度评估 → 选择是否需要LLM分类
   └─ LLM分类器 → 返回精确任务类型
```

**模型矩阵（9种任务×3级降级）：**

| 任务类型 | Primary（首选） | Fallback（降级） | Emergency（应急） |
|----------|---------|----------|-----------|
| chat     | mimo-v2.5 | mimo-v2.5 | mimo-v2-flash |
| code_small | mimo-v2.5-pro | mimo-v2.5 | mimo-v2.5 |
| code_medium | mimo-v2.5 | claude-haiku-4.5 | mimo-v2.5-pro |
| code_large | claude-sonnet-4.6 | mimo-v2.5 | claude-haiku-4.5 |
| reasoning_light | mimo-v2.5 | mimo-v2.5 | claude-haiku-4.5 |
| reasoning | opus-4.7 | claude-sonnet-4.6 | gpt-5.5 |
| vision   | mimo-v2-omni | gemini-3-flash | claude-sonnet-4.6 |
| routing  | mimo-v2-flash | mimo-v2.5 | mimo-v2.5 |
| subtask  | mimo-v2.5 | mimo-v2.5 | mimo-v2-flash |

**意义：** 每种任务都有3级保障。首选模型挂了自动切降级模型，降级也挂了还有应急模型，确保系统永远可用。

**熔断机制：** 连续3次失败 → 熔断（open）→ 60秒冷却 → 半开（half-open）→ 成功 → 恢复（closed）

**子Agent路由（并行执行）：**
- 纯文本推理 → mimo-v2.5（快速便宜）
- 代码任务 → claude-sonnet-4.6（代码质量高）
- 深度推理 → claude-opus-4.7（最强推理）
- 多模型协作 → opus+gpt-5.5（顶级策划）

---

### 2.3 ② Executor 执行管道

**文件路径：** `executor/pipeline.py`，`executor/circuit_breaker.py`，`executor/response_cache.py`，`executor/skill_loader.py`，`executor/deep_compact.py`，`executor/micro_compact.py`，`executor/task_decomposer.py`，`executor/parallel_executor.py`，`executor/progressive_loader.py`，`executor/deferred_tool_loader.py`

**作用：** 路由决定用哪个模型后，执行管道负责实际调用的全流程管控——审批、压缩、调用、缓存、并行。

**执行流程（8阶段）：**

```
TaskDecomposer → PreApprover → MicroCompact → CreditMonitor(信用检查) → API调用 → DeepCompact → ResponseCache → ParallelExecutor(并行)
```

**8阶段详解：**

| 阶段 | 作用 | 说明 |
|------|------|------|
| **1. TaskDecomposer** | 任务拆解 | 将复杂任务拆解为子任务 |
| **2. PreApprover** | 预审批 | 按P0/P1/P2三级权限审批操作是否允许 |
| **3. MicroCompact** | 微压缩 | 消息超过500字符时自动摘要，节省token |
| **4. CreditMonitor** | 信用检查 | 检查三方API是否欠费，欠费时切换兜底模型 |
| **5. API调用** | 实际调用 | 执行实际的模型调用 |
| **6. DeepCompact** | 深度压缩 | 对话超过20条时压缩历史，防止上下文溢出 |
| **7. ResponseCache** | 响应缓存 | 缓存结果，重复查询直接返回不重复调用 |
| **8. ParallelExecutor** | 并行执行 | 多个子Agent并行执行，提升效率 |
- `parallel_executor.py`：并行子Agent执行器，支持ThreadPoolExecutor
- `progressive_loader.py`：渐进式技能加载+缓存+优先级排序
- `deferred_tool_loader.py`：工具延迟加载+按需发现

---

### 2.4 ③ Memory 记忆系统

**文件路径：** `memory/memory_system.py`，`memory/structured_memory.py`，`memory/auto_extract.py`，`memory/memory_refiner.py`，`memory/diary.py`

**作用：** 让AI拥有跨会话的记忆能力，能记住用户的偏好、习惯、工作背景等，越用越懂你。

**四层记忆架构：**

| 层级 | 存储位置 | 生命周期 | 用途 | 意义 |
|------|---------|----------|------|------|
| 短期记忆 | 内存 | 当前会话 | 对话上下文 | 保证单次对话连贯 |
| 事实记忆 | facts.json | 永久 | 用户偏好/环境 | 记住用户的基本信息（如"我用Mac"） |
| 长期记忆 | SQLite | 永久 | 跨会话知识 | 累积的项目知识、经验 |
| 结构化记忆 | JSON | 永久 | UserContext/History/Facts | 结构化的个人画像 |

**结构化记忆详细结构：**

```
结构化记忆
├── UserContext（用户画像）
│   ├── work_context：工作背景（如"我是前端开发"）
│   ├── personal_context：个人偏好（如"我喜欢简洁代码"）
│   └── top_of_mind：当前关注（如"我正在做登录模块"）
├── HistoryContext（历史背景）
│   ├── recent_months：近几个月的重要事件
│   ├── earlier_context：更早期的上下文
│   └── long_term_background：长期背景
└── Facts（事实记录）
    ├── id：唯一标识
    ├── content：事实内容
    ├── category：分类
    └── confidence：置信度
```

**记忆循环流程：**

```
对话输入
  → process_message()：处理用户消息
  → 提取事实：从对话中自动提取关键信息
  → add_fact()：将事实写入记忆
  → 写入facts.json：持久化保存
  → 下次会话
  → load()：加载历史记忆
  → 注入system prompt：将记忆注入到AI的上下文中
```

---

### 2.5 ④ Self-heal 自愈系统

**文件路径：** `self_heal/antibody.py`，`self_heal/error_processor.py`，`self_heal/fault_playbook.py`，`self_heal/evolution.py`，`self_heal/skill_crystallizer.py`

**作用：** 系统出错时自动修复，像免疫系统一样。遇到过的错误下次自动处理，不需要人工干预。

**四个核心组件：**

#### 组件1：抗体库（antibody.py）
- **功能：** 预定义的错误处理规则集
- **工作方式：** 每次遇到错误，自动记录"错误模式+修复方案"。下次遇到同样错误，直接应用修复
- **内置8个抗体：** 404-ban（禁止访问）、timeout-retry（超时重试）、rate-limit-wait（限流等待）等
- **意义：** 类似人体免疫系统的"抗体"，犯过的错误不会再犯

#### 组件2：错误处理器（error_processor.py）
- **功能：** 10步系统化排查法
- **流程：** 记录→分类→检查熔断器→匹配抗体→降级模型→修复→验证→记录→更新→报告
- **意义：** 标准化的错误处理流程，不遗漏任何步骤

#### 组件3：故障处理卡（fault_playbook.py）
- **功能：** 4类常见故障的标准处理流程
- **分类：**
  - 网络故障 → 重试+降级
  - API错误 → 模型切换
  - 资源耗尽 → 压缩+清理
  - 逻辑错误 → 回滚+报告
- **意义：** 每种故障都有预案，避免临时手忙脚乱

#### 组件4：进化引擎（evolution.py）
- **功能：** 分析所有成功和失败的案例，自动生成新规则，更新抗体库
- **意义：** 系统越用越"聪明"，自我进化

---

### 2.6 ⑤ Integration 集成桥梁

**文件路径：** `integration/gateway_api.py`，`integration/hooks.py`，`integration/cron_sync.py`，`integration/startup.py`，`integration/hermes_bridge.py`

**作用：** 让系统能够连接各种外部平台（飞书、微信、企业微信等）和工具。

**核心组件：**

| 组件 | 功能 | 意义 |
|------|------|------|
| **GatewayAPI** | 单例模式，统一管理所有平台的消息收发 | 一个入口管所有平台 |
| `route_and_get_runtime()` | 一次调用返回模型+运行时配置 | 简化调用链路 |
| `health_check()` | 系统健康检查 | 实时监控系统状态 |
| `reset()` | 重置状态 | 出问题时快速恢复 |
| **Hook系统** | 在API调用前/后/出错时执行自定义逻辑 | 灵活扩展 |
| `pre_api_request` | API调用前钩子 | 可以做预处理 |
| `post_api_response` | API响应后钩子 | 可以做后处理 |
| `error_occurred` | 错误发生钩子 | 可以做错误通知 |
| **CronSync** | 定时同步任务到Hermes调度系统 | 自动化定期任务 |

---

### 2.7 ⑥ Security 安全防护

**文件路径：** `security/approval.py`，`security/permission_system.py`，`security/token_tracker.py`，`security/guardrail_middleware.py`

**作用：** 保护系统安全，防止误操作和恶意操作，控制成本。

**三级权限系统：**

| 级别 | 操作类型 | 审批方式 | 意义 |
|------|---------|---------|------|
| **P0** | 删除文件/改配置/密钥操作 | 双重确认 | 最危险的操作，必须二次确认 |
| **P1** | 终端执行/网络请求/文件写入 | 单次确认 | 中等风险，确认一次即可 |
| **P2** | 代码生成/文本分析/搜索查询 | 自动通过 | 低风险，直接执行 |

**Token追踪（token_tracker.py）：**
- 记录每次API调用的token消耗
- 按模型/日期/会话统计
- 预算告警（月$100 / 日$3.3）

**中间件护栏（guardrail_middleware.py）：**
- 可插拔安全检查
- 内置3个护栏：
  - 危险命令拦截（如 rm -rf、curl|bash）
  - 密钥泄露检测（防止API Key意外暴露）
  - 预算限制检查
- 严重级别分级：info / warning / block

---

### 2.8 ⑦ Adapt 自动适配

**文件路径：** `adapt/adapter.py`，`adapt/scanner.py`，`adapt/compat_report.py`，`adapt/run.py`

**作用：** Hermes升级后自动检测版本差异并修复配置，确保系统平滑升级。

**流程：**
1. `HermesScanner.scan()`：检测版本差异
2. 生成兼容性报告
3. `HermesAdapter.adapt()`：自动修复配置
4. 生成适配报告

---

### 2.9 ⑧ Sandbox 沙箱

**文件路径：** `sandbox/manager.py`，`sandbox/executor.py`

**作用：** 用Docker容器隔离执行环境，防止代码执行影响主系统。

**流程：**
1. `SandboxManager.create("sandbox_name")`：创建容器
2. `SandboxExecutor.run_code(code)`：执行代码
3. 结果收集
4. `SandboxManager.cleanup()`：清理

---

### 2.10 ⑨ Workflow 工作流

**文件路径：** `workflow/engine.py`，`workflow/step.py`

**作用：** 管理多步骤的复杂任务，支持暂停/恢复，断电重启也能继续。

**功能：**
1. `WorkflowEngine.register("action", executor)`：注册步骤
2. `run()`：执行流程
3. `pause()` / `resume()`：暂停/恢复控制
4. 状态持久化到磁盘，断电重启自动续上

---

### 2.11 ⑩ GitHub 集成

**文件路径：** `github/client.py`

**作用：** 提供完整的GitHub操作能力。

**功能：**
- `create_issue()`：创建Issue
- `create_pr()`：创建PR
- `review_pr()`：审查PR
- `merge_pr()`：合并PR
- `list_issues()` / `list_prs()`：列表查询
- `commit()` / `push()`：代码提交

---

## 3. 核心模块详解

### 3.1 Credit Monitor（信用监控）

**文件路径：** `core/credit_monitor.py`

**核心类：** `CreditMonitor`，`CreditStatus`

**作用：** 实时监控第三方API的账户余额，欠费时自动切换到兜底模型，充值后自动切回。

**完整流程：**

```
正常工作 → 使用用户配置的三方API（任何Provider）
    ↓
API调用 → CreditMonitor.check_credit()
    ↓
响应200 → 继续使用
响应401/402/403 → is_exhausted = True
    ↓
should_fallback() → True
    ↓
get_primary_model_config() → 获取用户配置的兜底模型
    ↓
自动切换到兜底模型
    ↓
get_notification() → 通知用户"三方API已欠费"
    ↓
用户充值 → confirm_topup() → 自动切回三方API
```

**配置项 (config.json)：**

```json
{
  "credit_monitor": {
    "enabled": true,
    "auto_fallback_to_primary": true,
    "auto_recover_on_topup": true
  },
  "router": {
    "primary_model": {
      "provider": "用户配置",
      "model": "用户配置",
      "api_key": "$ENV_VAR",
      "base_url": "用户配置"
    }
  }
}
```

---

### 3.2 Skill Reviewer（技能审稿）

**文件路径：** `core/skill_reviewer.py`

**核心类：** `SkillReviewer`，`SkillScore`

**功能：**
- **评分算法：** 基础分50 + 内容质量(40分) + 使用次数(20分) = 最高110分
- **查重：** Jaccard相似度检测，防止重复技能
- **截断：** 30天未使用+评分<30分的技能自动清理
- **报告：** 总技能数/平均分/重复组/死技能数

---

### 3.3 Parallel Executor（并行执行）

**文件路径：** `executor/parallel_executor.py`

**核心类：** `ParallelSubAgentExecutor`，`SubAgentTask`，`ParallelResult`

**功能：**
- ThreadPoolExecutor并行执行多个子Agent任务
- 最大并行数：3（可配置）
- 任务超时：300秒
- 状态追踪：pending / running / completed / failed

---

### 3.4 Progressive Loader（渐进式加载）

**文件路径：** `executor/progressive_loader.py`

**核心类：** `ProgressiveSkillLoader`，`CachedSkill`

**功能：**
- 技能缓存（JSON持久化）
- 使用频率追踪+优先级自动提升（越常用的技能加载越快）
- 过期清理（30天未使用自动清除）
- 统计报告

---

### 3.5 Deferred Tool Loader（延迟工具加载）

**文件路径：** `executor/deferred_tool_loader.py`

**核心类：** `DeferredToolLoader`，`ToolInfo`

**功能：**
- 工具注册但不立即加载（节省启动时间）
- 按需搜索匹配（名称+描述）
- 使用频率排序（常用工具优先加载）
- 延迟加载+优先级提升

---

### 3.6 Structured Memory（结构化记忆）

**文件路径：** `memory/structured_memory.py`

**核心类：** `StructuredMemory`，`UserContext`，`HistoryContext`，`MemoryFact`

**功能：**
- 三层结构：UserContext / HistoryContext / Facts
- UserContext分层：工作/个人/当前关注
- 自动保存到JSON
- 生成上下文提示词

---

### 3.7 Guardrail Middleware（中间件护栏）

**文件路径：** `security/guardrail_middleware.py`

**核心类：** `GuardrailMiddleware`，`GuardrailResult`

**内置护栏：**
- `check_no_dangerous_commands`：拦截 rm -rf / curl|bash 等危险命令
- `check_no_secrets_leak`：检测 API Key 泄露
- `check_budget_limit`：预算限制检查

---

## 4. 路由系统详解

### 4.1 五档路由模式

| 档位 | 占比 | 自动执行 | 需确认 | 成本阈值 | 使用模型 |
|------|------|---------|--------|---------|---------|
| **日常** | 40% | ✔ | ✖ | $0.01 | mimo-v2.5 |
| **中等** | 40% | ✔ | ✖ | $0.05 | mimo-v2.5-pro |
| **深度** | 15% | ✖ | ✔ | $1.0 | claude-sonnet-4.6 |
| **大神** | 4% | ✖ | ✔ | $5.0 | opus-4.7 |
| **真神** | 1% | ✖ | ✔ | $10.0 | opus-4.7+gpt-5.5 |

**意义：** 80%的日常任务用便宜模型自动处理（省钱），15%的复杂任务用中等模型（需要确认），5%的极难任务用顶级模型（成本高，必须确认）。实现了成本和质量的自动平衡。

### 4.2 路由决策树

```
用户消息
├─ 有图片？ ── YES ── vision (mimo-v2-omni)
├─ 关键词匹配
│  ├─ "你好/嗨/在吗" ── chat
│  ├─ "写代码/编程/脚本" ── code_medium
│  ├─ "重构/架构/系统设计" ── code_large
│  ├─ "分析/推理/逻辑" ── reasoning
│  ├─ "搜索/查一下" ── search
│  └─ 无匹配 ── 进入LLM分类
└─ LLM意图分类
   ├─ 简单对话 ── chat
   ├─ 代码任务 ── code_small/medium/large (按复杂度)
   ├─ 深度推理 ── reasoning_light/reasoning
   └─ 视觉任务 ── vision
```

### 4.3 复杂度评估

```
消息长度 < 20字   → code_small
消息长度 20-100字 → code_medium
消息长度 > 100字 或 包含"系统/架构" → code_large
包含"分析/推理/复杂" → reasoning
包含"简单/快速/一下" → reasoning_light
```

---

## 5. 信用监控+兜底模型

### 5.1 信用监控完整流程

```
正常工作 → 使用三方API
    ↓
API调用 → 检查响应
    ↓
200 OK → 继续使用
401/402/403 → is_exhausted=True
    ↓
should_fallback()=True
    ↓
get_primary_model_config() → 获取兜底模型配置
    ↓
自动切换到兜底模型
    ↓
通知用户"三方API已欠费"
    ↓
用户充值 → confirm_topup()
    ↓
自动切回三方API
```

### 5.2 支持的第三方Provider

信用监控支持**任何三方API**，不限于特定Provider：
- Nous Portal
- OpenRouter
- 其他中转站
- 任何兼容OpenAI API协议的服务

### 5.3 兜底模型配置

安装时引导用户配置：

```bash
# 安装过程中
请输入兜底模型Provider名称：xiaomi
请输入兜底模型Base URL：https://token-plan-cn.xiaomimimo.com/v1
请输入兜底模型API Key：****

# 或手动配置
hermes config set router.primary_model.model xiaomi/mimo-v2.5
```

---

## 6. 记忆系统循环

### 6.1 记忆系统循环流程

```
对话输入
  → process_message()：处理用户消息
  → 提取事实：从对话中自动提取关键信息
  → add_fact()：将事实写入记忆
  → 写入facts.json：持久化保存
  → 下次会话
  → load()：加载历史记忆
  → 注入system prompt：将记忆注入到AI的上下文中
```

---

## 7. 自查系统循环

### 7.1 自查系统完整工作流

自查系统（Self-heal）是Phoenix的"免疫系统"，它不是一个简单的try-catch，而是一个完整的"识别→诊断→治疗→免疫→进化"闭环。

```
                        ┌──────────────┐
                        │  API 调用    │
                        └──────┬───────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
                成功(200)              失败(错误)
                    │                     │
          report_success()      report_failure(error)
                    │                     │
                    │           ErrorProcessor.classify_error()
                    │                     │
                    │           ┌─────────┴──────────┐
                    │           │  错误分类            │
                    │           ├─────────────────────┤
                    │           │ 404 Not Found       │──→ ban_provider + 切换Provider
                    │           │ Timeout             │──→ retry(降级模型) + 超时延长
                    │           │ Rate Limit (429)    │──→ wait(指数退避) + retry
                    │           │ Unknown Error       │──→ 10步系统化排查
                    │           └─────────┬───────────┘
                    │                     │
                    │           AntibodyLibrary.match(error_pattern)
                    │                     │
                    │           ┌─────────┴──────────┐
                    │           │ 找到匹配抗体？      │
                    │           ├─ YES → apply(antibody) 直接修复
                    │           └─ NO  → generate_new_antibody() 生成新抗体
                    │                     │
                    │           修复执行 → 验证结果
                    │                     │
                    │           EvolutionEngine.evolve()
                    │                     │
                    │           更新抗体库 + 更新故障处理卡
                    │                     │
                    └─────────────────────┘
                              （下次遇到同类错误，直接免疫）
```

### 7.2 错误分类详解

每种错误类型都有不同的处理策略：

| 错误类型 | HTTP状态码 | 处理策略 | 示例场景 |
|---------|-----------|---------|---------|
| **404 Not Found** | 404 | ban_provider()：将该Provider标记为不可用，切换到备用Provider | 模型API端点被废弃 |
| **Timeout** | - | retry降级模型：重试时使用更轻量的模型，同时延长超时时间 | 网络延迟或模型响应慢 |
| **Rate Limit** | 429 | wait + retry：指数退避等待（1s→2s→4s→8s），然后重试 | API调用频率超限 |
| **Unknown Error** | 其他 | 触发完整10步系统化排查 | 未知异常、数据格式错误等 |

### 7.3 AntibodyLibrary.match() 匹配流程

```
新错误发生
    ↓
提取错误特征（error_type + error_code + context）
    ↓
AntibodyLibrary.match(error_pattern)
    ↓
遍历抗体库中的所有抗体
    ↓
比对错误模式（pattern matching）
    ↓
┌─ 匹配成功 → 返回对应抗体（antibody）
│   → apply(antibody)：执行预定义的修复方案
│   → 记录成功日志
│   → 更新抗体的使用频率和成功率
│
└─ 匹配失败 → 调用 generate_new_antibody()
    → 分析错误特征
    → 生成修复方案（基于LLM推理或规则推导）
    → 创建新抗体对象
    → 存入抗体库
    → 尝试应用新抗体修复
```

### 7.4 内置8个抗体详解

| 抗体名称 | 匹配的错误模式 | 修复动作 | 何时触发 |
|---------|--------------|---------|---------|
| `404-ban` | API端点404 | 禁用该Provider，切换备用 | 模型端点被废弃或迁移 |
| `timeout-retry` | 请求超时 | 重试+降级模型+延长超时 | 网络波动或模型负载高 |
| `rate-limit-wait` | 429限流 | 指数退避等待后重试 | 调用频率超限 |
| `auth-refresh` | 401/403认证失败 | 刷新Token或切换Key | Token过期 |
| `json-parse-fix` | JSON解析失败 | 重试请求或修复响应格式 | 模型返回格式异常 |
| `context-trim` | 上下文过长 | 自动压缩历史对话 | 对话太长导致超token |
| `model-switch` | 模型不可用 | 按降级链切换模型 | 当前模型维护/下线 |
| `generic-catch` | 其他未知错误 | 触发10步排查流程 | 未分类的错误 |

### 7.5 EvolutionEngine.evolve() 进化流程

```
每次错误处理完成后
    ↓
收集本次处理的全部数据（错误特征、修复方案、结果）
    ↓
EvolutionEngine.evolve()
    ↓
├─ 分析成功案例
│   → 提取有效的修复模式
│   → 优化现有抗体的匹配条件
│   → 提升高成功率抗体的优先级
│
├─ 分析失败案例
│   → 识别修复失败的原因
│   → 调整或淘汰低效抗体
│   → 记录需要人工介入的场景
│
└─ 生成进化报告
    → 新增抗体数
    → 优化抗体数
    → 淘汰抗体数
    → 整体修复成功率趋势
```

**意义：** 自查系统形成了一个完整的"免疫记忆"——系统遇到过的每一种错误都会被记录并生成抗体。随着使用时间增长，系统的自愈能力会越来越强，最终实现绝大部分错误的自动修复，无需人工干预。

---

## 8. Hermes融合机制

### 8.1 融合的78个核心能力

通过Hermes融合，整合了78个核心能力到系统中。这78个能力按6大类别组织：

#### 类别1：工具集（18个）

| # | 工具名称 | 功能说明 |
|---|---------|---------|
| 1 | file_read | 读取文件内容 |
| 2 | file_write | 写入文件 |
| 3 | file_edit | 编辑文件（增删改指定内容） |
| 4 | file_search | 搜索文件（按名称/内容/模式） |
| 5 | terminal_exec | 执行终端命令 |
| 6 | web_search | 网络搜索 |
| 7 | web_fetch | 抓取网页内容 |
| 8 | code_execute | 代码执行（沙箱内） |
| 9 | git_operation | Git操作（commit/push/pull/branch） |
| 10 | http_request | HTTP请求（GET/POST/PUT/DELETE） |
| 11 | json_parse | JSON解析和处理 |
| 12 | regex_match | 正则表达式匹配 |
| 13 | diff_compare | 文件差异对比 |
| 14 | archive_zip | 压缩/解压文件 |
| 15 | process_manager | 进程管理（启动/停止/监控） |
| 16 | env_manager | 环境变量管理 |
| 17 | package_manager | 包管理（pip/npm等） |
| 18 | db_query | 数据库查询 |

#### 类别2：插件（4个）

| # | 插件名称 | 功能说明 |
|---|---------|---------|
| 1 | phoenix-full | Phoenix完整功能插件（核心插件） |
| 2 | disk-cleanup | 磁盘清理插件，自动清理临时文件和缓存 |
| 3 | kanban | 看板管理插件，任务可视化追踪 |
| 4 | observability | 可观测性插件，日志/指标/链路追踪 |

#### 类别3：子系统（14个）

| # | 子系统名称 | 功能说明 |
|---|---------|---------|
| 1 | MemorySystem | 记忆系统（四层记忆架构） |
| 2 | RouterEngine | 路由引擎（五档路由+双维度分类） |
| 3 | ExecutorPipeline | 执行管道（8阶段流水线） |
| 4 | SelfHealSystem | 自愈系统（抗体库+进化引擎） |
| 5 | SecuritySystem | 安全系统（三级权限+护栏） |
| 6 | IntegrationBridge | 集成桥梁（多平台连接） |
| 7 | SandboxManager | 沙箱管理（Docker隔离） |
| 8 | WorkflowEngine | 工作流引擎（多步骤任务） |
| 9 | GitHubClient | GitHub集成 |
| 10 | AdaptSystem | 自动适配系统 |
| 11 | CronSync | 定时同步系统 |
| 12 | HookSystem | 钩子系统 |
| 13 | CacheSystem | 缓存系统 |
| 14 | PluginManager | 插件管理器 |

#### 类别4：算法命令（15个）

| # | 命令名称 | 功能说明 |
|---|---------|---------|
| 1 | analyze | 深度分析（代码/数据/文本） |
| 2 | refactor | 智能重构（代码结构调整） |
| 3 | optimize | 性能优化（代码/查询/配置） |
| 4 | test_gen | 自动生成测试用例 |
| 5 | doc_gen | 自动生成文档 |
| 6 | debug | 智能调试（定位+修复Bug） |
| 7 | review | 代码审查 |
| 8 | migrate | 迁移辅助（框架/版本升级） |
| 9 | deploy | 部署管理 |
| 10 | monitor | 实时监控 |
| 11 | backup | 备份管理 |
| 12 | restore | 恢复管理 |
| 13 | schedule | 定时任务管理 |
| 14 | transform | 数据转换（格式/结构） |
| 15 | validate | 数据/配置验证 |

#### 类别5：显示（5个）

| # | 显示组件 | 功能说明 |
|---|---------|---------|
| 1 | streaming_output | 流式输出（逐字显示，提升交互体验） |
| 2 | progress_bar | 进度条（长时间操作的可视化进度） |
| 3 | syntax_highlight | 语法高亮（代码输出自动着色） |
| 4 | markdown_render | Markdown渲染（表格、列表、代码块） |
| 5 | theme_engine | 主题引擎（支持多种皮肤，默认phoenix主题） |

#### 类别6：扩展能力（22个）

在原有56个能力基础上，扩展了22个能力：

| # | 能力名称 | 功能说明 |
|---|---------|---------|
| 1 | CreditMonitor | 信用监控（实时API余额检测+自动切换） |
| 2 | ParallelExecutor | 并行执行器（多子Agent并行处理） |
| 3 | ProgressiveLoader | 渐进式加载（技能按需加载+缓存+优先级） |
| 4 | DeferredToolLoader | 延迟工具加载（工具注册但不立即加载） |
| 5 | StructuredMemory | 结构化记忆（UserContext/HistoryContext/Facts） |
| 6 | GuardrailMiddleware | 中间件护栏（危险命令拦截+密钥检测+预算限制） |
| 7 | SkillReviewer | 技能审稿（评分+查重+截断+报告） |
| 8 | SelfImprovement | 自我改进（基于历史数据自动优化） |
| 9 | LMStudio | LM Studio本地模型支持 |
| 10 | ComfyUI | ComfyUI工作流集成 |
| 11 | TouchDesigner | TouchDesigner实时视觉集成 |
| 12 | MiniMax | 元宝（MiniMax）模型集成 |
| 13 | TUI优化 | 终端界面体验优化 |
| 14 | SecretRedaction | 敏感信息自动脱敏 |
| 15 | Provider管理增强 | 更完善的Provider注册/发现/健康检查 |
| 16 | 深度加速器 | 深度推理任务加速 |
| 17 | 免疫模型升级 | 自愈系统抗体匹配算法升级 |
| 18 | 更新预存 | 升级包预存机制 |
| 19 | AutoFusion | 自动融合引擎 |
| 20 | FeatureRegistry | 特性注册表 |
| 21 | TaskDecomposer | 任务分解器（复杂任务拆解） |
| 22 | CircuitBreaker | 熔断器（连续失败自动熔断） |

### 8.2 Auto-Fusion Engine 自动融合引擎

Auto-Fusion Engine是核心创新之一，它负责自动检测和融合Hermes的升级能力，无需手动配置。

#### 完整工作流程：

```
Auto-Fusion Engine 启动
    ↓
① 扫描（Scan）
    → 扫描Hermes全部78个能力
    → 读取每个能力的元数据（名称、版本、描述、依赖）
    ↓
② 对比（Compare）
    → 读取本地 Feature Registry（feature_registry.json）
    → 将Hermes能力与已融合特性逐一对比
    → 识别未融合的新特性（unfused features）
    ↓
③ 决策（Decide）
    → 对每个未融合特性评估融合优先级
    → P0（必须融合）：核心功能、安全相关
    → P1（建议融合）：增强功能、效率提升
    → P2（可选融合）：辅助功能、体验优化
    ↓
④ 融合（Fuse）
    → 对P0特性：立即自动融合
    → 对P1插件：自动启用
    → 对P2功能：标记为"可启用"，等待用户确认
    → 更新 feature_registry.json
    ↓
⑤ 验证（Verify）
    → 运行兼容性测试
    → 检查功能是否正常工作
    → 生成融合报告
    ↓
⑥ 报告（Report）
    → 新融合的特性列表
    → 更新的特性列表
    → 跳过的特性及原因
    → 建议手动处理的特性
```

#### Feature Registry 结构：

```json
{
  "features": [
    {
      "name": "credit_monitor",
      "source": "phoenix_native",
      "version": "1.0.0",
      "fused": true,
      "fused_at": "2026-05-02",
      "priority": "P0",
      "dependencies": []
    },
    {
      "name": "comfyui_integration",
      "source": "hermes_v0.12",
      "version": "0.12.0",
      "fused": true,
      "fused_at": "2026-05-02",
      "priority": "P1",
      "dependencies": ["http_request", "code_execute"]
    }
  ],
  "last_scan": "2026-05-02T10:00:00Z",
  "fusion_count": 78,
  "pending_count": 0
}
```

**意义：** Auto-Fusion Engine让Phoenix成为一个"自我进化"的系统。当Hermes框架发布新版本时，Phoenix不需要人工更新配置——它会自动扫描、评估、融合新能力，然后验证并报告结果。这极大地降低了运维成本，同时确保系统始终保持最新。

---

## 9. 完整执行流程

### 9.1 一次完整对话的执行路径（通用流程图）

```
用户输入
  → Core（配置加载+状态初始化）
  → Router（意图分类+复杂度评估+模型选择）
  → Executor
    → TaskDecomposer（任务拆解）
    → PreApprover（权限审批）
    → MicroCompact（消息压缩）
    → CreditMonitor（信用检查）
    → API调用（实际模型调用）
    → DeepCompact（历史压缩）
    → ResponseCache（结果缓存）
    → ParallelExecutor（并行执行）
  → Memory（记忆提取+存储）
  → 输出响应（格式化 + 流式输出）
```

### 9.2 具体示例："帮我写一个Flask API"

以一个真实的用户请求为例，完整走一遍从输入到输出的每一步：

```
用户输入："帮我写一个Flask API"
```

**第1步：Core 配置中心**
```
PhoenixConfig.load()
  → 加载 config.json
  → 读取路由配置、模型配置、安全配置
AppState.initialize()
  → 初始化会话状态
  → 设置 session_id = "sess_abc123"
TaskManager.register()
  → 创建新任务 task_id = "task_001"
```

**第2步：Router 路由引擎**
```
IntentClassifier.classify("帮我写一个Flask API")
  → 关键词匹配：
    → "帮我写" 命中 code 类别 ✓
  → 返回 task_type = "code_medium"

ComplexityEstimator.estimate("帮我写一个Flask API")
  → 消息长度：13字（< 20字）
  → 但包含"Flask API"（具体技术框架）
  → 判定：code_medium（中等代码任务）

Gatekeeper.check("code_medium")
  → 复杂度级别：中等
  → 占比40%的任务
  → 自动执行：✔（无需用户确认）
  → 成本阈值：$0.05

LLMClassifier（关键词已匹配，跳过LLM分类）

模型选择：
  → Primary: mimo-v2.5
  → Fallback: claude-haiku-4.5
  → Emergency: mimo-v2.5-pro
```

**第3步：Executor 执行管道**
```
TaskDecomposer.decompose("帮我写一个Flask API")
  → 单一任务，无需拆解
  → 直接传递

PreApprover.approve(task)
  → 任务类型：code_medium（P2 - 自动通过）
  → 不涉及文件删除/配置修改等危险操作
  → 审批结果：APPROVED

MicroCompact.compact(messages)
  → 当前消息13字（< 500字阈值）
  → 无需压缩
  → 直接传递

CreditMonitor.check()
  → 检查三方API余额状态
  → is_exhausted = False
  → 继续使用三方API
  → （如果欠费 → should_fallback() → 切换兜底模型）

API调用
  → POST {provider_base_url}/chat/completions
  → Headers: Authorization: Bearer {api_key}
  → Body: { model: "mimo-v2.5", messages: [...] }
  → 等待响应...
  → 响应200 OK
  → 返回生成的Flask API代码

DeepCompact.check(conversation_history)
  → 对话只有1条（< 20条阈值）
  → 无需压缩历史

ResponseCache.check_and_store(query, response)
  → 缓存本次查询结果
  → 下次相同查询直接返回缓存

ParallelExecutor
  → 本次只有一个任务，无需并行
  → 直接返回结果
```

**第4步：Memory 记忆处理**
```
process_message("帮我写一个Flask API")
  → 提取事实：
    → fact_001: "用户正在使用Flask框架" (category: tech_stack, confidence: 0.85)
  → add_fact(fact_001)
  → 写入 facts.json
  → 短期记忆更新：当前对话上下文
```

**第5步：输出响应**
```
  → 格式化代码输出（syntax_highlight）
  → 流式输出（streaming_output）
  → 用户看到：
    ┌─────────────────────────────────────────┐
    │ from flask import Flask, jsonify        │
    │                                          │
    │ app = Flask(__name__)                    │
    │                                          │
    │ @app.route('/api/hello')                 │
    │ def hello():                             │
    │     return jsonify({"message": "Hello"}) │
    │                                          │
    │ if __name__ == '__main__':               │
    │     app.run(debug=True)                  │
    └─────────────────────────────────────────┘
```

### 9.3 错误恢复路径（完整版，含信用监控）

当API调用失败时，系统如何自动恢复：

```
API调用 → 失败
    ↓
report_failure(error)
    ↓
ErrorProcessor.classify_error(error)
    ↓
┌─ 情况A：401/402/403（认证/欠费错误）
│   → CreditMonitor.is_exhausted = True
│   → should_fallback() = True
│   → get_primary_model_config()
│   → 自动切换到兜底模型（用户配置的本地/备用API）
│   → get_notification() → 通知用户"三方API已欠费，已切换兜底模型"
│   → 继续执行任务（使用兜底模型）
│   → 用户充值 → confirm_topup()
│   → is_exhausted = False
│   → 自动切回三方API
│
├─ 情况B：Timeout（超时）
│   → CircuitBreaker.record_failure()
│   → AntibodyLibrary.match("timeout")
│   → 找到 timeout-retry 抗体
│   → apply(antibody)：重试 + 降级模型 + 延长超时
│   → 成功 → report_success() → CircuitBreaker.record_success()
│   → 失败 → 继续降级到Emergency模型
│
├─ 情况C：429 Rate Limit（限流）
│   → AntibodyLibrary.match("rate_limit")
│   → 找到 rate-limit-wait 抗体
│   → apply(antibody)：指数退避等待（1s→2s→4s→8s）+ retry
│   → 最多重试3次
│   → 全部失败 → 降级模型
│
└─ 情况D：Unknown Error（未知错误）
    → 触发10步系统化排查：
    → ① 记录错误详情（error_type, message, stack_trace）
    → ② 分类错误（HTTP状态码 + 错误模式）
    → ③ 检查熔断器状态（是否需要熔断）
    → ④ 匹配抗体库（查找已知修复方案）
    → ⑤ 尝试降级模型（Primary → Fallback → Emergency）
    → ⑥ 执行修复（应用抗体方案）
    → ⑦ 验证修复结果（重新调用API）
    → ⑧ 记录处理过程（成功/失败的详细日志）
    → ⑨ 更新抗体库（新增/优化抗体）
    → ⑩ 生成错误报告（包含所有步骤和最终结果）
    → EvolutionEngine.evolve() → 更新免疫系统
```

**意义：** 错误恢复路径展示了Phoenix的"韧性设计"——无论是欠费、超时、限流还是完全未知的错误，系统都有自动化的应对方案。多层级的降级和自愈机制确保了即使在极端情况下，用户也能获得响应。

---

## 10. 配置参数速查

### 10.1 核心配置（config.json）

```json
{
  "display": {
    "skin": "phoenix",
    "streaming": true,
    "progress_bar": true,
    "syntax_highlight": true,
    "markdown_render": true
  },
  "compression": {
    "enabled": true,
    "micro_threshold": 500,
    "deep_threshold": 20,
    "algorithm": "sliding_window"
  },
  "checkpoints": {
    "enabled": true,
    "interval": 300,
    "max_checkpoints": 10,
    "auto_cleanup": true
  },
  "plugins": {
    "list": ["phoenix-full", "disk-cleanup", "kanban", "observability"],
    "auto_update": true,
    "registry_url": "https://plugins.phoenix.dev"
  },
  "security": {
    "approval_mode": "confirm",
    "max_budget_daily": 3.3,
    "max_budget_monthly": 100,
    "guardrails_enabled": true
  },
  "memory": {
    "auto_extract": true,
    "confidence_threshold": 0.7,
    "max_facts": 1000,
    "cleanup_interval": 86400
  }
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `display.skin` | `"phoenix"` | 主题皮肤名称，phoenix是默认主题 |
| `display.streaming` | `true` | 是否启用流式输出（逐字显示） |
| `display.progress_bar` | `true` | 长时间操作是否显示进度条 |
| `display.syntax_highlight` | `true` | 代码输出是否自动语法高亮 |
| `display.markdown_render` | `true` | 是否渲染Markdown格式 |
| `compression.enabled` | `true` | 是否启用消息压缩 |
| `compression.micro_threshold` | `500` | 微压缩阈值（字符数），超过此值触发MicroCompact |
| `compression.deep_threshold` | `20` | 深度压缩阈值（对话条数），超过此值触发DeepCompact |
| `compression.algorithm` | `"sliding_window"` | 压缩算法，滑动窗口策略 |
| `checkpoints.enabled` | `true` | 是否启用检查点（断电恢复） |
| `checkpoints.interval` | `300` | 检查点保存间隔（秒） |
| `checkpoints.max_checkpoints` | `10` | 最大检查点数量 |
| `checkpoints.auto_cleanup` | `true` | 是否自动清理过期检查点 |
| `plugins.list` | `["phoenix-full", ...]` | 启用的插件列表 |
| `plugins.auto_update` | `true` | 插件是否自动更新 |
| `security.approval_mode` | `"confirm"` | 审批模式（confirm=确认/auto=自动） |
| `security.max_budget_daily` | `3.3` | 每日预算上限（美元） |
| `security.max_budget_monthly` | `100` | 每月预算上限（美元） |
| `security.guardrails_enabled` | `true` | 是否启用中间件护栏 |
| `memory.auto_extract` | `true` | 是否自动从对话中提取事实 |
| `memory.confidence_threshold` | `0.7` | 事实提取的置信度阈值 |
| `memory.max_facts` | `1000` | 最大事实记录数量 |
| `memory.cleanup_interval` | `86400` | 记忆清理间隔（秒），默认24小时 |

### 10.2 路由配置（router）

```json
{
  "router": {
    "mode": "auto",
    "complexity_estimation": true,
    "llm_fallback": true,
    "primary_model": {
      "provider": "用户配置",
      "model": "用户配置",
      "api_key": "$ENV_VAR",
      "base_url": "用户配置"
    },
    "model_matrix": {
      "chat": {
        "primary": "mimo-v2.5",
        "fallback": "mimo-v2.5",
        "emergency": "mimo-v2-flash"
      },
      "code_small": {
        "primary": "mimo-v2.5-pro",
        "fallback": "mimo-v2.5",
        "emergency": "mimo-v2.5"
      },
      "code_medium": {
        "primary": "mimo-v2.5",
        "fallback": "claude-haiku-4.5",
        "emergency": "mimo-v2.5-pro"
      },
      "code_large": {
        "primary": "claude-sonnet-4.6",
        "fallback": "mimo-v2.5",
        "emergency": "claude-haiku-4.5"
      },
      "reasoning_light": {
        "primary": "mimo-v2.5",
        "fallback": "mimo-v2.5",
        "emergency": "claude-haiku-4.5"
      },
      "reasoning": {
        "primary": "opus-4.7",
        "fallback": "claude-sonnet-4.6",
        "emergency": "gpt-5.5"
      },
      "vision": {
        "primary": "mimo-v2-omni",
        "fallback": "gemini-3-flash",
        "emergency": "claude-sonnet-4.6"
      },
      "routing": {
        "primary": "mimo-v2-flash",
        "fallback": "mimo-v2.5",
        "emergency": "mimo-v2.5"
      },
      "subtask": {
        "primary": "mimo-v2.5",
        "fallback": "mimo-v2.5",
        "emergency": "mimo-v2-flash"
      }
    },
    "circuit_breaker": {
      "failure_threshold": 3,
      "cooldown_seconds": 60,
      "half_open_max_calls": 1
    }
  }
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `router.mode` | `"auto"` | 路由模式（auto=自动/manual=手动指定模型） |
| `router.complexity_estimation` | `true` | 是否启用复杂度评估 |
| `router.llm_fallback` | `true` | 关键词匹配失败时是否用LLM分类 |
| `router.primary_model.provider` | 用户配置 | 兜底模型Provider名称 |
| `router.primary_model.model` | 用户配置 | 兜底模型名称 |
| `router.primary_model.api_key` | `$ENV_VAR` | 兜底模型API Key（支持环境变量引用） |
| `router.primary_model.base_url` | 用户配置 | 兜底模型API地址 |
| `router.circuit_breaker.failure_threshold` | `3` | 连续失败多少次触发熔断 |
| `router.circuit_breaker.cooldown_seconds` | `60` | 熔断冷却时间（秒） |
| `router.circuit_breaker.half_open_max_calls` | `1` | 半开状态最大试调用次数 |

### 10.3 扩展配置

```json
{
  "credit_monitor": {
    "enabled": true,
    "auto_fallback_to_primary": true,
    "auto_recover_on_topup": true,
    "check_interval": 300,
    "notification_enabled": true
  },
  "executor": {
    "parallel_max_workers": 3,
    "task_timeout": 300,
    "progressive_loading": true,
    "deferred_tools": true,
    "cache_model_info": true
  },
  "lazy_load_skills": {
    "enabled": true,
    "cache_duration": 86400,
    "priority_boost_threshold": 5,
    "cleanup_interval": 2592000
  },
  "auto_fusion": {
    "enabled": true,
    "scan_on_startup": true,
    "auto_fuse_priority": ["P0", "P1"],
    "require_confirm_for": ["P2"]
  }
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `credit_monitor.enabled` | `true` | 是否启用信用监控 |
| `credit_monitor.auto_fallback_to_primary` | `true` | 欠费时是否自动切兜底模型 |
| `credit_monitor.auto_recover_on_topup` | `true` | 充值后是否自动切回三方API |
| `credit_monitor.check_interval` | `300` | 信用检查间隔（秒） |
| `credit_monitor.notification_enabled` | `true` | 是否发送欠费通知 |
| `executor.parallel_max_workers` | `3` | 并行执行器最大线程数 |
| `executor.task_timeout` | `300` | 单个任务超时时间（秒） |
| `executor.progressive_loading` | `true` | 是否启用渐进式技能加载 |
| `executor.deferred_tools` | `true` | 是否启用延迟工具加载 |
| `executor.cache_model_info` | `true` | 是否缓存模型信息 |
| `lazy_load_skills.enabled` | `true` | 是否启用技能延迟加载 |
| `lazy_load_skills.cache_duration` | `86400` | 技能缓存有效期（秒），默认1天 |
| `lazy_load_skills.priority_boost_threshold` | `5` | 使用多少次后提升优先级 |
| `lazy_load_skills.cleanup_interval` | `2592000` | 过期技能清理间隔（秒），默认30天 |
| `auto_fusion.enabled` | `true` | 是否启用自动融合引擎 |
| `auto_fusion.scan_on_startup` | `true` | 启动时是否扫描新特性 |
| `auto_fusion.auto_fuse_priority` | `["P0", "P1"]` | 自动融合的优先级范围 |
| `auto_fusion.require_confirm_for` | `["P2"]` | 需要用户确认的优先级 |

---

## 11. 文件结构

### 11.1 完整目录树

```
phoenix/
├── phoenix.py                          # 主入口文件，启动Phoenix系统
├── config.json                         # 全局配置文件（路由、模型、安全等）
├── feature_registry.json               # 特性注册表（78个能力的融合状态）
├── feature_fusion.py                   # 特性融合引擎（手动融合）
├── auto_fusion.py                      # 自动融合引擎（自动扫描+融合）
│
├── core/                               # ① Core 配置中心
│   ├── __init__.py
│   ├── config.py                       # PhoenixConfig 全局配置类
│   ├── state.py                        # AppState 状态管理类
│   ├── task_manager.py                 # TaskManager 任务调度类
│   ├── credit_monitor.py               # CreditMonitor 信用监控
│   └── skill_reviewer.py               # SkillReviewer 技能审稿
│
├── router/                             # ② Router 路由引擎
│   ├── __init__.py
│   ├── engine.py                       # RouterEngine 路由主引擎
│   ├── intent_classifier.py            # IntentClassifier 意图分类器
│   ├── query_complexity.py             # ComplexityEstimator 复杂度评估
│   ├── gatekeeper.py                   # Gatekeeper 准入控制
│   ├── llm_classifier.py               # LLMClassifier LLM意图分类
│   ├── subagent_router.py              # SubAgentRouter 子Agent路由
│   └── model_registry.py               # ModelRegistry 模型注册表
│
├── executor/                           # ③ Executor 执行管道
│   ├── __init__.py
│   ├── pipeline.py                     # ExecutorPipeline 主执行管道
│   ├── circuit_breaker.py              # CircuitBreaker 熔断器
│   ├── response_cache.py               # ResponseCache 响应缓存
│   ├── skill_loader.py                 # SkillLoader 技能加载器
│   ├── deep_compact.py                 # DeepCompact 深度压缩
│   ├── micro_compact.py                # MicroCompact 微压缩
│   ├── task_decomposer.py              # TaskDecomposer 任务分解器
│   ├── parallel_executor.py            # ParallelSubAgentExecutor 并行执行
│   ├── progressive_loader.py           # ProgressiveSkillLoader 渐进式加载
│   └── deferred_tool_loader.py         # DeferredToolLoader 延迟工具加载
│
├── memory/                             # ④ Memory 记忆系统
│   ├── __init__.py
│   ├── memory_system.py                # MemorySystem 主记忆系统
│   ├── structured_memory.py            # StructuredMemory 结构化记忆
│   ├── auto_extract.py                 # AutoExtract 自动事实提取
│   ├── memory_refiner.py               # MemoryRefiner 记忆精炼器
│   └── diary.py                        # Diary 日记系统
│
├── self_heal/                          # ④ Self-heal 自愈系统
│   ├── __init__.py
│   ├── antibody.py                     # AntibodyLibrary 抗体库
│   ├── error_processor.py              # ErrorProcessor 错误处理器（10步排查）
│   ├── fault_playbook.py               # FaultPlaybook 故障处理卡
│   ├── evolution.py                    # EvolutionEngine 进化引擎
│   └── skill_crystallizer.py           # SkillCrystallizer 技能结晶器
│
├── integration/                        # ⑤ Integration 集成桥梁
│   ├── __init__.py
│   ├── gateway_api.py                  # GatewayAPI 统一网关（单例模式）
│   ├── hooks.py                        # HookSystem 钩子系统
│   ├── cron_sync.py                    # CronSync 定时同步
│   ├── startup.py                      # StartupManager 启动管理
│   └── hermes_bridge.py                # HermesBridge Hermes桥接器
│
├── security/                           # ⑥ Security 安全防护
│   ├── __init__.py
│   ├── approval.py                     # ApprovalSystem 审批系统（P0/P1/P2三级）
│   ├── permission_system.py            # PermissionSystem 权限管理
│   ├── token_tracker.py                # TokenTracker Token消耗追踪
│   └── guardrail_middleware.py         # GuardrailMiddleware 中间件护栏
│
├── adapt/                              # ⑦ Adapt 自动适配
│   ├── __init__.py
│   ├── adapter.py                      # HermesAdapter Hermes适配器
│   ├── scanner.py                      # HermesScanner 版本扫描器
│   ├── compat_report.py                # CompatReport 兼容性报告
│   └── run.py                          # AdaptRunner 适配运行器
│
├── sandbox/                            # ⑧ Sandbox 沙箱
│   ├── __init__.py
│   ├── manager.py                      # SandboxManager Docker容器管理
│   └── executor.py                     # SandboxExecutor 沙箱代码执行
│
├── workflow/                           # ⑨ Workflow 工作流
│   ├── __init__.py
│   ├── engine.py                       # WorkflowEngine 工作流引擎
│   └── step.py                         # WorkflowStep 工作流步骤定义
│
├── github/                             # ⑩ GitHub 集成
│   ├── __init__.py
│   └── client.py                       # GitHubClient GitHub客户端
│
├── skills/                             # 技能目录（动态加载）
│   ├── __init__.py
│   ├── skill_001.py                    # 用户创建/自动生成的技能
│   ├── skill_002.py
│   └── ...
│
├── tests/                              # 测试目录
│   ├── __init__.py
│   ├── test_router.py                  # 路由引擎测试
│   ├── test_executor.py                # 执行管道测试
│   ├── test_memory.py                  # 记忆系统测试
│   ├── test_self_heal.py               # 自愈系统测试
│   └── ...
│
├── plugins/                            # 插件目录
│   ├── phoenix-full/                   # Phoenix完整功能插件
│   │   └── plugin.json
│   ├── disk-cleanup/                   # 磁盘清理插件
│   │   └── plugin.json
│   ├── kanban/                         # 看板管理插件
│   │   └── plugin.json
│   └── observability/                  # 可观测性插件
│       └── plugin.json
│
└── data/                               # 数据目录
    ├── facts.json                      # 事实记忆存储
    ├── structured_memory.json          # 结构化记忆存储
    ├── memory.db                       # 长期记忆SQLite数据库
    └── cache/                          # 缓存目录
        ├── response_cache.json         # 响应缓存
        ├── skill_cache.json            # 技能缓存
        └── model_info_cache.json       # 模型信息缓存
```

### 11.2 关键文件说明

| 文件 | 行数（估算） | 核心职责 |
|------|------------|---------|
| `phoenix.py` | ~200 | 系统入口，初始化所有模块，启动主循环 |
| `config.json` | ~150 | 全局配置，包含路由/模型/安全/记忆/插件等所有参数 |
| `feature_registry.json` | ~500 | 78个能力的注册表，记录每个能力的融合状态和元数据 |
| `auto_fusion.py` | ~300 | 自动融合引擎，扫描→对比→决策→融合→验证→报告 |
| `core/credit_monitor.py` | ~200 | 信用监控，实时检测API余额，自动切换兜底模型 |
| `core/skill_reviewer.py` | ~250 | 技能审稿，评分+查重+截断+报告 |
| `router/engine.py` | ~400 | 路由主引擎，协调整个路由决策流程 |
| `executor/pipeline.py` | ~500 | 执行管道主文件，8阶段流水线编排 |
| `executor/parallel_executor.py` | ~300 | 并行执行器，ThreadPoolExecutor多子Agent并行 |
| `memory/structured_memory.py` | ~250 | 结构化记忆，UserContext/HistoryContext/Facts |
| `self_heal/antibody.py` | ~350 | 抗体库，8个内置抗体+动态生成新抗体 |
| `self_heal/error_processor.py` | ~400 | 错误处理器，10步系统化排查法 |
| `self_heal/evolution.py` | ~250 | 进化引擎，分析历史案例，优化抗体库 |
| `security/guardrail_middleware.py` | ~200 | 中间件护栏，3个内置安全检查 |

**意义：** 文件结构体现了Phoenix的模块化设计理念——每个功能都是独立的模块，每个模块都有清晰的职责边界。

---
