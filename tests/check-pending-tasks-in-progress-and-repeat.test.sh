#!/bin/bash
# The Stop hook (src/check-pending-tasks.sh): a task the core is working RIGHT
# NOW does not block its turn end, and the same queue cannot block a turn end
# forever (user reports 2026-09-24, three of them).
#
#   - IN PROGRESS: the core-mode loop skips a task whose activity snapshot
#     (src/activity_bus.py, state/activity/<id>.json) says RUNNING with fresh
#     activity. Queued, waiting on a person, stale, absent or corrupt still blocks.
#   - REPEAT CAP: Claude Code passes stop_hook_active=true on stdin when the Stop
#     follows a block in this turn. The same unprocessed queue may block a turn
#     end SUTANDO_STOP_HOOK_REPEAT_CAP times (default 3); after that the gate
#     fails open, logged. A changed queue or a new turn starts the count over.
#   - REASON: the block's reason names the tasks and carries their bodies.
#
# Isolation follows check-pending-tasks-workspace.test.sh (SUTANDO_TEST_MODE=1 +
# SUTANDO_WORKSPACE pinned to a temp dir, asserted before any write).
# Run: bash tests/check-pending-tasks-in-progress-and-repeat.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-ip.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
export SUTANDO_CORE_SESSION=1
export SUTANDO_STOP_HOOK_WATCHER_GATE=0

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
cleanup() { rm -rf "$TMPWS"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }
record_delivery() { "$PYBIN" "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true; }
run_hook() {  # $1 = stdin json (may be empty); prints stdout, stderr to $TMPWS/err
  printf '%s' "$1" | bash "$HOOK" 2>"$TMPWS/err"
}
engaged() {  # $1 task id, $2 age seconds ago: a row from the session's activity hook
  mkdir -p "$WS/state"
  printf '{"ts":%s,"line":"reading it","kind":"working","task":{"id":"%s"}}\n' "$(( $(date +%s) - $2 ))" "$1" >> "$WS/state/agent-activity.jsonl"
}
snapshot() {  # $1 task id, $2 phase, $3 age seconds ago (or "none"/"future"/"nan")
  local stamp
  case "$3" in
    none) stamp=null ;;
    future) stamp="$(( $(date +%s) + 3600 ))" ;;
    *) stamp="$(( $(date +%s) - $3 ))" ;;
  esac
  mkdir -p "$WS/state/activity"
  printf '{"task_id":"%s","phase":"%s","started_at":%s,"last_activity_at":%s}\n' "$1" "$2" "$stamp" "$stamp" \
    > "$WS/state/activity/$1.json"
}
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries" "$WS/state"
TASK="task-ip-hooktest-$$"
printf 'id: %s\ntask: keep working\n' "$TASK" > "$WS/tasks/$TASK.txt"
record_delivery

echo "a task the core is working right now does not block, a merely delivered one does:"
snapshot "$TASK" RUNNING 60
OUT="$(run_hook '')"
case "$OUT" in *'"decision":"block"'*) ok "RUNNING but never engaged (delivered only): blocks -- delivery is not work" ;; *) bad "RUNNING but never engaged: blocks" "got: ${OUT:0:160}" ;; esac
printf '{"ts":%s,"line":"reading it","kind":"processing","task":{"id":"%s"}}\n' "$(date +%s)" "$TASK" >> "$WS/state/agent-activity.jsonl"
printf '{"ts":%s,"line":"hmm","kind":"thinking","task":{"id":"%s"}}\n' "$(date +%s)" "$TASK" >> "$WS/state/agent-activity.jsonl"
OUT="$(run_hook '')"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*) ok "RUNNING, read and thought about but never worked on: still blocks" ;; *) bad "RUNNING, read and thought about but never worked on: still blocks" "got: ${OUT:0:160}" ;; esac
engaged "$TASK" 30
OUT="$(run_hook '')"
[ "$OUT" = "{}" ] && ok "RUNNING with a fresh working row from the session: the turn may end" || bad "RUNNING with a fresh working row: the turn may end" "got: ${OUT:0:160}"
rm -f "$WS/state/agent-activity.jsonl"
engaged "$TASK" 7200
snapshot "$TASK" RUNNING 7200
OUT="$(run_hook '')"
case "$OUT" in *'"decision":"block"'*) ok "RUNNING but stale (2h): blocks -- an abandoned run is an orphan again" ;; *) bad "RUNNING but stale (2h): blocks" "got: ${OUT:0:160}" ;; esac
record_delivery  # each {} that reaches the turn-ledger gate spends one recorded no-send
OUT="$(SUTANDO_STOP_HOOK_IN_PROGRESS_MAX_AGE=10000 run_hook '')"
[ "$OUT" = "{}" ] && ok "...within a raised SUTANDO_STOP_HOOK_IN_PROGRESS_MAX_AGE it is in progress" || bad "...within a raised max age it is in progress" "got: ${OUT:0:160}"
rm -f "$WS/state/agent-activity.jsonl"
# The queue's own block, not the turn-ledger gate's: the reason names the queue.
for shape in "QUEUED 60" "WAITING 60" "RECEIVED 60" "COMPLETED 60"; do
  set -- $shape; snapshot "$TASK" "$1" "$2"; engaged "$TASK" 5
  OUT="$(run_hook '')"
  case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*) ok "$1 even with fresh engagement: blocks" ;; *) bad "$1 even with fresh engagement: blocks" "got: ${OUT:0:160}" ;; esac
done
rm -f "$WS/state/agent-activity.jsonl"
for shape in "RUNNING future" "RUNNING none"; do
  set -- $shape; snapshot "$TASK" "$1" "$2"
  OUT="$(run_hook '')"
  case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*) ok "$1 with stamp $2 and no engagement: blocks" ;; *) bad "$1 with stamp $2 and no engagement: blocks" "got: ${OUT:0:160}" ;; esac
done
snapshot "$TASK" RUNNING none; engaged "$TASK" 5; record_delivery
OUT="$(run_hook '')"
[ "$OUT" = "{}" ] && ok "RUNNING with no stamps but a fresh engagement row: the row is the evidence" || bad "RUNNING with no stamps but a fresh engagement row: the row is the evidence" "got: ${OUT:0:160}"
rm -f "$WS/state/agent-activity.jsonl"
printf 'not json' > "$WS/state/activity/$TASK.json"; engaged "$TASK" 5
OUT="$(run_hook '')"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*) ok "a corrupt snapshot blocks even with engagement (cannot prove progress)" ;; *) bad "a corrupt snapshot blocks" "got: ${OUT:0:160}" ;; esac
rm -f "$WS/state/activity/$TASK.json"
OUT="$(run_hook '')"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*) ok "no snapshot at all blocks even with engagement (the control)" ;; *) bad "no snapshot at all blocks" "got: ${OUT:0:160}" ;; esac
rm -f "$WS/state/agent-activity.jsonl"

echo "the reason names the tasks and carries their bodies:"
case "$OUT" in
  *'"reason":"Unprocessed tasks in tasks/: '"$TASK.txt"*) ok "the reason opens with the queue's task names" ;;
  *) bad "the reason opens with the queue's task names" "got: ${OUT:0:200}" ;;
esac
REASON="$(printf '%s' "$OUT" | "$PYBIN" -c 'import json,sys; print(json.load(sys.stdin)["reason"])')"
case "$REASON" in *"keep working"*) ok "...and carries the task body" ;; *) bad "...and carries the task body" "reason: ${REASON:0:200}" ;; esac
case "$REASON" in *"block 1 of 3 this turn end"*) ok "...and counts the block against the cap" ;; *) bad "...and counts the block against the cap" "reason: ${REASON:0:200}" ;; esac

echo "the same queue cannot block one turn end forever:"
COUNTER="$("$PYBIN" "$REPO/src/stop_hook_repeat.py" path --state "$WS/state")"
"$PYBIN" "$REPO/src/stop_hook_repeat.py" clear --state "$WS/state"
OUT="$(run_hook '{"stop_hook_active":false}')"
case "$OUT" in *'block 1 of 3'*) ok "the first Stop of a turn is block 1" ;; *) bad "the first Stop of a turn is block 1" "got: ${OUT:0:200}" ;; esac
OUT="$(run_hook '{"stop_hook_active":true}')"
case "$OUT" in *'block 2 of 3'*) ok "a Stop that follows a block (stop_hook_active) is block 2" ;; *) bad "a Stop that follows a block is block 2" "got: ${OUT:0:200}" ;; esac
OUT="$(run_hook '{"stop_hook_active":true}')"
case "$OUT" in *'block 3 of 3'*) ok "block 3 of 3 still blocks" ;; *) bad "block 3 of 3 still blocks" "got: ${OUT:0:200}" ;; esac
OUT="$(run_hook '{"stop_hook_active":true}')"
[ "$OUT" = "{}" ] && ok "the fourth Stop on the same queue fails open ({})" || bad "the fourth Stop on the same queue fails open ({})" "got: ${OUT:0:160}"
grep -q "blocked this turn end 4 times (cap 3); failing open" "$TMPWS/err" && ok "...and says so on stderr" || bad "...and says so on stderr" "stderr: $(cat "$TMPWS/err")"
[ ! -e "$COUNTER" ] && ok "...and the counter is cleared, so the next turn starts at one" || bad "...and the counter is cleared" "counter: $(cat "$COUNTER" 2>/dev/null)"
OUT="$(run_hook '{"stop_hook_active":true}')"
case "$OUT" in *'block 1 of 3'*) ok "after failing open, the next Stop blocks again from 1" ;; *) bad "after failing open, the next Stop blocks again from 1" "got: ${OUT:0:200}" ;; esac
OUT="$(run_hook '{"stop_hook_active":true}')"
OUT="$(run_hook '{"stop_hook_active":false}')"
case "$OUT" in *'block 1 of 3'*) ok "a new turn (stop_hook_active false) starts the count over" ;; *) bad "a new turn starts the count over" "got: ${OUT:0:200}" ;; esac
OUT="$(run_hook '{"stop_hook_active":true}')"
TASK2="task-ip-hooktest-second-$$"
printf 'id: %s\ntask: another\n' "$TASK2" > "$WS/tasks/$TASK2.txt"
OUT="$(run_hook '{"stop_hook_active":true}')"
case "$OUT" in *'block 1 of 3'*) ok "a changed queue (a new task) starts the count over" ;; *) bad "a changed queue starts the count over" "got: ${OUT:0:200}" ;; esac
case "$OUT" in *"$TASK.txt $TASK2.txt"*) ok "...and the reason names both tasks in queue order" ;; *) bad "...and the reason names both tasks" "got: ${OUT:0:200}" ;; esac
rm -f "$WS/tasks/$TASK2.txt"
OUT="$(SUTANDO_STOP_HOOK_REPEAT_CAP=1 run_hook '{"stop_hook_active":true}')"
OUT="$(SUTANDO_STOP_HOOK_REPEAT_CAP=1 run_hook '{"stop_hook_active":true}')"
[ "$OUT" = "{}" ] && ok "SUTANDO_STOP_HOOK_REPEAT_CAP sets the cap" || bad "SUTANDO_STOP_HOOK_REPEAT_CAP sets the cap" "got: ${OUT:0:160}"
OUT="$(run_hook 'not json at all')"
case "$OUT" in *'"decision":"block"'*) ok "unparseable stdin reads as a first Stop and still blocks" ;; *) bad "unparseable stdin still blocks" "got: ${OUT:0:160}" ;; esac
OUT="$(bash "$HOOK" </dev/null 2>/dev/null)"
case "$OUT" in *'"decision":"block"'*) ok "no stdin at all still blocks (the hook never waits on it)" ;; *) bad "no stdin at all still blocks" "got: ${OUT:0:160}" ;; esac
printf 'done\n' > "$WS/results/$TASK.txt"
record_delivery
OUT="$(run_hook '{"stop_hook_active":true}')"
[ "$OUT" = "{}" ] && [ ! -e "$COUNTER" ] && ok "an answered queue lets the turn end and clears the counter" || bad "an answered queue lets the turn end and clears the counter" "out=${OUT:0:80} counter=$(cat "$COUNTER" 2>/dev/null)"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
