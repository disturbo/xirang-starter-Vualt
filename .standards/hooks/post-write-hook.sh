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

VAULT_ROOT="${VAULT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
EVENT_FILE="$VAULT_ROOT/02-项目管理/智能体状态/智能体事件.jsonl"

V8_AGENT_ID="${V8_AGENT_ID:-claudian}"
V8_PLATFORM="${V8_PLATFORM:-claude-code}"

_normalize_agent_id() {
  case "$1" in
    claudian)                        echo "claudian" ;;
    xiaochong|amoxicillin|amox)      echo "xiaochong" ;;
    toubao|cephalosporin|ceph)       echo "toubao" ;;
    hongmeisu|erythromycin|eryth)    echo "hongmeisu" ;;
    workbuddy)                       echo "workbuddy" ;;
    qingmeisu|penicillin|peni)       echo "qingmeisu" ;;
    *) echo "$1" ;;
  esac
}

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
# Claude 兼容格式带 file_path；Codex apply_patch 把补丁文本放在 tool_input.command。
INPUT=$(cat)
FILE_PATH=""

if command -v python3 &>/dev/null; then
  FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    fp = ti.get('file_path', '') if isinstance(ti, dict) else ''
    if not fp:
        fp = d.get('file_path', '')
    if not fp and d.get('tool_name') == 'apply_patch' and isinstance(ti, dict):
        paths = re.findall(r'^\\*\\*\\* (?:Add|Update|Delete) File: (.+?)\\s*$', str(ti.get('command', '')), re.M)
        fp = paths[0] if len(paths) == 1 else ''
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
AGENT=$(_normalize_agent_id "$V8_AGENT_ID")
if [[ -n "$STATUS_FILE" && -f "$STATUS_FILE" ]]; then
  TASK_ID=$(grep '^current_task_id:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  AGENT=$(grep '^agent_id:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  AGENT=$(_normalize_agent_id "${AGENT:-$V8_AGENT_ID}")
fi

# 追加单行、可解析的 file_write 事件。
TS=$(date '+%Y-%m-%dT%H:%M:%S+08:00')
EVENT=$(_V8_EVENT_INPUT="$INPUT" _V8_EVENT_TS="$TS" _V8_EVENT_AGENT="$AGENT" \
  _V8_EVENT_PLATFORM="$V8_PLATFORM" _V8_EVENT_TASK_ID="$TASK_ID" \
  _V8_EVENT_FILE="$REL_PATH" python3 - <<'PY'
import json
import os

try:
    source = json.loads(os.environ.get("_V8_EVENT_INPUT", "{}"))
except json.JSONDecodeError:
    source = {}
tool_input = source.get("tool_input") if isinstance(source.get("tool_input"), dict) else {}
event = {
    "ts": os.environ["_V8_EVENT_TS"],
    "event": "file_write",
    "agent": os.environ["_V8_EVENT_AGENT"],
    "platform": os.environ.get("_V8_EVENT_PLATFORM"),
    "task_id": os.environ.get("_V8_EVENT_TASK_ID") or None,
    "file": os.environ["_V8_EVENT_FILE"],
    "operation": tool_input.get("codex_operation") or "write",
    "tool_name": source.get("tool_name"),
    "tool_use_id": source.get("tool_use_id"),
    "session_id": source.get("session_id"),
    "turn_id": source.get("turn_id"),
}
print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
PY
)

# 非阻塞写入（如果事件文件不存在或权限错误，不影响主流程）
printf '%s\n' "$EVENT" >> "$EVENT_FILE" 2>/dev/null || true

exit 0
