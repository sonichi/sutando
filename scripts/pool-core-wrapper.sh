#!/bin/bash
# Staged into the install bin dir (scripts/install-core-pool.sh owns that
# path): launchd's TCC blocks shebang-exec on
# scripts under ~/Documents, so ProgramArguments must point OUTSIDE it.
set -u
SESSION="core-${SUTANDO_CORE_ID}"

# Persistent form: the follower is an interactive claude session inside a
# tmux session (attachable via `tmux attach -t core-N`), not a one-shot
# --print pass. launchd restarts this wrapper when the session ends.
if ! "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; then
  "$POOL_TMUX_BIN" new-session -d -s "$SESSION" -c "$POOL_REPO_DIR" \
    "env CLAUDE_CONFIG_DIR='${CLAUDE_CONFIG_DIR:-}' \
         SUTANDO_CORE_ID='$SUTANDO_CORE_ID' \
         SUTANDO_CORE_POOL_SIZE='${SUTANDO_CORE_POOL_SIZE:-}' \
     '$POOL_CLAUDE_BIN' --dangerously-skip-permissions \
       --add-dir '$POOL_WORKSPACE' -- '/proactive-loop-pool'"
fi

PANE_PID="$("$POOL_TMUX_BIN" list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
"$(dirname "$0")/pool-follower-beat.sh" \
  "core-${SUTANDO_CORE_ID}" "$POOL_WORKSPACE" "$PANE_PID" &
BEAT=$!

# Sweep nudge: the in-session cron expires after 7 days and the watcher can
# miss events; this keystroke is the durable backstop (same pattern as the
# app's checkWatcher). A duplicate sweep is a no-op (acquire returns None).
NUDGE_S="${SUTANDO_POOL_SWEEP_NUDGE_S:-1800}"
LAST_NUDGE=$(date +%s)
while "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; do
  sleep "${SUTANDO_POOL_SESSION_POLL:-30}"
  NOW=$(date +%s)
  if [ $((NOW - LAST_NUDGE)) -ge "$NUDGE_S" ]; then
    "$POOL_TMUX_BIN" send-keys -t "$SESSION" "/proactive-loop-pool pass" Enter 2>/dev/null
    LAST_NUDGE=$NOW
  fi
done
kill "$BEAT" 2>/dev/null
exit 0
