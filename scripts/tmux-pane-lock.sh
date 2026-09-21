#!/usr/bin/env bash
# tmux-pane-lock.sh <socket> <session> — print the per-pane writer lock every pane writer flocks
# (scripts/tmux-send-line.sh), so one caller can hold it across a whole multi-key transaction.
set -u
SOCK="${1:?socket}"; SESSION="${2:?session}"
PY="$(bash "$(cd "$(dirname "$0")" && pwd)/sutando-config.sh" python-bin)"
H="$(printf '%s' "$SOCK:$SESSION" | "$PY" -c 'import sys,hashlib;print(hashlib.sha1(sys.stdin.read().encode()).hexdigest()[:12])' 2>/dev/null)" && [ -n "$H" ] \
  || { echo "tmux-pane-lock: could not hash $SOCK:$SESSION (python: $PY)" >&2; exit 7; }
printf '%s/tmux-send-line.%s.lock\n' "${TMPDIR:-/tmp}" "$H"
