#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 3 ]]; then
  echo 'usage: launch.sh manifest directory log' >&2
  exit 64
fi
TASK_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK_REPO="$(cd "$TASK_SCRIPT_DIR/../../.." && pwd)"
source "$TASK_REPO/scripts/python-binary.sh"
TASK_PY="$(resolve_python "$TASK_REPO")"
[[ -n "$TASK_PY" ]] || { echo "learning dispatcher: no runnable Python" >&2; exit 1; }
umask 077
mkdir -p "$(dirname "$3")"
nohup "$TASK_PY" "$TASK_SCRIPT_DIR/dispatch_collection.py" --config "$1" --directory "$2" >"$3" 2>&1 &
TASK_JOB_PID=$!
disown 2>/dev/null || true
echo "learning collection job started (pid $TASK_JOB_PID); consumer and learning outcome pending; log=$3"
