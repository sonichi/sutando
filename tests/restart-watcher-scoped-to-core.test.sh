#!/usr/bin/env bash
# restart.sh's watcher stop must target only THIS core's own watcher, never a
# pool worker's -- a bare `pkill -f "watch-tasks"` matches a worker's command
# line too (it's a substring of "watch-tasks-stream.sh <delivery-path>"),
# which killed every worker's watcher on a real restart (2026-09-20).
#
# Mocks `kill`/`pkill` rather than relying on real signal delivery to a
# background job (unreliable inside some sandboxes) -- what must be proven is
# WHICH pid/pattern the function targets, not that SIGTERM itself works.
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

# --- Case 1: sentinel resolves -- must kill exactly that pid, nothing else ---
mkdir -p "$TMP/state" "$TMP/src"
CORE_PID=54321
echo "$CORE_PID" > "$TMP/state/sentinel"
cat > "$TMP/src/watcher_sentinel.sh" << EOF
sentinel_path_for() { printf '%s' "$TMP/state/sentinel"; }
EOF
KILL_LOG="$TMP/kill.log"
PKILL_LOG="$TMP/pkill.log"
: > "$KILL_LOG"; : > "$PKILL_LOG"

(
  REPO="$TMP"
  _WS="$TMP"
  kill() {
    printf '%s\n' "$*" >> "$KILL_LOG"
    [ "${2:-}" = "$CORE_PID" ]  # -0 probe on the fake pid: pretend it's alive
  }
  pkill() { printf '%s\n' "$*" >> "$PKILL_LOG"; }
  eval "$FUNC_SRC"
  _stop_core_watcher
)

if grep -qF -- "-TERM $CORE_PID" "$KILL_LOG"; then
  ok "sentinel-resolved pid gets SIGTERM"
else
  bad "sentinel-resolved pid gets SIGTERM" "kill.log: $(cat "$KILL_LOG")"
fi
if [ -s "$PKILL_LOG" ]; then
  bad "no fallback pkill fired when the sentinel resolved" "pkill.log: $(cat "$PKILL_LOG")"
else
  ok "no fallback pkill fired when the sentinel resolved"
fi

# --- Case 2: sentinel absent -- fallback pattern must discriminate core vs
# worker argv (the actual shapes watch-tasks-stream.sh runs under) ---
rm -f "$TMP/state/sentinel"
: > "$KILL_LOG"; : > "$PKILL_LOG"
(
  REPO="$TMP"
  _WS="$TMP"
  kill() { printf '%s\n' "$*" >> "$KILL_LOG"; return 1; }
  pkill() { printf '%s\n' "$*" >> "$PKILL_LOG"; }
  eval "$FUNC_SRC"
  _stop_core_watcher
)
FALLBACK_PATTERN="$(grep -oE 'pkill -f "[^"]*watch-tasks-stream[^"]*"' "$REPO/src/restart.sh" | sed -E 's/^pkill -f "//; s/"$//')"
if [ -z "$FALLBACK_PATTERN" ]; then
  bad "extracted the fallback pkill pattern from restart.sh" "not found"
else
  ok "extracted the fallback pkill pattern: $FALLBACK_PATTERN"
  CORE_ARGV="/usr/bin/bash /path/to/watch-tasks-stream.sh"
  WORKER_ARGV="/usr/bin/bash /path/to/watch-tasks-stream.sh /workspace/deliveries/abc123"
  if [[ "$CORE_ARGV" =~ $FALLBACK_PATTERN ]]; then
    ok "fallback pattern matches a core-style invocation (no trailing argument)"
  else
    bad "fallback pattern matches a core-style invocation (no trailing argument)" "no match against: $CORE_ARGV"
  fi
  if [[ "$WORKER_ARGV" =~ $FALLBACK_PATTERN ]]; then
    bad "fallback pattern does NOT match a worker-style invocation" "matched: $WORKER_ARGV -- this is the exact regression"
  else
    ok "fallback pattern does NOT match a worker-style invocation"
  fi
fi
if grep -qF -- "watch-tasks-stream.sh\$" "$PKILL_LOG"; then
  ok "fallback pkill actually fired with the anchored pattern when the sentinel was absent"
else
  bad "fallback pkill actually fired with the anchored pattern when the sentinel was absent" "pkill.log: $(cat "$PKILL_LOG")"
fi

# --- Regression check: the OLD unscoped line must be gone, not just
# shadowed by the new function (a stray leftover would still fire). ---
if grep -q '^pkill -f "watch-tasks" 2>/dev/null$' "$REPO/src/restart.sh"; then
  bad "the old unscoped pkill line is gone" "still present verbatim"
else
  ok "the old unscoped pkill line is gone"
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
