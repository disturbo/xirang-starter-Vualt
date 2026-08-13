#!/bin/bash
# sync-to-dist.sh — 将 Vault 基建文件同步到 xi-rang-v9-starter 分发仓库
# 用法: bash sync-to-dist.sh
# 只同步基建，不动个人项目数据。可重复执行。

set -euo pipefail

VAULT="${VAULT:-$HOME/Desktop/obsidianVault}"
DIST="${DIST:-$HOME/Desktop/xi-rang-v9-starter}"

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
  "02-项目管理/脚本"
  "30-规范"
  "50-经验/Agent协作方法论"
  "90-模板"
)

# --- 需要同步的根文件 ---
FILES=(
  "setup.sh"
  "02-项目管理/巡检/README.md"
  "02-项目管理/evals/README.md"
  "50-经验/Agent进化/不死鸟Phoenix-借鉴评估报告.md"
  "50-经验/Agent进化/不死鸟Phoenix-技术文档.md"
)

RSYNC_EXCLUDES=(
  --exclude=".DS_Store"
  --exclude="__pycache__/"
  --exclude="*.pyc"
  --exclude="settings.local.json"
  --exclude="settings.json"
  --exclude="飞书文档采集规范.md"
  --exclude="agents/dongfeng.md"
  --exclude="agents/dongfeng.delta.md"
  --exclude="奕境门店组织架构.png"
  --exclude="projects/"
  --exclude="sessions/"
  --exclude="logs/"
  --exclude="cache/"
  --exclude="statsig/"
  --exclude=".claudian/"
  --exclude=".claudian/**"
  --exclude="/_build/"
  --exclude="/diagram-governance/candidates/"
  --exclude="/diagram-governance/node_modules/"
  --exclude="/diagram-governance/previews/"
  --exclude="/diagram-governance/reports/"
  --exclude="/diagram-governance/references/"
  --exclude="/diagram-governance/fixtures/"
  --exclude="/_archive/"
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

# 共享 skill 在生产 Vault 中可能是指向本机 skills-manager 的绝对路径软链接；
# starter 只分发仓库内自包含的 skill，避免泄漏用户名并确保跨机器可移植。
if [[ -d "$DIST/.skills" ]]; then
  find "$DIST/.skills" -type l -delete
fi

# --- 同步根文件 ---
for file in "${FILES[@]}"; do
  if [[ -f "$VAULT/$file" ]]; then
    echo "[sync] $file"
    mkdir -p "$(dirname "$DIST/$file")"
    cp "$VAULT/$file" "$DIST/$file"
  fi
done

# --- 排除不该给的文件 ---
echo ""
echo "[clean] 移除看板..."
rm -f "$DIST/00-MOC/多智能体协作看板.md"
rm -f "$DIST/00-MOC/奕境DMS-MOC.md"
rm -f "$DIST/00-MOC/待办汇总.md"
rm -f "$DIST/00-MOC/项目运营笔记本.md"
rm -f "$DIST/30-规范/奕境PRD硬约束-v3.3.md"
rm -f "$DIST/30-规范/奕境项目-主数据字典与全局枚举.md"
rm -f "$DIST/30-规范/奕境项目-助手端UI输出规范.md"
rm -f "$DIST/90-模板"/奕境PRD模板-*.md

# 将生产 Vault 中的本机绝对路径机械改写为 starter 的可移植默认路径。
rg --hidden --no-ignore -l0 '/Users/yudongbo|余东波|波波|奕境|东风|联友|花都|保险经纪|YJDMS|DFIB|DMS|dongfeng|@im\.wechat|openclaw-memory-promotion' "$DIST" \
  -g '!.git/**' -g '!.standards/tests/**' -g '!02-项目管理/脚本/v9-starter-leak-check.py' -g '!**/sync-to-dist.sh' \
  | xargs -0 perl -pi -e '
    s#/Users/yudongbo/Desktop/obsidianVault#\$HOME/Desktop/obsidianVault#g;
    s#/Users/yudongbo#\$HOME#g;
    s/余东波|波波/用户/g;
    s/奕境/示例项目/g;
    s/东风/协作助手/g;
    s/联友|花都|保险经纪/示例组织/g;
    s/YJDMS|DFIB|DMS/EXAMPLE/g;
    s/dongfeng/assistant/g;
    s/\@im\.wechat/\@example.invalid/g;
    s/openclaw-memory-promotion/session-memory-example/g;
  ' || true

# LLM-Wiki 的生产原型绝对路径在 starter 中改为显式、可覆盖的便携默认值。
perl -pi -e 's/^import argparse$/import argparse\nimport os/; s#^VAULT = .*$#VAULT = Path(os.environ.get("VAULT_ROOT", Path.home() / "Desktop" / "obsidianVault"))#; s#^PROTOTYPE_ROOT = .*$#PROTOTYPE_ROOT = Path(os.environ.get("XIRANG_PROTOTYPE_ROOT", VAULT / "10-项目" / "示例项目" / "prototype"))#' \
  "$DIST/.standards/scripts/llm_wiki_check.py"
perl -0pi -e 's/(    def test_llm_wiki_uses_only_exact_725_relative_paths)/    \@unittest.skip("production-only prototype path is intentionally absent from starter")\n$1/' \
  "$DIST/.standards/tests/test_v9_phase_e.py"

# README.md 与 GOVERNANCE.md 是 starter 专用入口，不从生产 Vault 覆盖。
# 巡检和 eval 只分发说明文件；生产快照、审计报告和本地 fixture 不属于 starter。
for clean_dir in "$DIST/02-项目管理/巡检" "$DIST/02-项目管理/evals"; do
  if [[ -d "$clean_dir" ]]; then
    find "$clean_dir" -mindepth 1 ! -name "README.md" ! -name "v9-release-manifest.json" -delete
  fi
done

# Generate a portable starter-local distribution manifest. It records every
# trusted runtime file except the manifest itself, and contains no user path.
XIRANG_STARTER_DIST="$DIST" python3 - <<'PY'
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

root = Path(os.environ["XIRANG_STARTER_DIST"])
trust = root / ".standards/harness-tested-files.txt"
paths = [
    Path(line.strip()) for line in trust.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
artifacts = []
for relative in paths:
    if relative.as_posix() == "02-项目管理/巡检/v9-release-manifest.json":
        continue
    target = root / relative
    if not target.is_file():
        raise SystemExit(f"starter trusted file missing before manifest build: {relative}")
    artifacts.append({
        "root": "starter",
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    })
tree_rows = [f"{item['path']}:{item['sha256']}" for item in sorted(artifacts, key=lambda item: item["path"])]
tree_sha256 = hashlib.sha256(("\n".join(tree_rows) + "\n").encode()).hexdigest()
payload = {
    "schema_version": 1,
    "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "distribution": "starter-local",
    "roots": {"starter": "."},
    "releases": [{"name": "starter", "root": "starter", "tree_sha256": tree_sha256}],
    "artifacts": artifacts,
}
target = root / "02-项目管理/巡检/v9-release-manifest.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

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
SENSITIVE_PATTERN='(https://[a-z0-9]+\.feishu\.cn/(wiki|docx)/[A-Za-z0-9_-]{12,}|cli_[a-z0-9]{12,}|"app_secret"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]{20,}"|APP_SECRET[[:space:]]*=[[:space:]]*"[A-Za-z0-9_-]{20,}"|Session ID|Conversation Summary)'
if grep -RInIE "$SENSITIVE_PATTERN" "$DIST" \
  --exclude-dir=".git" \
  --exclude="v9-harness-eval-runner.py" \
  --exclude="v9-starter-leak-check.py" \
  --exclude="sync-to-dist.sh" \
  >/tmp/xirang-starter-sensitive-scan.txt 2>/dev/null; then
  echo "ERROR: 分发目录仍包含敏感信息，已停止。命中如下："
  cat /tmp/xirang-starter-sensitive-scan.txt
  exit 1
fi
rm -f /tmp/xirang-starter-sensitive-scan.txt

if [[ -f "$DIST/02-项目管理/脚本/v9-starter-leak-check.py" ]]; then
  python3 "$DIST/02-项目管理/脚本/v9-starter-leak-check.py" --root "$DIST" --strict
fi

echo ""
echo "=== 同步完成 ==="
echo "文件数: $(find "$DIST" -type f | wc -l | tr -d ' ')"
echo ""
echo "下一步:"
echo "  1. 检查敏感路径: grep -RIn '/Users/' \"$DIST/\""
echo "  2. 如有新文件需排除，编辑本脚本的 RSYNC_EXCLUDES 或 clean 区"
