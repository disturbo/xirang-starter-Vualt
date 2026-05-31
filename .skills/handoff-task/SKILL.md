# Handoff Task

## When

任务完成、暂停、转交、被阻塞，或需要其它 agent 接手时使用。

## Required Fields

```markdown
📦 {任务名}
- owner: {当前负责人}
- status: todo / doing / blocked / done
- 产物路径: {vault 或沙箱路径}
- 来源/依据: {source / summary / PRD / 规范}
- next action: {下一步最小动作}
- blocked by: {无 / 需波波确认 / 需某 agent / 需资料}
- 风险: {可选}
```

## Steps

1. 更新 `00-MOC/多智能体协作看板.md` 的任务队列或交接记录。
2. 如果任务改变项目进度，同步 `00-MOC/{项目名}-MOC.md`。
3. 如果产出可复用，补到模块 README/模块笔记。
4. 如果失败或踩坑，补到 `50-经验/教训库.md`。

## Quality Bar

- 别人只读 handoff 就能继续干。
- 必须有产物路径。
- blocked 不能写“待确认”就结束，要写清楚谁确认什么。
