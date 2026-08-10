# 息壤 V9 · Starter Vault

> 多智能体协作方法论的最小可运行 Obsidian Vault 骨架。
> V9.4.3 · 2026-08-10 distribution refresh · Clone 即用，3 分钟从零到可运行。

---

## 什么是息壤

息壤是一套**基于 Obsidian Vault 的多智能体协作方法论**，核心理念：

- 文件系统即协作黑板 — Agent 通过读写 Markdown 文件协作
- 二元触发器 — 写文件要声明，不写直接做
- 第一反射器 — 巡检任务卡、状态、心跳、规范冲突、分发泄漏、状态机和 Handoff，区分真静默与假静默
- 自证门禁 — harness eval 用 positive/negative fixture 验证门禁、巡检、状态机不会假绿
- 渐进收敛 — L0→L3 四阶段，按需升级，不强制完全体

详细文档：[息壤 V9 在线文档](https://disturbo.github.io/xirang/)

当前版本把“可运行”与“已治理完成”分开：新部署首先进入 `observing`，只有调度、输入、输出、消费者、行为效果、新鲜度与责任出口均有证据，并连续观察通过后，才允许标记为 `green`。完整规则见 [GOVERNANCE.md](GOVERNANCE.md)。

---

## Starter 分发边界

这个仓库是给组内同事一键启用 V9 的**干净启动骨架**，只保留通用运行时、方法论、规范模板和空项目目录。

不应包含：

- 个人姓名、个人路径、会话记录
- 具体项目文档、客户资料、业务数据
- API Key、App Secret、登录 token、账号配置
- 巡检快照、Agent 事件流等运行期产物；历史成本事件仅作隔离审计，不随 starter 运行
- Obsidian 插件的本机 `data.json` 配置

如需从个人 Vault 同步到 starter，请先运行 `sync-to-dist.sh`，再用 `v9-starter-leak-check.py --strict` 确认无个人/项目痕迹。

---

## 快速开始

### 方式一：Git Clone（推荐）

```bash
git clone https://github.com/disturbo/xirang-starter-Vualt.git ~/Desktop/obsidianVault
```

### 方式二：下载 ZIP

从 [Releases](https://github.com/disturbo/xirang-starter-Vualt/releases) 下载最新 zip，解压到 `~/Desktop/obsidianVault/`。

### 打开 Vault

1. 安装 [Obsidian](https://obsidian.md/download)
2. 打开 Obsidian → "Open folder as vault" → 选择 `obsidianVault` 目录
3. 按 `Cmd/Ctrl + O` → 搜索 "Home" → 开始浏览

---

## 四阶段部署

不是每个人都需要完全体。根据你的角色选择合适的阶段：

| 阶段 | 画像 | 耗时 | 需要什么 |
|:---:|------|:---:|------|
| **L0** | 浏览者（看文档） | 2 min | Obsidian |
| **L1** | 问答者（偶尔问 AI） | 10 min | + Claude Code / OpenClaw |
| **L2** | 产出者（AI 帮出活） | 20 min | + 角色设定 + V9 二元触发器 |
| **L3** | 协作者（多 AI 跑流程） | 1 h | + 完整 V9 运行时 |

详细操作步骤见 Vault 内 `50-经验/Agent协作方法论/息壤V9-部署指南.md`。

---

## 环境初始化（L2+）

```bash
cd ~/Desktop/obsidianVault
bash setup.sh
```

脚本会检查：Python、Node.js、Ollama、脚本权限、prompt-build 一致性。

---

## 目录结构

```
.claude/            ← Claude Code 项目配置 + Agent prompts
.codex/             ← Codex 平台指令
.prompt-src/        ← 合规内容单源分发系统
.standards/         ← V9 运行时工具链（零外部依赖）
.skills/            ← 工作流技能定义
00-MOC/             ← 导航中枢
02-项目管理/        ← 运行时状态（Agent 状态、日志、任务卡、巡检）
10-项目/            ← 你的项目文档（空，待填充）
20-资料/            ← 原始资料（空，待填充）
30-规范/            ← 输出规范（通用版，需按项目扩展）
40-决策/            ← 决策记录（空，待填充）
50-经验/            ← V9 方法论文档
60-归档/            ← 历史存档（空）
90-模板/            ← 文件模板
```

---

## 部署后的第一件事

1. **填写品牌规范** — 打开 `30-规范/品牌规范.md`，填入你的项目品牌信息
2. **定制 Home** — 编辑 `00-MOC/🏠-Home.md`，写上你的项目信息
3. **创建项目 MOC** — 复制 `00-MOC/T-项目MOC.md` 到 `00-MOC/`，重命名并填入具体内容
4. **生成项目规范** — 根据需要在 `30-规范/` 下创建项目专属规范

## 当前运行真实性基线

| 能力 | 文件 |
|---|---|
| 十源第一反射器聚合器 + 治理债/运行时消费链自检 | `02-项目管理/脚本/v9-reflex-check.py` |
| 任务卡/运行日志 JSON 巡检 | `02-项目管理/脚本/project-ops-check.py` |
| 规范冲突扫描 | `02-项目管理/脚本/v9-policy-conflict-check.py` |
| starter 泄漏扫描 | `02-项目管理/脚本/v9-starter-leak-check.py` |
| 任务验收状态机 | `02-项目管理/脚本/v9-task-state-check.py` |
| Handoff 可接手性扫描 | `02-项目管理/脚本/v9-handoff-check.py` |
| Harness 回归测试 | `02-项目管理/脚本/v9-harness-eval-runner.py` |
| 安全验收正门 | `.standards/v9-accept.py` |
| 风险触发型多 Agent 对抗审查 | `50-经验/Agent协作方法论/息壤V9-多Agent对抗性审查协议.md` + `.standards/adversarial-review-check.py` |
| 成本治理（已退役） | 历史脚本只读保留至 2026-08-02，不计入运行能力 |
| Codex Desktop 门禁/生命周期适配 | `.standards/hooks/codex-hook-adapter.py` |
| SessionStart/任务握手 GBrain 自动召回 | `.standards/semantic-recall.py` + `semantic_recall` 事件 |
| Harness trust/hash 新鲜度校验 | `.standards/harness-eval-verify.py` / `.standards/harness-tested-files.txt` |
| skill 版本遮蔽校验 | `02-项目管理/脚本/v9-skill-shadow-check.py` |
| 巡检输出目录 | `02-项目管理/巡检/` |
| 自证回归目录 | `02-项目管理/evals/` |
| 公开迭代记录 | `50-经验/Agent协作方法论/V9.4.3-迭代记录-2026-06-27.md` |

即时巡检：

```bash
python3 02-项目管理/脚本/v9-reflex-check.py
```

健康口径：`summary.active=0`、`sources_failed=[]` 且 `runtime_checks` 无 `failed/stale` 才是真静默。任务评审债、Frontmatter lint 债和 deferred 熵 backlog 会保持 advisory/yellow；GBrain、LLM Wiki 等部署依赖未配置时保持红色，不以脚本退出成功冒充生效。成本治理已退役，不参与健康度。

Skills 扫描默认覆盖共享、Codex、Claude、Hermes、OpenClaw 及 OpenClaw workspace 等实际入口。同名副本版本或内容不一致时必须保持红色，不能以漏扫根目录换取绿色。

回归测试：

```bash
python3 02-项目管理/脚本/v9-harness-eval-runner.py
python3 .standards/harness-eval-verify.py --json
```

分发前脱敏扫描：

```bash
python3 02-项目管理/脚本/v9-starter-leak-check.py --root . --strict
```

Phoenix 在本版本中仅为 design/reference；starter 未部署 scheduler 或 executor，不包含自动自愈能力。

---

## 相关资源

- [息壤方法论文档站](https://disturbo.github.io/xirang/) — 架构、流程、规则、演进历史
- [息壤方法论仓库](https://github.com/disturbo/xirang) — 完整文档源码

---

## License

MIT
