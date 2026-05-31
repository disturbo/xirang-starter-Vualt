# /feishu — 飞书文档采集到 Obsidian Vault

## 用法

```
/feishu <飞书URL> [保存路径]
```

## 参数

- `<飞书URL>`：必填。支持两种格式：
  - `https://xxx.feishu.cn/wiki/TOKEN`（知识库文档）
  - `https://xxx.feishu.cn/docx/TOKEN`（独立文档）
- `[保存路径]`：可选。默认根据文档类型自动选择：
  - PRD/产品文档 → `20-资料/业务文件/{标题}.md`
  - 会议纪要 → `20-资料/会议纪要/YYYYMMDD-{主题}.md`

## 执行流程

请严格按以下步骤执行：

### Step 1: 识别 URL 类型和租户

从 URL 中提取：
- **路径类型**：`/wiki/` 或 `/docx/`
- **租户域名**：`xxx.feishu.cn` 的 `xxx` 部分
- 判断是否为已知租户（`acno1000gd58` 有 APP 凭证）

### Step 2: 选择采集路径

**路径 A（API 全自动）**——同租户或有凭证：
```bash
python3 .scripts/feishu_to_md.py "<URL>" "<保存路径>"
```

**路径 B（浏览器提取）**——跨租户或 API 失败时降级：
1. 确认 Chrome 已打开该文档：
   ```bash
   osascript -e 'tell application "Google Chrome" to get URL of active tab of front window'
   ```
2. 通过 AppleScript 注入 JavaScript 提取内容：
   ```bash
   osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "
       var el = document.querySelector(\"[data-content-editable-root]\");
       el ? el.innerText : \"NOT_FOUND\";
   "'
   ```
3. Python 后处理：bullet 符号（• ◦ ▪）转 Markdown 列表，清理零宽字符
4. 添加 frontmatter，保存到目标路径

### Step 3: 图片处理（仅路径 A）

脚本自动下载图片到 `{文档名}-images/` 目录，使用 `![[路径/img_001.png]]` wikilink 嵌入。

如路径 B 提取，告知用户"图片需手动截图或另行处理"。

### Step 4: 后处理

1. **标题编号**（大型文档 >50 标题时）：
   - 跳过第一个 H1（文档标题不编号）
   - 从第二个 H1 开始编号：1, 2, 3...
   - 子标题：1.1, 1.2, 1.1.1...

2. **格式修正**：
   - 确保表格前有空行
   - 非内容性标题（通知/注释）转为 `> [!warning]` callout

### Step 5: 质量检查

逐项确认并报告：
- [ ] 大纲可见（标题层级正确）
- [ ] 表格渲染正常（抽查 3 个）
- [ ] 图片显示正常（抽查 5 个，或确认无 token 残留）
- [ ] frontmatter 齐全（title/source/fetched/type）
- [ ] 无乱码

## 关联规范

- 完整规范：[[30-规范/飞书文档采集规范]]
- 采集脚本：`.scripts/feishu_to_md.py`
- 教训参考：[[50-经验/教训库]] § E-20260516-01

## 示例

```
/feishu https://acno1000gd58.feishu.cn/wiki/VfibwtkJdiBGGFk5gyZchyaDnNb
→ 自动识别: wiki 类型, 同租户, 走 API 路径
→ 产出: 20-资料/业务文件/{项目}服务模块PRD-飞书源文档.md + 260 张图片

/feishu https://dongfengyipai.feishu.cn/docx/Yec5d1iLwoFzBNxR4vfcXSIbnLb
→ 自动识别: docx 类型, 跨租户, API 失败后降级浏览器提取
→ 产出: 20-资料/会议纪要/20260514-智能纪要-xxx.md（无图片）
```
