#!/usr/bin/env bash
# heartbeat-update.sh — V9 心跳更新（由 agent wrapper 层调用）
#
# 设计原则：
#   - 由 agent session 的 wrapper 层调用，不是独立后台进程
#   - 绑定 pid + session_id，检测时可校验进程是否真实存在
#   - 快速执行 (<50ms)，不阻断主流程
#
# 用法：
#   bash .standards/hooks/heartbeat-update.sh [agent_id] [session_id] [agent_pid]
#   环境变量：
#     V8_AGENT_ID   — agent 标识符（默认 claudian）
#     V8_AGENT_PID  — 真实 agent/session 进程 PID（由 wrapper 传入）
#
# PID 设计：
#   PID 必须由调用方（wrapper）传入真实 agent 进程的 PID。
#   如果未传入，不写 heartbeat_pid 字段——避免假阳性。
#   heartbeat-check.sh 检测时：有 PID → kill -0 校验；无 PID → 仅靠时间戳。
#
# 退出码：总是 0（心跳失败不应影响主流程）
#
# 版本: 1.1.0 | 创建: 2026-05-31 | 修订: 2026-05-31（PID假阳性修复） | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$VAULT_ROOT}"
STATUS_DIR="$VAULT_ROOT/02-项目管理/智能体状态"

AGENT_ID="${1:-${V8_AGENT_ID:-claudian}}"
SESSION_ID="${2:-}"
AGENT_PID="${3:-${V8_AGENT_PID:-}}"  # 必须由 wrapper 显式传入，不默认 $$

# 复用 v8-handshake.sh 的安全更新函数
_hb_safe_update_yaml() {
  local file="$1" field="$2" value="$3"
  local tmpfile="${file}.hbtmp"
  _V8_FIELD="$field" _V8_VALUE="$value" awk '
    BEGIN { field=ENVIRON["_V8_FIELD"]; value=ENVIRON["_V8_VALUE"]; found=0 }
    /^---$/ && NR==1 { print; next }
    /^---$/ && NR>1 && !found { print field ": " value; print; found=1; next }
    $0 ~ "^" field ":" { print field ": " value; found=1; next }
    { print }
  ' "$file" > "$tmpfile"
  if [[ $? -eq 0 && -s "$tmpfile" ]]; then
    mv "$tmpfile" "$file"
  else
    rm -f "$tmpfile"
  fi
}

# 解析状态文件路径
_resolve_status_file() {
  case "$1" in
    claudian|dongfeng)               echo "$STATUS_DIR/Claudian.md" ;;
    xiaochong|amoxicillin|amox)      echo "$STATUS_DIR/阿莫西林.md" ;;
    toubao|cephalosporin|ceph)       echo "$STATUS_DIR/头孢.md" ;;
    hongmeisu|erythromycin|eryth)    echo "$STATUS_DIR/红霉素.md" ;;
    workbuddy)                       echo "$STATUS_DIR/WorkBuddy.md" ;;
    *) echo "" ;;
  esac
}

STATUS_FILE=$(_resolve_status_file "$AGENT_ID")

if [[ -z "$STATUS_FILE" || ! -f "$STATUS_FILE" ]]; then
  # 静默退出——未知 agent 或文件不存在
  exit 0
fi

# 只在 busy 状态时更新心跳（idle 状态不需要心跳）
CURRENT_STATUS=$(grep '^status:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
if [[ "$CURRENT_STATUS" != "busy" ]]; then
  exit 0
fi

TS=$(date '+%Y-%m-%dT%H:%M:%S+08:00')

# 更新心跳时间戳（总是写）
_hb_safe_update_yaml "$STATUS_FILE" "last_heartbeat" "\"$TS\""

# 更新心跳元数据
# heartbeat_pid: 只在 wrapper 传入真实 PID 时写入；否则不写（避免假阳性）
if [[ -n "$AGENT_PID" ]]; then
  # 校验 PID 格式（纯数字）且进程确实存在
  if [[ "$AGENT_PID" =~ ^[0-9]+$ ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
    _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_pid" "$AGENT_PID"
    _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_source" "\"wrapper\""
  else
    # PID 无效或进程不存在——不写 PID，标记来源为 manual
    _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_pid" "null"
    _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_source" "\"manual\""
  fi
else
  # 未传 PID——只更新时间戳，不碰 PID 字段
  _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_source" "\"timestamp_only\""
fi

# session_id（可选）
if [[ -n "$SESSION_ID" ]]; then
  _hb_safe_update_yaml "$STATUS_FILE" "heartbeat_session" "\"$SESSION_ID\""
fi

exit 0
