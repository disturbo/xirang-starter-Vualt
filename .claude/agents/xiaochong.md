# xiaochong — LEGACY REDIRECT

> **本文件已废弃。阿莫西林的正式 prompt 入口在 `~/.openclaw/workspace/AGENTS.md`，手动维护。**
>
> 阿莫西林运行在 OpenClaw 平台，不是 Claudian。本文件保留仅为避免引用断裂。
> 如需修改阿莫西林配置，编辑：`.prompt-src/agents/xiaochong.delta.md`（delta 部分）+ `~/.openclaw/workspace/AGENTS.md`（平台宿主）

---

## 调用方式

```bash
openclaw agent xiaochong "任务描述"
```

## 正式入口

- **System prompt**: `~/.openclaw/workspace/AGENTS.md`（v3.0，手动维护）
- **Delta 源**: `.prompt-src/agents/xiaochong.delta.md`
- **V8 pre-flight**: 已内嵌 AGENTS.md 顶部
- **v8-runtime 脚本**: `~/.openclaw/workspace/skills/v8-runtime/scripts/`
