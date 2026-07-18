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
# 版本: 1.4.1 | 创建: 2026-05-23 | 修订: 2026-05-31（cost事件集成+文档头订正） | 息壤 V9.2

VAULT_ROOT="${VAULT_ROOT:-$(pwd)}"
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
    BEGIN { field=ENVIRON["_V8_FIELD"]; value=ENVIRON["_V8_VALUE"]; found=0 }
    /^---$/ && NR==1 { print; next }
    /^---$/ && NR>1 && !found { print field ": " value; print; found=1; next }
    $0 ~ "^" field ":" { print field ": " value; found=1; next }
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
    claudian|legacy_agent)               echo "$base/Claudian.md" ;;
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
    claudian|legacy_agent)               echo "claudian" ;;
    workbuddy)                       echo "workbuddy" ;;
    xiaochong|amoxicillin|amox)      echo "xiaochong" ;;
    toubao|cephalosporin|ceph)       echo "toubao" ;;
    qingmeisu|penicillin|peni)       echo "qingmeisu" ;;
    hongmeisu|erythromycin|eryth)    echo "hongmeisu" ;;
    *) echo "$agent" ;;
  esac
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
# 注意：本函数不建卡。Agent 需在调用后自行创建 task card / 更新看板。
# ============================================================
v8_handshake() {
  local gear="$1"        # M3 / M4 / M5
  local task="$2"        # 任务名（支持任意字符：/ & " 等）
  local scope="$3"       # 写入范围
  local reviewer="${4:-用户}"  # 验收方，默认"用户"
  local agent="${5:-claudian}" # agent_id，默认 Claudian

  if [[ -z "$gear" || -z "$task" || -z "$scope" ]]; then
    echo "[ERROR] 用法: v8_handshake <档位> <任务名> <写入范围> [验收方] [agent_id]"
    return 1
  fi

  # M4/M5 收工必须登记看板；默认补入 00-MOC/，避免收工阶段被自己的 scope 门禁拦住。
  scope=$(_v8_scope_with_moc_for_m4plus "$gear" "$scope")

  local ts
  ts=$(date '+%Y-%m-%dT%H:%M:%S+08:00')
  local task_id="T-$(date '+%Y%m%d')-$(printf '%02d' $((RANDOM % 99 + 1)))"

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
    echo "V8 已激活：M3 | $task | 写入：$scope"
    echo "$task_id"
    return 0
  fi

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
    if ! grep -q '^status: busy' "$status_file"; then
      echo "[ERROR] 状态文件验证失败：status 未变为 busy" >&2
    fi
  else
    echo "[WARN] 状态文件不存在: $status_file"
  fi

  # V8.5 Phase 2: 创建任务卡（外部授权源）
  local card_dir="$VAULT_ROOT/_temp/$task_id"
  local card_file="$card_dir/task-card.yaml"
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

  # 更新状态文件: scope_source = task_card
  _v8_safe_update_yaml "$status_file" "scope_source" ""task_card""

  echo "            任务卡: $card_file"

  # 追加 task_start 事件（JSON 安全转义）
  local task_escaped
  task_escaped=$(_v8_json_escape "$task")
  local event="{\"ts\":\"$ts\",\"event\":\"task_start\",\"agent\":\"$agent\",\"task_id\":\"$task_id\",\"task\":\"$task_escaped\",\"gear\":\"$gear\"}"
  echo "$event" >> "$EVENT_FILE"

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
  local agent="${2:-claudian}"
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
  _v8_gate_check pre-end --task-id "$task_id" --agent "$agent" --gear "$gear"
  if [[ $? -eq 1 ]]; then
    echo "[GATE-BLOCK] 门禁不通过，任务未关闭" >&2
    return 1
  fi

  # 更新状态文件
  local status_file
  status_file=$(_v8_resolve_status_file "$agent")

  if [[ -n "$status_file" && -f "$status_file" ]]; then
    _v8_safe_update_yaml "$status_file" "status" "idle"
    _v8_safe_update_yaml "$status_file" "current_task" "null"
    _v8_safe_update_yaml "$status_file" "current_task_id" "null"
    _v8_safe_update_yaml "$status_file" "last_heartbeat" "\"$ts\""
    _v8_safe_update_yaml "$status_file" "active_subtasks" "[]"
    _v8_safe_update_yaml "$status_file" "spawn_count" "0"
    _v8_safe_update_yaml "$status_file" "write_scope" "null"

    # 验证
    if ! grep -q '^status: idle' "$status_file"; then
      echo "[ERROR] 收工验证失败：status 未变为 idle" >&2
      return 1
    fi
  fi

  # 追加 task_end 事件
  local event="{\"ts\":\"$ts\",\"event\":\"task_end\",\"agent\":\"$agent\",\"task_id\":\"$task_id\",\"result\":\"$result\",\"tokens\":0,\"cost_cny\":0}"
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
    current_count=$(grep '^spawn_count:' "$status_file" | awk '{print $2}')
    current_count=${current_count:-0}
    local new_count=$((current_count + 1))
    _v8_safe_update_yaml "$status_file" "spawn_count" "$new_count"

    # 追加 active_subtasks（读取现有值，追加新 sub_id）
    local current_subs
    current_subs=$(grep '^active_subtasks:' "$status_file" | sed 's/^active_subtasks: *//')
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
    current_count=$(grep '^spawn_count:' "$status_file" | awk '{print $2}')
    current_count=${current_count:-0}
    local new_count=$((current_count > 0 ? current_count - 1 : 0))
    _v8_safe_update_yaml "$status_file" "spawn_count" "$new_count"

    # active_subtasks 移除 sub_id
    local current_subs
    current_subs=$(grep '^active_subtasks:' "$status_file" | sed 's/^active_subtasks: *//')
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
