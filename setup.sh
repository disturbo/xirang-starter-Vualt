#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo '息壤 V9.7.0 当前正式支持 macOS；本次未写入任何文件。' >&2
  exit 20
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo '需要 Python 3.11 或更高版本。请让 Agent 安装后重试。' >&2
  exit 21
fi

PYTHON_BIN="$(command -v python3)"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo 'Python 版本低于 3.11。请让 Agent 升级后重试。' >&2
  exit 21
fi

exec "$PYTHON_BIN" "$PACKAGE_ROOT/installer/xirang_install.py" "$@"
