#!/usr/bin/env bash
# pre-write-hook.sh — V8.5 Layer 1: 文件写入硬门禁
# 由平台 PreToolUse hook 自动调用，Agent 无法绕过。
#
# 输入：通过 stdin 接收 JSON 格式的 tool input
#       {"file_path": "...", "content": "..."}  (Write)
#       {"file_path": "...", "old_string": "...", "new_string": "..."} (Edit)
#
# 环境变量（可选，用于多平台支持）：
#   V8_AGENT_ID    — agent 标识符（默认: claudian）
#   V8_PLATFORM    — 平台名称（默认: claude-code）
#
# 退出码：
#   0 = 放行
#   2 = 阻断（stdout 作为拒绝原因返回给 Claude）
#
# 版本: 1.1.0 | 创建: 2026-05-26 | 修订: 2026-05-31（多平台参数化） | 息壤 V9.2

set -uo pipefail
# 注意：不用 set -e，因为需要手动处理各步退出码

VAULT_ROOT="${VAULT_ROOT:-$VAULT_ROOT}"
STATUS_DIR="$VAULT_ROOT/02-项目管理/智能体状态"
EVENT_FILE="$STATUS_DIR/智能体事件.jsonl"

# ============================================================
# 多平台参数化：通过环境变量或 agent-contract.yaml 解析 agent
# ============================================================
V8_AGENT_ID="${V8_AGENT_ID:-claudian}"
V8_PLATFORM="${V8_PLATFORM:-claude-code}"

# 根据 agent_id 解析状态文件名
_resolve_status_name() {
  case "$1" in
    claudian|dongfeng)               echo "Claudian" ;;
    xiaochong|amoxicillin|amox)      echo "阿莫西林" ;;
    toubao|cephalosporin|ceph)       echo "头孢" ;;
    hongmeisu|erythromycin|eryth)    echo "红霉素" ;;
    workbuddy)                       echo "WorkBuddy" ;;
    qingmeisu|penicillin|peni)       echo "青霉素" ;;
    *) echo "$1" ;;
  esac
}

# ============================================================
# 从 stdin 读取 tool input JSON，提取 file_path
# ============================================================
INPUT=$(cat)
FILE_PATH=""

# 尝试用 python3 提取 file_path（最可靠）
# Claudian hook stdin 格式: {"tool_name":"Write","tool_input":{"file_path":"...","content":"..."}}
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

# 如果拿不到 file_path，放行（不误杀）
if [[ -z "$FILE_PATH" ]]; then
  exit 0
fi

# ============================================================
# 规范化路径：转为相对于 vault root 的路径
# ============================================================
normalize_path() {
  local p="$1"
  # 去掉 vault root 前缀
  p="${p#$VAULT_ROOT/}"
  # 去掉开头的 ./
  p="${p#./}"
  # 去掉开头的 /
  p="${p#/}"
  echo "$p"
}

REL_PATH=$(normalize_path "$FILE_PATH")

# 如果文件不在 vault 内，不管
if [[ "$FILE_PATH" != "$VAULT_ROOT"* && "$FILE_PATH" != "$REL_PATH" ]]; then
  exit 0
fi

# ============================================================
# ============================================================
# Layer 0: 不可篡改路径（优先级高于白名单）
# ============================================================
# 任务卡是授权源，只允许 v8_handshake (Bash) 创建/修改
if [[ "$REL_PATH" == _temp/*/task-card.yaml ]]; then
  echo "[V8-HOOK-BLOCK] task-card.yaml 只允许 v8_handshake (Bash) 创建/修改。" >&2
  echo "  路径: $REL_PATH" >&2
  echo "  原因: 任务卡是授权源，Agent 不可通过 Write/Edit 篡改。" >&2
  exit 2
fi

# ============================================================
# 白名单：这些路径永远放行（M0-M2 不需要门禁）
# ============================================================
WHITELIST=(
  "_temp/"
  "02-项目管理/运行日志/"
  ".obsidian/"
)

for prefix in "${WHITELIST[@]}"; do
  if [[ "$REL_PATH" == "$prefix"* ]]; then
    exit 0
  fi
done

# ============================================================
# Layer 1A: 禁止路径硬拦截（无条件执行，不依赖 busy/idle 状态）
# ============================================================
FORBIDDEN_PATHS=("00-MOC/" "30-规范/" "40-决策/" "02-项目管理/智能体状态/" ".standards/")

# 交付物路径：未激活 handshake 时阻断（防止跳过档位判定直接产出）
DELIVERABLE_PATHS=("02-项目管理/交付物/")

# 读取当前 Agent 的 write_scope 和状态
# 根据 V8_AGENT_ID 动态解析状态文件
AGENT_STATUS_NAME=$(_resolve_status_name "$V8_AGENT_ID")
if [[ -f "$STATUS_DIR/${AGENT_STATUS_NAME}.md" ]]; then
  AGENT_STATUS="$STATUS_DIR/${AGENT_STATUS_NAME}.md"
else
  AGENT_STATUS=""
fi
WRITE_SCOPE=""
CURRENT_TASK=""
AGENT_STATE=""

if [[ -n "$AGENT_STATUS" && -f "$AGENT_STATUS" ]]; then
  AGENT_STATE=$(grep '^status:' "$AGENT_STATUS" | awk '{print $2}' | tr -d '"')
  CURRENT_TASK=$(grep '^current_task:' "$AGENT_STATUS" | sed 's/^current_task: *//' | tr -d '"')
  WRITE_SCOPE=$(grep '^write_scope:' "$AGENT_STATUS" 2>/dev/null | sed 's/^write_scope: *//' | tr -d '"')
  if [[ "$WRITE_SCOPE" == "null" ]]; then
    WRITE_SCOPE=""
  fi
fi

# -------- 禁止路径无条件拦截 --------
# 即使 busy 状态，禁止路径也必须在 write_scope 中才能放行。
# 这确保 gate-enforce 缺失时禁止路径仍被保护。
IS_FORBIDDEN=false
for forbidden in "${FORBIDDEN_PATHS[@]}"; do
  if [[ "$REL_PATH" == "$forbidden"* ]]; then
    IS_FORBIDDEN=true
    break
  fi
done

if [[ "$IS_FORBIDDEN" == "true" ]]; then
  if [[ "$AGENT_STATE" != "busy" ]]; then
    # 非 busy 状态 + 禁止路径 = 阻断
    echo "[V8-HOOK-BLOCK] 路径 '$REL_PATH' 在禁止目录中。" >&2
    echo "需要先执行 v8_handshake 并在 write_scope 中声明此路径。" >&2
    echo "当前状态: ${AGENT_STATE:-unknown} (非 busy)" >&2
    exit 2
  fi

  # busy 状态：检查 write_scope 是否包含此路径
  SCOPE_MATCH=false
  if [[ -n "$WRITE_SCOPE" ]]; then
    # write_scope 是逗号分隔的路径列表
    IFS=',' read -ra SCOPE_PARTS <<< "$WRITE_SCOPE"
    for sp in "${SCOPE_PARTS[@]}"; do
      sp=$(echo "$sp" | xargs)  # trim whitespace
      if [[ -n "$sp" ]]; then
        if [[ "$sp" == */ ]]; then
          [[ "$REL_PATH" == "$sp"* ]] && SCOPE_MATCH=true
        else
          [[ "$REL_PATH" == "$sp" || "$REL_PATH" == "$sp/"* ]] && SCOPE_MATCH=true
        fi
      fi
      [[ "$SCOPE_MATCH" == "true" ]] && break
    done
  fi

  if [[ "$SCOPE_MATCH" == "false" ]]; then
    # busy 但禁止路径不在 write_scope 中 = 阻断
    echo "[V8-HOOK-BLOCK] 路径 '$REL_PATH' 在禁止目录中且不在 write_scope 中。" >&2
    echo "  write_scope: ${WRITE_SCOPE:-<empty>}" >&2
    echo "  操作: 需在 v8_handshake 时声明此路径" >&2
    exit 2
  fi
  # busy + 禁止路径 + 在 scope 中 → 继续到 Layer 1B 进一步验证
fi

# 如果读不到状态文件，非禁止路径放行
if [[ -z "$AGENT_STATE" ]]; then
  exit 0
fi

# 如果 Agent 不是 busy 状态
if [[ "$AGENT_STATE" != "busy" ]]; then
  # Layer 1A.5: 交付物路径要求 handshake（未激活任务时阻断）
  for dpath in "${DELIVERABLE_PATHS[@]}"; do
    if [[ "$REL_PATH" == "$dpath"* ]]; then
      echo "[V8-HOOK-BLOCK] 交付物路径写入需先执行 v8_handshake。" >&2
      echo "  路径: $REL_PATH" >&2
      echo "  当前状态: ${AGENT_STATE:-idle}（未激活任务）" >&2
      echo "  操作: 这看起来是 M4 交付物。请先判定档位并输出握手。" >&2
      exit 2
    fi
  done
  # 非 busy 状态 + 非禁止路径 + 非交付物路径 = 放行
  exit 0
fi

# ============================================================
# Layer 1B: busy 状态 — 任务卡授权验证 + gate-enforce pre-write
# ============================================================
GATE_SCRIPT="$VAULT_ROOT/.standards/gate-enforce.py"

# 读取任务 ID 和 scope_source（WRITE_SCOPE 已在上方读取）
TASK_ID=$(grep '^current_task_id:' "$AGENT_STATUS" 2>/dev/null | awk '{print $2}' | tr -d '"')
SCOPE_SOURCE=$(grep '^scope_source:' "$AGENT_STATUS" 2>/dev/null | awk '{print $2}' | tr -d '"')

if [[ "$SCOPE_SOURCE" == "null" ]]; then
  SCOPE_SOURCE=""
fi

# gate-enforce 降级处理：
# 如果 gate 不存在，禁止路径已在上方无条件拦截过，此处只影响非禁止路径。
# 非禁止路径在 gate 缺失时可以安全放行（只是失去了 advisory 检查）。
if [[ ! -f "$GATE_SCRIPT" ]]; then
  exit 0
fi

# -------- Layer 1B-1: 任务卡授权验证 --------
# 核心目录写入要求有 task_card 授权源（不接受 Agent 自声明）
CORE_DIRS=("10-项目/" "20-知识/" "50-经验/" "02-项目管理/交付物/")
IS_CORE_PATH=false
for cdir in "${CORE_DIRS[@]}"; do
  if [[ "$REL_PATH" == "$cdir"* ]]; then
    IS_CORE_PATH=true
    break
  fi
done

if [[ "$IS_CORE_PATH" == "true" ]]; then
  # 核心目录：必须有任务卡 + scope_source=task_card
  TASK_CARD="$VAULT_ROOT/_temp/$TASK_ID/task-card.yaml"

  if [[ -z "$TASK_ID" || "$TASK_ID" == "null" ]]; then
    echo "[V8-HOOK-BLOCK] 核心目录写入需有效 task_id。当前无任务激活。" >&2
    echo "  路径: $REL_PATH" >&2
    echo "  操作: 先执行 v8_handshake 激活任务" >&2
    exit 2
  fi

  if [[ ! -f "$TASK_CARD" ]]; then
    echo "[V8-HOOK-BLOCK] 核心目录写入需任务卡授权。" >&2
    echo "  路径: $REL_PATH" >&2
    echo "  期望: _temp/$TASK_ID/task-card.yaml（由 v8_handshake 自动创建）" >&2
    echo "  操作: 确认已正确执行 v8_handshake" >&2
    exit 2
  fi

  # 验证 scope_source=task_card（handshake 唯一设置路径）
  if [[ "$SCOPE_SOURCE" != "task_card" ]]; then
    echo "[V8-HOOK-BLOCK] 核心目录写入需 scope_source=task_card。" >&2
    echo "  路径: $REL_PATH" >&2
    echo "  当前值: '${SCOPE_SOURCE:-<empty>}'" >&2
    echo "  操作: 通过 v8_handshake 激活任务以设置正确的 scope_source" >&2
    exit 2
  fi

  # 验证路径在任务卡 authorized_paths 中
  if command -v python3 &>/dev/null; then
    CARD_AUTH=$(python3 -c "
import sys, re
try:
    content = open('$TASK_CARD', 'r').read()
    # 解析 authorized_paths 列表
    section = re.search(r'authorized_paths:\s*\n((?:\s+-\s+.+\n?)*)', content)
    if not section:
        print('NO_PATHS')
        sys.exit(0)
    paths = re.findall(r'^\s+-\s+[\"'']?(.+?)[\"'']?\s*$', section.group(1), re.MULTILINE)
    # 检查文件是否在授权路径中
    rel = '$REL_PATH'
    for p in paths:
        p = p.strip()
        if p.endswith('/'):
            if rel.startswith(p):
                print('AUTHORIZED')
                sys.exit(0)
        else:
            if rel == p or rel.startswith(p + '/'):
                print('AUTHORIZED')
                sys.exit(0)
    print('NOT_AUTHORIZED')
except Exception as e:
    print('PARSE_ERROR')
" 2>/dev/null)

    if [[ "$CARD_AUTH" == "NOT_AUTHORIZED" ]]; then
      echo "[V8-HOOK-BLOCK] 路径未在任务卡 authorized_paths 中。" >&2
      echo "  路径: $REL_PATH" >&2
      echo "  任务卡: _temp/$TASK_ID/task-card.yaml" >&2
      echo "  操作: 如需写入此路径，需在 v8_handshake 时声明" >&2
      exit 2
    fi
    # AUTHORIZED / NO_PATHS / PARSE_ERROR → 继续到 gate-enforce
  fi
fi

# -------- Layer 1B-2: gate-enforce pre-write --------
GATE_OUTPUT=""
GATE_EXIT=0
GATE_OUTPUT=$(python3 "$GATE_SCRIPT" pre-write \
  --file "$REL_PATH" \
  ${TASK_ID:+--task-id "$TASK_ID"} \
  ${WRITE_SCOPE:+--write-scope "$WRITE_SCOPE"} \
  --json 2>&1)
GATE_EXIT=$?

if [[ $GATE_EXIT -eq 1 ]]; then
  # P0 违规 — 硬阻断（输出到 stderr，Claudian 会将其反馈给 Agent）
  echo "[V8-HOOK-BLOCK] gate-enforce pre-write 拒绝写入:" >&2
  echo "$GATE_OUTPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for v in d.get('violations', []):
        print(f\"  [{v['rule_id']}] {v['message']}\", file=sys.stderr)
except:
    print('  (无法解析门禁输出)', file=sys.stderr)
" 2>/dev/null || echo "  $GATE_OUTPUT" >&2
  exit 2
fi

# 通过门禁，放行
exit 0
