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
# 版本: 1.2.0 | 创建: 2026-05-26 | 修订: 2026-07-18（反射器调度/新鲜度自检） | 息壤 V9

VAULT_ROOT="${VAULT_ROOT:-$HOME/Desktop/obsidianVault}"
V8_AGENT_ID="${V8_AGENT_ID:-claudian}"

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

AGENT_STATUS_NAME=$(_resolve_status_name "$V8_AGENT_ID")
if [[ -f "$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md" ]]; then
  STATUS_FILE="$VAULT_ROOT/02-项目管理/智能体状态/${AGENT_STATUS_NAME}.md"
else
  STATUS_FILE=""
fi
# 每个 agent 独立的节流戳（避免多 agent 并行互相干扰）
GUARD_STAMP="/tmp/.v8-session-guard-stamp-${V8_AGENT_ID}"

_frontmatter_value() {
  local file="$1"
  local field="$2"
  awk -v field="$field" '
    NR == 1 && $0 == "---" { in_fm=1; next }
    in_fm && $0 == "---" { exit }
    in_fm && index($0, field ":") == 1 {
      sub("^[^:]+:[[:space:]]*", "")
      gsub(/^"|"$/, "")
      print
      exit
    }
  ' "$file" 2>/dev/null
}

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

AGENT_STATE=$(_frontmatter_value "$STATUS_FILE" status)
CURRENT_TASK=$(_frontmatter_value "$STATUS_FILE" current_task)
LAST_HB=$(_frontmatter_value "$STATUS_FILE" last_heartbeat)

# 第一反射器独立新鲜度看门狗：调度器死亡时，不能依赖反射器自报。
if [[ -n "${XIRANG_V9_INSPECT_DIR:-}" ]]; then
  REFLEX_HEALTH_FILE="$XIRANG_V9_INSPECT_DIR/health-latest.json"
else
  REFLEX_RUNTIME_ROOT="${XIRANG_V9_RUNTIME_DIR:-$HOME/.xirang/v9-runtime}"
  REFLEX_HEALTH_FILE="$REFLEX_RUNTIME_ROOT/巡检/health-latest.json"
fi

REFLEX_FRESHNESS=$(python3 - "$REFLEX_HEALTH_FILE" <<'PY' 2>/dev/null
import os
import sys
import time

path = sys.argv[1]
if not os.path.isfile(path):
    print("missing")
else:
    age = max(0, int(time.time() - os.path.getmtime(path)))
    print(f"stale:{age}" if age > 2 * 3600 else f"fresh:{age}")
PY
)
case "$REFLEX_FRESHNESS" in
  missing)
    echo "[V9-REFLEX-WARN] 第一反射器快照不存在: $REFLEX_HEALTH_FILE" >&2
    ;;
  stale:*)
    REFLEX_AGE_SECONDS="${REFLEX_FRESHNESS#stale:}"
    echo "[V9-REFLEX-WARN] 第一反射器快照已超过 2 小时（age=${REFLEX_AGE_SECONDS}s）: $REFLEX_HEALTH_FILE" >&2
    ;;
esac

# watcher-of-watcher：即使旧快照仍新鲜，也要检查 launchd 是否真的加载。
LAUNCHCTL_BIN="${XIRANG_LAUNCHCTL:-/bin/launchctl}"
if [[ -x "$LAUNCHCTL_BIN" ]]; then
  if ! "$LAUNCHCTL_BIN" print "gui/$(id -u)/com.xirang.v9reflex" >/dev/null 2>&1; then
    echo "[V9-REFLEX-WARN] 第一反射器 launchd 未加载: com.xirang.v9reflex" >&2
  fi
fi

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
