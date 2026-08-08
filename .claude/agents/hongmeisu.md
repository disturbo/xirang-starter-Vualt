# hongmeisu — LEGACY REDIRECT

> **本文件已废弃。红霉素的正式 prompt 入口在 `.codex/instructions.md`，由 `prompt-build.py` 生成维护。**
>
> 红霉素运行在 Codex 平台，不是 Claudian。本文件保留仅为避免引用断裂。
> 如需修改红霉素配置，编辑：`.prompt-src/agents/hongmeisu.delta.md` → 运行 `python3 .prompt-src/prompt-build.py --apply`

---

## 调用方式

```bash
codex exec -C $HOME/Desktop/obsidianVault "任务描述"
```

## 正式入口

- **System prompt**: `.codex/instructions.md`（AUTO-GENERATED）
- **Delta 源**: `.prompt-src/agents/hongmeisu.delta.md`
- **Sandbox**: `~/.codex/config.toml` 已配置 `sandbox_permissions`
