#!/bin/bash
# A task another instance already holds is not this one's unprocessed work.
#
# THE SEAM. Once more than one instance drains the same queue, a task can be
# claimed by a peer: the claiming instance drops a sentinel in its own delivery
# folder, <workspace>/deliveries/<instance-id>/. The Stop hook must not then
# block this session on work it deliberately declined — a guard that cannot be
# satisfied stops the session forever.
#
# WHOSE INBOX IS "OWN" IS PER-INSTANCE. `deliveries/<SUTANDO_INSTANCE_ID>/` is
# this instance's own inbox, so a sentinel there is NOT a peer holding the task:
# honoring it would turn the hook off for every task this instance accepted.
# Unset, the instance IS the core (runtime-api/rundir.py), so the core's folder
# is its own and a worker's is a peer's — and running as a worker that reverses.
# A fix that hardcodes `core` passes every core case and inverts every worker one,
# so both perspectives are run against the same three folders.
#
# EVERY SENTINEL SHAPE. A folder holds a task as `<id>.txt` (the delivered copy),
# `<id>.accepted` (the receipt after it drained), or the legacy `<id>.claimed`.
# Which names count is pool_delivery's to say; a re-spelling here reads a held
# task as unprocessed, so the suite runs all three and a non-recipient folder.
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

PEER="0123456789abcdef0123456789abcdef"   # a 32-char instance id, as the pool issues
OTHER="fedcba9876543210fedcba9876543210"  # a third instance, peer to both
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries/core" "$WS/deliveries/$PEER"

PROBE="task-zz-heldtest-$$.txt"
TASK_ID="${PROBE%.txt}"
reset_queue() {
  rm -f "$WS/deliveries/core/$TASK_ID".* "$WS/deliveries/$PEER/$TASK_ID".* \
        "$WS/deliveries/$OTHER/$TASK_ID".* 2>/dev/null
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

# 7. THE LEGACY RECEIPT. pool_delivery.find()/accepted() still recognize
# `<id>.claimed`, so an upgraded workspace holds work under that name too.
reset_queue
: > "$WS/deliveries/$PEER/$TASK_ID.claimed"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "a peer's legacy <id>.claimed exempts the task" ;;
  *) bad "a peer's legacy <id>.claimed exempts the task" "still blocks: ${OUT:0:120}" ;;
esac

# 8. A directory no recipient could be named cannot hold anything — the delivery
# owner rejects the id, so a sentinel inside it must not suppress the task.
reset_queue
mkdir -p "$WS/deliveries/NOT_A_RECIPIENT"
: > "$WS/deliveries/NOT_A_RECIPIENT/$TASK_ID.accepted"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "an invalid recipient folder does not exempt" ;;
  *) bad "an invalid recipient folder does not exempt" "got: ${OUT:0:120}" ;;
esac
rm -rf "$WS/deliveries/NOT_A_RECIPIENT"

# 9-11. THE WORKER'S PERSPECTIVE. Same three folders, read as the worker: its own
# is no longer exempt and the core's now is. Hardcoding `core` reverses both.
reset_queue
printf 'id: %s\n' "$TASK_ID" > "$WS/deliveries/$PEER/$PROBE"
OUT="$(SUTANDO_INSTANCE_ID="$PEER" bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "a worker still blocks on its OWN delivery" ;;
  *) bad "a worker still blocks on its OWN delivery" "got: ${OUT:0:120}" ;;
esac

reset_queue
: > "$WS/deliveries/core/$TASK_ID.accepted"
OUT="$(SUTANDO_INSTANCE_ID="$PEER" bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "a worker does not answer work the core holds" ;;
  *) bad "a worker does not answer work the core holds" "still blocks: ${OUT:0:120}" ;;
esac

reset_queue
mkdir -p "$WS/deliveries/$OTHER"
: > "$WS/deliveries/$OTHER/$TASK_ID.accepted"
OUT="$(SUTANDO_INSTANCE_ID="$PEER" bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "a worker does not answer work a sibling worker holds" ;;
  *) bad "a worker does not answer work a sibling worker holds" "still blocks: ${OUT:0:120}" ;;
esac
rm -rf "$WS/deliveries/$OTHER"

# 12. UNKNOWN IS NOT "FREE". An unreadable deliveries/ tree makes the owner exit
# >1; the hook must neither report the task nor swallow the fault in silence.
reset_queue
if [ "$(id -u)" = 0 ]; then
  echo "  skip running as root: an unreadable directory is still readable"
else
  chmod 000 "$WS/deliveries"
  OUT="$(bash "$HOOK" 2>&1)"
  chmod 755 "$WS/deliveries"
  case "$OUT" in
    *'"decision":"block"'*) bad "an unreadable deliveries/ tree is not a free task" "reported it: ${OUT:0:120}" ;;
    *"could not say whether another instance holds it"*) ok "an unreadable deliveries/ tree is logged, not reported" ;;
    *) bad "an unreadable deliveries/ tree is logged, not reported" "got: ${OUT:0:200}" ;;
  esac
fi

# 13. No deliveries/ tree at all — the hook behaves as it always did.
rm -rf "$WS/deliveries"
reset_queue
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "no deliveries/ tree still blocks" ;;
  *) bad "no deliveries/ tree still blocks" "got: ${OUT:0:120}" ;;
esac

# 14. From #4110: the owner of the grammar answers directly, so the hook's
# exemption and this exit code are one decision rather than two that agree today.
mkdir -p "$WS/deliveries/$PEER"
reset_queue
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -n "$PYBIN" ] && [ -x "$PYBIN" ]; then
  : > "$WS/deliveries/$PEER/$TASK_ID.claimed"
  if "$PYBIN" "$REPO/src/pool_delivery.py" --workspace "$WS" --held "$TASK_ID" >/dev/null 2>&1; then
    ok "pool_delivery --held reports the legacy hold"
  else
    bad "pool_delivery --held reports the legacy hold" "exit was non-zero"
  fi
  rm -f "$WS/deliveries/$PEER/$TASK_ID.claimed"
  if "$PYBIN" "$REPO/src/pool_delivery.py" --workspace "$WS" --held "$TASK_ID" >/dev/null 2>&1; then
    bad "pool_delivery --held reports no hold when there is none" "exit was zero"
  else
    ok "pool_delivery --held reports no hold when there is none"
  fi
else
  printf '  skip pool_delivery --held: the resolver supplied no interpreter\n'
fi

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
