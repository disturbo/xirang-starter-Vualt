---
tags: [巡检, V9, 第一反射器]
title: "V9 第一反射器巡检目录"
created: 2026-06-26
owner: Codex
---

# V9 第一反射器巡检目录

> 本目录是 V9 第一反射器的唯一输出区。巡检脚本只写本目录，绝不自动写看板、运行日志或模块文档。

## 部署状态

Starter Vault 默认**不安装** launchd。你可以手动运行巡检，也可以按下方模板启用 macOS 定时任务。

| 项 | 状态 |
|---|---|
| 聚合脚本 | `02-项目管理/脚本/v9-reflex-check.py` |
| 触发方式 | 手动运行；可选 launchd 常驻 |
| 最近快照 | 见 `health-latest.json` 的 `generated_at` |
| 自动写入边界 | 只写 `02-项目管理/巡检/` |

## 文件说明

| 文件 | 用途 | 是否人工编辑 |
|------|------|:---:|
| `health-latest.json` | 最近一次巡检快照（统一 severity schema） | 否，脚本覆盖 |
| `reflex-state.json` | 去重/冷却状态（每个幂等键的 first_seen/last_reported/count） | 否，脚本维护 |

`health-latest.json` 的自省字段：

| 字段 | 含义 |
|------|------|
| `sources_run` | 每个巡检源的 `{source,status,findings}`，用于区分"跑了且干净"和"没跑" |
| `sources_ok` | status 为 `ok` 的源数量 |
| `sources_failed` | status 非 `ok` 的源名列表；非空时说明巡检未看全 |

## 巡检源

1. `project-ops-check.py --json`（任务卡 + 运行日志）
2. `agent-state-lint.py --json`（Agent 状态 schema）
3. 内置 heartbeat 检查（status=busy 但 last_heartbeat 超时）
4. `v9-policy-conflict-check.py --json`（规范管辖权索引 + 冲突扫描）

severity 统一为 `p0 / p1 / advisory`。冷却默认 24h，心跳阈值默认 24h。

## 会话启动 checklist

会话开始时读 `health-latest.json`：

- `sources_failed` 非空 → 先修巡检源；这是假静默风险，不按"健康"处理。
- `summary.active > 0` 且含 p0/p1 → 报告项目 owner，确认后才登记看板/指派 owner。
- `summary.active=0` 且 `sources_failed=[]` → 真静默，不打扰。

## 手动巡检

```bash
python3 02-项目管理/脚本/v9-reflex-check.py
python3 02-项目管理/脚本/v9-reflex-check.py --quiet
python3 02-项目管理/脚本/v9-reflex-check.py --strict
```

## 可选：launchd 常驻

macOS 可用 launchd 每天定时刷新快照。请把 `WorkingDirectory` 改成你的 Vault 路径。

```bash
cat > ~/Library/LaunchAgents/com.xirang.v9reflex.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.xirang.v9reflex</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>02-项目管理/脚本/v9-reflex-check.py</string>
    <string>--quiet</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOUR_NAME/Desktop/obsidianVault</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>17</integer></dict>
</dict></plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.xirang.v9reflex.plist
```
