#!/usr/bin/env bash
# The retry driver has to be reachable on the UNBOUND event path (@keweichen's
# [P2] on #4110, now skills/worker-pool/scripts/pool_route_handler.py main()).
#
# THE DEFECT. dispatch_task probes first and only queues a real run on exit 0/4.
# An unbound task classifies 3, so the real run — the only thing that called
# retry_pass — never happened, and a host whose traffic is all core-bound left
# every deferred delivery parked. The probe must stay read-only, so the driver
# cannot live there; the watcher now runs the same pass explicitly on decline.
#
# SHAPE. dispatch_task is lifted out of the production script and executed with
# the real handler, so this is the watcher's own branch logic, not a restatement
# of it. Only the emit/queue/activity collaborators are stubbed, and they RECORD
# which branch ran — a run that queued instead of declining would be caught.
#
# WHAT SEPARATES A FIX FROM THE BUG. The decline case alone is not enough: a
# watcher that ran the pass unconditionally would also pass it. So the bound
# case (probe 0 -> queued, no extra pass) and the read-only probe are pinned
# too, and the marker is asserted CONSUMED rather than merely visited.
#
# ISOLATION: a mktemp workspace; nothing here starts, signals or reads the live
# watcher, and no fswatch is launched.
#
# Run: bash skills/worker-pool/tests/pool-route-retry-driver-dispatch.test.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SRC="$REPO/src"
HANDLER="$HERE/../scripts/pool_route_handler.py"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PYBIN="$(command -v python3 || true)"
if [ -z "$PYBIN" ]; then echo "  skip: no python3"; echo PASS; exit 0; fi

WORKER="$(printf 'a%.0s' $(seq 1 32))"

# One workspace per case: a roster binding !room:x to WORKER, a task deferred
# under a retry marker, and the event that is about to be dispatched.
setup() {   # $1 = channel_id of the waking task
  WS="$(mktemp -d "${TMPDIR:-/tmp}/pool-retry-dispatch.XXXXXX")"
  mkdir -p "$WS/tasks" "$WS/results" "$WS/state/pool-route-retry" "$WS/deliveries"
  printf '{"version":1,"workers":{"%s":{"state":"live"}},"bindings":{"!room:x":"%s"}}\n' \
    "$WORKER" "$WORKER" > "$WS/state/roster.json"
  printf 'id: task-old\nchannel_id: !room:x\ntask: deferred earlier\n' > "$WS/tasks/task-old.txt"
  printf 'refused: earlier\n' > "$WS/state/pool-route-retry/task-old"
  printf 'id: task-new\nchannel_id: %s\ntask: the waking event\n' "$1" > "$WS/tasks/task-new.txt"
}

# dispatch_task, lifted verbatim from the production watcher and run against the
# real handler. The stubs are the collaborators only; the branch logic is real.
run_dispatch() {
  BODY="$(awk '/^dispatch_task\(\) \{/,/^\}/' "$SRC/watch-tasks-stream.sh")"
  SUTANDO_TASK_EVENT_HANDLER="$HANDLER" WORKSPACE_DIR="$WS" RESULTS_DIR="$WS/results" \
  DISPATCH_DIR="$WS/dispatch" FALLBACKS_DIR="$WS/fallbacks" __REPO_ROOT="$REPO" \
  BODY="$BODY" bash -c '
    set -u
    queued_activity_row()     { :; }
    emit_dispatch_task_file() { printf "CORE_EVENT %s\n" "$1"; }
    queue_handler_task()      { printf "QUEUED %s %s\n" "$(basename "$1")" "$2"; return 0; }
    publish_terminal_failure(){ :; }
    eval "$BODY"
    dispatch_task "$1"
  ' _ "$WS/tasks/task-new.txt" 2>/dev/null
}

delivered() { [ -e "$WS/deliveries/$WORKER/task-old.txt" ]; }
marked()    { [ -e "$WS/state/pool-route-retry/task-old" ]; }

# 1. THE CASE. An unbound event: the core takes it, and the deferred delivery
#    still gets made. Fails at the parent commit — only --probe was ever called.
setup "!unbound:x"
OUT="$(run_dispatch)"
case "$OUT" in
  *"CORE_EVENT task-new.txt"*) ok "an unbound event still goes to the core" ;;
  *) bad "an unbound event still goes to the core" "dispatch printed: $OUT" ;;
esac
if delivered; then ok "the deferred task is delivered on the unbound path"
else bad "the deferred task is delivered on the unbound path" "no deliveries/$WORKER/task-old.txt"; fi
if marked; then bad "the retry marker is consumed" "state/pool-route-retry/task-old survived"
else ok "the retry marker is consumed"; fi
rm -rf "$WS"

# 2. CONTROL — the bound path is unchanged: probe 0 queues a real run, which is
#    the driver there, so dispatch must NOT also emit the task to the core.
setup "!room:x"
OUT="$(run_dispatch)"
case "$OUT" in
  *"QUEUED task-new.txt fallback"*) ok "a bound event is queued for the real run" ;;
  *) bad "a bound event is queued for the real run" "dispatch printed: $OUT" ;;
esac
case "$OUT" in
  *CORE_EVENT*) bad "a bound event is not shown to the core" "dispatch printed: $OUT" ;;
  *) ok "a bound event is not shown to the core" ;;
esac
rm -rf "$WS"

# 3. CONTROL — the probe stays read-only. The same unbound classification asked
#    directly delivers nothing, so case 1's delivery came from the new pass.
setup "!unbound:x"
"$PYBIN" "$HANDLER" --task-file "$WS/tasks/task-new.txt" --workspace "$WS" --probe >/dev/null 2>&1
if delivered; then bad "the probe alone delivers nothing" "the probe made a delivery"
else ok "the probe alone delivers nothing"; fi
if marked; then ok "the probe alone consumes no marker"
else bad "the probe alone consumes no marker" "the marker is gone"; fi
rm -rf "$WS"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
