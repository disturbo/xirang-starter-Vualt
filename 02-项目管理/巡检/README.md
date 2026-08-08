---
tags: [巡检, V9, 第一反射器]
title: "V9 第一反射器运行态迁移说明"
type: 运行时说明
created: 2026-06-25
owner: Claudian
---

# V9 第一反射器运行态迁移说明

> 本 Vault 是成果文件管理区，不保存巡检快照、截图、日志等过程文件。2026-07-18 起，V9 运行态输出固定迁移到 Vault 外、且不受 macOS Desktop 权限约束的 `~/.xirang/v9-runtime/巡检/`。

## 运行态位置

| 项 | 当前口径 |
|---|---|
| 默认 runtime | `~/.xirang/v9-runtime/巡检/` |
| 覆盖整个 runtime 根 | `XIRANG_V9_RUNTIME_DIR=/path/to/runtime` |
| 覆盖巡检输出目录 | `XIRANG_V9_INSPECT_DIR=/path/to/inspect` |
| Vault 内本目录 | 仅保留说明，不作为脚本默认输出区 |

## 部署状态

| 项 | 状态 |
|---|---|
| launchd 服务 | `com.xirang.v9reflex` **已部署并加载**，采用一次性巡检，不使用无界 `KeepAlive` |
| 触发频率 | RunAtLoad + 每 30 分钟 + 每日 09:17 |
| 最近快照 | 见运行态目录 `health-latest.json` 的 `generated_at` |
| Codex 复核 | 聚合器 + 加固补丁 + scope bugfix 均通过，无阻断 finding |

> 注意：反射器已进入**自动周期巡检**状态，`health-latest.json` 由 launchd 定时刷新；输出固定指向 Vault 外 runtime。
> 会话启动 checklist 直接读最新快照即可，无需手动跑（手动跑用于即时复检）。

---

> 防污染硬约束（对标 6-18 反思 Hindsight `_converted` 污染教训）：
> 巡检脚本只写 Vault 外 runtime，**不写 Vault 内过程文件，也绝不自动写看板 / 运行日志 / 模块文档**。
> 正式看板/日志的提升，由会话启动 checklist 人工确认后进行。

## 文件说明

| 文件 | 用途 | 是否人工编辑 |
|------|------|:---:|
| `health-latest.json` | 最近一次巡检快照（统一 severity schema） | Vault 外 runtime |
| `reflex-state.json` | 去重/冷却状态（每个幂等键的 first_seen/last_reported/count） | Vault 外 runtime |
| `harness-eval-latest.json` | 最近一次 V9.4 harness 机械回归测试结果（手动 `--write-latest` 生成） | Vault 外 runtime |

`health-latest.json` 的自省字段：

| 字段 | 含义 |
|------|------|
| `sources_run` | 每个巡检源的 `{source,status,findings}`，用于区分"跑了且干净"和"没跑" |
| `sources_ok` | status 为 `ok` 的源数量 |
| `sources_failed` | status 非 `ok` 的源名列表；非空时说明巡检未看全 |

## 巡检脚本

`02-项目管理/脚本/v9-reflex-check.py` — 聚合器，汇集九源：

1. `project-ops-check.py --json`（任务卡 + 运行日志）
2. `agent-state-lint.py --json`（Agent 状态 schema）
3. 内置 heartbeat 检查（status=busy 但 last_heartbeat 超时）
4. `v9-policy-conflict-check.py --json`（规范管辖权索引 + 冲突扫描）
5. `v9-starter-leak-check.py --json`（starter 分发泄漏扫描）
6. `v9-task-state-check.py --json`（任务验收状态扫描）
7. `v9-scope-tamper-check.py --json`（write_scope Bash 旁路扩权扫描）
8. `v9-handoff-check.py --json`（Handoff 可接手性扫描）
9. `v9-iteration-ops-check.py --json`（月度迭代 Ops 结构扫描）

severity 统一为 `p0 / p1 / advisory`。冷却默认 24h，心跳阈值默认 24h。

## 第二阶段扩展

| 动作 | 状态 | 说明 |
|---|---|---|
| C. 规范冲突扫描 | 已接入 | `V9-规范管辖权索引-2026-06-25.md` 定义 primary/supporting/inactive；扫描结果进入 `health-latest.json` |
| D. 成本 token/model 遥测 | 已退役 | 2026-07-19 起不采集、不估算、不熔断，也不参与 health/status；历史脚本仅作隔离期回滚证据 |
| Phoenix 自愈 | 设计态 | 当前无 scheduler/executor/state/log；反射器显示 `design`，错误链跳过 L1，直接进入 Owner 修复 |
| LLM Wiki 725 | 已接入 | 精确校验 29 模块，当前 0 error / 0 warning；缺页必须显式标记未落地 |
| 14 天冻结探针 | 已接入 | 身份、Harness、Hook、消费顺序、分发版本连续观测；当前 1/14，禁止提前解冻 |
| V9.4 starter 泄漏扫描 | 已接入 | 只读扫描 `XIRANG_STARTER_ROOT` 或兄弟目录 `../xi-rang-v9-starter`；命中个人/项目/秘钥痕迹进入 `health-latest.json` |
| V9.4 任务状态扫描 | 已接入 | 只读扫描任务卡验收状态；硬查 self-accept、缺验收人、submitted_at、changes_requested note 等问题 |
| V9.4.1 scope tamper 扫描 | 已接入 | 只读比较 Agent 状态文件 `write_scope` 与当前任务卡授权范围；发现 Bash 旁路扩权进入 `health-latest.json` |
| V9.4.2 Handoff 扫描 | 已接入 | 只读检查显式 `handoff_required: true` 与新 M5 done 任务的交接块是否可接手；历史债默认不打红 |

## 两层触发（方案 v2.0 第 4.3 节）

### A 层：launchd 单一调度所有者（只写 JSON，不调用模型）

> 2026-07-18 Phase D 校准：`com.xirang.v9reflex` 已恢复并成为唯一生产调度器。Hermes job `bdc10d963088` 保持 `enabled=false/state=paused`，旧 wrapper 已显式退役；OpenClaw V8 watchdog 已从 crontab 移除。

当前生产配置：

```text
label: com.xirang.v9reflex
schedule: RunAtLoad + every 1800s + daily 09:17
runner: ~/.xirang/bin/v9-reflex-run.sh
runtime: ~/.xirang/v9-runtime/巡检
```

runner 在同一事务内按顺序刷新 `health-latest.json` 与 `status-latest.json`，随后验证路径、生成时间、来源顺序和最终状态。Harness badge 还会核对 19 文件 trust set 的当前 hash，不能只凭旧报告 `failed=0` 变绿。

验证命令：

```bash
launchctl print "gui/$(id -u)/com.xirang.v9reflex"
launchctl kickstart -k "gui/$(id -u)/com.xirang.v9reflex"
python3 .standards/harness-eval-verify.py --report ~/.xirang/v9-runtime/巡检/harness-eval-latest.json --json
```

回滚与旧入口证据保存在 `~/.xirang/rollback/v9-recovery-20260718-phase-d/`。`com.xirang.v9reflex.plist.pre-20260717-python-pin.bak` 指向旧 Desktop runtime，只能作为历史证据，不得直接加载。

### C 层：会话启动 checklist（解读 + 人工提升）

会话开始时读 Vault 外 runtime 的 `health-latest.json`：
- 文件不存在或 mtime 超过 26 小时 → 先修调度链；`session-guard.sh` 会独立告警。
- `sources_failed` 非空 → 先报告/修复巡检源；这是假静默风险，不按"健康"处理。
- `summary.active > 0` 且含 p0/p1 → 向用户报告，确认后才登记看板/指派 owner。
- `summary.active=0` 只表示当前没有需要重复通知的信号，不能表示无问题；健康 badge 始终按 `summary.p0/p1/advisory` 全量未解决数判断。
- 只有 `summary.total=0`、`sources_failed=[]` 且快照新鲜，才是真正绿灯。

> 2026-07-17 语义校准：cooldown 仅控制通知频率，不得改变健康事实。被 suppressed 的未解决 P1 仍使反射器 badge 保持 red。

## 手动巡检

```bash
python3 02-项目管理/脚本/v9-reflex-check.py            # 打印 + 写快照
python3 02-项目管理/脚本/v9-reflex-check.py --quiet    # 仅写文件
python3 02-项目管理/脚本/v9-reflex-check.py --strict   # 有 active 时退出码 1
python3 02-项目管理/脚本/v9-task-state-check.py --json # 任务验收状态只读扫描
python3 02-项目管理/脚本/v9-scope-tamper-check.py --json # write_scope 扩权只读扫描
python3 02-项目管理/脚本/v9-handoff-check.py --json # Handoff 可接手性只读扫描
```

## 手动自测

```bash
python3 02-项目管理/脚本/v9-harness-eval-runner.py
python3 02-项目管理/脚本/v9-harness-eval-runner.py --write-latest
```

重点看两个数：
- `passed_positive`: 干净样本是否保持绿灯。
- `blocked_negative`: 坏样本是否被成功拦住；这比单纯 `passed=N/N` 更重要。
