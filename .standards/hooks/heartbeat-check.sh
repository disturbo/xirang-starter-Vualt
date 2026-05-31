#!/usr/bin/env bash
# heartbeat-check.sh — V9 心跳检测脚本
#
# 扫描所有 agent 状态文件，检测心跳是否超时。
# 三级超时策略：
#   15min → WARNING（通知用户）
#   30min → STALE（标记状态）
#   60min → DEAD（自动 idle 化 + 记录事件）
#
# 用法：
#   bash .standards/hooks/heartbeat-check.sh          # 检查所有 agent
#   bash .standards/hooks/heartbeat-check.sh claudian  # 检查指定 agent
#
# 可由 cron / ScheduleWakeup 定期调用（建议每 10 分钟）
#
# 退出码：
#   0 = 所有 agent 健康
#   1 = 至少一个 agent 超时
#
# 版本: 1.0.0 | 创建: 2026-05-31 | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$VAULT_ROOT}"
STATUS_DIR="$VAULT_ROOT/02-项目管理/智能体状态"
EVENT_FILE="$STATUS_DIR/智能体事件.jsonl"

# 超时阈值（秒）
WARN_THRESHOLD=900    # 15 分钟
STALE_THRESHOLD=1800  # 30 分钟
DEAD_THRESHOLD=3600   # 60 分钟

# 要检查的 agent 列表
if [[ -n "$1" ]]; then
  AGENTS=("$1")
else
  AGENTS=(claudian xiaochong toubao hongmeisu workbuddy)
fi

# agent_id → 状态文件名映射
_status_file_for() {
  case "$1" in
    claudian)    echo "$STATUS_DIR/Claudian.md" ;;
    xiaochong)   echo "$STATUS_DIR/阿莫西林.md" ;;
    toubao)      echo "$STATUS_DIR/头孢.md" ;;
    hongmeisu)   echo "$STATUS_DIR/红霉素.md" ;;
    workbuddy)   echo "$STATUS_DIR/WorkBuddy.md" ;;
    *) echo "" ;;
  esac
}

HAS_TIMEOUT=false

for agent in "${AGENTS[@]}"; do
  STATUS_FILE=$(_status_file_for "$agent")

  [[ -z "$STATUS_FILE" || ! -f "$STATUS_FILE" ]] && continue

  # 只检查 busy 状态的 agent
  CURRENT_STATUS=$(grep '^status:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  [[ "$CURRENT_STATUS" != "busy" ]] && continue

  # 读取心跳时间和任务信息
  LAST_HB=$(grep '^last_heartbeat:' "$STATUS_FILE" 2>/dev/null | sed 's/^last_heartbeat: *//' | tr -d '"')
  CURRENT_TASK=$(grep '^current_task:' "$STATUS_FILE" 2>/dev/null | sed 's/^current_task: *//' | tr -d '"')
  TASK_ID=$(grep '^current_task_id:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')
  HB_PID=$(grep '^heartbeat_pid:' "$STATUS_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"')

  [[ -z "$LAST_HB" ]] && continue

  # 计算心跳距今秒数
  ELAPSED=$(python3 -c "
from datetime import datetime, timezone, timedelta
try:
    hb = datetime.fromisoformat('$LAST_HB')
    now = datetime.now(timezone(timedelta(hours=8)))
    print(int((now - hb).total_seconds()))
except:
    print(-1)
" 2>/dev/null)

  [[ "$ELAPSED" == "-1" ]] && continue

  # 如果有 pid，验证进程是否真实存在
  # 注意：heartbeat_pid 只在 wrapper 显式传入时才有值。
  #       null 或空 = 无 PID 信息，仅靠时间戳判断。
  PID_ALIVE="no_pid"
  if [[ -n "$HB_PID" && "$HB_PID" != "null" && "$HB_PID" =~ ^[0-9]+$ ]]; then
    if kill -0 "$HB_PID" 2>/dev/null; then
      PID_ALIVE="alive"
    else
      PID_ALIVE="dead"
    fi
  fi

  # 三级超时判定
  if [[ $ELAPSED -ge $DEAD_THRESHOLD ]]; then
    # DEAD: 自动 idle 化
    echo "[HEARTBEAT-DEAD] agent=$agent elapsed=${ELAPSED}s pid=$PID_ALIVE task=$CURRENT_TASK"
    echo "  操作: 自动回收为 idle 状态"

    # 更新状态文件
    source "$VAULT_ROOT/.standards/v8-handshake.sh"
    _v8_safe_update_yaml "$STATUS_FILE" "status" "idle"
    _v8_safe_update_yaml "$STATUS_FILE" "current_task" "null"
    _v8_safe_update_yaml "$STATUS_FILE" "current_task_id" "null"
    _v8_safe_update_yaml "$STATUS_FILE" "write_scope" "null"
    _v8_safe_update_yaml "$STATUS_FILE" "heartbeat_pid" "null"
    _v8_safe_update_yaml "$STATUS_FILE" "heartbeat_source" "null"

    # 记录事件
    TS=$(date '+%Y-%m-%dT%H:%M:%S+08:00')
    EVENT="{\"ts\":\"$TS\",\"event\":\"heartbeat_dead\",\"agent\":\"$agent\",\"task_id\":\"${TASK_ID:-none}\",\"elapsed_s\":$ELAPSED,\"pid_alive\":\"$PID_ALIVE\",\"auto_action\":\"idle\"}"
    echo "$EVENT" >> "$EVENT_FILE" 2>/dev/null

    HAS_TIMEOUT=true

  elif [[ $ELAPSED -ge $STALE_THRESHOLD ]]; then
    # STALE: 标记但不自动回收
    echo "[HEARTBEAT-STALE] agent=$agent elapsed=${ELAPSED}s pid=$PID_ALIVE task=$CURRENT_TASK"
    echo "  建议: 确认 agent 是否仍在工作，或手动执行 v8_end 回收"
    HAS_TIMEOUT=true

  elif [[ $ELAPSED -ge $WARN_THRESHOLD ]]; then
    # WARNING: 仅警告
    echo "[HEARTBEAT-WARN] agent=$agent elapsed=${ELAPSED}s pid=$PID_ALIVE task=$CURRENT_TASK"
    HAS_TIMEOUT=true

  else
    # 时间戳健康
    if [[ "$PID_ALIVE" == "dead" ]]; then
      # 进程已死但心跳还在阈值内——真实崩溃信号
      echo "[HEARTBEAT-PID-DEAD] agent=$agent elapsed=${ELAPSED}s 心跳未超时但进程已不存在"
      echo "  建议: 进程可能刚崩溃，下次检查可能升级为 STALE"
      HAS_TIMEOUT=true
    fi
    # PID_ALIVE=="no_pid" 且时间戳健康 → 正常，不输出
  fi
done

if [[ "$HAS_TIMEOUT" == "true" ]]; then
  exit 1
else
  exit 0
fi
