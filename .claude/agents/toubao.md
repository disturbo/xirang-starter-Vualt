# toubao — LEGACY REDIRECT

> **本文件已废弃。头孢的正式 prompt 入口在 `~/.hermes/SOUL.md`，手动维护。**
>
> 头孢运行在 Hermes CLI 平台，不是 Claudian。本文件保留仅为避免引用断裂。
> 如需修改头孢配置，编辑：`.prompt-src/agents/toubao.delta.md`（delta 部分）+ `~/.hermes/SOUL.md`（平台宿主）

---

## 调用方式

```bash
hermes chat -q "任务描述" -Q --max-turns 5
```

## 正式入口

- **System prompt**: `~/.hermes/SOUL.md`（v3.0，手动维护）
- **Delta 源**: `.prompt-src/agents/toubao.delta.md`
- **V8 pre-flight**: 已内嵌 SOUL.md 顶部
