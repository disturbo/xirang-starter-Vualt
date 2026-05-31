#!/bin/bash
# 息壤 V9 环境初始化脚本
# 用途：检查运行依赖 + 设置权限 + 验证完整性
# 适用：L2+ 用户（L0/L1 无需运行）

set -e

echo "╔══════════════════════════════════════╗"
echo "║   息壤 V9 · 环境初始化              ║"
echo "╚══════════════════════════════════════╝"
echo ""

VAULT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PASS=0
WARN=0
FAIL=0

check() {
  local label="$1"
  local cmd="$2"
  local required="$3"  # "required" or "optional"

  printf "  %-30s" "$label"
  if eval "$cmd" > /dev/null 2>&1; then
    echo "✅"
    PASS=$((PASS + 1))
  elif [ "$required" = "optional" ]; then
    echo "⚠️  (可选，跳过)"
    WARN=$((WARN + 1))
  else
    echo "❌"
    FAIL=$((FAIL + 1))
  fi
}

# ─── 1. 基础工具 ───────────────────────────────────────

echo "▶ 基础工具检查"
check "Python 3.9+" "python3 -c 'import sys; assert sys.version_info >= (3,9)'" "required"
check "Node.js 22+" "node -e 'process.exit(+process.version.slice(1).split(\".\")[0] < 22)'" "required"
check "Ollama" "command -v ollama" "optional"
check "bge-m3 模型" "ollama list 2>/dev/null | grep -q bge-m3" "optional"
check "Git" "command -v git" "required"
echo ""

# ─── 2. AI 工具 ────────────────────────────────────────

echo "▶ AI 工具检查"
check "Claude Code" "command -v claude" "optional"
check "GBrain" "command -v gbrain" "optional"
check "OpenClaw" "command -v openclaw" "optional"
echo ""

# ─── 3. Vault 完整性 ──────────────────────────────────

echo "▶ Vault 结构检查"
check ".standards/ 存在" "[ -d '$VAULT_ROOT/.standards' ]" "required"
check ".prompt-src/ 存在" "[ -d '$VAULT_ROOT/.prompt-src' ]" "required"
check ".claude/CLAUDE.md" "[ -f '$VAULT_ROOT/.claude/CLAUDE.md' ]" "required"
check "Agent 状态文件" "[ $(ls '$VAULT_ROOT/02-项目管理/智能体状态/'*.md 2>/dev/null | wc -l) -ge 6 ]" "required"

SCRIPT_COUNT=$(find "$VAULT_ROOT/.standards" -name "*.py" -o -name "*.sh" | wc -l | tr -d ' ')
check ".standards/ 脚本 (≥25)" "[ $SCRIPT_COUNT -ge 25 ]" "required"
echo ""

# ─── 4. 权限设置 ──────────────────────────────────────

echo "▶ 设置脚本执行权限"
chmod +x "$VAULT_ROOT/.standards/hooks/"*.sh 2>/dev/null && echo "  hooks/*.sh → +x ✅" || echo "  hooks/*.sh → 无文件"
chmod +x "$VAULT_ROOT/.standards/"*.sh 2>/dev/null && echo "  .standards/*.sh → +x ✅" || echo "  .standards/*.sh → 无文件"
chmod +x "$VAULT_ROOT/setup.sh" 2>/dev/null
echo ""

# ─── 5. Prompt-build 验证 ─────────────────────────────

echo "▶ Prompt-build 一致性"
if python3 "$VAULT_ROOT/.prompt-src/prompt-build.py" --verify > /dev/null 2>&1; then
  echo "  prompt-build --verify ✅ No drift"
  PASS=$((PASS + 1))
else
  echo "  prompt-build --verify ⚠️  DRIFT detected (run: python3 .prompt-src/prompt-build.py --apply)"
  WARN=$((WARN + 1))
fi
echo ""

# ─── 6. Python 脚本语法检查 ───────────────────────────

echo "▶ Python 脚本语法校验"
SYNTAX_OK=true
while IFS= read -r pyfile; do
  if ! python3 -c "import py_compile; py_compile.compile('$pyfile', doraise=True)" 2>/dev/null; then
    echo "  ❌ $pyfile"
    SYNTAX_OK=false
  fi
done < <(find "$VAULT_ROOT/.standards" -name "*.py")

if [ "$SYNTAX_OK" = true ]; then
  echo "  全部 .py 语法正确 ✅"
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi
echo ""

# ─── 汇总 ─────────────────────────────────────────────

echo "═══════════════════════════════════════"
echo "  结果：✅ $PASS 通过 | ⚠️  $WARN 跳过 | ❌ $FAIL 失败"
echo "═══════════════════════════════════════"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "有必要项未通过。请参考文档修复："
  echo "  50-经验/Agent协作方法论/息壤V9-环境还原清单.md"
  exit 1
else
  echo ""
  echo "环境就绪！打开 Obsidian → 00-MOC/🏠-Home.md 开始使用。"
  echo ""
  echo "下一步建议："
  echo "  1. 填写 30-规范/品牌规范.md"
  echo "  2. 编辑 00-MOC/🏠-Home.md 写入项目信息"
  echo "  3. 复制 00-MOC/T-项目MOC.md 创建你的项目 MOC"
fi
