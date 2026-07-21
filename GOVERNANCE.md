# 息壤 V9 运行真实性治理基线

> 适用于 starter 的通用治理契约。它不携带任何生产 Vault 的任务、巡检快照或验收结论。

## 1. 绿色不是退出码

模块只有同时具备以下证据，才允许标记为 `green`：

```text
调度者 → 有效输入 → 可验证输出 → 明确消费者
      → 可观察行为效果 → 新鲜度证明 → 责任人与失败出口
```

脚本存在、任务定时运行、报告生成或面板显示绿色，都只能证明局部事实。

统一状态：

| 状态 | 含义 |
|---|---|
| `design` | 只有设计或文档，没有执行面 |
| `retired` | 已退役，且不存在活跃 writer、scheduler 或 consumer |
| `failed` | 关键证据缺失或验证失败 |
| `observing` | 已通过当前检查，正在积累连续证据 |
| `green` | 证据完整并通过连续观察门禁 |

## 2. 部署后的冻结观察

新安装或运行链修复后，默认进入 14 个连续日历日的 `observing`：

1. 每日保留 health、Harness 哈希、Hook canary、身份归属、知识索引新鲜度和 Skills 完整根目录扫描证据；
2. 快照不得只覆盖 `latest`，必须能够复算 streak；
3. 未登记的状态写入者、错误绿色、身份误记、哈希过期、扫描根目录遗漏会立即归零 streak；
4. 外部服务短时抖动若在两个检查周期内恢复，且没有错误绿色、证据丢失或行为影响，可独立复核为 transient；
5. 观察最多延长到 28 天。届时仍不通过，应缩小承诺、退役模块或重新设计，不能无限冻结。

## 3. 独立验收

`submitted` 不等于 `accepted`。accepted 必须由非作者、非执行者复核，并至少记录：

- task id、reviewer、accepted time；
- 产物哈希与验证报告；
- 行为效果证据；
- 结论和治理规则版本。

禁止批量补写虚假历史验收，也禁止用“字段填写率”替代真实验收比例。

## 4. 默认治理边界

- Phoenix 仅为 design/reference，不包含 executor 或 scheduler；
- 成本治理已退役，不得接回任务完成条件或健康状态；
- 熵候选先分类并小批次处置；`deferred` 仍是未解决 backlog，不得自动归档成已收敛；
- GBrain 自动召回已接入 SessionStart 与 M4/M5 握手；它不是任务启动的强依赖，超时/无结果时 fail-open，但必须留下失败事件并使健康状态转红；
- Skills 检查必须覆盖真实平台入口，包括 `~/.openclaw/workspace/skills`；
- starter 不分发运行期快照、个人 Agent 状态、生产任务卡、审计报告、prompt 构建缓存或业务候选产物。

## 5. 最小验证命令

```bash
python3 02-项目管理/脚本/v9-harness-eval-runner.py
python3 .standards/harness-eval-verify.py \
  --report 02-项目管理/巡检/harness-eval-latest.json \
  --root . --json
python3 02-项目管理/脚本/v9-skill-shadow-check.py --json
python3 02-项目管理/脚本/v9-starter-leak-check.py --root . --strict
```

Harness 必须同时满足：全部 positive 通过、全部 negative 被阻断、meta failure 为 0、trust set 文件哈希全部与当前仓库一致。
