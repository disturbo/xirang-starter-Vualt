#!/bin/bash
# post-write-check.sh — 息壤 V8 产出后觉察提示
# v1.0 · 2026-05-17 | 息壤 V8.5.0
#
# 用途：Agent 写完文件后立即调用，执行 pre-write-check 并输出合规提醒。
# 设计意图：解决 retrospective E 类根因"觉察缺失"——Agent 写完文件后
#           没有意识到需要自查合规。本脚本强制触发检查并输出结果。
#
# 用法：
#   bash .standards/post-write-check.sh <file1> [file2] [file3] ...
#   bash .standards/post-write-check.sh --recent     # 检查最近 5 分钟修改的文件
#
# 退出码：
#   0 = 全部通过
#   1 = 有违规
#   2 = 参数错误

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRE_CHECK="$SCRIPT_DIR/pre-write-check.py"
PYTHON="python3"

# 颜色（如果终端支持）
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# === 参数处理 ===
if [[ $# -eq 0 ]]; then
    echo "用法: post-write-check.sh <file1> [file2] ... | --recent"
    exit 2
fi

FILES=()

if [[ "$1" == "--recent" ]]; then
    # 查找最近 5 分钟内修改的 .md/.html/.css/.svg 文件
    while IFS= read -r f; do
        FILES+=("$f")
    done < <(find . -maxdepth 5 \
        \( -name "*.md" -o -name "*.html" -o -name "*.css" -o -name "*.svg" \) \
        -newer <(date -v-5M +%Y%m%d%H%M 2>/dev/null || date -d '5 minutes ago' +%Y%m%d%H%M) \
        -not -path "./.obsidian/*" \
        -not -path "./.git/*" \
        -not -path "./_archive/*" \
        2>/dev/null || true)

    if [[ ${#FILES[@]} -eq 0 ]]; then
        echo -e "${GREEN}[OK]${NC} 最近 5 分钟无文件修改，无需检查。"
        exit 0
    fi
else
    FILES=("$@")
fi

# === 执行检查 ===
TOTAL=0
PASSED=0
FAILED=0
FAIL_LIST=()

echo ""
echo "=========================================="
echo "  Post-Write Compliance Check"
echo "  Files: ${#FILES[@]}"
echo "=========================================="
echo ""

for file in "${FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo -e "  ${YELLOW}[SKIP]${NC} $file (not found)"
        continue
    fi

    TOTAL=$((TOTAL + 1))

    # 运行 pre-write-check
    result=$($PYTHON "$PRE_CHECK" "$file" --json 2>/dev/null || echo '{"status":"error"}')
    status=$(echo "$result" | $PYTHON -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")

    if [[ "$status" == "pass" ]]; then
        PASSED=$((PASSED + 1))
        echo -e "  ${GREEN}[PASS]${NC} $file"
    else
        FAILED=$((FAILED + 1))
        FAIL_LIST+=("$file")
        echo -e "  ${RED}[FAIL]${NC} $file"
        # 显示违规详情
        echo "$result" | $PYTHON -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for v in d.get('violations', []):
        print(f'         - {v}')
except:
    pass
" 2>/dev/null || true
    fi
done

echo ""
echo "------------------------------------------"
echo "  Results: ${PASSED}/${TOTAL} passed, ${FAILED} failed"
echo "------------------------------------------"

# === 觉察提醒（核心功能） ===
if [[ $FAILED -gt 0 ]]; then
    echo ""
    echo -e "${RED}[REMINDER]${NC} Post-write check FAILED. Before proceeding:"
    echo "  1. Fix violations above"
    echo "  2. Re-run: bash .standards/post-write-check.sh ${FAIL_LIST[*]}"
    echo "  3. Priority: P0 path > P1 format > P2 brand > P3 content"
    echo ""
    exit 1
else
    echo ""
    echo -e "${GREEN}[OK]${NC} All files compliant. Proceed with kanban update + Handoff."
    echo ""
    exit 0
fi
