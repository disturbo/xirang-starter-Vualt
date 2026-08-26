# 息壤 V9.7 统一安装与升级包

息壤把普通文件夹变成一个可授权、可约束、可恢复、可验收的 Agent 工作区。

普通同事不需要判断自己是新安装还是升级，也不需要理解 StateStore、Hook、任务号、平台模板或版本迁移。无论正在使用 Codex、Claude、OpenClaw、Hermes、DeepSeek Harness、WorkBuddy 或其他 Agent，下载同一个包后交给当前 Agent 即可。

## 给普通同事

1. 从官网或 GitHub Release 下载 `xi-rang-v9.7.0-universal.zip`。
2. 解压后打开 [START-HERE.md](START-HERE.md)。
3. 把其中的一句话复制给 Agent。
4. Agent 先只读检查；你看完确认卡后回复“开始”。

不要从旧官网 ZIP、别人工作区或 Git clone 直接覆盖自己的目录。

## 一个包，四种判断

| 检查结果 | Agent 的动作 |
|---|---|
| 没有息壤 | 在目标工作区全新安装 |
| 已识别旧版 | 先备份，再升级和迁移 |
| 已是 V9.7.0 | 校验或修复，不重复安装 |
| 版本不明、目标不唯一或无法恢复 | 停止写入并给出诊断编号 |

## 发行物结构

```text
START-HERE.md                 给普通同事
AGENT-INSTALL.md              给 Agent 的执行契约
setup.sh                      统一入口
installer/xirang_install.py   诊断、安装、升级、恢复
payload/                      唯一 V9.7 Core
baselines/                    受支持旧版本指纹
manifests/                    Core 与 Package Manifest
templates/                    五类 Agent 原生入口模板与统一根约束规范
```

源码仓库包含 `tools/` 和 `tests/`；正式 ZIP 由 `tools/build_release.py` 白名单构建，不包含源码仓库的 Git 历史和测试缓存。

## 当前平台承诺

V9.7.0 首个公开发行只承诺 macOS 和 Python 3.11+。包内置多种常见 Agent 宿主模板并通过 `platforms.json` 开放扩展，不把任何名单定义为固定角色。安装器只应用当前调用 Agent 的入口；未登记 Agent 使用通用工作区入口，其他模板不冒充已安装。安装器会在写入前检查平台；不满足条件时停止，不会尝试半安装。

完整发行与恢复口径见 [GOVERNANCE.md](GOVERNANCE.md)。
