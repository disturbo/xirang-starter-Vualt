# 息壤 V9.7.2 发行、安装与恢复口径

## 唯一发行真源

- 公开源码与 Release：`disturbo/xirang-starter-Vualt`。
- 官网只负责说明与分流，不能成为第二套产物真源。
- `payload/` 是唯一 Core；构建器从白名单生成一个完整 Vault，隐藏分发层中的升级载荷与可见 Vault 绑定同一 Core Manifest。

## 发行闭包

正式 Release 必须同时提供：

- `xi-rang-v9.7.2-starter.zip`
- `release-manifest.json`
- `SHA256SUMS`
- Release Notes
- Tag、Commit 和构建 ID

完整包的 `package-manifest.json` 校验知识库、Obsidian、Skill 与隐藏分发层；隐藏升级载荷拥有独立 Package Manifest，并与根分发层使用字节级相同的 `core-manifest.json`。

ZIP 根目录必须直接是可读知识库；`installer/`、`baselines/`、`manifests/`、`payload/` 和 `templates/` 不得作为顶层用户界面，只能存在于 `.xirang/distribution/upgrade/`。插件程序、主题、CSS 和 Skill 使用独立白名单、许可证与泄漏扫描。

## 所有权

| 类别 | 处理规则 |
|---|---|
| Managed core | 机器契约、控制面和明确登记的宪法文档由新版本替换，写入前保存逐文件 pre-image |
| Root AGENTS | 只更新带标记的息壤受管区，保留原项目规则 |
| Seed if absent | 首页、MOC、模板、教训库和其他知识库示例仅在缺失时补齐，绝不覆盖同事内容 |
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

V9.7.2 发行目标支持 macOS 与 Python 3.11+。V9.7.1、V9.5.0 和已登记的 V9.4.3 基线可以自动识别；核心已定制或来源未知时进入辅助迁移，不强行覆盖。

## 每版强制门禁

正式候选必须由自动化证明：生命周期清单覆盖全部 payload；受管 Core 两份 manifest 一致；知识库种子在升级时不被覆盖；Obsidian 注册表、程序、许可证和通用预设闭包一致；规范来源映射无漏项；Wiki 链接无断链；首次打开前精确校验和打开后的安全校验都成立；无个人、项目、账号、密钥和运行数据泄漏。任何一项失败都只能保留为候选，不得发布。

## 验收

构建者负责构建和验证，但不能验收自己的 Release。正式发布状态只能是待验收；是否接受由发布责任人决定。
