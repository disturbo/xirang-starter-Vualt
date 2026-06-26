#!/usr/bin/env bash
# post-write-hook.sh — V8.5 Layer 3: 写入审计自动追踪
# 由平台 PostToolUse hook 自动调用。
# 每次文件写入后自动记录到事件流，不依赖 Agent 主动报告。
#
# 环境变量（可选，用于多平台支持）：
#   V8_AGENT_ID    — agent 标识符（默认: claudian）
#
# 退出码：总是 0（审计不阻断）
#
# 版本: 1.1.0 | 创建: 2026-05-26 | 修订: 2026-05-31（多平台参数化） | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$VAULT_ROOT}"
EVENT_FILE="$VAULT_ROOT/02-项目管理/智能体状态/智能体事件.jsonl"

V8_AGENT_ID="${V8_AGENT_ID:-claudian}"

# 根据 agent_id 解析状态文件名
_resolve_status_name() {
  case "$1" in
    claudian)               echo "Claudian" ;;
    xiaochong|amoxicillin|amox)      echo "阿莫西林" ;;
    toubao|cephalosporin|ceph)       echo "头孢" ;;
    hongmeisu|erythromycin|eryth)    echo "红霉素" ;;
    workbuddy)                       echo "WorkBuddy" ;;
    *) echo "$1" ;;
  esac
}

AGENT_STATUS_NAME=$(_resolve_status_name "$V8_AGENT_ID")
if [[ -f "$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md" ]]; then
  STATUS_FILE="$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md"
else
  STATUS_FILE=""
fi

# 从 stdin 读取 tool input
# Claudian hook stdin 格式: {"tool_name":"Write","tool_input":{"file_path":"...","content":"..."}}
INPUT=$(cat)
FILE_PATH=""

if command -v python3 &>/dev/null; then
  FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # 优先从 tool_input 中取（Claudian 实际格式）
    ti = d.get('tool_input', {})
    fp = ti.get('file_path', '') if isinstance(ti, dict) else ''
    # 兜底：顶层 file_path（仿真测试用）
    if not fp:
        fp = d.get('file_path', '')
    print(fp)
except:
    print('')
" 2>/dev/null)
fi

# 如果拿不到路径，静默退出
if [[ -z "$FILE_PATH" ]]; then
  exit 0
fi

# 规范化路径
REL_PATH="${FILE_PATH#$VAULT_ROOT/}"
REL_PATH="${REL_PATH#./}"
REL_PATH="${REL_PATH#/}"

# 如果不在 vault 内，忽略
if [[ "$FILE_PATH" != "$VAULT_ROOT"* && "$FILE_PATH" != "$REL_PATH" ]]; then
  exit 0
fi

# 白名单：不记录这些路径的写入（噪音太大）
SKIP_PATTERNS=(
  "_temp/"
  ".obsidian/"
  ".standards/hooks/"
  "02-项目管理/运行日志/"
  "02-项目管理/智能体状态/智能体事件.jsonl"
)

for pattern in "${SKIP_PATTERNS[@]}"; do
  if [[ "$REL_PATH" == "$pattern"* || "$REL_PATH" == "$pattern" ]]; then
    exit 0
  fi
done

# 读取当前任务信息
TASK_ID=""
AGENT="$V8_AGENT_ID"
if [[ -n "$STATUS_FILE" && -f "$STATUS_FILE" ]]; then
  TASK_ID=$(grep '^current_task_id:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  AGENT=$(grep '^agent_id:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  AGENT="${AGENT:-$V8_AGENT_ID}"
fi

# 追加 file_write 事件
TS=$(date '+%Y-%m-%dT%H:%M:%S+08:00')

# JSON escape file path
ESCAPED_PATH=$(python3 -c "
import json
print(json.dumps('$REL_PATH')[1:-1])
" 2>/dev/null || echo "$REL_PATH")

EVENT="{\"ts\":\"$TS\",\"event\":\"file_write\",\"agent\":\"$AGENT\",\"task_id\":\"${TASK_ID:-none}\",\"file\":\"$ESCAPED_PATH\"}"

# 非阻塞写入（如果事件文件不存在或权限错误，不影响主流程）
echo "$EVENT" >> "$EVENT_FILE" 2>/dev/null || true

exit 0
