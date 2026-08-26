# 息壤 V9.7 通用包平台能力矩阵

| 平台 | 安装后模式 | 初始状态 | 说明 |
|---|---|---|---|
| Codex | `manual_guard` | 已配置，待验证 | 项目 `AGENTS.md` 与生成的 Hook 配置生效后，仍需新会话 canary |
| Claude Code | `manual_guard` | 已配置，待验证 | 生成 `.claude/settings.json`；宿主加载后再形成行为证据 |
| OpenClaw | `manual_guard` | 模板可用；按当前调用平台应用 | 合并到原生 `AGENTS.md`，不替换 IDENTITY/SOUL/USER/MEMORY |
| Hermes | `manual_guard` | 模板可用；按当前调用平台应用 | 合并到原生 SOUL，不替换身份、记忆或工具规则 |
| DeepSeek Harness | `manual_guard` | 模板可用；按当前调用平台应用 | 无自动 Hook 时显式执行写前校验与写后取证 |
| WorkBuddy / CodeBuddy | `manual_guard` | 模板可用；按当前调用平台应用 | 合并原生 `CODEBUDDY.md`，不替换既有项目规则 |
| 未登记 Agent | 通用契约 | 工作区入口可用，原生入口未应用 | 不猜外部路径；以后通过开放平台注册表扩展 |

配置文件存在不等于可信接通。只有调度、输入、输出、消费者、行为效果和新鲜证据齐全时，才可报告 `connected`。通用包不要求用户选择平台；执行安装的 Agent 识别自身宿主，只应用对应入口。平台注册表开放扩展，不构成固定 Agent 名单。
