# Skill Inventory — 全平台技能清单

> **定位**：跨平台 skill 统一索引。Agent 启动时扫一眼，定位能力边界和调用入口。
> **维护**：技能新增/退役时同步更新本文件。
> **最后更新**：2026-06-09 · Codex

---

## 速查表

| 我要做什么 | 用哪个 Skill | 平台 | 入口 |
|---|---|---|---|
| 开始一个任务 | start-task | Vault | `.skills/start-task/SKILL.md` |
| 飞书/Lark 文档采集、编辑、归档、排障 | feishu-collection | Codex + Vault + OpenClaw + Hermes + WorkBuddy | `~/.skills-manager/skills/feishu-collection/SKILL.md` |
| 本地/URL 文件转 Markdown | markitdown | Codex + Vault + OpenClaw + Hermes + WorkBuddy | `~/.skills-manager/skills/markitdown/SKILL.md` |
| 图片/截图/扫描件文字识别 | paddleocr-text-recognition | Codex + Vault + Hermes + Hermes Desktop + Reasonix + OpenClaw + WorkBuddy + Claude + Agents | `~/.skills-manager/skills/paddleocr-text-recognition/SKILL.md` |
| 扫描 PDF/图片文档结构化解析 | paddleocr-doc-parsing | Codex + Vault + Hermes + Hermes Desktop + Reasonix + OpenClaw + WorkBuddy + Claude + Agents | `~/.skills-manager/skills/paddleocr-doc-parsing/SKILL.md` |
| 外部平台搜索/阅读/采集 | agent-reach | Codex + Vault + OpenClaw + Hermes + WorkBuddy + Claude + Agents | `~/.skills-manager/skills/agent-reach/SKILL.md` |
| 最近 30 天趋势/社区舆情研究 | last30days | Codex + Vault + OpenClaw + Hermes + WorkBuddy + Claude + Agents | `~/.skills-manager/skills/last30days/SKILL.md` |
| 采集原始资料（docx/截图/纪要） | ingest-source | Vault | `.skills/ingest-source/SKILL.md` |
| 写 PRD / 设计方案 / 规范 | publish-wiki | Vault | `.skills/publish-wiki/SKILL.md` |
| 发现资料冲突 | review-contradictions | Vault | `.skills/review-contradictions/SKILL.md` |
| 前端 UI 审美/颜色/字体/图标一致性审查 | taste-skill | Codex + Vault + Hermes + OpenClaw + WorkBuddy | `~/.skills-manager/skills/taste-skill/SKILL.md` |
| 任务交接 / 结束 | handoff-task | Vault | `.skills/handoff-task/SKILL.md` |
| PRD 品牌合规审查 | prd-brand-audit | Hermes | `~/.hermes/skills/yijing-dms/prd-brand-audit/` |
| PRD 骨架生成 + 校验 | yijing-prd-spec | Codex + Vault + OpenClaw + Hermes + WorkBuddy | `~/.skills-manager/skills/yijing-prd-spec/SKILL.md` |
| AI 方法论采集 | agent-methodology-collection | Hermes | `~/.hermes/skills/yijing-dms/agent-methodology-collection/` |
| 企业系统字段采集 | enterprise-system-field-collection | Hermes | `~/.hermes/skills/yijing-dms/enterprise-system-field-collection/` |
| 知识沉淀合成 | obsidian-knowledge-synthesis | Hermes | `~/.hermes/skills/note-taking/obsidian-knowledge-synthesis/` |
| Wikilink 批量替换 | obsidian-batch-wikilink-update | Hermes | `~/.hermes/skills/note-taking/obsidian-batch-wikilink-update/` |
| AI 幻觉审计 | ai-execution-hallucination-audit | Hermes | `~/.hermes/skills/productivity/ai-execution-hallucination-audit/` |
| 自动化编码流水线 | flowforge | Codex + OpenClaw + WorkBuddy | `~/.skills-manager/skills/flowforge/SKILL.md` |
| GBrain 健康检查 | gbrain | OpenClaw | `~/.openclaw/skills/gbrain/run.sh` |
| Obsidian 笔记操作 | obsidian | Hermes | `~/.hermes/skills/note-taking/obsidian/` |

---

## 一、Vault 工作流 Skills（`.skills/`）

> 所有 agent 共用的 vault 协作工作流。路由入口：`.skills/RESOLVER.md`

### 1. start-task · 任务启动

- **用途**：任意任务开始前的上下文初始化
- **触发**：agent 开始 vault/PRD/原型/资料/评审/规范相关工作
- **关键步骤**：
  1. 判断任务层（source / summary / published / prototype / standard）
  2. 确认 owner、next action、blocked by、产物路径
  3. 检查是否有其他 agent 正在改同一文件
  4. 只读必要文件，动手前说明要改哪些文件
- **质量线**：输入/输出/路径清晰；区分"读过"和"验证过"；不覆盖他人进行中的工作

### 2. ingest-source · 源文件采集

- **用途**：处理飞书/docx/xlsx/截图/竞品采集/系统导出等原始资料
- **触发**：拿到新的业务资料需要入库
- **关键步骤**：
  1. 登记原文件路径/来源（不整段复制进 PRD）
  2. 抽取模块级摘要：背景、角色、流程、字段、状态、权限、接口、待确认
  3. 每条摘要保留来源指针（文件名/tab/章节/截图编号/飞书段落）
  4. 遇冲突走 review-contradictions，不私自裁定
  5. 完成后写看板 Handoff
- **产物路径**：当前工作稿进入 `10-项目/迭代/{迭代号}迭代/{迭代号}-{模块}-资料摘要工作稿.md`；封版后归集到 `10-项目/基线/{模块}/资料摘要.md`；跨模块资料摘要可放 `20-资料/业务文件/{名称}-摘要.md`
- **质量线**：摘要 ≠ 原文拼接；PRD 可直接引用摘要层；关键判断可追溯到 source

### 3. publish-wiki · 发布到知识图谱

- **用途**：将摘要层结晶为 PRD、设计方案、规范、模块 README
- **触发**：资料整理完毕，需要产出交付物
- **关键步骤**：
  1. 从 Summary 层抽取（不从 Source 原文直接搬）
  2. 发布层文件必须有 frontmatter（version / status / last-edited-by / last-edited-at）
  3. 新增或大改时更新模块 README
  4. 影响项目进度时更新 MOC
  5. 涉及业务规则时更新规范 + 决策日志/变更日志
- **质量线**：发布层可直接执行；来源/状态/待确认项清晰；无孤儿文件——从 MOC 或模块入口能找到

### 4. handoff-task · 任务交接

- **用途**：任务完成/暂停/转交/受阻时的交接记录
- **触发**：任务状态变更
- **必填字段**：owner、status（todo/doing/blocked/done）、产物路径、来源/依据、next action、blocked by
- **关键步骤**：
  1. 更新看板任务队列或 Handoff 区
  2. 影响项目进度时同步 MOC
  3. 可复用产出物挂入模块 README
  4. 踩坑/教训写入教训库
- **质量线**：下一个人只看 Handoff 就能接手；必须有产物路径；blocked 项说清谁确认什么

### 5. review-contradictions · 冲突审查

- **用途**：标记和追踪跨资料的不一致（文档/PRD/原型/规范之间）
- **触发**：不同来源对同一功能描述不一致；路径/菜单/模块冲突；agent 产出矛盾
- **关键步骤**：
  1. 不要靠悄悄改 PRD 来掩盖冲突
  2. 记录冲突对象、证据路径、影响范围
  3. 分级：P0（阻断交付）/ P1（影响理解）/ P2（可延后）
  4. 给建议但标注"推断"或"待确认"
  5. 写看板 blocked by；酌情同步决策日志
- **质量线**：冲突必须有证据路径；建议 ≠ 事实；P0/P1 未解决时不发布受影响文档

---

## 二、Claude 命令（`.claude/commands/`）

### 6. /feishu · 飞书文档采集

- **用途**：一条命令完成飞书文档→Vault 的全流程采集
- **语法**：`/feishu <飞书URL> [保存路径]`
- **当前定位**：Claude 斜杠命令入口；通用能力以共享 `feishu-collection` 为准
- **路径 A（CLI 优先）**：`lark-cli docs +fetch --doc "<URL-or-token>" --api-version v2`
- **路径 B（项目脚本归档）**：`python3 .scripts/feishu_to_md.py "<URL>" "<路径>"` — 图片下载 / Obsidian Markdown 归档兜底
- **路径 C（浏览器提取）**：AppleScript + Chrome JS 注入 — CLI/脚本失败后的短文档降级
- **产出**：Markdown + 图片文件夹 + frontmatter
- **质量检查**：大纲可见 / 表格渲染 / 图片显示 / frontmatter 齐全 / 无乱码
- **关联规范**：[[飞书文档采集规范]]、[[教训库]] E-20260516-01

---

## 三、Hermes Skills（`~/.hermes/skills/`）

> 🌊 头孢主平台。33 类 100+ 子技能，以下仅列示例项目 EXAMPLE 相关 + vault 协作相关。

### 示例项目 EXAMPLE 专用（`yijing-dms/`）

#### 7. agent-methodology-collection · AI 方法论采集

- **用途**：从 Web 采集外部 AI agent 方法论，合成后存入 Agent 进化知识库
- **触发**：需要研究/采集框架、设计模式、工程实践
- **流程**：多关键词搜索 → 抓取解析 → 合成 → 写入 `50-经验/Agent进化/`
- **产物模板**：背景/核心概念/与当前体系关系/数据支撑/借鉴分析

#### 8. enterprise-system-field-collection · 企业系统字段采集

- **用途**：访问企业 UAT/测试环境时快速判断可达性，无法访问时切换降级采集
- **触发**：需要登录 SSP/奕派等企业系统采集字段但网络不通
- **流程**：3 步验证（浏览器/curl/API）→ 不通则改截图/导出/API 文档/路径转发
- **SSP 特有策略**：菜单搜索、browser console 提取下拉选项

#### 9. feishu-collection · 飞书/Lark CLI 路由

- **用途**：飞书/Lark 文档、知识库、表格、会议纪要、Markdown、白板的采集、编辑、归档和排障
- **入口**：`~/.hermes/skills/yijing-dms/feishu-collection/SKILL.md`
- **备注**：软链到 `~/.skills-manager/skills/feishu-collection/`；默认优先 `lark-cli`，项目脚本和浏览器为降级路径

#### 10. prd-brand-audit · PRD 品牌合规审查

- **用途**：自动扫描 Markdown PRD 和 HTML 原型，检查 6 条品牌合规规则
- **触发**：PRD/原型生成后；写入 Vault 前的质量门
- **6 条检查项**：品牌用词 / 品牌色值 / 编码规则 / 按钮数量 / 字段表格式 / 章节骨架
- **产出**：自动修复高置信度违规 + JSON 格式 P0/P1 问题报告

#### 11. yijing-prd-spec · PRD 骨架生成与校验

- **用途**：标准化 PRD 产出——6 章固定骨架（背景/流程/用户故事/权限/功能/变更日志）
- **触发**：编写或审查示例项目 EXAMPLE 任意模块 PRD
- **工具**：`prd-init.py` 生成骨架 → 填写章节 → `prd-check.py` 校验
- **入口**：`~/.skills-manager/skills/yijing-prd-spec/SKILL.md`（Hermes 路径为软链）
- **版本**：v3.3.5 跨平台共享完整版
- **产出路径**：当前迭代先写 Vault `10-项目/迭代/260725迭代/`，封版后归集到 `10-项目/基线/{模块}/`；`.md` 为唯一维护格式

### 笔记工具（`note-taking/`）

#### 12. obsidian · Obsidian 笔记操作

- **用途**：读取/搜索/创建 Obsidian vault 笔记，打开 GUI 查看图谱
- **触发**：管理知识图谱、wikilink、图谱可视化
- **能力**：打开 Obsidian.app → 查找/创建/链接笔记 → 验证图谱健康度（笔记数、wikilink 数）

#### 13. obsidian-batch-wikilink-update · Wikilink 批量替换

- **用途**：规范升级后批量替换全 vault 的 wikilink 引用
- **触发**：规范文件版本升级（如 v2.0 → v2.1）需同步所有引用
- **流程**：定位 vault → grep 搜索旧版本 → sed 多模式替换（wikilink/行内代码/纯文本 3 种格式）→ 验证零残留 + 无双替换

#### 14. obsidian-knowledge-synthesis · 知识沉淀合成

- **用途**：将多份零散原始资料（蓝图/截图/笔记）合成为统一的知识沉淀文档
- **触发**：某模块有多份碎片资料，需要整合为结构化参考文档
- **流程**：全 vault 搜索 → 读取所有来源 → 解决冲突（蓝图 vs 实际系统、枚举不一致）→ 产出知识沉淀
- **产出**：当前迭代先写 `10-项目/迭代/{迭代号}迭代/{迭代号}-{模块}-知识沉淀工作稿.md`，封版后归集到 `10-项目/基线/{模块}/{模块名}-知识沉淀.md`；14 节标准模板（定位/流程/子功能矩阵/业务规则/编码/状态机/权限/字段枚举/品牌差异/接口/决策点/设计输入/关联模块/变更日志）

### 生产力工具（`productivity/`）

#### 15. ai-execution-hallucination-audit · AI 幻觉审计

- **用途**：系统性审计 AI"声称完成但实际未执行"的幻觉，识别根因，强制预防
- **触发**：怀疑 AI 声称完成了但没做；多步文件操作前的预防
- **6 阶段流程**：
  1. 分离记忆与执行
  2. 文件真实性扫描（ground-truth）
  3. 会话日志考古
  4. 根因分类（模拟执行 / 静默失败 / 陈旧记忆 / 路径混淆 / 幻影迭代）
  5. 诚实报告
  6. 强制三元组原子性（执行→验证→确认）

### Hermes 其他高频 Skills（按需使用）

| 类别 | 技能 | 说明 |
|------|------|------|
| `creative/` | excalidraw, claude-design, ascii-art | 创意图表和设计 |
| `github/` | github-pr-workflow, codebase-inspection | GitHub PR 流程和代码检查 |
| `media/` | edge-tts, whisper-stt | 语音合成/识别 |
| `hermes/` | gbrain, self-evolution, rtk | Hermes 核心运维 |
| `research/` | 多个 | Web 研究和信息采集 |
| `software-development/` | 17 个子 skill | 代码生成/测试/部署 |

---

## 四、OpenClaw Skills（`~/.openclaw/skills/`）

> 🐛 阿莫西林主平台。3 个核心 skill。

### 16. feishu-collection（OpenClaw 入口）

- **同 Codex / Vault / Hermes 版**，软链到共享主目录
- **入口**：`~/.openclaw/skills/feishu-collection/SKILL.md`

### 17. flowforge · 自动化编码流水线

- **用途**：Spec → Plan → Code → QA 全自动化流水线，通过 Claudian 执行
- **触发**：新功能/重构/bug fix；"forge"、"plan and build"、"auto-implement"
- **流程**：
  1. 初始化工作区：`bash ~/.openclaw/skills/flowforge/scripts/init_forge.sh "<描述>" "<repo>"`
  2. 澄清：问 2-4 个针对性问题（范围/约束/集成点/成功定义）
  3. 执行流水线：`bash ~/.openclaw/skills/flowforge/scripts/run_forge.sh ~/.forge/<时间戳>/`
     → 链式调用 4 次 Claudian（Spec/Plan/Code/QA）
  4. 监控：`tail -f ~/.forge/<时间戳>/progress.log`
  5. 可选：`--rubric` 启用 200 条准则评分（≥180 Ship / 150-179 返工 / <150 大改）
- **产物**：clarifications.md, spec.md, implementation_plan.json, qa_report.md
- **账户轮换**：3 个 Claude Max 账户自动轮换应对限流，配置在 `~/.flowforge/accounts.txt`

### 18. gbrain · GBrain 运维

- **用途**：GBrain CLI 的健康检查、自修复、监控封装
- **入口**：`bash ~/.openclaw/skills/gbrain/run.sh <子命令>`
- **子命令**：
  - `check` — 全量健康检查（默认）
  - `fix` — 自修复 + 迁移
  - `save` — 保存报告到 `50-经验/gbrain-reports/`
  - `help` — 使用帮助
  - 其它参数直接透传 `gbrain` CLI
- **数据**：`~/.gbrain/brain.pglite/`
- **状态**：实验性，完整 GBrain 暂停在实验目录

---

## 五、本机共享 Skills（`~/.skills-manager/skills/`）

> 作为跨智能体共享 skill 的主目录。各平台目录通过软链指向这里，避免多份 skill 漂移。

### 19. feishu-collection · 飞书/Lark CLI 路由

- **用途**：飞书/Lark 文档、知识库、表格、会议纪要、Markdown、白板的采集、编辑、归档和排障
- **主入口**：`~/.skills-manager/skills/feishu-collection/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/feishu-collection`；Vault `.skills/feishu-collection`；Hermes `~/.hermes/skills/yijing-dms/feishu-collection`；OpenClaw `~/.openclaw/skills/feishu-collection`；WorkBuddy `~/.workbuddy/skills/feishu-collection`
- **工具**：`lark-cli` 优先；`.scripts/feishu_to_md.py` 仅作项目归档/图片下载兜底
- **备份**：旧 Hermes/OpenClaw 实体目录在 `~/.hermes/skills/.backups/feishu-collection-20260608-before-share/`

### 20. markitdown · Microsoft MarkItDown 文件转 Markdown

- **用途**：将 PDF、DOCX、PPTX、XLS/XLSX、HTML、CSV/JSON/XML、ZIP、EPUB、图片、音频、YouTube transcript 等转换/抽取为 LLM 友好的 Markdown
- **主入口**：`~/.skills-manager/skills/markitdown/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/markitdown`；Vault `.skills/markitdown`；Hermes `~/.hermes/skills/software-development/markitdown`；OpenClaw `~/.openclaw/skills/markitdown`；WorkBuddy `~/.workbuddy/skills/markitdown`
- **工具**：`markitdown 0.1.6`，通过 `uv tool install --upgrade --prerelease=allow 'markitdown[all]==0.1.6'` 安装
- **备注**：Feishu/Lark 仍优先走 `feishu-collection`；音频/视频类转换可能需要补装 `ffmpeg`

### 21. agent-reach · 外部平台搜索/阅读路由

- **用途**：给 agent 提供网页、GitHub、YouTube/B站、RSS、V2EX、微信公众号、Exa 搜索和可选社媒平台的读取/搜索路由
- **主入口**：`~/.skills-manager/skills/agent-reach/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/agent-reach`；Vault `.skills/agent-reach`；Hermes `~/.hermes/skills/research/agent-reach`；OpenClaw `~/.openclaw/skills/agent-reach`；WorkBuddy `~/.workbuddy/skills/agent-reach`；Claude `~/.claude/skills/agent-reach`；Agents `~/.agents/skills/agent-reach`
- **工具**：`agent-reach 1.4.0`（本地 editable 安装自 `~/.agent-reach/tools/agent-reach`）；`yt-dlp 2026.03.17`；`mcporter` home scope 配置 Exa
- **当前状态**：`agent-reach doctor` 显示 8/16 渠道可用；Cookie 类平台（Twitter/X、小红书、雪球等）未配置
- **备注**：GitHub main 直接 wheel 构建存在重复 include 问题，CLI 采用本地 clone + editable 安装；自动生成的 Claude/Agents 实体目录已备份并替换为共享软链

### 22. last30days · 最近 30 天趋势研究

- **用途**：围绕任意主题聚合 Reddit、X、YouTube、TikTok、HN、Polymarket、GitHub、Web 等最近 30 天信号，产出带社区热度和引用的研究简报
- **主入口**：`~/.skills-manager/skills/last30days/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/last30days`；Vault `.skills/last30days`；Hermes `~/.hermes/skills/research/last30days`；OpenClaw `~/.openclaw/skills/last30days`；WorkBuddy `~/.workbuddy/skills/last30days`；Claude `~/.claude/skills/last30days`；Agents `~/.agents/skills/last30days`
- **版本**：v3.3.2；脚本入口 `python3 ~/.skills-manager/skills/last30days/scripts/last30days.py`
- **备注**：零配置可用源以 Reddit/HN/Polymarket/GitHub/Web 等为主；X/TikTok/Instagram/Perplexity 等需要额外 token 或 cookie

### 23. paddleocr-text-recognition · 图片/截图/扫描件文字识别

- **用途**：给不具备原生识图能力的 Reasonix / Hermes Desktop 等 agent 提供图片、截图、扫描件、无文字层 PDF 的文字抽取
- **主入口**：`~/.skills-manager/skills/paddleocr-text-recognition/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/paddleocr-text-recognition`；Vault `.skills/paddleocr-text-recognition`；Hermes `~/.hermes/skills/productivity/paddleocr-text-recognition`；Hermes Desktop `~/Library/Application Support/hermes-desktop/skills/paddleocr-text-recognition`；Reasonix `.agents/skills` 与 `.claude/skills`；OpenClaw `~/.openclaw/skills/paddleocr-text-recognition`；WorkBuddy `~/.workbuddy/skills/paddleocr-text-recognition`；Claude `~/.claude/skills/paddleocr-text-recognition`；Agents `~/.agents/skills/paddleocr-text-recognition`
- **工具**：`paddleocr 3.6.0` + `paddlepaddle 3.3.1`；本地 helper `uv tool run --from paddleocr python ~/.skills-manager/skills/paddleocr-text-recognition/scripts/local_ocr.py <image>`
- **当前状态**：本地 mobile OCR 冒烟和长截图实测通过（`AgentOCR123`、`last30days-skitl`、`Agent-Reach`）；云 API 仍需 `PADDLEOCR_ACCESS_TOKEN`
- **质量线**：输出必须标注 `[OCR:PaddleOCR-local]` / `[OCR:PaddleOCR-api]` / `[Vision:multimodal]` / `[Hybrid]` / `[Unverified]`；只用 OCR 时不能声称完整视觉理解

### 24. paddleocr-doc-parsing · 扫描文档结构化解析

- **用途**：处理扫描 PDF、复杂图片文档、表格、公式、印章、图表、多栏版面、阅读顺序等结构化解析
- **主入口**：`~/.skills-manager/skills/paddleocr-doc-parsing/SKILL.md`
- **共享入口**：同 `paddleocr-text-recognition`，各平台均为软链指向共享主目录
- **工具**：`paddleocr api --model_type doc_parsing ...`；本地 `PPStructureV3` / `PaddleOCRVL` 可 import，但复杂解析默认走云 API
- **当前状态**：需 `PADDLEOCR_ACCESS_TOKEN` 才能完整调用官方文档解析 API；无 token 时只能降级到本地文字 OCR 或 Hermes 既有 `ocr-and-documents` 路由
- **质量线**：字段/表格/金额/编号/日期以 OCR/parser 为准；版面关系、视觉层级、图标意图必须另标 `[Vision:multimodal]` 或 `[Hybrid]`

### 25. yijing-prd-spec · PRD 规范与校验

- **用途**：示例项目 EXAMPLE PRD 编写、评审、升级、骨架生成和格式校验
- **主入口**：`~/.skills-manager/skills/yijing-prd-spec/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/yijing-prd-spec`；Vault `.skills/yijing-prd-spec`；Hermes `~/.hermes/skills/yijing-dms/yijing-prd-spec` 与 `~/.hermes/skills/software-development/gstack-openclaw-skills/yijing-prd-spec`；OpenClaw `~/.openclaw/skills/yijing-prd-spec`；WorkBuddy `~/.workbuddy/skills/yijing-prd-spec`
- **版本**：v3.3.5 跨平台共享完整版，含 `scripts/`、`references/`、`templates/`
- **备份**：旧 v3.1.0 / gstack 适配版实体目录在 `~/.hermes/skills/.backups/yijing-prd-spec-20260608-before-share/`

### 26. taste-skill · UI 审美与一致性守门

- **用途**：前端/HTML 原型/应用 UI 编码、评审、视觉修复时，收敛颜色、字体、图标、圆角、间距和布局密度
- **触发**：用户提到 taste、审美、视觉一致性、颜色很乱、字体很多、图标乱用、页面不够精致；或 agent 修改 UI 前需要先做设计系统发现
- **主入口**：`~/.skills-manager/skills/taste-skill/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/taste-skill`；Vault `.skills/taste-skill`；Hermes `~/.hermes/skills/software-development/taste-skill`；OpenClaw `~/.openclaw/skills/taste-skill`；WorkBuddy `~/.workbuddy/skills/taste-skill`
- **工具**：`python3 ~/.skills-manager/skills/taste-skill/scripts/visual_audit.py <project-or-file>`
- **质量线**：先读现有设计系统；优先使用 token/component；不新增随机色值、字体族或图标库；前端变更后做浏览器视觉验收

### 27. flowforge · 自动化编码流水线

- **用途**：Spec → Plan → Code → QA 自动化编码流水线，适合新功能、重构和 bug fix
- **主入口**：`~/.skills-manager/skills/flowforge/SKILL.md`
- **共享入口**：Codex `~/.codex/skills/flowforge`；OpenClaw `~/.openclaw/skills/flowforge`；WorkBuddy `~/.workbuddy/skills/flowforge`
- **备注**：当前按代码执行类 agent 共享；如需纳入 Hermes/Vault 路由，先补入口再更新本清单和 `.skills/RESOLVER.md`

---

## 六、平台对照

| 平台 | 主要 Agent | Skill 数量 | 存放位置 | 特点 |
|------|-----------|-----------|---------|------|
| **本机共享** `~/.skills-manager/skills/` | 多智能体共享 | 9 个共享 skill | 用户目录 | 作为主目录，平台目录用软链引用 |
| **Vault** `.skills/` | 所有 agent 共用 | 15 个工作流/共享 skill | vault 内 | 协作工作流骨架，与看板联动 |
| **Claude** `.claude/` | ⚡Claudian / Claude Code | 1 个命令 + 4 个共享 skill | 用户目录 + vault 内 | `/feishu` 斜杠命令；Agent-Reach / Last30Days / PaddleOCR 入口 |
| **Agents** `~/.agents/skills/` | Agent Skills hosts | 4 个共享 skill | 用户目录 | Agent-Reach / Last30Days / PaddleOCR 通用入口 |
| **Codex** `~/.codex/skills/` | Codex | 9 个本机共享 skill + 系统 skill | 用户目录 | 编码和本机工具调用 |
| **Hermes** `~/.hermes/skills/` | 🌊 头孢 | 33 类 100+ + 共享 skill | 用户目录 | 最丰富，含通用 + EXAMPLE 专用 |
| **Hermes Desktop** `~/Library/Application Support/hermes-desktop/skills/` | Hermes Desktop | 2 个共享 skill | 用户目录 | PaddleOCR 识图补盲入口 |
| **Reasonix** global workspace | 克拉霉素 / Reasonix | 2 个共享 skill | App Support | `.agents/skills` 与 `.claude/skills` 双入口挂 PaddleOCR |
| **OpenClaw** `~/.openclaw/skills/` | 🐛 阿莫西林 | 10 个核心/共享 skill | 用户目录 | 飞书/Lark + 外部搜索 + 趋势研究 + 文件转换 + OCR + PRD + 代码流水线 + GBrain + UI 审美守门 |
| **WorkBuddy** `~/.workbuddy/skills/` | WorkBuddy / Claudian | 平台私有 skill + 9 个共享 skill | 用户目录 | 崩溃修复、Agent 排障、漂移检查 |

### Skill 去重说明

| Skill 名 | 部署位置 | 共享状态 |
|----------|---------|---------|
| agent-reach | Codex + Vault + Hermes + OpenClaw + WorkBuddy + Claude + Agents（7 处软链） | 共享 `~/.skills-manager/skills/agent-reach/`；CLI `agent-reach 1.4.0`；doctor 当前 8/16 渠道可用 |
| flowforge | Codex + OpenClaw + WorkBuddy（3 处软链） | 共享 `~/.skills-manager/skills/flowforge/`；OpenClaw 旧实体副本已备份到 `~/.openclaw/skills/.backups/flowforge-20260608-before-share/` |
| last30days | Codex + Vault + Hermes + OpenClaw + WorkBuddy + Claude + Agents（7 处软链） | 共享 `~/.skills-manager/skills/last30days/`；官方 v3.3.2 |
| markitdown | Codex + Vault + Hermes + OpenClaw + WorkBuddy（5 处软链） | 共享 `~/.skills-manager/skills/markitdown/`；CLI 执行器为 `~/.local/bin/markitdown` |
| paddleocr-text-recognition | Codex + Vault + Hermes + Hermes Desktop + Reasonix + OpenClaw + WorkBuddy + Claude + Agents（10 处软链） | 共享 `~/.skills-manager/skills/paddleocr-text-recognition/`；本地 helper 已验证；云 API 需 `PADDLEOCR_ACCESS_TOKEN` |
| paddleocr-doc-parsing | Codex + Vault + Hermes + Hermes Desktop + Reasonix + OpenClaw + WorkBuddy + Claude + Agents（10 处软链） | 共享 `~/.skills-manager/skills/paddleocr-doc-parsing/`；官方文档解析 API 需 `PADDLEOCR_ACCESS_TOKEN` |
| taste-skill | Codex + Vault + Hermes + OpenClaw + WorkBuddy（5 处软链） | 共享 `~/.skills-manager/skills/taste-skill/` |
| feishu-collection | Codex + Vault + Hermes + OpenClaw + WorkBuddy（5 处软链） | 共享 `~/.skills-manager/skills/feishu-collection/`；旧分叉已退役备份 |
| yijing-prd-spec | Codex + Vault + Hermes + OpenClaw + WorkBuddy（6 处软链，Hermes 有两个入口） | 共享 `~/.skills-manager/skills/yijing-prd-spec/`；统一 v3.3.5 |
| gbrain | Hermes(`hermes/gbrain/`) + OpenClaw(`gbrain/`) | 共享 `~/.gbrain/brain.pglite/` 数据库；OpenClaw 侧以 `run.sh` 为执行入口 |

### 安装/升级规则

1. 新增 skill 前先查本文件、`.skills/RESOLVER.md`、`~/.skills-manager/skills/`、`~/.codex/skills/`、`~/.hermes/skills/`、`~/.openclaw/skills/`、`~/.workbuddy/skills/`、`~/.claude/skills/`、`~/.agents/skills/`、`~/Library/Application Support/reasonix/global-workspace/`、`~/Library/Application Support/hermes-desktop/skills/`。
2. 若 skill 可能被多个智能体调用，主目录必须放在 `~/.skills-manager/skills/{skill-name}/`，各平台目录只建软链。
3. 只服务单一平台的 skill 可以保留在平台目录，但必须在本文件标注为“平台私有”或“平台分叉版”。
4. 同名 skill 如保留多份实体目录，必须说明版本差异、主入口、共享资源和同步责任，不能默认为已共享。
5. 安装或升级后必须同步更新本文件；涉及 Vault 路由的，还要更新 `.skills/RESOLVER.md`。
6. 校验命令：

```bash
find ~/.skills-manager/skills ~/.codex/skills ~/.openclaw/skills ~/.workbuddy/skills ~/.hermes/skills ~/.claude/skills ~/.agents/skills ~/Desktop/obsidianVault/.skills ~/Library/Application\ Support/reasonix/global-workspace ~/Library/Application\ Support/hermes-desktop/skills -maxdepth 3 \( -name SKILL.md -o -type l \) -print
```

---

## 七、Skill 生命周期

| 状态 | 含义 | 当前 Skill |
|------|------|-----------|
| 🟢 Active | 生产可用 | start-task, ingest-source, publish-wiki, handoff-task, review-contradictions, /feishu, agent-reach, feishu-collection, last30days, markitdown, paddleocr-text-recognition, paddleocr-doc-parsing, prd-brand-audit, yijing-prd-spec, obsidian-knowledge-synthesis, flowforge, taste-skill |
| 🟡 Experimental | 实验中，功能不完整 | gbrain, enterprise-system-field-collection |
| ⚪ Passive | 按需调用，非核心链路 | obsidian, obsidian-batch-wikilink-update, ai-execution-hallucination-audit, agent-methodology-collection |

---

## 八、关联文件

- **Skill 路由**：`.skills/RESOLVER.md` — agent 不知道用哪个 skill 时先读这个
- **共享技能目录**：`~/.skills-manager/skills/` — 跨 Codex / Vault / Hermes / Hermes Desktop / Reasonix / OpenClaw / WorkBuddy 复用的主目录
- **Agent 注册表**：`30-规范/agents-registry.md` — 谁是谁、能干啥
- **工具路径**：`30-规范/agent-paths.md` — 命令/数据/配置路径统一登记
- **协作机制**：`00-MOC/多智能体协作机制.md` — 黑板模式规则
- **协作看板**：`00-MOC/多智能体协作看板.md` — 任务队列和交接记录
