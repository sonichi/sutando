#!/usr/bin/env bash
# restart.sh's watcher stop must target only THIS core's own watcher, never a
# pool worker's -- a bare `pkill -f "watch-tasks"` matches a worker's command
# line too (it's a substring of "watch-tasks-stream.sh <delivery-path>"),
# which killed every worker's watcher on a real restart (2026-09-20).
#
# _stop_core_watcher() delegates ownership-proof-then-kill to
# reap_stale_task_watcher() (src/startup-runtime.sh) -- the exact function
# startup.sh's own boot reaper uses, already covered end-to-end by
# tests/startup-watcher-reaper-ownership.test.sh (PID-reuse protection,
# TERM/KILL escalation, re-proof before escalating). This suite is a WIRING
# test: does restart.sh call the shared function with the right sentinel and
# nothing else, not a second copy of the ownership-proof logic itself
# (kewei's #4569 review: a bare `kill -0` here was a PID-reuse trap, and no
# argv-pattern fallback is safe -- both production launchers invoke
# watch-tasks-stream.sh with a trailing TASKS_DIR argument even for core, so
# there is no shape that distinguishes core's own invocation by pattern).
#
# Run: bash tests/restart-watcher-scoped-to-core.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s -- %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FUNC_SRC="$(sed -n '/^_stop_core_watcher() {/,/^}/p' "$REPO/src/restart.sh")"
if [ -z "$FUNC_SRC" ]; then
  bad "extracted _stop_core_watcher() from src/restart.sh" "function not found -- has it been renamed?"
  echo "restart.sh watcher-stop scoping: FAILURES ABOVE"
  exit 1
fi
ok "extracted _stop_core_watcher() from src/restart.sh"

# --- wiring: sourced from src/startup-runtime.sh, not a second copy of the
# sentinel/ownership logic ---
if grep -qF 'src/startup-runtime.sh' "$REPO/src/restart.sh"; then
  ok "restart.sh sources startup-runtime.sh (the shared reaper), not a private copy"
else
  bad "restart.sh sources startup-runtime.sh (the shared reaper), not a private copy" "no reference found"
fi

# --- Case 1: sentinel resolves -- reap_stale_task_watcher is called with
# EXACTLY that sentinel path, and nothing else touches kill/pkill directly. ---
mkdir -p "$TMP/state" "$TMP/src"
SENTINEL="$TMP/state/sentinel"
echo "54321" > "$SENTINEL"
cat > "$TMP/src/startup-runtime.sh" << EOF
sentinel_path_for() { printf '%s' "$SENTINEL"; }
reap_stale_task_watcher() { printf 'reap_stale_task_watcher %s\n' "\$1" >> "$TMP/reap.log"; }
EOF
REAP_LOG="$TMP/reap.log"
KILL_LOG="$TMP/kill.log"
PKILL_LOG="$TMP/pkill.log"
: > "$REAP_LOG"; : > "$KILL_LOG"; : > "$PKILL_LOG"

(
  REPO="$TMP"
  _WS="$TMP"
  kill() { printf '%s\n' "$*" >> "$KILL_LOG"; }
  pkill() { printf '%s\n' "$*" >> "$PKILL_LOG"; }
  eval "$FUNC_SRC"
  _stop_core_watcher
)

if grep -qF "reap_stale_task_watcher $SENTINEL" "$REAP_LOG"; then
  ok "reap_stale_task_watcher called with the resolved sentinel path"
else
  bad "reap_stale_task_watcher called with the resolved sentinel path" "reap.log: $(cat "$REAP_LOG")"
fi
if [ -s "$KILL_LOG" ] || [ -s "$PKILL_LOG" ]; then
  bad "no direct kill/pkill call -- ownership proof is the reaper's job" \
    "kill.log: $(cat "$KILL_LOG") / pkill.log: $(cat "$PKILL_LOG")"
else
  ok "no direct kill/pkill call -- ownership proof is the reaper's job"
fi

# --- Case 2: sentinel absent -- must NOT fall back to any pattern-match
# kill (there is no safe pattern: core's own real invocation always carries
# a trailing TASKS_DIR argument, same shape as a worker's). Warn and leave
# every watcher untouched. ---
: > "$REAP_LOG"; : > "$KILL_LOG"; : > "$PKILL_LOG"
cat > "$TMP/src/startup-runtime.sh" << EOF
sentinel_path_for() { return 1; }
reap_stale_task_watcher() { printf 'reap_stale_task_watcher %s\n' "\$1" >> "$TMP/reap.log"; }
EOF
OUT="$(
  REPO="$TMP"
  _WS="$TMP"
  kill() { printf '%s\n' "$*" >> "$KILL_LOG"; }
  pkill() { printf '%s\n' "$*" >> "$PKILL_LOG"; }
  eval "$FUNC_SRC"
  _stop_core_watcher
)"
if [ -s "$REAP_LOG" ] || [ -s "$KILL_LOG" ] || [ -s "$PKILL_LOG" ]; then
  bad "no kill of any kind when the sentinel can't be resolved" \
    "reap.log: $(cat "$REAP_LOG") / kill.log: $(cat "$KILL_LOG") / pkill.log: $(cat "$PKILL_LOG")"
else
  ok "no kill of any kind when the sentinel can't be resolved -- fails closed"
fi
case "$OUT" in
  *"could not resolve"*) ok "unresolved sentinel is warned about, not silently ignored" ;;
  *) bad "unresolved sentinel is warned about, not silently ignored" "output: $OUT" ;;
esac

# --- Regression check: the OLD unscoped line must be gone. ---
if grep -q '^pkill -f "watch-tasks" 2>/dev/null$' "$REPO/src/restart.sh"; then
  bad "the old unscoped pkill line is gone" "still present verbatim"
else
  ok "the old unscoped pkill line is gone"
fi

# --- Regression check: no anchored pkill fallback either -- proven above to
# be unable to match a real core invocation (both launchers pass a trailing
# TASKS_DIR argument), so its mere presence would be a false sense of safety. ---
if grep -qF 'pkill -f "watch-tasks-stream.sh$"' "$REPO/src/restart.sh"; then
  bad "no anchored no-argument pkill fallback remains" \
    "present -- this pattern cannot match a real core watcher (see PR discussion)"
else
  ok "no anchored no-argument pkill fallback remains"
fi

# --- The drain-wait loop's STOP_PATTERNS has the identical unscoped-match
# problem: waiting on a worker's watcher that was never stopped. ---
patterns_block="$(sed -n '/^STOP_PATTERNS=(/,/^)/p' "$REPO/src/restart.sh")"
if printf '%s\n' "$patterns_block" | grep -q '"watch-tasks"'; then
  bad "STOP_PATTERNS no longer waits on the unscoped watch-tasks pattern" \
    "still present -- would spin the full drain loop whenever a worker is running"
else
  ok "STOP_PATTERNS no longer waits on the unscoped watch-tasks pattern"
fi

echo "restart.sh watcher-stop scoping:"
if [ "$fails" -eq 0 ]; then
  echo "  ALL PASS"
  exit 0
else
  echo "  $fails FAILURE(S)"
  exit 1
fi
