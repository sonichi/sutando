#!/bin/bash
# Staged into the install bin dir (scripts/install-worker-pool.sh owns that
# path): launchd's TCC blocks shebang-exec on
# scripts under ~/Documents, so ProgramArguments must point OUTSIDE it.
set -u
# The worker's name comes from the plist env (SUTANDO_WORKER_ID, else the seat);
# src/pool_names.py owns the spelling, so a legacy SUTANDO_CORE_ID still resolves.
WORKER_ID="$(python3 "$POOL_REPO_DIR/src/pool_names.py" from_env)" || {
  echo "pool-worker-wrapper: no worker identity in the environment (SUTANDO_WORKER_ID / SUTANDO_WORKER_SEAT)" >&2
  exit 2
}
WORKER_SEAT="$(python3 "$POOL_REPO_DIR/src/pool_names.py" seat_of "$WORKER_ID")"
POOL_SIZE="${SUTANDO_WORKER_POOL_SIZE:-${SUTANDO_CORE_POOL_SIZE:-}}"
SESSION="$WORKER_ID"

# Per-runtime session-driving policy has ONE owner; this wrapper is an adapter.
DRIVE_LIB="$(dirname "$0")/pool-runtime-drive.sh"
if ! [ -r "$DRIVE_LIB" ]; then
  echo "pool-worker-wrapper: missing $DRIVE_LIB — re-run scripts/install-worker-pool.sh" >&2
  exit 2
fi
# shellcheck source=./pool-runtime-drive.sh
. "$DRIVE_LIB"

# Runtime dimension: the installer declares it per worker. A plist written before
# the dimension existed carries only POOL_CLAUDE_BIN, so default to claude.
POOL_RUNTIME="${POOL_RUNTIME:-claude}"
POOL_RUNTIME_BIN="${POOL_RUNTIME_BIN:-${POOL_CLAUDE_BIN:-}}"

POOL_CODEX_ENTRY="$(pool_drive_nudge_text codex "$WORKER_ID")"

case "$POOL_RUNTIME" in
  claude)
    # Persistent form: the follower is an interactive claude session inside a
    # tmux session (attachable via `tmux attach -t worker-N`), not a one-shot
    # --print pass. launchd restarts this wrapper when the session ends.
    LAUNCH_CMD="env CLAUDE_CONFIG_DIR='${CLAUDE_CONFIG_DIR:-}' \
         SUTANDO_WORKER_ID='$WORKER_ID' SUTANDO_WORKER_SEAT='$WORKER_SEAT' \
         SUTANDO_WORKER_POOL_SIZE='$POOL_SIZE' \
         SUTANDO_CORE_ID='$WORKER_SEAT' SUTANDO_CORE_POOL_SIZE='$POOL_SIZE' \
     '$POOL_RUNTIME_BIN' --dangerously-skip-permissions \
       --add-dir '$POOL_WORKSPACE' -- '/proactive-loop-pool'"
    NUDGE_DEFAULT=1800
    POLL_DEFAULT=30
    ;;
  codex)
    # Same persistent tmux form; flags mirror src/agent/codex/cli/start-cli.sh,
    # and the pool entry rides codex's optional [PROMPT] positional.
    RUNTIME_CFG=""
    if [ -n "${POOL_RUNTIME_CONFIG_ENV:-}" ] && [ -n "${POOL_RUNTIME_CONFIG_DIR:-}" ]; then
      RUNTIME_CFG="$POOL_RUNTIME_CONFIG_ENV='$POOL_RUNTIME_CONFIG_DIR'"
    fi
    LAUNCH_CMD="env $RUNTIME_CFG \
         SUTANDO_WORKER_ID='$WORKER_ID' SUTANDO_WORKER_SEAT='$WORKER_SEAT' \
         SUTANDO_WORKER_POOL_SIZE='$POOL_SIZE' \
         SUTANDO_CORE_ID='$WORKER_SEAT' SUTANDO_CORE_POOL_SIZE='$POOL_SIZE' \
         SUTANDO_CORE_RUNTIME='codex' \
     '$POOL_RUNTIME_BIN' -C '$POOL_REPO_DIR' --add-dir '$POOL_WORKSPACE' \
       --sandbox danger-full-access --ask-for-approval never \
       --search --no-alt-screen '$POOL_CODEX_ENTRY'"
    # Codex has no session CronCreate or pool-mode notifier. Assignment files
    # wake it below; this 5-minute sweep remains the leaderless backstop.
    NUDGE_DEFAULT=300
    # An assigned file is Codex's durable wake request. Poll it closely, but
    # let pool_drive_nudge touch the pane only after a positive idle read.
    POLL_DEFAULT=1
    ;;
  *)
    # Mirrors src/agent/start-cli.sh: an unknown runtime fails loudly rather
    # than silently starting the other one.
    echo "pool-worker-wrapper: unsupported worker runtime: $POOL_RUNTIME" >&2
    exit 2
    ;;
esac

if [ -z "$POOL_RUNTIME_BIN" ]; then
  echo "pool-worker-wrapper: no binary for runtime $POOL_RUNTIME" >&2
  exit 2
fi

if ! "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; then
  "$POOL_TMUX_BIN" new-session -d -s "$SESSION" -c "$POOL_REPO_DIR" "$LAUNCH_CMD"
fi

PANE_PID="$("$POOL_TMUX_BIN" list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
"$(dirname "$0")/pool-follower-beat.sh" \
  "$WORKER_ID" "$POOL_WORKSPACE" "$PANE_PID" &
BEAT=$!

# Sweep nudge: the in-session cron expires after 7 days and the watcher can
# miss events; this keystroke is the durable backstop (same pattern as the
# app's checkWatcher). A duplicate sweep is a no-op (acquire returns None).
# The keystroke policy itself belongs to pool-runtime-drive.sh; this function
# is only this wrapper's tmux binding.
pool_tmux() { "$POOL_TMUX_BIN" "$@" 2>/dev/null; }

# The assigned file is the durable pending-wake record. Return one exact path
# so a successful send can latch it until acquire_work renames or moves it.
pool_first_assignment() {
  local task
  [ "$POOL_RUNTIME" = "codex" ] || return 1
  for task in "$POOL_WORKSPACE"/tasks/task-*.assigned-"$WORKER_ID".txt; do
    [ -e "$task" ] || return 1
    printf '%s' "$task"
    return 0
  done
  return 1
}

NUDGE_S="${SUTANDO_POOL_SWEEP_NUDGE_S:-$NUDGE_DEFAULT}"
POLL_S="${SUTANDO_POOL_SESSION_POLL:-$POLL_DEFAULT}"
LAST_NUDGE=$(date +%s)
WAKE_TARGET=""
WORK_WAKE_PENDING=0
while "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; do
  sleep "$POLL_S"
  NOW=$(date +%s)
  ASSIGNMENT="$(pool_first_assignment || true)"
  if [ -n "$WAKE_TARGET" ] && [ ! -e "$WAKE_TARGET" ]; then
    WAKE_TARGET=""
  fi
  if [ "$POOL_RUNTIME" = "codex" ] && [ -z "$WAKE_TARGET" ]; then
    if [ -n "$ASSIGNMENT" ]; then
      WORK_WAKE_PENDING=1
    else
      WORK_WAKE_PENDING=0
    fi
  fi
  PERIODIC_DUE=0
  [ $((NOW - LAST_NUDGE)) -ge "$NUDGE_S" ] && PERIODIC_DUE=1
  if [ "$PERIODIC_DUE" -eq 1 ] || [ "$WORK_WAKE_PENDING" -eq 1 ]; then
    if pool_drive_nudge "$POOL_RUNTIME" "$SESSION" pool_tmux "$WORKER_ID"; then
      LAST_NUDGE=$NOW
      if [ "$WORK_WAKE_PENDING" -eq 1 ]; then
        WAKE_TARGET="$ASSIGNMENT"
        WORK_WAKE_PENDING=0
      fi
    fi
  fi
done
kill "$BEAT" 2>/dev/null
exit 0
