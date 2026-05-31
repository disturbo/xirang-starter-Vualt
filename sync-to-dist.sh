#!/bin/bash
# sync-to-dist.sh — 将 Vault 基建文件同步到 xirang-dist 分发仓库
# 用法: bash sync-to-dist.sh
# 只同步基建，不动个人项目数据。可重复执行。

set -euo pipefail

VAULT="${VAULT:-$HOME/Desktop/obsidianVault}"
DIST="${DIST:-$HOME/Desktop/xirang-dist}"

echo "=== 息壤分发同步 ==="
echo "源: $VAULT"
echo "目标: $DIST"
echo ""

# 确保目标目录存在
mkdir -p "$DIST"

# --- 需要同步的目录 ---
DIRS=(
  ".claude"
  ".codex"
  ".standards"
  ".prompt-src"
  ".skills"
  "00-MOC"
  "30-规范"
  "50-经验/Agent协作方法论"
  "90-模板"
)

# --- 需要同步的根文件 ---
FILES=(
  "AGENTS.md"
  "🚀启动说明.md"
)

# --- 同步目录 ---
for dir in "${DIRS[@]}"; do
  echo "[sync] $dir/"
  mkdir -p "$DIST/$dir"
  rsync -a --delete \
    --exclude=".DS_Store" \
    "$VAULT/$dir/" "$DIST/$dir/"
done

# --- 同步根文件 ---
for file in "${FILES[@]}"; do
  if [[ -f "$VAULT/$file" ]]; then
    echo "[sync] $file"
    cp "$VAULT/$file" "$DIST/$file"
  fi
done

# --- 排除不该给的文件 ---
echo ""
echo "[clean] 移除看板..."
rm -f "$DIST/00-MOC/多智能体协作看板.md"

# --- 移除 settings.local.json（如果有）---
rm -f "$DIST/.claude/settings.local.json"

echo ""
echo "=== 同步完成 ==="
echo "文件数: $(find "$DIST" -type f | wc -l | tr -d ' ')"
echo ""
echo "下一步:"
echo "  1. 检查敏感路径: grep -r '/Users/' \"$DIST/\""
echo "  2. 如有新文件需排除，编辑本脚本的 rm -f 行"
