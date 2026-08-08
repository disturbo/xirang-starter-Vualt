---
tags: [方法论, V9, CodeRef, 代码反射, 影响分析, advisory]
title: "息壤 V9 CodeRef 代码反射层"
type: technical_design
version: v1.1
status: implemented_workspace
created: 2026-07-24
updated: 2026-07-24
owner: Codex
related:
  - "[[息壤方法论-V9]]"
  - "[[V9-工具注册表-2026-06-27]]"
  - "[[息壤V9-Gate与Hook机制]]"
---

# 息壤 V9 CodeRef 代码反射层

## 1. 定位

CodeRef 是息壤 V9 的**只读代码与编排关系索引**。它解决的不是全文检索，而是：

- 某个 Checker 被哪些 Gate、Hook 或测试调用；
- 修改一个脚本会影响哪些上游入口；
- Schema 由哪个 Checker 校验；
- runtime latest 文件由谁生产、被谁消费；
- 工具在哪个注册表登记、由哪个 Harness 验证。

CodeRef 不是新的知识图谱，也不是权威数据源。`graph.json`、`cache.json` 和 `status.json` 都是可删除重建的派生缓存。

自 v1.1 起，`$HOME/Desktop/沙箱` 是统一工作区入口。它只负责项目发现与聚合，不被初始化为单体 Git 仓库；各项目继续保持独立版本、缓存和单一事实源。

## 2. 为什么不直接采用 claude-graph

息壤运行时主要是编排模式：

```text
Shell Hook
  └── Python Gate
        └── subprocess + 脚本路径
              └── Python Checker
```

通用代码图擅长库式 `import + function call`，但无法仅靠 AST 恢复 subprocess 字符串、Shell 命令、Hook 注册、Schema 与 runtime 数据契约。CodeRef 因此借鉴内容哈希和稳定图 ID，但关系模型以息壤运行事实为主体。

## 3. 边界

CodeRef 必须遵守：

1. **只读扫描**：不得执行被扫描的 Python/Shell 文件。
2. **Vault 外缓存**：默认写入 `~/.xirang/v9-runtime/code-ref/`。
3. **Advisory**：不得进入 Gate、Hook、验收正门或反射器健康判定。
4. **可重建**：删除整个 `code-ref/` 后重新 `build` 即可恢复。
5. **不替代 GBrain**：GBrain 负责语义检索，CodeRef 负责工程关系。
6. **不替代注册表**：声明配置和工具注册表仍是真相源，图只是投影。

## 4. 产物

```text
~/.xirang/v9-runtime/code-ref/
├── graph.json   # 节点、边、证据与统计
├── cache.json   # 文件 SHA-256 与抽取片段
├── status.json  # 单仓 changed/reused/deleted 与路径
└── workspaces/
    └── desktop-sandbox/
        ├── workspace-graph.json
        ├── workspace-status.json
        └── projects/<project-id>/{graph,cache,status}.json
```

环境覆盖：

```bash
export XIRANG_V9_RUNTIME_DIR=/custom/runtime
```

也可通过 `--output-dir` 显式指定隔离目录。

## 5. 关系模型

| 关系 | 方向 | 来源 |
|---|---|---|
| `imports` | Script → Module | Python AST；JS/TS 的本地 import、re-export、require、dynamic import |
| `invokes` | Caller → Script | Python subprocess/helper、Shell python/bash |
| `sources` | Shell → Shell | Shell `source` |
| `consumes` | Consumer → Data/Schema | Python 文件读取 |
| `produces` | Producer → Data | Python 文件写入或声明关系 |
| `consumed_by` | Data → Consumer | 声明关系 |
| `validated_by` | Schema → Checker | Schema 读取或声明关系 |
| `registered_in` | Tool → Registry | V9 工具注册表、声明关系 |
| `verified_by` | Subject → Harness | `.standards/harness-tested-files.txt` |

无法安全推断的关系写入：

```text
.standards/coderef-relations.json
```

每条声明边必须能回到已有代码、配置或运行契约，不允许为了图好看而杜撰关系。

## 6. 增量与稳定性

每个扫描源保存完整 SHA-256：

```text
内容未变 → 复用上次抽取片段
内容变化 → 只重抽该文件
文件删除 → 删除对应缓存片段
模块清单变化 → 重抽该项目，避免新增模块沿用旧的 unresolved import
```

节点 ID 由 `kind + key` 生成，与文件内容无关。因此修改函数体或脚本内容不会改变文件节点 ID；影响分析引用保持稳定。

`status` 同时检查：

- 已缓存文件内容是否变化；
- 已缓存文件是否删除；
- 是否出现尚未进入缓存的新代码源。

工作区模式在此之上增加两层隔离：

- 每个项目使用独立的 graph/cache/status；
- 聚合图的 key 使用 `project://<project-id>/<source-key>`，同名文件不会跨项目碰撞。

JS/TS 仅解析仓库本地关系。裸 npm 包 import 不进入项目关系图；`node_modules`、`.next`、`dist`、`build`、`coverage` 等可重建目录不扫描。

## 7. 命令

构建：

```bash
python3 .standards/xirang-coderef.py build --repo-root . --json
```

检查新鲜度：

```bash
python3 .standards/xirang-coderef.py status --repo-root . --json
```

校验图完整性：

```bash
python3 .standards/xirang-coderef.py validate --repo-root . --json
```

查看 Gate 一跳关系：

```bash
python3 .standards/xirang-coderef.py query \
  --repo-root . \
  --path .standards/gate-enforce.py \
  --depth 1 \
  --json
```

查看修改 `pre-write-check.py` 的两跳影响：

```bash
python3 .standards/xirang-coderef.py impact \
  --repo-root . \
  --path .standards/pre-write-check.py \
  --depth 2 \
  --json
```

`query --depth 2` 可能经过工具注册表或 Harness 形成大范围结果；普通排查优先使用一跳，影响分析再按需扩到两跳。

沙箱工作区构建：

```bash
python3 .standards/xirang-coderef-workspace.py build \
  --manifest $HOME/Desktop/沙箱/.xirang-coderef-workspace.json \
  --json
```

检查四个项目的增量新鲜度：

```bash
python3 .standards/xirang-coderef-workspace.py status \
  --manifest $HOME/Desktop/沙箱/.xirang-coderef-workspace.json \
  --json
```

校验项目图与聚合图：

```bash
python3 .standards/xirang-coderef-workspace.py validate \
  --manifest $HOME/Desktop/沙箱/.xirang-coderef-workspace.json \
  --json
```

工作区清单明确纳入：

- 个人产品官网与原型工作台（外部活动源码 `ardot-local-clone`）；
- LifeOS；
- V9 Status Panel Obsidian；
- 示例项目 EXAMPLE。

旧 Ardot 副本、`code0711`、运行时、软件包、图片、本地运行数据和迁移暂存明确排除。清单只登记边界，不移动或删除目录。

## 8. MVP 验证

2026-07-24 真实 Vault 结果：

| 指标 | 结果 |
|---|---:|
| 扫描源 | 104 |
| 节点 | 128 |
| 边 | 187 |
| `invokes` | 30 |
| `imports` | 54 |
| `registered_in` | 30 |
| `verified_by` | 45 |
| warnings | 0 |
| 图完整性 | `valid=true` |
| 独立测试 | 12/12（含工作区、TypeScript 与 warning 状态语义） |

关键链路已恢复：

```text
.standards/hooks/pre-write-hook.sh
  → invokes .standards/gate-enforce.py
  → invokes .standards/pre-write-check.py
```

`pre-write-check.py` 两跳影响可返回：

- `.standards/gate-enforce.py`
- `.standards/post-write-check.sh`
- `.standards/hooks/pre-write-hook.sh`
- 相关测试入口

2026-07-24 真实沙箱工作区结果：

| 指标 | 结果 |
|---|---:|
| 活动项目 | 4 |
| 扫描源 | 196 |
| 节点 | 214 |
| 边 | 131 |
| `imports` | 124 |
| warnings | 0 |
| 二次构建 | 0 changed / 196 reused |
| 项目图 + 聚合图 | 全部 `valid=true` |

其中个人产品活动原型为 66 sources、75 nodes、21 edges；可用于今晚原型工作的增量监测。

首条真实增量样本已捕获：`workbench/frontend/src/api/editor.ts` 单文件变化时，个人产品项目报告 1 changed / 65 reused，其他三个项目均为 0 changed；构建后四个项目重新回到 `fresh=true`。

## 9. 已知限制

1. 关系抽取是确定性启发式，不是完整跨语言静态分析。
2. 动态拼接、间接 shell eval、运行时注册可能漏边。
3. 同名文件无法唯一解析时不猜测，需补声明关系。
4. 声明配置错误可能形成逻辑错误但结构合法的边，仍需人工评审。
5. 当前不提供 daemon、保存即更新或 Obsidian 图形界面；工作结束时按需 build/status。
6. 当前测试独立运行，CodeRef 未进入 V9 Harness trust set；这是其 advisory 边界的一部分。
7. 工作区不推断跨项目调用；需要跨项目关系时必须有明确声明或共享契约。

## 10. 后续触发条件

仅在出现真实需求时继续：

| 信号 | 后续 |
|---|---|
| Workbench 需要影响图 | 只读消费 `graph.json`，不在插件内运行构建 |
| 漏掉关键 subprocess/Shell 关系 | 增加 fixture 后扩展确定性 extractor |
| 声明边明显增多 | 为 `coderef-relations.json` 增加 schema validator |
| 需要持续新鲜 | 由独立调度器运行 build；不得塞入写入 Hook 热路径 |

---

*CodeRef 是息壤的代码反射镜，不是第二套真相。*
