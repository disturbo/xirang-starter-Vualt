---
title: 项目管理脚本索引
type: index
created: 2026-06-26
updated: 2026-06-26
---

# 项目管理脚本索引

> 这个目录只放项目治理脚本，不放业务文档。

| 脚本 | 用途 | 常用命令 |
|---|---|---|
| `project-ops-check.py` | 任务卡与运行日志守门：检查最近是否建卡、今日是否有日志、任务卡字段是否齐；支持 `--json` | `python3 02-项目管理/脚本/project-ops-check.py --json` |
| `v9-reflex-check.py` | V9 第一反射器聚合器：任务卡/状态/心跳/规范冲突四源巡检，写入 `health-latest.json` | `python3 02-项目管理/脚本/v9-reflex-check.py` |
| `v9-policy-conflict-check.py` | 规范管辖权冲突扫描：检查 primary/supporting/inactive 关系 | `python3 02-项目管理/脚本/v9-policy-conflict-check.py --json` |

## 使用规则

- 每轮 M4/M5 任务开工前先看 `02-项目管理/巡检/health-latest.json`；需要即时复检时跑 `v9-reflex-check.py`。
- 入口页/索引页出现断链或 `\|` 误写时，先修活跃入口；模板、示例和历史归档断链另开专题处理。
- 账号、密码、VPN、Key 等只放 `.private/` 或密码管理工具，不进入 Git 跟踪文件。
- 发现历史缺口时只记录缺口，不无依据补写历史日志。
