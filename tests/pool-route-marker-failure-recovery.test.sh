#!/usr/bin/env bash
# A failed retry-marker write must not settle unrecoverable work
# (@keweichen's REQUEST_CHANGES blocker 1 on #4110, src/pool_route_handler.py:163-178).
#
# THE DEFECT. `_defer` wrote ONE marker store. With that path obstructed the run
# still exited 0, so the watcher released its claim, `retry_pass` — which
# enumerates markers — never saw the task, and the Stop hook then reported a
# worker-addressed payload to the core (handler rc=0; marker=False; delivery=False
# -> stop-hook decision=block). The handler now writes to a SECOND store that the
# same pass enumerates, and exits UNSETTLED when no store takes it at all.
#
# SHAPE. The production handler, the production hook and `finish_handler_task`
# lifted verbatim out of the production watcher; only the emit/claim collaborators
# are stubbed, and they RECORD which branch ran.
#
# WHAT SEPARATES A FIX FROM THE BUG. A hook that exempted everything would also
# stop reporting the parked task, so an unparked task is asserted REPORTED in the
# same workspace; and a watcher that never fell back would pass case 4 for the
# wrong reason, so an ordinary handler failure is asserted to still fall back.
#
# ISOLATION: a mktemp workspace, asserted to be neither the live one nor the repo
# before anything is written. Nothing here starts, signals or reads a live watcher.
#
# Run: bash tests/pool-route-marker-failure-recovery.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HANDLER="$REPO/src/pool_route_handler.py"
HOOK="$REPO/src/check-pending-tasks.sh"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-markerfail.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ] \
   || { [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; }; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  rm -rf "$TMPWS"; exit 1
fi
trap 'rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  printf '  skip: the resolver supplied no interpreter\n'; echo PASS; exit 0
fi
W="$(printf 'a%.0s' $(seq 1 32))"
V="$(printf 'f%.0s' $(seq 1 32))"

mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$WS/deliveries"
# Two targets for one source: the router refuses fan-out, so the run defers.
printf '{"version":1,"workers":{"%s":{"state":"live"},"%s":{"state":"live"}},"bindings":{"!room:x":["%s","%s"]}}\n' \
  "$W" "$V" "$W" "$V" > "$WS/state/roster.json"
printf 'id: task-park\nchannel_id: !room:x\ntask: a worker-addressed payload\n' > "$WS/tasks/task-park.txt"
# The obstruction kewei used: a FILE where the marker directory belongs.
printf 'not a directory\n' > "$WS/state/pool-route-retry"

"$PYBIN" "$HANDLER" --task-file "$WS/tasks/task-park.txt" --workspace "$WS" 2>/dev/null
RC=$?
[ "$RC" = 0 ] && ok "an obstructed store still exits 0 (the task stays the worker's)" \
              || bad "an obstructed store still exits 0" "exit was $RC"
if [ -f "$WS/tasks/.pool-route-retry/task-park" ]; then
  ok "the marker lands in the second store"
else
  bad "the marker lands in the second store" "no tasks/.pool-route-retry/task-park"
fi
"$PYBIN" "$HANDLER" --workspace "$WS" --parked task-park >/dev/null 2>&1
[ $? = 0 ] && ok "the handler answers that the task is parked" \
           || bad "the handler answers that the task is parked" "--parked did not exit 0"

# 2. THE STOP HOOK must not tell the core to process a parked task...
OUT="$(bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  *task-park*) bad "a parked task is not reported to the core" "hook said: $OUT" ;;
  *) ok "a parked task is not reported to the core" ;;
esac
# ...and the CONTROL: an ordinary undelivered task in the same tree still is.
printf 'id: task-plain\ntask: nobody owns this\n' > "$WS/tasks/task-plain.txt"
OUT="$(bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  *task-plain*) ok "an unparked task is still reported (the hook did not go blind)" ;;
  *) bad "an unparked task is still reported" "hook said: $OUT" ;;
esac
rm -f "$WS/tasks/task-plain.txt"

# 3. Storage repaired -> the next pass delivers it, with no restart.
rm -f "$WS/state/pool-route-retry"
printf '{"version":1,"workers":{"%s":{"state":"live"}},"bindings":{"!room:x":"%s"}}\n' \
  "$W" "$W" > "$WS/state/roster.json"
"$PYBIN" "$HANDLER" --workspace "$WS" --retry-pass >/dev/null 2>&1
if [ -e "$WS/deliveries/$W/task-park.txt" ]; then
  ok "the repaired task is delivered by the automatic retry pass"
else
  bad "the repaired task is delivered by the automatic retry pass" "no deliveries/$W/task-park.txt"
fi
"$PYBIN" "$HANDLER" --workspace "$WS" --parked task-park >/dev/null 2>&1
[ $? = 1 ] && ok "the marker is consumed once delivered" \
           || bad "the marker is consumed once delivered" "--parked still exits 0"

# 4. NO store at all: the run must not report settled, and the watcher must keep
#    its claim instead of publishing a worker's task to the core.
printf 'id: task-nowhere\nchannel_id: !room:x\ntask: nowhere to park it\n' > "$WS/tasks/task-nowhere.txt"
printf '{"version":1,"workers":{"%s":{"state":"live"},"%s":{"state":"live"}},"bindings":{"!room:x":["%s","%s"]}}\n' \
  "$W" "$V" "$W" "$V" > "$WS/state/roster.json"
rm -rf "$WS/state/pool-route-retry" "$WS/tasks/.pool-route-retry"
printf 'not a directory\n' > "$WS/state/pool-route-retry"
printf 'not a directory\n' > "$WS/tasks/.pool-route-retry"
ERR="$("$PYBIN" "$HANDLER" --task-file "$WS/tasks/task-nowhere.txt" --workspace "$WS" 2>&1 >/dev/null)"
RC=$?
UNSETTLED="$("$PYBIN" -c 'import sys; sys.path.insert(0, sys.argv[1]); import pool_route_handler as h; print(h.UNSETTLED)' "$REPO/src")"
[ "$RC" = "$UNSETTLED" ] && ok "no store anywhere exits UNSETTLED ($UNSETTLED), not 0" \
                         || bad "no store anywhere exits UNSETTLED" "exit was $RC, expected $UNSETTLED"
case "$ERR" in
  *"DEFERRED WITHOUT RETRY MARKER"*) ok "the loss is named on stderr" ;;
  *) bad "the loss is named on stderr" "stderr was: $ERR" ;;
esac

# finish_handler_task, lifted with any watcher-local function it transitively
# calls (e.g. settle_handler_outcome), so a refactor can't leave the lift stale.
STUB_COLLABORATORS=" claim_is_ours claim_disposition release_task_claim emit_fallback_task_file publish_terminal_failure drain_dispatch_queue "
ERRFILE="$WS/dispatch/finish.stderr"
run_finish() {   # $1 = rc the handler returned; stderr lands in $ERRFILE
  local all_fns queue seen name body
  all_fns="$(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(\) \{' "$REPO/src/watch-tasks-stream.sh" | sed 's/() {//')"
  queue="finish_handler_task"; seen=""; BODY=""
  while [ -n "$queue" ]; do
    name="${queue%% *}"
    case "$queue" in *" "*) queue="${queue#* }" ;; *) queue="" ;; esac
    case " $seen " in *" $name "*) continue ;; esac
    seen="$seen $name"
    body="$(awk -v name="$name" '$0 == name "() {" {p=1} p {print} p && /^\}/ {p=0}' "$REPO/src/watch-tasks-stream.sh")"
    [ -n "$body" ] || continue
    BODY="$BODY
$body"
    for cand in $all_fns; do
      case "$STUB_COLLABORATORS" in *" $cand "*) continue ;; esac
      case " $seen $queue " in *" $cand "*) continue ;; esac
      printf '%s\n' "$body" | grep -v '^[[:space:]]*#' | grep -qw "$cand" && queue="$queue $cand"
    done
  done
  mkdir -p "$WS/dispatch/settled" "$WS/dispatch/workers" "$WS/fallbacks"
  printf '%s\n' "$WS/tasks/task-nowhere.txt" > "$WS/dispatch/running-marker"
  HANDLER_UNSETTLED_RC="$UNSETTLED" DISPATCH_DIR="$WS/dispatch" FALLBACKS_DIR="$WS/fallbacks" \
  BODY="$BODY" bash -c '
    set -u
    claim_is_ours()          { return 0; }
    claim_disposition()      { return 1; }   # the fallback disposition
    release_task_claim()     { printf "RELEASED %s\n" "$1"; }
    emit_fallback_task_file(){ printf "CORE_EVENT %s\n" "$1"; }
    publish_terminal_failure(){ :; }
    drain_dispatch_queue()   { :; }
    eval "$BODY"
    finish_handler_task "$1" "$2" "$3"
  ' _ "$WS/dispatch/running-marker" "$WS/tasks/task-nowhere.txt" "$1" 2>"$ERRFILE"
}
# A dropped callee fails as "command not found" on stderr — capture it (a
# command substitution subshell can't leak a var back, so read $ERRFILE after).
OUT="$(run_finish "$UNSETTLED")"
FINISH_ERR="$(cat "$ERRFILE" 2>/dev/null)"
MSG="an unsettled run keeps the claim and tells no core"
if printf '%s' "$FINISH_ERR" | grep -qF "command not found"; then
  bad "$MSG" "lifted watcher body hit an undefined command: $FINISH_ERR"
elif ! printf '%s' "$FINISH_ERR" | grep -qF "keeping the claim"; then
  bad "$MSG" "no positive marker from the UNSETTLED branch; stderr was: $FINISH_ERR"
elif printf '%s' "$OUT" | grep -qE 'CORE_EVENT|RELEASED'; then
  bad "$MSG" "watcher printed: $OUT"
else
  ok "$MSG"
fi
# CONTROL — an ordinary handler failure is unchanged: fall back, release.
OUT="$(run_finish 1)"
FINISH_ERR="$(cat "$ERRFILE" 2>/dev/null)"
MSG="an ordinary handler failure still falls back to the core"
if printf '%s' "$FINISH_ERR" | grep -qF "command not found"; then
  bad "$MSG" "lifted watcher body hit an undefined command: $FINISH_ERR"
elif printf '%s' "$OUT" | grep -qF "CORE_EVENT"; then
  ok "$MSG"
else
  bad "$MSG" "watcher printed: $OUT"
fi

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
