# /feishu — 飞书/Lark 内容采集到 Obsidian Vault

## 用法

```bash
/feishu <飞书或 Lark URL> [保存路径]
```

## 当前定位

这是 Claude 侧的斜杠命令入口。完整流程以共享 skill 为准：

```bash
~/.skills-manager/skills/feishu-collection/SKILL.md
```

执行时先读该 skill；不要只按本文件硬编码路径。

## 执行流程

### Step 1: CLI 健康检查

```bash
lark-cli doctor
```

若 doctor 不通过，先报告失败项；不要直接进入浏览器提取。

### Step 2: CLI 优先读取

文档或知识库 URL：

```bash
lark-cli docs +fetch --doc "<URL-or-token>" --api-version v2
```

按内容类型选择其他 CLI：

```bash
lark-cli wiki --help
lark-cli sheets --help
lark-cli minutes --help
lark-cli markdown --help
lark-cli whiteboard --help
```

### Step 3: 项目归档脚本兜底

仅当需要图片下载、Obsidian Markdown 归档，或 CLI 不适合当前文档时使用：

```bash
cd ~/Desktop/obsidianVault
python3 .scripts/feishu_to_md.py "<URL>" "<保存路径>"
```

默认路径：

- PRD/产品文档：`20-资料/业务文件/{标题}.md`
- 会议纪要：`20-资料/会议纪要/YYYYMMDD-{主题}.md`
- 外部系统接口：`20-资料/外部系统接口/{系统或主题}/`

### Step 4: 浏览器/手动降级

只有 CLI 和项目脚本都不适合时才考虑浏览器。浏览器提取前必须先跑前置检查：

```bash
osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "1+1"' 2>&1
```

前置检查失败时，直接让用户手动复制：

```bash
pbpaste > /tmp/feishu.txt
```

## 质量检查

- [ ] 保存路径符合 vault 规则
- [ ] frontmatter 齐全：title/source/fetched/type
- [ ] 表格前有空行，Obsidian 可渲染
- [ ] 图片或媒体无失效 token 残留
- [ ] 长文档不要依赖浏览器虚拟滚动提取

## 关联

- 共享 skill：`~/.skills-manager/skills/feishu-collection/SKILL.md`
- 完整规范：[[30-规范/飞书文档采集规范]]
- 项目脚本：`.scripts/feishu_to_md.py`
