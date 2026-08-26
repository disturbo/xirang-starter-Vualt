# 息壤完整包：给当前 Agent 的安装与升级入口

本包不预设你是 Codex、Hermes、OpenClaw、WorkBuddy、DeepSeek Harness 或其他 Agent。先按当前宿主真实能力工作，未登记的平台使用通用工作区入口。

## 第一步：只读检查

1. 读取根 `AGENTS.md`、`.xirang/adapters/PROTOCOL.md`、`🏠-Home.md` 和本文件。
2. 运行完整包校验：

   ```bash
   python3 .xirang/distribution/verify_complete.py
   ```

3. 判断目标：
   - 全新使用：目标就是当前解压后的完整包根目录；
   - 旧版升级：目标是用户已有的息壤工作区，不要用新目录覆盖旧目录。
4. 先运行息壤计划，不写文件：

   ```bash
   bash .xirang/distribution/upgrade/setup.sh plan --target "<目标工作区>" --platform auto
   ```

5. 再运行插件、主题和 Skill 的合并计划：

   ```bash
   python3 .xirang/distribution/install_extras.py plan --target "<目标工作区>"
   ```

把两份计划合成一张简短确认卡，告诉用户：判断、目标、保留内容、备份、修改范围、冲突和支持状态。目标不唯一时只问一个必要问题。

## 第二步：用户确认后执行

用户明确回复开始、执行或同等当前决定后：

```bash
bash .xirang/distribution/upgrade/setup.sh apply --target "<目标工作区>" --platform auto
python3 .xirang/distribution/install_extras.py apply --target "<目标工作区>"
```

安装器负责息壤机器契约、StateStore、恢复和平台入口；extras 安装器只做可恢复的缺失补齐与安全配置合并：

- 不覆盖同名但内容不同的 Skill、插件、主题或 CSS；遇到冲突先停止。
- 不复制插件 `data.json`、账号、Cookie、Token、密钥或个人工作区历史。
- 旧工作区的 `workspace.json`、项目内容、未知文件和现有 Agent 规则保持不动。

最后运行安装器 `verify`，并报告实际状态。执行者只能提交为待验收，不能替用户接受结果。
