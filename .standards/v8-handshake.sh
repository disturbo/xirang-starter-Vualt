#!/usr/bin/env bash
# v8-handshake.sh — V8.5 运行时握手工具
# 用法：source .standards/v8-handshake.sh
#       v8_handshake <档位> <任务名> <写入范围> [验收方] [agent_id]
#       v8_end <task_id> <agent_id> <结果>
#       v9_accept <task_id> <accepted_by> [--reviewer NAME] [--require-fresh-eval]
#       v8_spawn <parent_task_id> <sub_id> <model> <type> <name> <write_scope> [timeout]
#       v8_collect <parent_task_id> <sub_id> [tokens] [cost]
#
# 职责边界：本工具做"握手声明 + 状态文件更新 + 事件流打点 + 任务卡创建"。
# 看板更新由 Agent 在握手之后手动执行。
#
# 版本: 1.4.4 | 创建: 2026-05-23 | 修订: 2026-07-21（任务开始自动语义召回） | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$HOME/Desktop/obsidianVault}"
EVENT_FILE="$VAULT_ROOT/02-项目管理/智能体状态/智能体事件.jsonl"
LOG_DIR="$VAULT_ROOT/02-项目管理/运行日志"
V8_PYTHON="${XIRANG_PYTHON_BIN:-/usr/bin/python3}"

# ============================================================
# _v8_safe_update_yaml - 安全更新 YAML frontmatter 字段（用 awk 避免 sed 分隔符问题）
# 用法: _v8_safe_update_yaml <file> <field> <value>
# ============================================================
_v8_safe_update_yaml() {
  local file="$1"
  local field="$2"
  local value="$3"
  local tmpfile="${file}.v8tmp"

  # 用 ENVIRON 传 value 避免 awk -v 对反斜杠的二次转义
  _V8_FIELD="$field" _V8_VALUE="$value" awk '
    BEGIN { field=ENVIRON["_V8_FIELD"]; value=ENVIRON["_V8_VALUE"]; found=0; in_fm=0 }
    NR==1 && /^---$/ { in_fm=1; print; next }
    in_fm && /^---$/ {
      if (!found) print field ": " value
      print
      in_fm=0
      next
    }
    in_fm && $0 ~ "^" field ":" && !found {
      print field ": " value
      found=1
      next
    }
    { print }
  ' "$file" > "$tmpfile"

  if [[ $? -eq 0 && -s "$tmpfile" ]]; then
    mv "$tmpfile" "$file"
    return 0
  else
    rm -f "$tmpfile"
    echo "[ERROR] 状态文件更新失败: $file / $field" >&2
    return 1
  fi
}

_v8_frontmatter_value() {
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

_v8_formal_task_card_path() {
  local task_id="$1"
  local compact_date="${task_id#T-}"
  compact_date="${compact_date%%-*}"
  if [[ ! "$compact_date" =~ ^[0-9]{8}$ ]]; then
    return 1
  fi
  echo "$VAULT_ROOT/02-项目管理/任务卡/${compact_date:0:4}-${compact_date:4:2}/${task_id}.md"
}

_v8_create_formal_task_card() {
  local task_id="$1" agent="$2" gear="$3" task="$4" scope="$5" reviewer="$6" ts="$7"
  local card
  card=$(_v8_formal_task_card_path "$task_id") || return 1
  _V8_FORMAL_CARD="$card" _V8_TASK_ID="$task_id" _V8_AGENT="$agent" _V8_GEAR="$gear" \
    _V8_TASK="$task" _V8_SCOPE="$scope" _V8_REVIEWER="$reviewer" _V8_TS="$ts" "$V8_PYTHON" - <<'PY'
import json
import os
import tempfile
from pathlib import Path

path = Path(os.environ["_V8_FORMAL_CARD"])
if path.exists():
    raise SystemExit(f"formal task card already exists: {path}")
path.parent.mkdir(parents=True, exist_ok=True)
task_id = os.environ["_V8_TASK_ID"]
agent = os.environ["_V8_AGENT"]
gear = os.environ["_V8_GEAR"]
task = os.environ["_V8_TASK"]
reviewer = os.environ["_V8_REVIEWER"]
ts = os.environ["_V8_TS"]
scope = [item.strip() for item in os.environ["_V8_SCOPE"].split(",") if item.strip()]
allowed = "\n".join(f"    - {json.dumps(item, ensure_ascii=False)}" for item in scope) or "    []"
text = f'''---
task_id: {task_id}
title: {json.dumps(task, ensure_ascii=False)}
module: "息壤 V9 runtime"
min_level: {gear}
task_size: {"L" if gear == "M5" else "M"}
owner: {json.dumps(agent, ensure_ascii=False)}
author: {json.dumps(agent, ensure_ascii=False)}
participants: []
status: in_progress
review_status: draft
reviewer: {json.dumps(reviewer, ensure_ascii=False)}
submitted_at: null
accepted_by: null
accepted_at: null
acceptance_result: null
acceptance_note: ""
priority: P1
sla:
  target_hours: 2
  hard_deadline: null
deliverables:
  - path: _temp/{task_id}/task-card.yaml
    type: runtime-authorization
    state: verified
created_at: {json.dumps(ts, ensure_ascii=False)}
updated_at: {json.dumps(ts, ensure_ascii=False)}
completed_at: null
paths:
  allowed_write_roots:
{allowed}
  temp_root: _temp/{task_id}/
gates:
  pre_start: passed
  pre_write: pending
  handoff: pending
---

# {task}

## 运行授权

- agent: `{agent}`
- gear: `{gear}`
- source: `_temp/{task_id}/task-card.yaml`
- reviewer: `{reviewer}`

## Handoff

- status: in_progress
- artifacts: 以看板产物列、事件流 file_write 与本卡声明范围为准
- verification: pending
- next action: execute declared scope and submit for review
'''
fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_name, path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
PY
}

_v8_finalize_formal_task_card() {
  local task_id="$1" result="$2" ts="$3"
  local card
  card=$(_v8_formal_task_card_path "$task_id") || return 1
  [[ -f "$card" ]] || { echo "[ERROR] 正式任务卡不存在: $card" >&2; return 1; }
  _V8_FORMAL_CARD="$card" _V8_RESULT="$result" _V8_TS="$ts" "$V8_PYTHON" - <<'PY'
import os
import tempfile

path = os.environ["_V8_FORMAL_CARD"]
result = os.environ["_V8_RESULT"]
ts = os.environ["_V8_TS"]
normalized = result.strip().lower()
if normalized in {"done", "success", "completed"} or not normalized.startswith(("abort", "cancel", "block", "fail", "error")):
    status = "done"
elif normalized.startswith(("abort", "cancel")):
    status = "cancelled"
else:
    status = "blocked"
review = "submitted" if status == "done" else "draft"
updates = {
    "status": status,
    "review_status": review,
    "submitted_at": f'"{ts}"' if review == "submitted" else "null",
    "updated_at": f'"{ts}"',
    "completed_at": f'"{ts}"',
}
lines = open(path, encoding="utf-8").readlines()
closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
seen = set()
for i in range(1, closing):
    if ":" not in lines[i]:
        continue
    key = lines[i].split(":", 1)[0]
    if key in updates:
        lines[i] = f"{key}: {updates[key]}\n"
        seen.add(key)
for key, value in updates.items():
    if key not in seen:
        lines.insert(closing, f"{key}: {value}\n")
        closing += 1
text = "".join(lines).replace("- status: in_progress\n", f"- status: {status}\n", 1).replace(
    "- verification: pending\n", "- verification: submitted for user review\n", 1
).replace(
    "- next action: execute declared scope and submit for review\n",
    "- next action: user review; accepted must be recorded through v9_accept\n",
    1,
)
fd, tmp_name = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_name, path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
PY
}

# ============================================================
# _v8_task_id_in_history / _v8_generate_task_id / _v8_reserve_task_id
#
# 任务 ID 仍保持 T-YYYYMMDD-NN 兼容格式，但不得覆盖历史任务卡：
# - 事件流查重覆盖“旧 _temp 已清理”的历史任务；
# - mkdir 原子预留覆盖多个 Agent 同时握手的竞争窗口；
# - 99 个槽位耗尽时明确失败，不降级为复用已有 ID。
# ============================================================
_v8_task_id_in_history() {
  local candidate="$1"
  [[ -f "$EVENT_FILE" ]] && LC_ALL=C grep -Fq "$candidate" "$EVENT_FILE"
}

_v8_generate_task_id() {
  local prefix="T-$(date '+%Y%m%d')"
  local candidate attempt n

  attempt=0
  while (( attempt < 128 )); do
    n=$((RANDOM % 99 + 1))
    candidate="$prefix-$(printf '%02d' "$n")"
    if [[ ! -e "$VAULT_ROOT/_temp/$candidate" ]] && ! _v8_task_id_in_history "$candidate"; then
      echo "$candidate"
      return 0
    fi
    attempt=$((attempt + 1))
  done

  n=1
  while (( n <= 99 )); do
    candidate="$prefix-$(printf '%02d' "$n")"
    if [[ ! -e "$VAULT_ROOT/_temp/$candidate" ]] && ! _v8_task_id_in_history "$candidate"; then
      echo "$candidate"
      return 0
    fi
    n=$((n + 1))
  done

  echo "[ERROR] 当日任务 ID T-$(date '+%Y%m%d')-01..99 已耗尽，拒绝复用历史 ID。" >&2
  return 1
}

_v8_reserve_task_id() {
  local prefix="T-$(date '+%Y%m%d')"
  local candidate attempt n

  mkdir -p "$VAULT_ROOT/_temp" || return 1

  attempt=0
  while (( attempt < 128 )); do
    n=$((RANDOM % 99 + 1))
    candidate="$prefix-$(printf '%02d' "$n")"
    if ! _v8_task_id_in_history "$candidate" && mkdir "$VAULT_ROOT/_temp/$candidate" 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
    attempt=$((attempt + 1))
  done

  n=1
  while (( n <= 99 )); do
    candidate="$prefix-$(printf '%02d' "$n")"
    if ! _v8_task_id_in_history "$candidate" && mkdir "$VAULT_ROOT/_temp/$candidate" 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
    n=$((n + 1))
  done

  echo "[ERROR] 当日任务 ID T-$(date '+%Y%m%d')-01..99 已耗尽，拒绝覆盖历史任务卡。" >&2
  return 1
}

# ============================================================
# _v8_close_status_atomic - 原子完成任务状态清理
# 仅修改首块 YAML frontmatter，避免命中文档正文中嵌入的约束副本。
# ============================================================
_v8_close_status_atomic() {
  local file="$1"
  local ts="$2"

  _V8_STATUS_FILE="$file" _V8_STATUS_TS="$ts" "$V8_PYTHON" - <<'PY'
import os
import stat
import tempfile

path = os.environ["_V8_STATUS_FILE"]
ts = os.environ["_V8_STATUS_TS"]
updates = {
    "status": "idle",
    "current_task": "null",
    "current_task_id": "null",
    "last_heartbeat": f'"{ts}"',
    "heartbeat_pid": "null",
    "heartbeat_session": "null",
    "heartbeat_source": "null",
    "active_subtasks": "[]",
    "spawn_count": "0",
    "write_scope": "null",
    "scope_source": "null",
}

with open(path, "r", encoding="utf-8") as handle:
    lines = handle.readlines()

if not lines or lines[0].strip() != "---":
    raise SystemExit("status file has no leading frontmatter")

closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
if closing is None:
    raise SystemExit("status file frontmatter is not closed")

seen = set()
for i in range(1, closing):
    key = lines[i].split(":", 1)[0].strip() if ":" in lines[i] else ""
    if key in updates:
        lines[i] = f"{key}: {updates[key]}\n"
        seen.add(key)

missing = [f"{key}: {value}\n" for key, value in updates.items() if key not in seen]
if missing:
    lines[closing:closing] = missing

mode = stat.S_IMODE(os.stat(path).st_mode)
directory = os.path.dirname(path) or "."
fd, tmp_path = tempfile.mkstemp(prefix=".v8-status-", dir=directory, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
PY
}

# ============================================================
# _v8_json_escape - 转义字符串以安全嵌入 JSON value
# ============================================================
_v8_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"    # \ -> \\
  s="${s//\"/\\\"}"    # " -> \"
  s="${s//$'\n'/\\n}"  # newline -> \n
  s="${s//$'\t'/\\t}"  # tab -> \t
  printf '%s' "$s"
}

# ============================================================
# _v8_yaml_escape - 转义字符串以安全嵌入 YAML quoted value
# 输出不含外层引号，调用方负责包裹 "..."
# ============================================================
_v8_yaml_escape() {
  local s="$1"
  s="${s//\\/\\\\}"    # \ -> \\
  s="${s//\"/\\\"}"    # " -> \"
  printf '%s' "$s"
}

# ============================================================
# _v8_scope_with_moc_for_m4plus - M4/M5 自动补看板写入范围
# ============================================================
_v8_scope_with_moc_for_m4plus() {
  local gear="$1"
  local scope="$2"

  if [[ "$gear" != "M4" && "$gear" != "M5" ]]; then
    printf '%s' "$scope"
    return 0
  fi

  # 用 python3 做解析+去重+补 00-MOC/，避免 bash/zsh `read -a` 差异（zsh 不支持 -ra）。
  # 与本脚本下方 scope->YAML 列表的解析方式保持一致。
  "$V8_PYTHON" -c "
import sys
scope = sys.argv[1]
parts = [p.strip() for p in scope.split(',') if p.strip()]
norm = [p.rstrip('/').lstrip('./').lstrip('/') for p in parts]
if '00-MOC' not in norm:
    parts.append('00-MOC/')
print(','.join(parts), end='')
" "$scope"
}

# ============================================================
# _v8_resolve_status_file - 根据 agent_id 返回状态文件路径
# ============================================================
_v8_resolve_status_file() {
  local agent="$1"
  local base="$VAULT_ROOT/02-项目管理/智能体状态"
  case "$agent" in
    claudian|assistant)               echo "$base/Claudian.md" ;;
    workbuddy)                       echo "$base/WorkBuddy.md" ;;
    xiaochong|amoxicillin|amox)      echo "$base/阿莫西林.md" ;;
    toubao|cephalosporin|ceph)       echo "$base/头孢.md" ;;
    qingmeisu|penicillin|peni)       echo "$base/青霉素.md" ;;
    hongmeisu|erythromycin|eryth)    echo "$base/红霉素.md" ;;
    *) echo "" ;;
  esac
}

# ============================================================
# _v8_normalize_agent_id - 将别名转为规范 agent_id（给 subtask-record.py 用）
# ============================================================
_v8_normalize_agent_id() {
  local agent="$1"
  case "$agent" in
    claudian|assistant)               echo "claudian" ;;
    workbuddy)                       echo "workbuddy" ;;
    xiaochong|amoxicillin|amox)      echo "xiaochong" ;;
    toubao|cephalosporin|ceph)       echo "toubao" ;;
    qingmeisu|penicillin|peni)       echo "qingmeisu" ;;
    hongmeisu|erythromycin|eryth)    echo "hongmeisu" ;;
    *) echo "$agent" ;;
  esac
}

_v8_default_agent_id() {
  # Codex Desktop exposes CODEX_THREAD_ID. Prefer the live platform identity
  # instead of silently attributing an omitted fifth argument to Claudian.
  if [[ -n "${CODEX_THREAD_ID:-}" || -n "${CODEX_CI:-}" ]]; then
    echo "hongmeisu"
  else
    echo "claudian"
  fi
}

_v8_platform_id() {
  local agent="$1"
  case "$agent" in
    hongmeisu) echo "codex" ;;
    claudian) echo "claude" ;;
    workbuddy) echo "workbuddy" ;;
    *) echo "$agent" ;;
  esac
}

_v8_semantic_recall() {
  local task_id="$1" task="$2" agent="$3"
  local script="$VAULT_ROOT/.standards/semantic-recall.py"
  [[ -f "$script" ]] || { echo "[semantic-recall] 召回器缺失，已跳过。" >&2; return 0; }
  local query="息壤V9-运行时契约卡 当前任务：$task"
  "$V8_PYTHON" "$script" --query "$query" --source task_start \
    --task-id "$task_id" --agent "$agent" --platform "$(_v8_platform_id "$agent")" \
    --vault "$VAULT_ROOT" --timeout 15 --quiet
  local recall_exit=$?
  if [[ $recall_exit -ne 0 ]]; then
    echo "[semantic-recall] GBrain 召回失败（任务继续，事件已记录）。" >&2
  else
    echo "[semantic-recall] 已消费 GBrain 并记录 semantic_recall 事件。"
  fi
  return 0
}

# ============================================================
# _v8_gate_check - 统一门禁检查入口（graceful degradation）
# 用法: _v8_gate_check <subcommand> <args...>
# 返回: gate-enforce.py 的退出码（0=pass, 1=P0 block）
#       如果 gate-enforce.py 不存在，返回 0 并输出 warning
# ============================================================
_v8_gate_check() {
  local gate_script="$VAULT_ROOT/.standards/gate-enforce.py"
  if [[ ! -f "$gate_script" ]]; then
    echo "[GATE-SKIP] gate-enforce.py 不存在，跳过门禁" >&2
    return 0
  fi

  local output
  output=$("$V8_PYTHON" "$gate_script" "$@" 2>&1)
  local gate_exit=${PIPESTATUS[0]:-$?}

  # 输出 gate 结果到 stderr（缩进，不干扰主输出）
  if [[ -n "$output" && "$gate_exit" -ne 0 ]]; then
    echo "$output" | while IFS= read -r line; do
      echo "  [GATE] $line" >&2
    done
  fi

  return $gate_exit
}

# ============================================================
# v8_handshake - 输出握手声明 + 自动打点
#
# M4/M5 同时创建运行授权卡（_temp）与正式审计卡；看板仍由 Agent 更新。
# ============================================================
v8_handshake() {
  local gear="$1"        # M3 / M4 / M5
  local task="$2"        # 任务名（支持任意字符：/ & " 等）
  local scope="$3"       # 写入范围
  local reviewer="${4:-用户}"  # 验收方，默认"用户"
  local agent="${5:-$(_v8_default_agent_id)}" # agent_id，Codex 环境默认红霉素

  if [[ -z "$gear" || -z "$task" || -z "$scope" ]]; then
    echo "[ERROR] 用法: v8_handshake <档位> <任务名> <写入范围> [验收方] [agent_id]"
    return 1
  fi

  # M4/M5 收工必须登记看板；默认补入 00-MOC/，避免收工阶段被自己的 scope 门禁拦住。
  scope=$(_v8_scope_with_moc_for_m4plus "$gear" "$scope")

  local ts task_id
  ts=$(date '+%Y-%m-%dT%H:%M:%S+08:00')

  # === Phase 4 Gate: M4/M5 only ===
  if [[ "$gear" == "M4" || "$gear" == "M5" ]]; then
    _v8_gate_check pre-start --agent "$agent" --gear "$gear" --write-scope "$scope"
    if [[ $? -eq 1 ]]; then
      echo "[GATE-BLOCK] 门禁不通过，任务未激活。修复后重试。" >&2
      return 1
    fi
  fi

  # --- M3: 一行极简握手 ---
  if [[ "$gear" == "M3" ]]; then
    if [[ "$scope" == *,* || "$scope" == */ ]]; then
      echo "[ERROR] M3 仅允许声明一个精确文件；多文件或目录范围请升级为 M4。" >&2
      return 1
    fi
    local normalized_agent
    normalized_agent=$(_v8_normalize_agent_id "$agent")
    task_id=$(_v8_generate_task_id) || return 1
    local m3_context="/tmp/.v8-m3-context-${normalized_agent}.json"
    _V8_M3_PATH="$m3_context" _V8_M3_TASK="$task" _V8_M3_SCOPE="$scope" \
      _V8_M3_AGENT="$normalized_agent" "$V8_PYTHON" - <<'PY'
import json
import os
import tempfile
import time

path = os.environ["_V8_M3_PATH"]
payload = {
    "agent": os.environ["_V8_M3_AGENT"],
    "task": os.environ["_V8_M3_TASK"],
    "scope": os.environ["_V8_M3_SCOPE"].lstrip("./"),
    "created_at_epoch": int(time.time()),
    "max_writes": 1,
}
fd, tmp_path = tempfile.mkstemp(prefix=".v8-m3-", dir="/tmp", text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
PY
    echo "V8 已激活：M3 | $task | 写入：$scope"
    echo "            轻量授权: $m3_context（20 分钟内单次有效）"
    echo "$task_id"
    return 0
  fi

  # M4/M5：通过 gate 后再原子预留任务目录，消除并发 TOCTOU 与历史 ID 复用。
  task_id=$(_v8_reserve_task_id) || return 1

  # --- M4/M5: 标准/完整握手 ---
  echo "V8 已激活："
  echo "- 档位：$gear"
  echo "- 任务：$task"
  echo "- 写入范围：$scope"
  if [[ "$gear" == "M5" ]]; then
    echo "- 正式路径：待确认"
  fi
  echo "- 验收方：$reviewer"

  # 更新状态文件（用 awk 安全替换）
  local status_file
  status_file=$(_v8_resolve_status_file "$agent")

  if [[ -z "$status_file" ]]; then
    echo "[WARN] 未知agent_id: $agent, 跳过状态文件更新"
  elif [[ -f "$status_file" ]]; then
    local update_ok=true
    local task_yaml_safe
    task_yaml_safe=$(_v8_yaml_escape "$task")
    _v8_safe_update_yaml "$status_file" "status" "busy" || update_ok=false
    _v8_safe_update_yaml "$status_file" "current_task" "\"$task_yaml_safe\"" || update_ok=false
    _v8_safe_update_yaml "$status_file" "current_task_id" "\"$task_id\"" || update_ok=false
    _v8_safe_update_yaml "$status_file" "last_heartbeat" "\"$ts\"" || update_ok=false
    # V8.5 Layer 1: 持久化 write_scope 供 hook 读取
    local scope_yaml_safe
    scope_yaml_safe=$(_v8_yaml_escape "$scope")
    _v8_safe_update_yaml "$status_file" "write_scope" "\"$scope_yaml_safe\"" || update_ok=false

    if [[ "$update_ok" == "false" ]]; then
      echo "[ERROR] 状态文件部分更新失败，请手动检查: $status_file" >&2
      # 不 return——事件流仍然追加，确保至少有审计记录
    fi

    # 写入验证：检查 status 确实变成了 busy
    if [[ "$(_v8_frontmatter_value "$status_file" status)" != "busy" ]]; then
      echo "[ERROR] 状态文件验证失败：status 未变为 busy" >&2
    fi
  else
    echo "[WARN] 状态文件不存在: $status_file"
  fi

  # V8.5 Phase 2: 创建任务卡（外部授权源）
  local card_dir="$VAULT_ROOT/_temp/$task_id"
  local card_file="$card_dir/task-card.yaml"
  # card_dir 已由 _v8_reserve_task_id 原子创建；保留 mkdir -p 兼容旧调用。
  mkdir -p "$card_dir"

  # 将 scope 拆成 YAML 列表（兼容 bash/zsh）
  local scope_yaml_list=""
  scope_yaml_list=$("$V8_PYTHON" -c "
import sys
scope = sys.argv[1]
parts = [p.strip() for p in scope.split(',') if p.strip()]
for p in parts:
    print(f'  - "{p}"')
" "$scope" 2>/dev/null)
  # 加换行确保 YAML 格式正确
  if [[ -n "$scope_yaml_list" ]]; then
    scope_yaml_list="${scope_yaml_list}
"
  fi

  # 写入任务卡
  printf "task_id: \"%s\"\nagent: \"%s\"\ngear: \"%s\"\ntask: \"%s\"\nauthorized_paths:\n%sscope_source: handshake\ncreated_at: \"%s\"\nreviewer: \"%s\"\n" \
    "$task_id" "$agent" "$gear" "$task_yaml_safe" "$scope_yaml_list" "$ts" "$reviewer" > "$card_file"

  if ! _v8_create_formal_task_card "$task_id" "$agent" "$gear" "$task" "$scope" "$reviewer" "$ts"; then
    echo "[ERROR] 正式任务卡创建失败，拒绝激活仅有 _temp 的任务: $task_id" >&2
    return 1
  fi

  # 更新状态文件: scope_source = task_card
  _v8_safe_update_yaml "$status_file" "scope_source" ""task_card""

  echo "            任务卡: $card_file"
  echo "            正式卡: $(_v8_formal_task_card_path "$task_id")"

  # 追加 task_start 事件（JSON 安全转义）
  local task_escaped
  task_escaped=$(_v8_json_escape "$task")
  local event="{\"ts\":\"$ts\",\"event\":\"task_start\",\"agent\":\"$agent\",\"task_id\":\"$task_id\",\"task\":\"$task_escaped\",\"gear\":\"$gear\"}"
  echo "$event" >> "$EVENT_FILE"
  _v8_semantic_recall "$task_id" "$task" "$agent"

  echo ""
  echo "[handshake] task_id=$task_id"
  echo "            状态:busy | 事件:已写入"
  echo "            任务卡已创建（scope_source: task_card）"
  echo "$task_id"
}

# ============================================================
# v8_end - 收工：状态回idle + task_end事件
# V8.5: 增加子任务完成检查（警告，不硬阻断）
# ============================================================
v8_end() {
  local task_id="$1"     # 任务ID
  local agent="${2:-$(_v8_default_agent_id)}"
  local result="${3:-done}"
  local gear="${4:-M4}"   # 档位（影响 closeout 检查严格度）

  if [[ -z "$task_id" ]]; then
    echo "[ERROR] 用法: v8_end <task_id> [agent_id] [结果] [档位M4/M5]"
    return 1
  fi

  local ts
  ts=$(date '+%Y-%m-%dT%H:%M:%S+08:00')

  # V8.5: 检查是否有未收集的子任务
  local subtasks_dir="$VAULT_ROOT/_temp/$task_id/subtasks"
  if [[ -d "$subtasks_dir" ]]; then
    local active_count=0
    for f in "$subtasks_dir"/*.json; do
      [[ -f "$f" ]] || continue
      local state
      state=$("$V8_PYTHON" -c "import json; print(json.load(open('$f'))['state'])" 2>/dev/null)
      if [[ "$state" != "COLLECTED" && "$state" != "DESTROYED" ]]; then
        active_count=$((active_count + 1))
      fi
    done
    if [[ $active_count -gt 0 ]]; then
      echo "[WARNING] $active_count 个子任务未收集（目录: $subtasks_dir）" >&2
      echo "          建议先调用 v8_collect 收集子任务结果" >&2
    fi
  fi

  # === Phase 4 Gate ===
  # 取消/中止不是成功交付，不得为了通过 closeout 伪造产物或看板记录。
  # 其他结果仍必须完整通过 pre-end。
  local normalized_result
  normalized_result=$(printf '%s' "$result" | tr '[:upper:]' '[:lower:]')
  if [[ "$normalized_result" != abort* && "$normalized_result" != cancel* ]]; then
    _v8_gate_check pre-end --task-id "$task_id" --agent "$agent" --gear "$gear"
    if [[ $? -eq 1 ]]; then
      echo "[GATE-BLOCK] 门禁不通过，任务未关闭" >&2
      return 1
    fi
  else
    echo "[v8_end] $task_id 走审计式取消；跳过成功交付门禁，不生成假产物。" >&2
  fi

  if ! _v8_finalize_formal_task_card "$task_id" "$result" "$ts"; then
    echo "[ERROR] 正式任务卡未能完成状态转移，任务保持 busy: $task_id" >&2
    return 1
  fi

  # 更新状态文件
  local status_file
  status_file=$(_v8_resolve_status_file "$agent")

  if [[ -n "$status_file" && -f "$status_file" ]]; then
    local active_task_id
    active_task_id=$(_v8_frontmatter_value "$status_file" current_task_id)
    if [[ "$active_task_id" != "$task_id" ]]; then
      echo "[ERROR] 状态文件活动任务为 ${active_task_id:-null}，拒绝由 $task_id 清理。" >&2
      return 1
    fi
    if ! _v8_close_status_atomic "$status_file" "$ts"; then
      echo "[ERROR] 收工状态原子清理失败: $status_file" >&2
      return 1
    fi

    # 验证
    if ! "$V8_PYTHON" - "$status_file" <<'PY'
import sys

lines = open(sys.argv[1], encoding="utf-8").readlines()
closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
frontmatter = dict(
    line.rstrip("\n").split(":", 1)
    for line in lines[1:closing]
    if ":" in line
)
expected = {
    "status": " idle",
    "current_task": " null",
    "current_task_id": " null",
    "write_scope": " null",
    "scope_source": " null",
    "heartbeat_pid": " null",
    "heartbeat_session": " null",
    "heartbeat_source": " null",
}
raise SystemExit(0 if all(frontmatter.get(k) == v for k, v in expected.items()) else 1)
PY
    then
      echo "[ERROR] 收工验证失败：status 未变为 idle" >&2
      return 1
    fi
  fi

  # 追加 task_end 事件
  local event="{\"ts\":\"$ts\",\"event\":\"task_end\",\"agent\":\"$agent\",\"task_id\":\"$task_id\",\"result\":\"$result\"}"
  echo "$event" >> "$EVENT_FILE"

  echo "[v8_end] $task_id -> $result, 状态已回idle"
}

# ============================================================
# v8_spawn - V8.5: 创建子任务记录 + 更新父Agent状态
# 用法: v8_spawn <parent_task_id> <sub_id> <parent_agent> <model> <type> <name> <write_scope> [timeout]
# ============================================================
v8_spawn() {
  local task_id="$1"
  local sub_id="$2"
  local agent="${3:-claudian}"
  local model="${4:-sonnet}"
  local task_type="${5:-research}"
  local name="$6"
  local write_scope="$7"
  local timeout="${8:-300}"

  if [[ -z "$task_id" || -z "$sub_id" || -z "$name" ]]; then
    echo "[ERROR] 用法: v8_spawn <task_id> <sub_id> <agent> <model> <type> <name> <write_scope> [timeout]"
    return 1
  fi

  # === Phase 4 Gate: hard enforce ===
  local norm_agent
  norm_agent=$(_v8_normalize_agent_id "$agent")
  _v8_gate_check pre-spawn \
    --task-id "$task_id" --sub-id "$sub_id" --agent "$norm_agent" \
    --model "$model" --type "$task_type" --write-scope "$write_scope"
  if [[ $? -eq 1 ]]; then
    echo "[GATE-BLOCK] 门禁不通过，子任务未创建" >&2
    return 1
  fi

  # 调用 subtask-record.py create（norm_agent 已在 gate check 中计算）
  local result
  result=$("$V8_PYTHON" "$VAULT_ROOT/.standards/subtask-record.py" create \
    --task-id "$task_id" --sub-id "$sub_id" \
    --parent "$norm_agent" --model "$model" --type "$task_type" \
    --name "$name" --write-scope "$write_scope" --timeout "$timeout" 2>&1)

  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    echo "[ERROR] subtask-record.py create 失败: $result" >&2
    return 1
  fi

  # 更新父 Agent 状态文件: spawn_count + 1, active_subtasks 追加 sub_id
  local status_file
  status_file=$(_v8_resolve_status_file "$agent")
  if [[ -n "$status_file" && -f "$status_file" ]]; then
    # 更新 spawn_count
    local current_count
    current_count=$(_v8_frontmatter_value "$status_file" spawn_count)
    current_count=${current_count:-0}
    local new_count=$((current_count + 1))
    _v8_safe_update_yaml "$status_file" "spawn_count" "$new_count"

    # 追加 active_subtasks（读取现有值，追加新 sub_id）
    local current_subs
    current_subs=$(_v8_frontmatter_value "$status_file" active_subtasks)
    if [[ "$current_subs" == "[]" || -z "$current_subs" ]]; then
      _v8_safe_update_yaml "$status_file" "active_subtasks" "[\"$sub_id\"]"
    else
      # 去掉尾部 ]，追加新条目
      local new_subs="${current_subs%]}, \"$sub_id\"]"
      _v8_safe_update_yaml "$status_file" "active_subtasks" "$new_subs"
    fi
  fi

  echo "[v8_spawn] 子任务已创建: $sub_id (task=$task_id, type=$task_type)"
  echo "$result"
}

# ============================================================
# v8_collect - V8.5: 收集子任务结果 + 更新父Agent状态
# 用法: v8_collect <parent_task_id> <sub_id> [agent] [tokens] [cost]
# ============================================================
v8_collect() {
  local task_id="$1"
  local sub_id="$2"
  local agent="${3:-claudian}"
  local tokens="${4:-0}"
  local cost="${5:-0}"

  if [[ -z "$task_id" || -z "$sub_id" ]]; then
    echo "[ERROR] 用法: v8_collect <task_id> <sub_id> [agent] [tokens] [cost]"
    return 1
  fi

  # 调用 subtask-record.py collect（用数组避免 zsh word-splitting 问题）
  local -a cmd_args=(--task-id "$task_id" --sub-id "$sub_id")
  if [[ "$tokens" != "0" ]]; then
    cmd_args+=(--tokens "$tokens")
  fi
  if [[ "$cost" != "0" ]]; then
    cmd_args+=(--cost "$cost")
  fi

  local result
  result=$("$V8_PYTHON" "$VAULT_ROOT/.standards/subtask-record.py" collect "${cmd_args[@]}" 2>&1)
  local exit_code=$?

  if [[ $exit_code -ne 0 ]]; then
    echo "[ERROR] subtask-record.py collect 失败: $result" >&2
    return 1
  fi

  # 更新父 Agent spawn_count - 1 + active_subtasks 移除 sub_id
  local status_file
  status_file=$(_v8_resolve_status_file "$agent")
  if [[ -n "$status_file" && -f "$status_file" ]]; then
    # spawn_count - 1
    local current_count
    current_count=$(_v8_frontmatter_value "$status_file" spawn_count)
    current_count=${current_count:-0}
    local new_count=$((current_count > 0 ? current_count - 1 : 0))
    _v8_safe_update_yaml "$status_file" "spawn_count" "$new_count"

    # active_subtasks 移除 sub_id
    local current_subs
    current_subs=$(_v8_frontmatter_value "$status_file" active_subtasks)
    if [[ -n "$current_subs" && "$current_subs" != "[]" ]]; then
      # 用 python 安全移除数组元素
      local new_subs
      new_subs=$("$V8_PYTHON" -c "
import json, sys
try:
    arr = json.loads('$current_subs')
    arr = [x for x in arr if x != '$sub_id']
    print(json.dumps(arr))
except:
    print('[]')
" 2>/dev/null)
      _v8_safe_update_yaml "$status_file" "active_subtasks" "${new_subs:-[]}"
    fi
  fi

  echo "[v8_collect] 子任务已收集: $sub_id (tokens=$tokens, cost=$cost)"
  echo "$result"
}

# ============================================================
# v9_accept - V9.4.1: 安全验收任务卡
# 用法: v9_accept <task_id> <accepted_by> [--reviewer NAME] [--require-fresh-eval]
# ============================================================
v9_accept() {
  local task_id="$1"
  local accepted_by="$2"

  if [[ -z "$task_id" || -z "$accepted_by" ]]; then
    echo "[ERROR] 用法: v9_accept <task_id> <accepted_by> [--reviewer NAME] [--require-fresh-eval]" >&2
    return 1
  fi

  shift 2
  "$V8_PYTHON" "$VAULT_ROOT/.standards/v9-accept.py" "$task_id" "$accepted_by" "$@"
}

# ============================================================
# v8_log - M3 轻量日志追加
# ============================================================
v8_log() {
  local summary="$1"
  local result="${2:-done}"
  local path="${3:-}"

  local ts
  ts=$(date '+%H:%M')
  local today
  today=$(date '+%Y-%m-%d')
  local log_file="$LOG_DIR/$today.md"

  # 确保日志文件存在
  if [[ ! -f "$log_file" ]]; then
    mkdir -p "$LOG_DIR"
    cat > "$log_file" <<LOGEOF
---
date: $today
type: 运行日志
tags: [运行日志]
---

# 运行日志 $today

LOGEOF
  fi

  local entry="- $ts | M3 | $summary | $result"
  if [[ -n "$path" ]]; then
    entry="$entry | $path"
  fi

  echo "$entry" >> "$log_file"
  echo "[v8_log] 已追加: $entry"
}

# ============================================================
# v8_upgrade - 升档声明
# ============================================================
v8_upgrade() {
  local from="$1"   # M3
  local to="$2"     # M4
  local reason="$3"

  echo "[升档] $from -> $to"
  echo "- 原因：$reason"

  if [[ "$to" == "M4" || "$to" == "M5" ]]; then
    echo "- 需补做：标准握手 + 建卡 + 亮灯（调用 v8_handshake）"
  fi
}
