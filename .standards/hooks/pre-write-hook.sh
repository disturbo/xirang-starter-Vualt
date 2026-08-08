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

VAULT_ROOT="${VAULT_ROOT:-$HOME/Desktop/obsidianVault}"
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
    claudian|assistant)               echo "Claudian" ;;
    xiaochong|amoxicillin|amox)      echo "阿莫西林" ;;
    toubao|cephalosporin|ceph)       echo "头孢" ;;
    hongmeisu|erythromycin|eryth)    echo "红霉素" ;;
    workbuddy)                       echo "WorkBuddy" ;;
    qingmeisu|penicillin|peni)       echo "青霉素" ;;
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

# ============================================================
# 从 stdin 读取 tool input JSON，提取 file_path
# ============================================================
INPUT=$(cat)
FILE_PATH=""

# 兼容已启动、仍持有旧 hook 命令的 Codex 会话；新会话由适配器显式注入身份。
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

# 尝试用 python3 提取 file_path（最可靠）
# Claude 兼容格式带 file_path；Codex apply_patch 把补丁文本放在 tool_input.command。
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
        fp = paths[0] if len(paths) == 1 else '__V9_APPLY_PATCH_MULTI__' if paths else '__V9_APPLY_PATCH_UNPARSED__'
    print(fp)
except:
    print('')
" 2>/dev/null)
fi

if [[ "$FILE_PATH" == __V9_APPLY_PATCH_* ]]; then
  echo "[V9-HOOK-BLOCK] 当前会话的旧 hook 入口无法安全展开此 apply_patch；请在新 Codex 任务中使用多文件适配器。" >&2
  exit 2
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
# Layer 0.6: 任务卡验收转移拦截（V9.4.1，热路径）
# 仅当"写任务卡 且 候选内容拟设 review_status: accepted"时介入；其余写入完全不进入。
# 内部任何异常一律 fail-open（exit 0 放行），把爆炸半径锁死在"自验收"这一种情形。
# ============================================================
if [[ "$REL_PATH" == 02-项目管理/任务卡/*/T-*.md || "$REL_PATH" == 02-项目管理/任务卡/T-*.md ]]; then
  ACCEPT_MSG=$(V9_INPUT="$INPUT" V9_VAULT="$VAULT_ROOT" V9_REL="$REL_PATH" python3 - <<'PYEOF'
import os, sys, json, re, tempfile, subprocess
try:
    raw = os.environ.get("V9_INPUT", "")
    d = json.loads(raw) if raw.strip() else {}
    ti = d.get("tool_input", {})
    ti = ti if isinstance(ti, dict) else {}
    vault = os.environ["V9_VAULT"]
    target = os.path.join(vault, os.environ["V9_REL"])
    patch = ti.get("codex_patch") or ti.get("command") or ""
    if re.search(r"^\+review_status:\s*accepted\b", patch, re.MULTILINE):
        sys.stdout.write("DIRECT_ACCEPTED_WRITE")
        sys.exit(2)  # Codex apply_patch 只能提交评审，验收必须走 v9_accept
    content = ti.get("content")
    if content is None:  # Edit：用 old->new 在当前文件上重建候选全文
        old = ti.get("old_string", "") or ""
        new = ti.get("new_string", "") or ""
        try:
            cur = open(target, encoding="utf-8").read()
        except OSError:
            sys.exit(0)
        content = cur.replace(old, new, 1) if (old and old in cur) else cur
    if not re.search(r"^review_status:\s*accepted\b", content, re.MULTILINE):
        sys.exit(0)  # 不涉及 accepted 转移 → 放行
    gate = os.path.join(vault, ".standards", "gate-enforce.py")
    if not os.path.exists(gate):
        sys.stdout.write("ACCEPT_GATE_MISSING")
        sys.exit(2)
    tf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    tf.write(content); tf.close()
    try:
        r = subprocess.run([sys.executable, gate, "pre-accept", "--candidate", tf.name, "--json"],
                           capture_output=True, text=True, timeout=30, cwd=vault)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    finally:
        os.unlink(tf.name)
    if out.get("p0_count", 0) > 0:
        sys.stdout.write(",".join(v.get("rule_id", "") for v in out.get("violations", [])))
        sys.exit(2)
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.stdout.write("ACCEPT_CHECK_FAILED")
    sys.exit(2)  # accepted 是不可逆语义转移，校验异常必须 fail-closed
PYEOF
)
  if [[ "$?" == "2" ]]; then
    echo "[V8-HOOK-BLOCK] 任务卡验收转移被拦：${ACCEPT_MSG}。" >&2
    echo "  请用 v9_accept 完成验收，且 accepted_by 不能是 owner/author（self-accept）。" >&2
    exit 2
  fi
fi

# ============================================================
# Layer 1A: 禁止路径硬拦截（无条件执行，不依赖 busy/idle 状态）
# ============================================================
FORBIDDEN_PATHS=("00-MOC/" "30-规范/" "40-决策/" "02-项目管理/智能体状态/" ".standards/")

# 交付物路径：未激活 handshake 时阻断（防止跳过档位判定直接产出）
DELIVERABLE_PATHS=("02-项目管理/交付物/")

# 核心内容目录：idle 时仅允许持有单文件、短时效 M3 授权的写入。
CORE_DIRS=("10-项目/" "20-资料/" "50-经验/" "02-项目管理/交付物/")

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
TASK_ID=""
SCOPE_SOURCE=""

if [[ -n "$AGENT_STATUS" && -f "$AGENT_STATUS" ]]; then
  STATUS_VALUES=$(_V8_STATUS_FILE="$AGENT_STATUS" python3 - <<'PY'
import os

path = os.environ["_V8_STATUS_FILE"]
values = {}
try:
    lines = open(path, encoding="utf-8").readlines()
    if lines and lines[0].strip() == "---":
        closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        for line in lines[1:closing]:
            if ":" not in line:
                continue
            key, value = line.rstrip("\n").split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
except (OSError, StopIteration):
    pass

for key in ("status", "current_task", "write_scope", "current_task_id", "scope_source"):
    value = values.get(key, "")
    print("" if value == "null" else value)
PY
)
  AGENT_STATE=$(printf '%s\n' "$STATUS_VALUES" | sed -n '1p')
  CURRENT_TASK=$(printf '%s\n' "$STATUS_VALUES" | sed -n '2p')
  WRITE_SCOPE=$(printf '%s\n' "$STATUS_VALUES" | sed -n '3p')
  TASK_ID=$(printf '%s\n' "$STATUS_VALUES" | sed -n '4p')
  SCOPE_SOURCE=$(printf '%s\n' "$STATUS_VALUES" | sed -n '5p')
fi

IS_CORE_PATH=false
for cdir in "${CORE_DIRS[@]}"; do
  if [[ "$REL_PATH" == "$cdir"* ]]; then
    IS_CORE_PATH=true
    break
  fi
done

_m3_scope_is_valid() {
  local normalized_agent
  normalized_agent=$(_normalize_agent_id "$V8_AGENT_ID")
  local marker="/tmp/.v8-m3-context-${normalized_agent}.json"
  _V8_M3_MARKER="$marker" _V8_M3_AGENT="$normalized_agent" _V8_M3_FILE="$REL_PATH" \
    python3 - <<'PY'
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
}

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

# 如果读不到状态文件，核心目录保持 fail-closed；其他非禁止路径放行。
if [[ -z "$AGENT_STATE" ]]; then
  if [[ "$IS_CORE_PATH" == "true" ]]; then
    echo "[V8-HOOK-BLOCK] 核心目录写入无法解析 Agent 状态。" >&2
    echo "  路径: $REL_PATH" >&2
    exit 2
  fi
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
  if [[ "$IS_CORE_PATH" == "true" ]]; then
    if _m3_scope_is_valid; then
      exit 0
    fi
    echo "[V8-HOOK-BLOCK] 核心目录写入需要活动的 M4/M5 任务，或 20 分钟内的单文件 M3 授权。" >&2
    echo "  路径: $REL_PATH" >&2
    echo "  当前状态: ${AGENT_STATE:-idle}" >&2
    exit 2
  fi
  # 非 busy 状态 + 非禁止路径 + 非交付物路径 = 放行
  exit 0
fi

# busy 状态必须具备完整、活动的任务上下文；陈旧 scope 不构成授权。
if [[ -z "$TASK_ID" || -z "$WRITE_SCOPE" || "$SCOPE_SOURCE" != "task_card" ]]; then
  echo "[V8-HOOK-BLOCK] busy 状态的任务上下文不完整。" >&2
  echo "  task_id: ${TASK_ID:-<empty>} | scope_source: ${SCOPE_SOURCE:-<empty>} | write_scope: ${WRITE_SCOPE:-<empty>}" >&2
  exit 2
fi

# ============================================================
# Layer 1B: busy 状态 — 任务卡授权验证 + gate-enforce pre-write
# ============================================================
GATE_SCRIPT="$VAULT_ROOT/.standards/gate-enforce.py"

# gate-enforce 降级处理：
# 如果 gate 不存在，禁止路径已在上方无条件拦截过，此处只影响非禁止路径。
# 非禁止路径在 gate 缺失时可以安全放行（只是失去了 advisory 检查）。
if [[ ! -f "$GATE_SCRIPT" ]]; then
  exit 0
fi

# -------- Layer 1B-1: 任务卡授权验证 --------
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
  --agent "$(_normalize_agent_id "$V8_AGENT_ID")" \
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
