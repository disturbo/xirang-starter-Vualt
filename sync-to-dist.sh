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
  ".scripts"
  ".standards"
  ".prompt-src"
  ".skills"
  "00-MOC"
  "02-项目管理/脚本"
  "02-项目管理/巡检"
  "30-规范"
  "50-经验/Agent协作方法论"
  "90-模板"
)

# --- 需要同步的根文件 ---
FILES=(
  "AGENTS.md"
  "🚀启动说明.md"
)

RSYNC_EXCLUDES=(
  --exclude=".DS_Store"
  --exclude="__pycache__/"
  --exclude="*.pyc"
  --exclude="settings.local.json"
  --exclude="projects/"
  --exclude="sessions/"
  --exclude="logs/"
  --exclude="cache/"
  --exclude="statsig/"
  --exclude=".claudian/"
  --exclude=".claudian/**"
  --exclude=".obsidian/plugins/**/data*.json"
  --exclude="智能体约束/*MEMORY.md"
  --exclude="health-latest.json"
  --exclude="reflex-state.json"
  --exclude=".reflex.lock"
  --exclude="launchd.*.log"
)

# --- 同步目录 ---
for dir in "${DIRS[@]}"; do
  if [[ ! -d "$VAULT/$dir" ]]; then
    echo "[skip] $dir/ (source missing)"
    continue
  fi
  echo "[sync] $dir/"
  mkdir -p "$DIST/$dir"
  rsync -a --delete \
    "${RSYNC_EXCLUDES[@]}" \
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

# --- 移除本地运行时和会话记录（如果有）---
rm -f "$DIST/.claude/settings.local.json"
rm -rf "$DIST/.claude/projects"
rm -rf "$DIST/.claude/sessions"
rm -rf "$DIST/.claude/logs"
rm -rf "$DIST/.claude/cache"
rm -rf "$DIST/.claude/statsig"
rm -rf "$DIST/.claudian"
rm -f "$DIST"/30-规范/智能体约束/*MEMORY.md

if [[ -d "$DIST/.obsidian/plugins" ]]; then
  find "$DIST/.obsidian/plugins" -type f -name "data*.json" -delete
fi

# --- 敏感信息扫描：旧租户、硬编码凭证、会话记录、个人账号 ---
echo "[scan] 敏感信息..."
SENSITIVE_PATTERN='(https://[a-z0-9]+\.feishu\.cn/(wiki|docx)/[A-Za-z0-9_-]{12,}|cli_[a-z0-9]{12,}|"app_secret"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]{20,}"|APP_SECRET[[:space:]]*=[[:space:]]*"[A-Za-z0-9_-]{20,}"|Session ID|Conversation Summary|openclaw-memory-promotion|\.claudian/sessions|@im\.wechat|Bot [0-9]{6,})'
if grep -RInIE "$SENSITIVE_PATTERN" "$DIST" --exclude-dir=".git" >/tmp/xirang-dist-sensitive-scan.txt 2>/dev/null; then
  echo "ERROR: 分发目录仍包含敏感信息，已停止。命中如下："
  cat /tmp/xirang-dist-sensitive-scan.txt
  exit 1
fi
rm -f /tmp/xirang-dist-sensitive-scan.txt

echo ""
echo "=== 同步完成 ==="
echo "文件数: $(find "$DIST" -type f | wc -l | tr -d ' ')"
echo ""
echo "下一步:"
echo "  1. 检查敏感路径: grep -RIn '/Users/' \"$DIST/\""
echo "  2. 如有新文件需排除，编辑本脚本的 RSYNC_EXCLUDES 或 clean 区"
