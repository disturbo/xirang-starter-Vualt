# Pre-flight Auto-Platform Template

> 本文件是自动平台（Claudian/WorkBuddy/青霉素/红霉素）的 pre-flight 流程模板。
> 手动平台（OpenClaw/Hermes/CLAUDE.md）在各自的目标文件中维护专属 pre-flight。
> {{变量}} 在 prompt-build.py 生成时替换。

### 写入声明格式（M3: 一行 / M4-M5: 完整块）

```
M3（改 1 个文件）：
  V9 写入声明：{文件路径} | {动作摘要}

M4（多文件产出）：
  V9 已激活：
  - 档位：M4
  - 任务：{任务名}
  - 写入范围：{将写入的文件/路径列表}
  - 验收方：{用户 / 具体Agent名}

M5（跨 Agent / 长任务）：
  V9 已激活：
  - 档位：M5
  - 任务：{任务名}
  - 写入范围：{路径}
  - 拆分计划：{子任务数} 个子任务
  - 验收方：{验收链}
```

### Pre-flight 三步（M4/M5）

```
1. 亮灯：更新 {{STATUS_PATH}}/{{AGENT_CN}}.md — status -> busy
   追加事件：{{STATUS_PATH}}/智能体事件.jsonl
   {"ts":"ISO","event":"task_start","agent":"{{AGENT_ID}}","task_id":"T-YYYYMMDD-NN","task":"任务名","task_size":"S|M|L"}

2. 声明：在回复开头输出写入声明（上方格式）

3. 执行：开始写文件
```

### 收工（M4/M5）

```
1. 更新状态：status -> idle, current_task -> null
2. 追加 task_end 事件（记录任务、结果与身份）
3. 看板 Handoff：{{KANBAN_PATH}}（产物路径 + 验证 + next action）
```

### M3 收工

```
追加一行到 {{STATUS_PATH}}/../运行日志/YYYY-MM-DD.md：
  - HH:MM | M3 | 动作摘要 | 结果 | 产物路径(可选)
```
