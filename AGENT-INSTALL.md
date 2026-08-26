# 息壤 V9.7 Agent 安装与升级契约

本文件面向能够访问本机文件的 Agent。普通用户不需要理解下面的命令和状态。

## 目标交互

用户只做三步：下载、把任务交给 Agent、确认一次。不得要求用户判断版本、选择新装/升级、运行命令或手工合并文件。

## 强制流程

1. 读取本文件、`VERSION`、`manifests/package-manifest.json` 和 `baselines/supported.json`。
2. 确认当前系统为 macOS，Python 为 3.11 或更高。
3. 识别自己当前的宿主平台。常见 ID 包括 `codex`、`claude`、`openclaw`、`hermes`、`deepseek_harness`、`workbuddy`，但平台不是固定名单。先查 `templates/platforms.json` 的开放注册表；未登记平台使用自己的稳定宿主 ID，安装器会安全降级为通用工作区入口。不得让用户选择平台。
4. 自动寻找候选工作区；只有一个候选时使用它，多个候选时只问一个目标位置问题。
5. 在任何写入前执行：

   ```bash
   bash setup.sh plan --target "目标工作区绝对路径" --platform "当前平台 ID"
   ```

6. 把计划结果转成人话确认卡，必须包含：判断、目标、保留、备份、修改、当前平台入口、支持状态和下一步。OpenClaw、Hermes 或 DeepSeek Harness 的原生入口位于工作区外时，必须显示精确路径。
7. 只有用户在看到确认卡后明确要求开始，才能执行：

   ```bash
   bash setup.sh apply --target "目标工作区绝对路径" --platform "当前平台 ID"
   ```

8. 安装器完成后执行：

   ```bash
   bash setup.sh verify --target "目标工作区绝对路径" --platform "当前平台 ID"
   ```

9. 报告 `installed`、`upgraded`、`current_verified`、`rolled_back` 或 `assistance_required` 中的真实结果。平台入口未经过新会话 canary 时只能说“已配置，待验证”。

## 失败和中断

- 安装器返回 `recovery_required` 时，执行：

  ```bash
  bash setup.sh recover --target "目标工作区绝对路径" --platform "当前平台 ID"
  ```

- 版本不明、核心文件已定制、目标为符号链接、Manifest 漂移、恢复根不可用或平台不支持时停止写入。
- 禁止用 `cp -R`、`rsync`、Git checkout 或手工覆盖代替安装器。
- 不得删除用户业务目录、项目资料、现有 Git 历史或未知文件。
- 不得一次性覆盖所有平台入口。只应用当前调用 Agent 的原生入口；其他模板保留在 `templates/` 供以后对应平台自行应用。

## 候选工作区判断

优先顺序：用户明确指出的目录 → 当前 Agent 工作区 → 桌面上唯一包含 `.xirang` 或旧版息壤特征的目录。仍有歧义时只询问目标目录，不让用户选择版本。
