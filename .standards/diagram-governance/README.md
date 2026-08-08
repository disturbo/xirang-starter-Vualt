# Vault 多格式流程图治理工具

当前版本 `v0.6` 已形成 Draw.io、Mermaid、Obsidian Excalidraw 三格式的“扫描、格式专属验收、候选生成、真实渲染、预览包、语义血缘与漂移检查”非破坏式闭环；仍不修改任何源图。Draw.io 继续使用 v0.5 的几何与布线引擎，Mermaid 使用官方解析器，Excalidraw 同时支持插件的 `json` 与 `compressed-json`。

## 当前能力

- 扫描 `.drawio`、Markdown 中的 Mermaid 代码块和 `.excalidraw.md`。
- 为每个图生成稳定 ID、内容哈希、格式、用途分级、权威源和修改策略。
- Draw.io 区分业务连接器与图例/装饰线，避免把图例自由端点误报成业务断线。
- 检查业务连接器绑定、自由端点、引用、正交样式、端口声明、跨泳道 parent、节点是否超出父泳道、判断出口、显式路径斜线、节点穿越、节点重叠和共线重叠。
- Mermaid 逐代码块使用官方 `mermaid@11.16.0` 验证语法，拒绝 `click` 与 JavaScript URL；候选保留业务语句并注入飞书色系与语义类。
- Excalidraw 检查元素 ID、容器、箭头绑定、boundElements、有限几何和手绘 roughness；候选保留全部 ID、坐标、文字和绑定，仅归一飞书色系与手绘样式。
- Mermaid 与 Excalidraw 预览通过 Google Chrome 调用各自官方 npm 渲染器，必须同时产生可验证 SVG、非空 PNG 和源标签匹配证据。
- 三种格式都可提取无坐标语义图（节点、边、泳道、标签）与稳定语义哈希；只有 `config/process-links.json` 显式声明的同流程资产才参与跨格式漂移判断。
- 本地 Ollama `bge-m3:latest` 可作为语义匹配增强器；模型不可用时降级为确定性哈希/结构检查，不影响扫描与候选生成。
- 候选修复会补全业务边端口，并对自动补线、穿节点、斜段和共线重叠重算显式正交路径；标签偏移点不再误判为路径 waypoint，已绑定边上的冗余自由端点会安全移除。
- 端口保留角点和上下分流槽位，避免修复后把多分支强行并回节点中心；分层重排按每个业务秩的最宽节点动态计算列宽。
- 节点落入错误父泳道时，候选会重挂到唯一包含它的泳道并保持绝对坐标；节点仅轻微超出本泳道右侧或下侧时，会统一扩展对齐泳道组并保留 `4px` 安全边，防止原生渲染裁剪。
- 候选修复默认不改节点、泳道、文字和配色；显式使用 `--layout` 时，无环长卷轴会重排为横向泳道，安全循环业务流会保留节点位置并把反馈边送入外围独立回流通道，小型状态机则保持状态节点坐标并重算转移线；显式使用 `--theme` 时，才会把候选副本归一为飞书参考画板色系。
- 业务条件无法唯一推断时保留为人工阻塞项。
- 批量规划会把图分为 `preserve-layout`、`horizontal-swimlane-reflow`、`dedicated-cyclic-flow` 和 `dedicated-state-machine`，循环图不会误套无环布局。
- 现有版式可读且无节点重叠的循环业务流只重算外围反馈通道；过高画布或节点重叠时，只有在泳道归属可恢复、移除反馈边后剩余图为 DAG、节点数不超过安全上限时才启用分层重排。
- 分层重排按业务秩布局，每条阅读轨最多 `12` 个阶段，长流程自动换轨，节点字号统一提升到 `13px`，空泳道轨道自动压缩；多条反馈边使用间隔 `36px` 的外环通道。
- 路由器会把已占用线段纳入代价，并使用细分端口槽位，避免多分支汇入同一节点时共线重叠。小型状态机按“正向直连、反向直连、斜向回退、自环”的优先级布线；同列向上回退使用独立 U 形通道和专用标签锚点，自环固定在状态节点外侧。
- 状态机只有在现有布局可读、无节点重叠、全部转移有绑定、节点不超过 `12` 且转移不超过 `24` 时才自动治理；超出边界仍安全阻塞。
- 批量生成逐图隔离错误，一张失败不会中断整批。
- 真实预览通过 Google Chrome 调用官方 `viewer.diagrams.net`，并同时验证页面无浏览器错误、Draw.io SVG 已生成、源图业务标签已出现，以及 PNG 尺寸、文件大小、亮度方差和非空像素比例。
- 官方 viewer 瞬时不可用时按固定次数退避重试；仍失败则关闭门禁并保留失败项。再次执行批次会复用已验证 bundle，只补跑失败项。
- 所有机器命令支持 `--json`。

## 使用

```bash
node .standards/diagram-governance/cli.mjs inventory \
  --out .standards/diagram-governance/reports/vault-manifest.json

node .standards/diagram-governance/cli.mjs audit \
  --strict-ports \
  --fail-on-errors \
  --out .standards/diagram-governance/reports/drawio-audit-strict.json

node .standards/diagram-governance/cli.mjs candidate \
  --source '10-项目/基线/01-PDI管理/流程01-PDI主业务流.drawio' \
  --out '.standards/diagram-governance/candidates/representatives/PDI主业务流.candidate.drawio' \
  --layout --theme

# 纵向长卷轴流程：先重排，再布线和配色
node .standards/diagram-governance/cli.mjs candidate \
  --source '10-项目/基线/05-取送车服务/流程01-取送车主流程.drawio' \
  --out '.standards/diagram-governance/candidates/representatives/取送车主流程.candidate.drawio' \
  --layout --theme

# 只读批量规划
node .standards/diagram-governance/cli.mjs batch-plan \
  --vault $HOME/Desktop/obsidianVault \
  --out .standards/diagram-governance/reports/batch-plan-v0.5.json

# 只生成候选副本，并隔离失败
node .standards/diagram-governance/cli.mjs batch-generate \
  --vault $HOME/Desktop/obsidianVault \
  --out-dir .standards/diagram-governance/candidates/batch-v0.5 \
  --theme --force \
  --report .standards/diagram-governance/reports/batch-generation-v0.5.json

# 将 A 层通过候选送入真实 diagrams.net 渲染
node .standards/diagram-governance/cli.mjs batch-preview \
  --vault $HOME/Desktop/obsidianVault \
  --input .standards/diagram-governance/reports/batch-generation-v0.5.json \
  --out-dir .standards/diagram-governance/previews/bundles \
  --report .standards/diagram-governance/reports/batch-preview-v0.5.json

# Mermaid / Excalidraw 全量候选（仍只写派生目录）
node .standards/diagram-governance/cli.mjs multi-generate \
  --vault $HOME/Desktop/obsidianVault \
  --out-dir .standards/diagram-governance/candidates/multiformat-v0.6 \
  --force \
  --report .standards/diagram-governance/reports/multiformat-generation-v0.6.json

# 官方渲染器批量预览，可用 --format/--filter/--limit 先跑代表图
node .standards/diagram-governance/cli.mjs multi-preview \
  --vault $HOME/Desktop/obsidianVault \
  --input .standards/diagram-governance/reports/multiformat-generation-v0.6.json \
  --out-dir .standards/diagram-governance/previews/multiformat-v0.6

# 建立语义血缘；默认连接本地 Ollama bge-m3:latest
node .standards/diagram-governance/cli.mjs lineage \
  --vault $HOME/Desktop/obsidianVault \
  --links .standards/diagram-governance/config/process-links.json \
  --out .standards/diagram-governance/reports/lineage-v0.6.json
```

默认不把现存文件缺少 `exitPerimeter/entryPerimeter` 作为错误，而是警告，便于先建立基线。使用 `--strict-ports` 后按《流程图绘制规范 v3.4.3》硬门禁执行。

`candidate` 命令拒绝让输出路径等于源文件；派生文件已存在时也默认拒绝覆盖，只有显式传入 `--force` 才能重新生成候选副本。

## 批处理结果语义

- `pass`：几何、布局、配色和业务语义全部通过。
- `review-required`：候选已生成，但至少一个门禁仍需修复或业务确认。
- `blocked`：当前没有安全的自动布局或转移线策略，不生成候选。
- `error`：单图执行异常；其他图继续处理。

`batch-preview` 只选择几何、布局和配色全部通过的候选。业务语义待确认的图可以预览，但结果会保留 `semantic_gate` 标记。

## 原生预览包

预览产物遵循 `preview-bundle/v1`：

```text
<preview-root>/drawio/quick/<bundle-id>/
├── manifest.json
├── summary.json
└── artifacts/hero.png
```

`preview-capture` 是生产者，使用真实 Chrome + 官方 diagrams.net viewer；`preview-latest` 只读查询通过 DOM 与 PNG 双门禁的缓存，不重新渲染。Chrome 缺失、浏览器错误页、源图标签未呈现、PNG 空白或格式异常都会明确失败。

Vault 的 `drawio-obsidian` 插件只登记为桌面端参考运行时，当前不作为 Node CLI 的生产渲染后端：其离线内核依赖 Electron/iframe 上下文，直接抽离会造成初始化阻塞。CLI 不会用它伪装“离线成功”。

## 布局可读性门禁

A 层现在会拒绝“几何不重叠，但整图不可读”的复杂流程：

- 复杂流程画布宽高比低于 `0.8` 或高于 `4.5`；
- 适配到 `1920×1080` 评审视口后，最小节点字体低于 `8px`；
- `--layout` 对无环、多泳道长卷轴使用横向重排；对版式已合格的循环业务流只使用外围回流通道；对泳道可恢复的过高/重叠循环业务流使用分层泳道重排；对安全小型状态机保持节点坐标，只治理转移线。状态机不会被静默拉平。

当前有意不做的自动动作：无法恢复泳道归属或超过安全规模的复杂循环流、需要移动状态节点的大型/重叠状态机，以及在没有显式流程身份/人工确认时把 Mermaid 或 Excalidraw 自动覆盖成正式 Draw.io。多格式治理不等于无条件格式互转。

## 飞书参考色系

色值直接提取自用户指定的飞书规范画板，不使用品牌色猜测：

| 语义 | 填充 | 边框 | 文字 |
|---|---|---|---|
| 入口 | `#e8f4fd` | `#4a90d9` | `#2c5f8a` |
| 普通动作 | `#ffffff` | `#d0d0d0` | `#333333` |
| 已处理/系统动作 | `#f5f5f5` | `#bdbdbd` | `#616161` |
| 判断/待处理 | `#fff8e1` | `#e6b800` | `#5d4e00` |
| 成功/完成 | `#e8f8e8` | `#5cb85c` | `#3d7a3d` |
| 异常/重试 | `#f2f6f8` | `#c64b4b` | `#a33a3a` |
| 普通连线 | — | `#666666` | `#1f2329` |
| 数据/异步虚线 | — | `#999999` | `#1f2329` |

泳道标题统一为 `#f5f5f5`，泳道内容区保持白色，表格/泳道边框使用 `#e0e0e0`。

## 权威关系

- 正式业务流程：Draw.io 是唯一可编辑正式基线。
- Mermaid：文档内解释或技术图，不自动覆盖 Draw.io。
- Excalidraw：讨论、探索和状态草图，保留手绘表达。
- 飞书画板：Draw.io 语义的原生协作副本，不反向替代正式基线。

生成报告属于派生产物；删除报告不会影响任何流程图源文件。

## v0.6 验证结果

- Vault 资产：`24` Draw.io、`145` Mermaid、`7` Excalidraw；Mermaid 与 Excalidraw 专属审计 `152/152` 通过，未实现状态 `0`。
- 多格式候选：`152/152` 生成并复验通过，错误 `0`，正式源修改 `0`。
- 官方预览代表图：PDI Mermaid 与延保 Excalidraw 均通过 SVG、PNG、源标签三重证据；视觉抽检确认语义色与手绘表达生效。
- 语义血缘：`176` 个资产入库；本地 `bge-m3:latest` 为 `1024` 维。PDI、取送车、延保状态机三组跨格式样本相似度分别为 `0.9282`、`0.8344`、`0.8280`，均通过 `0.82` 门槛。
- 测试：单元 `42/42`，真实 E2E `13/13`；所有宿主 Markdown、Excalidraw 与正式 Draw.io 源哈希保持不变。

## v0.5 Draw.io 验证结果

- `24/24` 张正式 Draw.io 均有安全自动策略，`0` 张阻塞。
- 候选生成：`18 pass`、`6 review-required`；6 张仅保留业务语义确认，几何/布局/配色失败均为 `0`。
- 原生预览：`24/24` 通过真实 Chrome + diagrams.net 双门禁，`0` 失败；最终批次复用 `19` 个已验证 bundle，重新渲染 `5` 个变化指纹。
- 测试：单元 `35/35`，真实文件 E2E `9/9`；所有正式源图哈希保持不变。
