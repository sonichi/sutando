#!/bin/bash
# The Stop hook must watch the WORKSPACE queue, not <repo>/tasks/.
#
# THE DEFECT. src/check-pending-tasks.sh resolved its queue as
# `$(dirname "$0")/../tasks` — the repo root. Every producer (voice, Discord,
# Telegram, Slack, chat) and every consumer writes <workspace>/tasks/. On a
# default install those are different directories, so the hook read an empty
# one, emitted `{}` on every Stop, and never blocked on anything. It is the
# fail-silent shape: the guard reports success in the only way it can, and a
# guard that cannot fire is one that has been switched off.
#
# WHY BOTH DIRECTIONS ARE PINNED. "Blocks when a task exists" is satisfied by a
# hook that blocks on either directory, and "does not block when the queue is
# empty" is satisfied by the broken version. The case that separates them is
# the third: a file in the LEGACY <repo>/tasks/ must NOT block, because reading
# that directory is the bug. Without it, a fix that watched both paths would
# pass and would keep resurrecting pre-migration leftovers.
#
# Run: bash tests/check-pending-tasks-workspace.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
[ -n "$WS" ] || WS="$REPO/workspace"

# A name no real producer emits, so cleanup can never touch a live task.
PROBE="task-zz-hooktest-$$.txt"
FAILED=0

cleanup() {
  rm -f "$WS/tasks/$PROBE" "$WS/results/$PROBE" "$REPO/tasks/$PROBE"
}
trap cleanup EXIT

ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

mkdir -p "$WS/tasks" "$WS/results"

# 1. A task in the workspace queue must block. FAILS against the old path.
printf 'id: probe\ntask: probe\n' > "$WS/tasks/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "workspace task blocks" ;;
  *) bad "workspace task blocks" "got: ${OUT:0:120}" ;;
esac

# 2. ...and the blocked payload must name the task, not just block generically.
case "$OUT" in
  *"$PROBE"*) ok "block payload names the task" ;;
  *) bad "block payload names the task" "payload omits $PROBE" ;;
esac

# 3. A task with a matching result is already handled — must not block.
printf 'done\n' > "$WS/results/$PROBE"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "result present suppresses the block" ;;
  *) bad "result present suppresses the block" "got: ${OUT:0:120}" ;;
esac
rm -f "$WS/tasks/$PROBE" "$WS/results/$PROBE"

# 4. THE DIRECTION CASE. A file in the legacy <repo>/tasks/ must NOT block.
#    Guarded: if the workspace IS the repo (a non-default config), this case
#    cannot distinguish the two and is skipped rather than reported as passing.
if [ "$(cd "$WS" && pwd)" = "$REPO" ]; then
  printf '  skip workspace resolves to the repo root; direction case is not decidable here\n'
else
  mkdir -p "$REPO/tasks"
  printf 'id: probe\ntask: legacy\n' > "$REPO/tasks/$PROBE"
  OUT="$(bash "$HOOK" 2>&1)"
  case "$OUT" in
    '{}') ok "legacy <repo>/tasks/ does not block" ;;
    *) bad "legacy <repo>/tasks/ does not block" "hook still reads the repo: ${OUT:0:120}" ;;
  esac
  rm -f "$REPO/tasks/$PROBE"
fi

# 5. Empty queue stays quiet — the control that proves case 1 measured something.
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "empty queue emits {}" ;;
  *) bad "empty queue emits {}" "got: ${OUT:0:120}" ;;
esac

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
