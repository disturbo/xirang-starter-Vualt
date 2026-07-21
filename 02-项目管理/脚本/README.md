---
title: 项目管理脚本索引
type: index
created: 2026-06-09
updated: 2026-07-19
---

# 项目管理脚本索引

> 这个目录只放项目治理脚本，不放业务文档。

| 脚本 | 用途 | 常用命令 |
|---|---|---|
| `vault-health-check.py` | Vault 孤立文件、模块状态、日期新鲜度、同名文件、模块编号唯一性、关键入口链接可解析性、敏感值检查 | `python3 02-项目管理/脚本/vault-health-check.py` |
| `project-ops-check.py` | 任务卡与运行日志守门：检查最近是否建卡、今日是否有日志、任务卡字段是否齐 | `python3 02-项目管理/脚本/project-ops-check.py` |
| `v9-reflex-check.py` | V9 第一反射器聚合器：九源巡检、运行时消费链与新鲜度检查 | `python3 02-项目管理/脚本/v9-reflex-check.py` |
| `v9-policy-conflict-check.py` | 规范管辖权与冲突扫描 | `python3 02-项目管理/脚本/v9-policy-conflict-check.py --json` |
| `v9-starter-leak-check.py` | starter 分发泄漏扫描 | `python3 02-项目管理/脚本/v9-starter-leak-check.py --json` |
| `v9-task-state-check.py` | V9.4 任务验收状态只读扫描，检查 self-accept、submitted_at、changes_requested note 等问题 | `python3 02-项目管理/脚本/v9-task-state-check.py --json` |
| `v9-scope-tamper-check.py` | V9.4.1 write_scope 越权事后检测，比较状态文件实际 scope 与任务卡授权范围 | `python3 02-项目管理/脚本/v9-scope-tamper-check.py --json` |
| `v9-handoff-check.py` | V9.4.2 Handoff 可接手性只读扫描，检查显式 handoff 任务是否含状态、产物、验证、next action | `python3 02-项目管理/脚本/v9-handoff-check.py --json` |
| `v9-harness-eval-runner.py` | V9.4 harness 机械回归测试，含 positive/negative fixture | `python3 02-项目管理/脚本/v9-harness-eval-runner.py` |
| `v9-skill-shadow-check.py` | 扫描跨平台同名 skill 的版本/内容遮蔽；默认包含 OpenClaw workspace | `python3 02-项目管理/脚本/v9-skill-shadow-check.py --json` |
| `v9-freeze-observation.py` | 按日维护连续观察证据，缺日或结构性失败会打断 streak | `python3 02-项目管理/脚本/v9-freeze-observation.py --json` |
| `v9-status-summary.py` | 从当前 health 真相源生成状态汇总，避免读取旧 runtime | `python3 02-项目管理/脚本/v9-status-summary.py` |

## 相关 V9 命令

| 命令 | 用途 | 常用命令 |
|---|---|---|
| `.standards/v9-accept.py` / `v9_accept` | 安全验收任务卡：候选 accepted → pre-accept 门禁 → 原子写回 | `v9_accept T-xxxx 人工Reviewer` |
| `.standards/agent-cost-events.py` | **已退役（2026-07-19）**：仅保留隔离期审计，不再进入运行链 | 不得用于当前任务完成条件 |
| `.standards/hooks/pre-commit-harness-eval.sh` | git pre-commit：当 staged 文件触达 `.standards/` 或 `02-项目管理/脚本/` 时自动跑 harness eval；中文路径用 `core.quotepath=false` 识别 | `.git/hooks/pre-commit` 自动调用 |

## 使用规则

- 每轮 M4/M5 任务开工前先跑 `project-ops-check.py`。
- V9 harness 改动后跑 `v9-harness-eval-runner.py`，看 `blocked_negative` 而不只看总通过数。
- 验收任务卡用 `v9_accept`，不要裸手改 `review_status: accepted`。
- M5 或显式 `handoff_required: true` 的任务收工前跑 `v9-handoff-check.py --task <任务卡> --json`。
- starter 发布前必须跑 `v9-starter-leak-check.py --root <starter> --json --strict`。
- 成本治理已退役；历史事件只作隔离审计，不得重新接回任务完成条件或健康状态。
- 大批量整理后跑 `vault-health-check.py`。
- 入口页/索引页出现断链或 `\|` 误写时，先修活跃入口；模板、示例和历史归档断链另开专题处理。
- 账号、密码、VPN、Key 等只放 `.private/` 或密码管理工具，不进入 Git 跟踪文件。
- 发现历史缺口时只记录缺口，不无依据补写历史日志。
