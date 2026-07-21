---
tags: [巡检, V9, 第一反射器]
title: "V9 第一反射器巡检目录"
created: 2026-06-25
owner: Claudian
---

# V9 第一反射器巡检目录

## 部署状态

| 项 | 状态 |
|---|---|
| launchd 服务 | Starter 默认不安装；部署后以 `launchctl print gui/$UID/com.xirang.v9reflex` 实测为准 |
| 触发频率 | 推荐登录即运行 + 每 30 分钟（`RunAtLoad` + `StartInterval=1800`） |
| 最近快照 | 以 Vault 外 `~/.xirang/v9-runtime/巡检/status-latest.json` 为唯一状态入口 |
| Harness | wrapper 在每轮反射前验证新鲜度和当前哈希；无效时自动重跑，失败时保持红色原因 |

> 注意：本文件是分发模板，不代表当前机器已经部署。只有调度器、运行时快照和消费者三者均有新鲜证据，才能报告常驻生效。

---

> 本目录是 V9 第一反射器 MVP 的**唯一输出区**。
> 防污染硬约束（对标 6-18 反思 Hindsight `_converted` 污染教训）：
> 巡检脚本只写本目录，**绝不自动写看板 / 运行日志 / 模块文档**。
> 正式看板/日志的提升，由会话启动 checklist 人工确认后进行。

## 文件说明

| 文件 | 用途 | 是否人工编辑 |
|------|------|:---:|
| `health-latest.json` | 最近一次巡检快照（统一 severity schema） | 否，脚本覆盖 |
| `reflex-state.json` | 去重/冷却状态（每个幂等键的 first_seen/last_reported/count） | 否，脚本维护 |
| `harness-eval-latest.json` | 最近一次 Harness 机械回归结果；由 wrapper 在过期或哈希漂移时自动刷新 | 否，脚本覆盖 |

`health-latest.json` 的自省字段：

| 字段 | 含义 |
|------|------|
| `sources_run` | 每个巡检源的 `{source,status,findings}`，用于区分"跑了且干净"和"没跑" |
| `sources_ok` | status 为 `ok` 的源数量 |
| `sources_failed` | status 非 `ok` 的源名列表；非空时说明巡检未看全 |

## 巡检脚本

`02-项目管理/脚本/v9-reflex-check.py` — 聚合器，汇集十源：

1. `project-ops-check.py --json`（任务卡 + 运行日志）
2. `agent-state-lint.py --json`（Agent 状态 schema）
3. 内置 heartbeat 检查（status=busy 但 last_heartbeat 超时）
4. `v9-policy-conflict-check.py --json`（规范管辖权索引 + 冲突扫描）
5. `v9-starter-leak-check.py --json`（starter 分发泄漏扫描）
6. `v9-task-state-check.py --json`（任务验收状态扫描）
7. `v9-scope-tamper-check.py --json`（write_scope Bash 旁路扩权扫描）
8. `v9-handoff-check.py --json`（Handoff 可接手性扫描）
9. `v9-iteration-ops-check.py --json`（迭代结构与证据链扫描）
10. `.standards/frontmatter-lint.py --all --json`（全库结构债务聚合）

独立 runtime checks 还验证 GBrain/LLM Wiki、SessionStart/任务握手的 `semantic_recall` 消费证据、熵 backlog、Skills 解析唯一性、Harness 当前哈希与冻结观察。`deferred` 熵项仍计入未解决积压。

severity 统一为 `p0 / p1 / advisory`。冷却默认 24h，心跳阈值默认 24h。

## 第二阶段扩展

| 动作 | 状态 | 说明 |
|---|---|---|
| C. 规范冲突扫描 | 已接入 | `V9-规范管辖权索引-2026-06-25.md` 定义 primary/supporting/inactive；扫描结果进入 `health-latest.json` |
| D. 成本仪表盘口径 | 已升级 | 成本周报区分 `usage_only` / `estimated` / `connected`；无平台 `cost_source` 时 CNY 不当账单 |
| V9.4 starter 泄漏扫描 | 已接入 | 只读扫描 `XIRANG_STARTER_ROOT` 或兄弟目录 `../xi-rang-v9-starter`；命中个人/项目/秘钥痕迹进入 `health-latest.json` |
| V9.4 任务状态扫描 | 已接入 | 只读扫描任务卡验收状态；硬查 self-accept、缺验收人、submitted_at、changes_requested note 等问题 |
| V9.4.1 scope tamper 扫描 | 已接入 | 只读比较 Agent 状态文件 `write_scope` 与当前任务卡授权范围；发现 Bash 旁路扩权进入 `health-latest.json` |
| V9.4.2 Handoff 扫描 | 已接入 | 只读检查显式 `handoff_required: true` 与新 M5 done 任务的交接块是否可接手；历史债默认不打红 |

## 两层触发（方案 v2.0 第 4.3 节）

### A 层：launchd 常驻（只写 JSON，不碰正式看板）

部署步骤（交人工Reviewer本机执行，**本任务不自动安装**）：

```bash
# 1. 在 Vault 根目录执行；把 wrapper 放到不受 Desktop TCC 影响的稳定目录
VAULT_ROOT="$(pwd -P)"
mkdir -p "$HOME/.xirang/bin" "$HOME/.xirang/v9-runtime/巡检" "$HOME/.xirang/logs"
cp "$VAULT_ROOT/.standards/v9-reflex-run.sh" "$HOME/.xirang/bin/v9-reflex-run.sh"
chmod +x "$HOME/.xirang/bin/v9-reflex-run.sh"

# 2. 创建 plist；环境变量明确绑定本机 Vault 和唯一 runtime
cat > "$HOME/Library/LaunchAgents/com.xirang.v9reflex.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.xirang.v9reflex</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HOME/.xirang/bin/v9-reflex-run.sh</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>XIRANG_V9_VAULT_DIR</key><string>$VAULT_ROOT</string>
    <key>XIRANG_V9_RUNTIME_DIR</key><string>$HOME/.xirang/v9-runtime</string>
  </dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>$HOME/.xirang/logs/v9-reflex.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.xirang/logs/v9-reflex.err.log</string>
</dict></plist>
PLIST

# 3. 加载并验证；不是 exit 0 就不能声明已部署
launchctl bootout "gui/$UID/com.xirang.v9reflex" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.xirang.v9reflex.plist"
launchctl kickstart -k "gui/$UID/com.xirang.v9reflex"
launchctl print "gui/$UID/com.xirang.v9reflex"
```

### C 层：会话启动 checklist（解读 + 人工提升）

会话开始时读 `health-latest.json`：
- `sources_failed` 非空 → 先报告/修复巡检源；这是假静默风险，不按"健康"处理。
- `summary.active > 0` 且含 p0/p1 → 向人工Reviewer报告，确认后才登记看板/指派 owner。
- `summary.active=0` 且 `sources_failed=[]` → 真静默，不打扰。

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
