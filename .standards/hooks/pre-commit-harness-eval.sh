#!/usr/bin/env bash
# V9.4.1 harness eval pre-commit hook.
#
# Runs only when staged files touch harness code:
#   - .standards/
#   - 02-项目管理/脚本/
#
# It intentionally does not write harness-eval-latest.json. Commits should not
# mutate the working tree; v9_accept is responsible for requiring a fresh
# persisted eval report before accepting harness tasks.

set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [[ -z "$ROOT" ]]; then
  exit 0
fi

cd "$ROOT" || exit 0

STAGED=()
while IFS= read -r path; do
  STAGED+=("$path")
done < <(git -c core.quotepath=false diff --cached --name-only --diff-filter=ACMRT)
if [[ ${#STAGED[@]} -eq 0 ]]; then
  exit 0
fi

NEEDS_EVAL=false
for path in "${STAGED[@]}"; do
  case "$path" in
    .standards/*|02-项目管理/脚本/*)
      NEEDS_EVAL=true
      break
      ;;
  esac
done

if [[ "$NEEDS_EVAL" != "true" ]]; then
  exit 0
fi

echo "[V9-PRE-COMMIT] harness files staged; running v9-harness-eval-runner.py" >&2
python3 "02-项目管理/脚本/v9-harness-eval-runner.py" >&2
status=$?
if [[ $status -ne 0 ]]; then
  echo "[V9-PRE-COMMIT-BLOCK] harness eval failed; fix before commit." >&2
  exit $status
fi

exit 0
