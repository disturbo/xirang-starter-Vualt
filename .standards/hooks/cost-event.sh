#!/usr/bin/env bash
# cost-event.sh — V9 成本事件流写入
#
# 将成本追踪从"收工必填"改为三阶段持续写入：
#   1. start      — 任务启动时记录初始 cost=0（不计入汇总）
#   2. checkpoint  — 关键阶段完成时追加"本阶段增量" cost（计入汇总）
#   3. finalize    — 任务结束时写入"本阶段增量" cost（计入汇总）
#
# 口径规则：每次写入的 tokens/cost_cny 都是"增量"（delta），不是累计。
# cost-fuse.py 汇总时只加 checkpoint + finalize，忽略 start。
#
# 用法：
#   bash .standards/hooks/cost-event.sh start     <task_id> <agent_id> [model]
#   bash .standards/hooks/cost-event.sh checkpoint <task_id> <agent_id> [tokens] [cost_cny] [description]
#   bash .standards/hooks/cost-event.sh finalize   <task_id> <agent_id> [tokens] [cost_cny] [result]
#
# 退出码：总是 0（成本追踪失败不应影响主流程）
#
# 版本: 1.0.0 | 创建: 2026-05-31 | 息壤 V9.2

echo "[V9-RETIRED] 成本治理已于 2026-07-19 退出当前运行能力；未写入事件。" >&2
exit 3

VAULT_ROOT="${VAULT_ROOT:-$HOME/Desktop/obsidianVault}"
EVENT_FILE="$VAULT_ROOT/02-项目管理/智能体状态/智能体事件.jsonl"

PHASE="$1"
TASK_ID="${2:-none}"
AGENT_ID="${3:-claudian}"

TS=$(date '+%Y-%m-%dT%H:%M:%S+08:00')

case "$PHASE" in
  start)
    MODEL="${4:-unknown}"
    EVENT="{\"ts\":\"$TS\",\"event\":\"cost_start\",\"agent\":\"$AGENT_ID\",\"task_id\":\"$TASK_ID\",\"model\":\"$MODEL\",\"tokens\":0,\"cost_cny\":0}"
    ;;

  checkpoint)
    TOKENS="${4:-0}"
    COST_CNY="${5:-0}"
    DESC="${6:-checkpoint}"
    # JSON escape description
    DESC_ESCAPED=$(printf '%s' "$DESC" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1])" 2>/dev/null || printf '%s' "$DESC")
    EVENT="{\"ts\":\"$TS\",\"event\":\"cost_checkpoint\",\"agent\":\"$AGENT_ID\",\"task_id\":\"$TASK_ID\",\"tokens\":$TOKENS,\"cost_cny\":$COST_CNY,\"description\":\"$DESC_ESCAPED\"}"
    ;;

  finalize)
    TOKENS="${4:-0}"
    COST_CNY="${5:-0}"
    RESULT="${6:-done}"
    EVENT="{\"ts\":\"$TS\",\"event\":\"cost_finalize\",\"agent\":\"$AGENT_ID\",\"task_id\":\"$TASK_ID\",\"tokens\":$TOKENS,\"cost_cny\":$COST_CNY,\"result\":\"$RESULT\"}"
    ;;

  *)
    echo "[cost-event] 未知阶段: $PHASE (需要 start|checkpoint|finalize)" >&2
    exit 0
    ;;
esac

# 非阻塞写入
echo "$EVENT" >> "$EVENT_FILE" 2>/dev/null || true

echo "[cost-event] $PHASE | task=$TASK_ID agent=$AGENT_ID"
exit 0
