#!/bin/bash
# Stable launchd entrypoint kept outside macOS protected Desktop directories.
# Publishes the UI status in the same scheduler transaction as health.
set -u

VAULT="${XIRANG_V9_VAULT_DIR:-$HOME/Desktop/obsidianVault}"
RUNTIME="${XIRANG_V9_RUNTIME_DIR:-$HOME/.xirang/v9-runtime}"
PYTHON="${XIRANG_V9_PYTHON:-/opt/homebrew/bin/python3}"
SCRIPT="${XIRANG_V9_REFLEX_SCRIPT:-$VAULT/02-项目管理/脚本/v9-reflex-check.py}"
SUMMARY_SCRIPT="${XIRANG_V9_STATUS_SCRIPT:-$VAULT/02-项目管理/脚本/v9-status-summary.py}"
HARNESS_SCRIPT="${XIRANG_V9_HARNESS_SCRIPT:-$VAULT/02-项目管理/脚本/v9-harness-eval-runner.py}"
HARNESS_VERIFY_SCRIPT="${XIRANG_V9_HARNESS_VERIFY_SCRIPT:-$VAULT/.standards/harness-eval-verify.py}"
HARNESS_REPORT="${XIRANG_V9_HARNESS_REPORT:-$RUNTIME/巡检/harness-eval-latest.json}"
HARNESS_MAX_AGE_HOURS="${XIRANG_V9_HARNESS_MAX_AGE_HOURS:-20}"
if [[ "${XIRANG_V9_TEST_MODE:-0}" == "1" ]]; then
  PHOENIX_SCRIPT="${XIRANG_V9_PHOENIX_SCRIPT:-$VAULT/02-项目管理/脚本/v9-phoenix.py}"
else
  PHOENIX_SCRIPT="$VAULT/02-项目管理/脚本/v9-phoenix.py"
fi
GBRAIN_CONTRACT_SOURCE="${XIRANG_GBRAIN_CONTRACT_SOURCE:-$VAULT/50-经验/Agent协作方法论/息壤V9-运行时契约卡.md}"
GBRAIN_CONTRACT_MIRROR="${XIRANG_GBRAIN_CONTRACT_MIRROR:-$HOME/.gbrain/runtime-contract-current.md}"
GBRAIN_MAINTENANCE="${XIRANG_GBRAIN_MAINTENANCE:-$HOME/.gbrain/maintenance-run.sh}"
STATE="$RUNTIME/巡检/reflex-scheduler-health.json"

write_state() {
  local status="$1"
  local exit_code="$2"
  local reason="$3"
  REFLEX_STATE_PATH="$STATE" REFLEX_STATE_STATUS="$status" \
  REFLEX_STATE_EXIT="$exit_code" REFLEX_STATE_REASON="$reason" \
    /usr/bin/python3 - <<'PY'
import json
import os
import tempfile
from datetime import datetime

path = os.environ["REFLEX_STATE_PATH"]
payload = {
    "status": os.environ["REFLEX_STATE_STATUS"],
    "exit_code": int(os.environ["REFLEX_STATE_EXIT"]),
    "reason": os.environ["REFLEX_STATE_REASON"],
    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
}
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".reflex-scheduler-", dir=directory, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

refresh_harness_if_needed() {
  [[ -f "$HARNESS_SCRIPT" && -f "$HARNESS_VERIFY_SCRIPT" ]] || return 78
  if "$PYTHON" "$HARNESS_VERIFY_SCRIPT" \
      --report "$HARNESS_REPORT" --root "$VAULT" \
      --max-age-hours "$HARNESS_MAX_AGE_HOURS" --json >/dev/null 2>&1; then
    return 0
  fi
  XIRANG_V9_RUNTIME_DIR="$RUNTIME" \
    "$PYTHON" "$HARNESS_SCRIPT" --write-latest --json >/dev/null || return $?
  "$PYTHON" "$HARNESS_VERIFY_SCRIPT" \
    --report "$HARNESS_REPORT" --root "$VAULT" \
    --max-age-hours "$HARNESS_MAX_AGE_HOURS" --json >/dev/null 2>&1
}

refresh_gbrain_contract_if_needed() {
  [[ -f "$GBRAIN_CONTRACT_SOURCE" && -x "$GBRAIN_MAINTENANCE" ]] || return 0
  local refresh_result
  refresh_result="$(
    GBRAIN_CONTRACT_SOURCE="$GBRAIN_CONTRACT_SOURCE" \
    GBRAIN_CONTRACT_MIRROR="$GBRAIN_CONTRACT_MIRROR" \
      "$PYTHON" - <<'PY'
import os
import tempfile
from pathlib import Path

source = Path(os.environ["GBRAIN_CONTRACT_SOURCE"])
mirror = Path(os.environ["GBRAIN_CONTRACT_MIRROR"])
payload = source.read_bytes()
if mirror.is_file() and mirror.read_bytes() == payload:
    print("unchanged")
    raise SystemExit(0)

mirror.parent.mkdir(parents=True, exist_ok=True)
fd, temp_name = tempfile.mkstemp(prefix=".runtime-contract-current.", dir=mirror.parent)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, mirror)
finally:
    if os.path.exists(temp_name):
        os.unlink(temp_name)
print("changed")
PY
  )" || return 73
  [[ "$refresh_result" == "unchanged" ]] && return 0
  [[ "$refresh_result" == "changed" ]] || return 73
  "$GBRAIN_MAINTENANCE" sync >/dev/null 2>&1
}

if [[ ! -f "$SCRIPT" ]]; then
  write_state "failed" 78 "reflex_script_missing_or_desktop_denied"
  exit 78
fi
if [[ ! -f "$SUMMARY_SCRIPT" ]]; then
  write_state "failed" 78 "status_summary_script_missing_or_desktop_denied"
  exit 78
fi
if [[ ! -f "$PHOENIX_SCRIPT" ]]; then
  write_state "failed" 78 "phoenix_script_missing_or_desktop_denied"
  exit 78
fi

write_state "running" 0 "started"
cd "$VAULT" || { write_state "failed" 72 "vault_unavailable"; exit 72; }
refresh_gbrain_contract_if_needed
gbrain_rc=$?
refresh_harness_if_needed
harness_rc=$?
run_started_epoch="$(date +%s)"
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$SCRIPT" --quiet
reflex_rc=$?
# Publish an interim status after the first health snapshot. The second reflex
# can then prove entropy -> health -> status and harness -> status consumption
# ordering within this same scheduler transaction.
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$SUMMARY_SCRIPT" --write-latest --json >/dev/null
interim_summary_rc=$?
# Re-observe after the interim status so Phoenix never learns from a transient
# output-consumption failure caused only by this transaction's write order.
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$SCRIPT" --quiet
reflex_pre_phoenix_rc=$?
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$PHOENIX_SCRIPT" --apply-safe --json >/dev/null
phoenix_rc=$?
# Phoenix may refresh an upstream runtime source. Re-observe unconditionally so
# status-latest never publishes the pre-repair health snapshot.
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$SCRIPT" --quiet
reflex_after_phoenix_rc=$?
XIRANG_V9_RUNTIME_DIR="$RUNTIME" "$PYTHON" "$SUMMARY_SCRIPT" --write-latest --json >/dev/null
summary_rc=$?
# Python launched from a macOS background agent may be denied while its cwd is
# Desktop even after the scan itself completes. Leave the protected directory
# before persisting the scheduler result.
cd "$HOME" || true

status_value="$(XIRANG_V9_RUNTIME_DIR="$RUNTIME" XIRANG_RUN_STARTED_EPOCH="$run_started_epoch" /usr/bin/python3 - <<'PY'
import json
import os
import sys
from datetime import datetime
from pathlib import Path

inspect = Path(os.environ["XIRANG_V9_RUNTIME_DIR"]).expanduser() / "巡检"
status_path = inspect / "status-latest.json"
health_path = inspect / "health-latest.json"
started = int(os.environ["XIRANG_RUN_STARTED_EPOCH"])
try:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    health = json.loads(health_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"runtime_output_invalid:{exc}", file=sys.stderr)
    raise SystemExit(1)

expected_status = str(status_path.resolve())
expected_inspect = str(inspect.resolve())
paths = status.get("paths") or {}
parts = status.get("parts") or {}
health_part = parts.get("health") or {}
level = status.get("status")
checks = [
    status.get("schema_version") == "v1",
    level in {"green", "yellow", "red"},
    str(Path(paths.get("runtime_dir", "")).expanduser().resolve()) == expected_inspect,
    str(Path(paths.get("status_latest", "")).expanduser().resolve()) == expected_status,
    health_part.get("generated_at") == health.get("generated_at"),
    status_path.stat().st_mtime >= started - 1,
    health_path.stat().st_mtime >= started - 1,
]
try:
    generated_epoch = datetime.fromisoformat(status["generated_at"].replace("Z", "+00:00")).timestamp()
except (KeyError, TypeError, ValueError):
    generated_epoch = 0
checks.append(generated_epoch >= started - 1)

part_levels = [part.get("status") for part in parts.values() if isinstance(part, dict)]
expected_level = "red" if "red" in part_levels else "yellow" if "yellow" in part_levels else "green"
checks.append(bool(part_levels) and level == expected_level)
if not all(checks):
    print("runtime_output_incoherent", file=sys.stderr)
    raise SystemExit(1)
print(level)
PY
)"
validate_rc=$?

if [[ $interim_summary_rc -gt 1 || $summary_rc -gt 1 || $validate_rc -ne 0 ]]; then
  write_state "failed" 70 "status_summary_output_invalid"
  exit 70
fi
if [[ ( $reflex_pre_phoenix_rc -ne 0 || $reflex_after_phoenix_rc -ne 0 ) && "$status_value" != "red" ]]; then
  write_state "failed" "$reflex_after_phoenix_rc" "reflex_exit_without_red_status"
  exit "$reflex_after_phoenix_rc"
fi
if [[ $gbrain_rc -ne 0 ]]; then
  write_state "failed" "$gbrain_rc" "gbrain_refresh_failed"
  exit "$gbrain_rc"
fi
if [[ $harness_rc -ne 0 ]]; then
  write_state "success" 0 "completed_status_red_harness_refresh_failed"
  exit 0
fi
if [[ $phoenix_rc -ne 0 ]]; then
  write_state "failed" "$phoenix_rc" "phoenix_failed"
  exit "$phoenix_rc"
fi
write_state "success" 0 "completed_status_${status_value}"
exit 0
