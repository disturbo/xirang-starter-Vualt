#!/usr/bin/env bash
# session-guard.sh — V8.5 Layer 2: V8 激活状态检测
# 由平台 PreToolUse hook 在首次 Bash/Edit/Write 时调用。
# 检测 Agent 状态是否一致，输出警告但不阻断。
#
# 环境变量（可选，用于多平台支持）：
#   V8_AGENT_ID    — agent 标识符（默认: claudian）
#
# 设计原则：
#   - 不阻断（exit 0）—— 只做 advisory
#   - 不频繁触发 —— 使用 heartbeat 时间戳做节流
#   - 快速执行 (<100ms)
#
# 退出码：总是 0
#
# 版本: 1.1.0 | 创建: 2026-05-26 | 修订: 2026-05-31（多平台参数化） | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$VAULT_ROOT}"
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
# 每个 agent 独立的节流戳（避免多 agent 并行互相干扰）
GUARD_STAMP="/tmp/.v8-session-guard-stamp-${V8_AGENT_ID}"

# ============================================================
# 节流：每 5 分钟最多检查一次
# ============================================================
if [[ -f "$GUARD_STAMP" ]]; then
  LAST_CHECK=$(stat -f %m "$GUARD_STAMP" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  ELAPSED=$(( NOW - LAST_CHECK ))
  if [[ $ELAPSED -lt 300 ]]; then
    exit 0
  fi
fi

# 更新时间戳
touch "$GUARD_STAMP"

# ============================================================
# 检查 Agent 状态一致性
# ============================================================
if [[ -z "$STATUS_FILE" || ! -f "$STATUS_FILE" ]]; then
  exit 0
fi

AGENT_STATE=$(grep '^status:' "$STATUS_FILE" | awk '{print $2}' | tr -d '"')
CURRENT_TASK=$(grep '^current_task:' "$STATUS_FILE" | sed 's/^current_task: *//' | tr -d '"')
LAST_HB=$(grep '^last_heartbeat:' "$STATUS_FILE" | sed 's/^last_heartbeat: *//' | tr -d '"')

# ============================================================
# 场景 1: Agent 状态为 busy 但心跳超时（可能是上次崩溃残留）
# ============================================================
if [[ "$AGENT_STATE" == "busy" && -n "$LAST_HB" ]]; then
  # 检查心跳是否超过 30 分钟（1800s）
  if command -v python3 &>/dev/null; then
    STALE=$(python3 -c "
from datetime import datetime, timezone, timedelta
import sys
try:
    hb = '$LAST_HB'
    # 处理 ISO 格式
    hb_time = datetime.fromisoformat(hb.replace('+08:00', '+08:00'))
    now = datetime.now(timezone(timedelta(hours=8)))
    diff = (now - hb_time).total_seconds()
    print('stale' if diff > 1800 else 'fresh')
except:
    print('unknown')
" 2>/dev/null)

    if [[ "$STALE" == "stale" ]]; then
      echo ""
      echo "[V8-SESSION-GUARD] Agent 状态异常检测："
      echo "  状态: busy（任务: $CURRENT_TASK）"
      echo "  心跳: $LAST_HB（已超 30 分钟未更新）"
      echo "  建议: 可能是上次崩溃残留，请执行 v8_end 清理状态"
      echo "        或确认当前确实在执行该任务后忽略此警告"
      echo ""
    fi
  fi
fi

# ============================================================
# 场景 2: Agent 状态为 error（需要介入）
# ============================================================
if [[ "$AGENT_STATE" == "error" ]]; then
  echo ""
  echo "[V8-SESSION-GUARD] Agent 状态为 error："
  echo "  任务: $CURRENT_TASK"
  echo "  请先处理错误状态再继续工作"
  echo ""
fi

exit 0
