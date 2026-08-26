# 息壤 V9.7 发行、安装与恢复口径

## 唯一发行真源

- 公开源码与 Release：`disturbo/xirang-starter-Vualt`。
- 官网只负责说明与分流，不能成为第二套产物真源。
- `payload/` 是唯一 Core；构建器从白名单生成一个完整 Vault，隐藏分发层中的升级载荷与可见 Vault 绑定同一 Core Manifest。

## 发行闭包

正式 Release 必须同时提供：

- `xi-rang-v9.7.0-complete-vault.zip`
- `release-manifest.json`
- `SHA256SUMS`
- Release Notes
- Tag、Commit 和构建 ID

完整包的 `package-manifest.json` 校验知识库、Obsidian、Skill 与隐藏分发层；隐藏升级载荷拥有独立 Package Manifest，并与根分发层使用字节级相同的 `core-manifest.json`。

ZIP 根目录必须直接是可读知识库；`installer/`、`baselines/`、`manifests/`、`payload/` 和 `templates/` 不得作为顶层用户界面，只能存在于 `.xirang/distribution/upgrade/`。插件程序、主题、CSS 和 Skill 使用独立白名单、许可证与泄漏扫描。

## 所有权

| 类别 | 处理规则 |
|---|---|
| Core managed | 由新版本替换，写入前保存逐文件 pre-image |
| Root AGENTS | 只更新带标记的息壤受管区，保留原项目规则 |
| Generated/runtime | 按本机路径生成，不进入发行包 |
| User content | 永不覆盖、删除或打包 |
| Legacy extras | 默认保留为非现行输入，不用旧文件反向恢复权威状态 |

## 事务与恢复

安装器在 `~/.xirang/recovery-*` 建立对象、Manifest 和审计记录，在任何目标写入前完成并校验快照。事务日志只有 `complete` 才表示成功；中断后的下一次运行必须先恢复。

恢复只针对本次登记的精确文件和运行根，不覆盖更新后的未知文件，不删除业务内容。失败后应回到完整旧闭包或空的新闭包，不能留下混配版本。

## 状态真实性

- 文件存在表示已复制，不表示平台已接通。
- StateStore 激活表示权威状态后端可用，不表示当前 Agent Hook 已通过。
- 安装后平台状态从 `unverified` 开始；只有当前会话正反 canary 通过后才能报告相应能力。
- Manual Guard 始终记录四项宿主证明为 false，不冒充强隔离或不可抵赖。

## 支持范围

V9.7.0 正式支持 macOS 与 Python 3.11+。V9.5.0 和已登记的 V9.4.3 基线可以自动识别；核心已定制或来源未知时进入辅助迁移，不强行覆盖。

## 验收

构建者负责构建和验证，但不能验收自己的 Release。正式发布状态只能是待验收；是否接受由发布责任人决定。
