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

VAULT_ROOT="${VAULT_ROOT:-$HOME/Desktop/obsidianVault}"
EVENT_FILE="$VAULT_ROOT/02-项目管理/智能体状态/智能体事件.jsonl"

V8_AGENT_ID="${V8_AGENT_ID:-claudian}"
V8_PLATFORM="${V8_PLATFORM:-claude-code}"

# 根据 agent_id 解析状态文件名
_resolve_status_name() {
  case "$1" in
    claudian|assistant)               echo "Claudian" ;;
    xiaochong|amoxicillin|amox)      echo "阿莫西林" ;;
    toubao|cephalosporin|ceph)       echo "头孢" ;;
    hongmeisu|erythromycin|eryth)    echo "红霉素" ;;
    workbuddy)                       echo "WorkBuddy" ;;
    *) echo "$1" ;;
  esac
}

_normalize_agent_id() {
  case "$1" in
    claudian|assistant)               echo "claudian" ;;
    xiaochong|amoxicillin|amox)      echo "xiaochong" ;;
    toubao|cephalosporin|ceph)       echo "toubao" ;;
    hongmeisu|erythromycin|eryth)    echo "hongmeisu" ;;
    workbuddy)                       echo "workbuddy" ;;
    qingmeisu|penicillin|peni)       echo "qingmeisu" ;;
    *) echo "$1" ;;
  esac
}

# 从 stdin 读取 tool input
# Claude 兼容格式带 file_path；Codex apply_patch 把补丁文本放在 tool_input.command。
INPUT=$(cat)
FILE_PATH=""

# 兼容已启动、仍持有旧 hook 命令的 Codex 会话。
if [[ "$V8_AGENT_ID" == "claudian" ]] && _V8_HOOK_INPUT="$INPUT" python3 - <<'PY' 2>/dev/null
import json
import os
try:
    data = json.loads(os.environ.get("_V8_HOOK_INPUT", "{}"))
    is_codex = data.get("tool_name") == "apply_patch" and bool(data.get("session_id") or data.get("turn_id"))
except (TypeError, json.JSONDecodeError):
    is_codex = False
raise SystemExit(0 if is_codex else 1)
PY
then
  V8_AGENT_ID="hongmeisu"
  V8_PLATFORM="codex"
fi

AGENT_STATUS_NAME=$(_resolve_status_name "$V8_AGENT_ID")
if [[ -f "$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md" ]]; then
  STATUS_FILE="$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md"
else
  STATUS_FILE=""
fi

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

# 只读取首块 frontmatter，避免正文中嵌入的 agent_id/current_task_id 污染事件。
TASK_ID=""
AGENT_STATE=""
AGENT=$(_normalize_agent_id "$V8_AGENT_ID")
if [[ -n "$STATUS_FILE" && -f "$STATUS_FILE" ]]; then
  STATUS_VALUES=$(_V8_STATUS_FILE="$STATUS_FILE" python3 - <<'PY'
import os

values = {}
try:
    lines = open(os.environ["_V8_STATUS_FILE"], encoding="utf-8").readlines()
    if lines and lines[0].strip() == "---":
        closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        for line in lines[1:closing]:
            if ":" not in line:
                continue
            key, value = line.rstrip("\n").split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
except (OSError, StopIteration):
    pass

for key in ("agent_id", "status", "current_task_id"):
    value = values.get(key, "")
    print("" if value == "null" else value)
PY
)
  FRONTMATTER_AGENT=$(printf '%s\n' "$STATUS_VALUES" | sed -n '1p')
  AGENT_STATE=$(printf '%s\n' "$STATUS_VALUES" | sed -n '2p')
  FRONTMATTER_TASK_ID=$(printf '%s\n' "$STATUS_VALUES" | sed -n '3p')
  if [[ -n "$FRONTMATTER_AGENT" ]]; then
    AGENT=$(_normalize_agent_id "$FRONTMATTER_AGENT")
  fi
  # 只有 busy 状态的活动任务才能为写入归因；idle 下的残留字段一律失效。
  if [[ "$AGENT_STATE" == "busy" ]]; then
    TASK_ID="$FRONTMATTER_TASK_ID"
  fi
fi

# M3 使用单文件短时授权，不写 event.jsonl；成功写入后立即消费授权。
M3_MARKER="/tmp/.v8-m3-context-${AGENT}.json"
if _V8_M3_MARKER="$M3_MARKER" _V8_M3_AGENT="$AGENT" _V8_M3_FILE="$REL_PATH" python3 - <<'PY'
import json
import os
import time

try:
    data = json.load(open(os.environ["_V8_M3_MARKER"], encoding="utf-8"))
    valid = (
        data.get("agent") == os.environ["_V8_M3_AGENT"]
        and data.get("scope", "").lstrip("./") == os.environ["_V8_M3_FILE"].lstrip("./")
        and data.get("max_writes") == 1
        and 0 <= int(time.time()) - int(data.get("created_at_epoch", 0)) <= 1200
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
then
  rm -f "$M3_MARKER"
  exit 0
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
