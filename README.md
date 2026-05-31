# 息壤 V9 · Starter Vault

> 多智能体协作方法论的最小可运行 Obsidian Vault 骨架。
> Clone 即用，3 分钟从零到可运行。

---

## 什么是息壤

息壤是一套**基于 Obsidian Vault 的多智能体协作方法论**，核心理念：

- 文件系统即协作黑板 — Agent 通过读写 Markdown 文件协作
- 二元触发器 — 写文件要声明，不写直接做
- 渐进收敛 — L0→L3 四阶段，按需升级，不强制完全体

详细文档：[息壤 V9 在线文档](https://disturbo.github.io/xirang/)

---

## 快速开始

### 方式一：Git Clone（推荐）

```bash
git clone https://github.com/disturbo/xirang-starter ~/Desktop/obsidianVault
```

### 方式二：下载 ZIP

从 [Releases](https://github.com/disturbo/xirang-starter/releases) 下载最新 zip，解压到 `~/Desktop/obsidianVault/`。

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
.standards/         ← V9 运行时工具链（32 文件，零外部依赖）
.skills/            ← 工作流技能定义
00-MOC/             ← 导航中枢
02-项目管理/        ← 运行时状态（Agent 状态、日志、任务卡）
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

---

## 相关资源

- [息壤方法论文档站](https://disturbo.github.io/xirang/) — 架构、流程、规则、演进历史
- [息壤方法论仓库](https://github.com/disturbo/xirang) — 完整文档源码

---

## License

MIT
