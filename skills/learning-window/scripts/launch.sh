#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 3 ]]; then
  echo 'usage: launch.sh manifest directory log' >&2
  exit 64
fi
TASK_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
umask 077
mkdir -p "$(dirname "$3")"
nohup python3 "$TASK_SCRIPT_DIR/dispatch_collection.py" --config "$1" --directory "$2" >"$3" 2>&1 &
TASK_JOB_PID=$!
disown 2>/dev/null || true
echo "learning collection job started (pid $TASK_JOB_PID); consumer and learning outcome pending; log=$3"
