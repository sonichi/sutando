#!/bin/bash
# A task another instance already holds is not this one's unprocessed work.
#
# THE SEAM. Once more than one instance drains the same queue, a task can be
# claimed by a peer: the claiming instance drops a sentinel in its own delivery
# folder, <workspace>/deliveries/<instance-id>/. The Stop hook must not then
# block this session on work it deliberately declined — a guard that cannot be
# satisfied stops the session forever.
#
# WHY THE `core` CARVE-OUT IS PINNED. `deliveries/core/` is this instance's own
# inbox, so a sentinel there is NOT a peer holding the task: honoring it would
# turn the hook off for every task the core itself accepted. A fix that skips
# on any delivery folder passes the exempt case and silently disables the guard,
# so the core case is what separates the two.
#
# BOTH SENTINEL SHAPES. A folder holds a task as `<id>.txt` (the delivered copy)
# or `<id>.accepted` (the receipt after it drained). Either means held; a fix
# that reads only one leaks blocks for tasks in the other state.
#
# ISOLATION follows tests/check-pending-tasks-workspace.test.sh: build a private
# workspace, ASSERT the resolver landed there before writing anything, and never
# touch the caller's live queue.
#
# Run: bash tests/check-pending-tasks-worker-held.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-heldtest.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  echo "      wanted: $TMPWS"
  echo "      got:    $WS"
  rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"; exit 1
fi

trap 'rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PEER="0123456789abcdef0123456789abcdef"   # any folder name that is not `core`
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries/core" "$WS/deliveries/$PEER"

PROBE="task-zz-heldtest-$$.txt"
TASK_ID="${PROBE%.txt}"
reset_queue() {
  rm -f "$WS/deliveries/core/$TASK_ID".* "$WS/deliveries/$PEER/$TASK_ID".*
  printf 'id: %s\ntask: probe\n' "$TASK_ID" > "$WS/tasks/$PROBE"
}

# 1. CONTROL. Unassigned, the task still blocks — so cases 2-3 measure the
# sentinel and not a hook that stopped reading the queue.
reset_queue
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "an unassigned task still blocks" ;;
  *) bad "an unassigned task still blocks" "got: ${OUT:0:120}" ;;
esac

# 2. A peer's delivered copy exempts it.
reset_queue
printf 'id: %s\n' "$TASK_ID" > "$WS/deliveries/$PEER/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "a peer's <id>.txt exempts the task" ;;
  *) bad "a peer's <id>.txt exempts the task" "still blocks: ${OUT:0:120}" ;;
esac

# 3. ...and so does its receipt, after the copy is gone.
reset_queue
: > "$WS/deliveries/$PEER/$TASK_ID.accepted"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "a peer's <id>.accepted exempts the task" ;;
  *) bad "a peer's <id>.accepted exempts the task" "still blocks: ${OUT:0:120}" ;;
esac

# 4. THE DIRECTION CASE. deliveries/core/ is this instance's OWN inbox — a
# sentinel there must NOT exempt anything, or the guard is off for its own work.
reset_queue
printf 'id: %s\n' "$TASK_ID" > "$WS/deliveries/core/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "a deliveries/core/ <id>.txt does not exempt" ;;
  *) bad "a deliveries/core/ <id>.txt does not exempt" "got: ${OUT:0:120}" ;;
esac

# 5. ...same for the core's receipt form.
reset_queue
: > "$WS/deliveries/core/$TASK_ID.accepted"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "a deliveries/core/ <id>.accepted does not exempt" ;;
  *) bad "a deliveries/core/ <id>.accepted does not exempt" "got: ${OUT:0:120}" ;;
esac

# 6. A sentinel for a DIFFERENT id is not this task's — the match must be on the
# id, not on the peer folder being non-empty.
reset_queue
: > "$WS/deliveries/$PEER/task-zz-someone-else.accepted"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "another id's sentinel does not exempt" ;;
  *) bad "another id's sentinel does not exempt" "got: ${OUT:0:120}" ;;
esac

# 7. No deliveries/ tree at all — the hook behaves as it always did.
rm -rf "$WS/deliveries"
reset_queue
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "no deliveries/ tree still blocks" ;;
  *) bad "no deliveries/ tree still blocks" "got: ${OUT:0:120}" ;;
esac

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
