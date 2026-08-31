#!/bin/bash
# Staged into the install bin dir (scripts/install-core-pool.sh owns that
# path): launchd's TCC blocks shebang-exec on
# scripts under ~/Documents, so ProgramArguments must point OUTSIDE it.
set -u
SESSION="core-${SUTANDO_CORE_ID}"

# Per-runtime session-driving policy has ONE owner; this wrapper is an adapter.
DRIVE_LIB="$(dirname "$0")/pool-runtime-drive.sh"
if ! [ -r "$DRIVE_LIB" ]; then
  echo "pool-core-wrapper: missing $DRIVE_LIB — re-run scripts/install-core-pool.sh" >&2
  exit 2
fi
# shellcheck source=./pool-runtime-drive.sh
. "$DRIVE_LIB"

# Runtime dimension: the installer declares it per core. A plist written before
# the dimension existed carries only POOL_CLAUDE_BIN, so default to claude.
POOL_RUNTIME="${POOL_RUNTIME:-claude}"
POOL_RUNTIME_BIN="${POOL_RUNTIME_BIN:-${POOL_CLAUDE_BIN:-}}"

POOL_CODEX_ENTRY="$(pool_drive_nudge_text codex "$SUTANDO_CORE_ID")"

case "$POOL_RUNTIME" in
  claude)
    # Persistent form: the follower is an interactive claude session inside a
    # tmux session (attachable via `tmux attach -t core-N`), not a one-shot
    # --print pass. launchd restarts this wrapper when the session ends.
    LAUNCH_CMD="env CLAUDE_CONFIG_DIR='${CLAUDE_CONFIG_DIR:-}' \
         SUTANDO_CORE_ID='$SUTANDO_CORE_ID' \
         SUTANDO_CORE_POOL_SIZE='${SUTANDO_CORE_POOL_SIZE:-}' \
     '$POOL_RUNTIME_BIN' --dangerously-skip-permissions \
       --add-dir '$POOL_WORKSPACE' -- '/proactive-loop-pool'"
    NUDGE_DEFAULT=1800
    ;;
  codex)
    # Same persistent tmux form; flags mirror src/agent/codex/cli/start-cli.sh,
    # and the pool entry rides codex's optional [PROMPT] positional.
    RUNTIME_CFG=""
    if [ -n "${POOL_RUNTIME_CONFIG_ENV:-}" ] && [ -n "${POOL_RUNTIME_CONFIG_DIR:-}" ]; then
      RUNTIME_CFG="$POOL_RUNTIME_CONFIG_ENV='$POOL_RUNTIME_CONFIG_DIR'"
    fi
    LAUNCH_CMD="env $RUNTIME_CFG \
         SUTANDO_CORE_ID='$SUTANDO_CORE_ID' \
         SUTANDO_CORE_POOL_SIZE='${SUTANDO_CORE_POOL_SIZE:-}' \
         SUTANDO_CORE_RUNTIME='codex' \
     '$POOL_RUNTIME_BIN' -C '$POOL_REPO_DIR' --add-dir '$POOL_WORKSPACE' \
       --sandbox danger-full-access --ask-for-approval never \
       --search --no-alt-screen '$POOL_CODEX_ENTRY'"
    # The wrapper nudge is a Codex follower's ONLY sweep: it has no session
    # CronCreate and no pool-mode notifier, so it runs at the 5-minute cadence.
    NUDGE_DEFAULT=300
    ;;
  *)
    # Mirrors src/agent/start-cli.sh: an unknown runtime fails loudly rather
    # than silently starting the other one.
    echo "pool-core-wrapper: unsupported core runtime: $POOL_RUNTIME" >&2
    exit 2
    ;;
esac

if [ -z "$POOL_RUNTIME_BIN" ]; then
  echo "pool-core-wrapper: no binary for runtime $POOL_RUNTIME" >&2
  exit 2
fi

if ! "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; then
  "$POOL_TMUX_BIN" new-session -d -s "$SESSION" -c "$POOL_REPO_DIR" "$LAUNCH_CMD"
fi

PANE_PID="$("$POOL_TMUX_BIN" list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
"$(dirname "$0")/pool-follower-beat.sh" \
  "core-${SUTANDO_CORE_ID}" "$POOL_WORKSPACE" "$PANE_PID" &
BEAT=$!

# Sweep nudge: the in-session cron expires after 7 days and the watcher can
# miss events; this keystroke is the durable backstop (same pattern as the
# app's checkWatcher). A duplicate sweep is a no-op (acquire returns None).
# The keystroke policy itself belongs to pool-runtime-drive.sh; this function
# is only this wrapper's tmux binding.
pool_tmux() { "$POOL_TMUX_BIN" "$@" 2>/dev/null; }

# Work-driven nudge: an assignment addressed to THIS core is a reason to sweep
# now, not at the next timer tick. pool_drive_nudge still defers unless the
# pane is idle, so "waiting on work" and "busy" stay distinguishable.
pool_has_assignment() {
  set -- "$POOL_WORKSPACE"/tasks/task-*.assigned-core-"$SUTANDO_CORE_ID".txt
  [ -e "$1" ]
}

NUDGE_S="${SUTANDO_POOL_SWEEP_NUDGE_S:-$NUDGE_DEFAULT}"
# Floor between work-driven nudges: a claim takes a moment to land, and
# re-prompting inside that window stacks a second entry on the same task.
NUDGE_MIN_S="${SUTANDO_POOL_NUDGE_MIN_S:-60}"
LAST_NUDGE=$(date +%s)
while "$POOL_TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; do
  sleep "${SUTANDO_POOL_SESSION_POLL:-30}"
  NOW=$(date +%s)
  SINCE=$((NOW - LAST_NUDGE))
  if [ "$SINCE" -ge "$NUDGE_S" ] \
     || { [ "$SINCE" -ge "$NUDGE_MIN_S" ] && pool_has_assignment; }; then
    if pool_drive_nudge "$POOL_RUNTIME" "$SESSION" pool_tmux "$SUTANDO_CORE_ID"; then
      LAST_NUDGE=$NOW
    fi
  fi
done
kill "$BEAT" 2>/dev/null
exit 0
