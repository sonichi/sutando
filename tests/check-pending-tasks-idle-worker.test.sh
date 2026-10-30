#!/bin/bash
# An idle pool worker's turn end owes no message: it has no delivery to answer.
#
# A worker re-arms its inbox watcher every 30 minutes, when its Monitor expires.
# That turn answers nobody, yet the Stop hook's turn-ledger gate refused it with
# "This turn is ending without a message and without an explicit no-send", so the
# worker learned to record a no-send on every re-arm: one extra full-context model
# call per wake, 48 a day per idle worker. A worker's obligations are its own
# deliveries, which the hook's worker branch already enforces; past that, nothing
# is owed. The core keeps the gate (control), and a worker that owes a delivery
# is still blocked (control).
#
# Run: bash tests/check-pending-tasks-idle-worker.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-idle.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
export SUTANDO_STOP_HOOK_WATCHER_GATE=0
unset SUTANDO_INSTANCE_ID

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"; exit 1
fi
trap 'rm -rf "$TMPWS"' EXIT
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

STOP='{"hook_event_name":"Stop","stop_hook_active":false}'
# Two real Stop events in one session: the first sets the boundary, the second is a
# turn that ended with no message after it.
silent_turn() {
  printf '%s' "$STOP" | env "$@" bash "$HOOK" >/dev/null 2>&1
  printf '%s' "$STOP" | env "$@" bash "$HOOK" 2>/dev/null
}

WORKER=worker-idle

# 1. The core is still held to "a message or a no-send" (proves the gate is live here).
OUT="$(silent_turn SUTANDO_CORE_SESSION=1 CLAUDE_CODE_SESSION_ID=core-sess)"
case "$OUT" in
  *'without an explicit no-send'*) ok "core: a silent turn is still refused" ;;
  *) bad "core: a silent turn is still refused" "got: ${OUT:0:200}" ;;
esac

# 2. An idle worker's silent turn (a watcher re-arm) ends without a reminder.
OUT="$(silent_turn SUTANDO_CORE_SESSION=0 SUTANDO_INSTANCE_ID="$WORKER" CLAUDE_CODE_SESSION_ID=worker-sess)"
case "$OUT" in
  '{}') ok "idle worker: a silent re-arm turn ends without a no-send reminder" ;;
  *) bad "idle worker: a silent re-arm turn ends without a no-send reminder" "got: ${OUT:0:200}" ;;
esac

# 3. A worker that owes a delivery is still blocked on it.
PROBE="task-idle-hooktest-$$"
printf 'id: %s\ntask: owed\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
mkdir -p "$WS/deliveries/$WORKER"
: > "$WS/deliveries/$WORKER/$PROBE.txt"
OUT="$(printf '%s' "$STOP" | SUTANDO_CORE_SESSION=0 SUTANDO_INSTANCE_ID="$WORKER" \
        CLAUDE_CODE_SESSION_ID=worker-sess bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  *'"decision":"block"'*"$PROBE"*) ok "worker with an owed delivery is still blocked on it" ;;
  *) bad "worker with an owed delivery is still blocked on it" "got: ${OUT:0:200}" ;;
esac

[ "$FAILED" -eq 0 ] && echo "PASS" || echo "FAILED"
exit "$FAILED"
