#!/bin/bash
# The Stop hook must understand worker-pool delegation (sonichi/sutando#4281,
# #4338), not just the core's tasks/ vs results/ shape that
# check-pending-tasks-workspace.test.sh already pins.
#
# THE DEFECTS.
#   #4338 (core-side): pool_route_handler.py delegates a task by writing a
#   SENTINEL into deliveries/<worker>/<task-id>.txt — the payload stays in
#   tasks/ by design (the worker reads it from there via the inbox resolver).
#   The core's watcher correctly never reprocesses a routed task, but the old
#   hook only asked "does tasks/<id>.txt have a matching results/<id>.txt?" —
#   which is false for a LONG time while the worker is still working, and the
#   hook blocked the core's Stop on work that was never the core's to finish.
#
#   #4281 (worker-side): a worker session (SUTANDO_INSTANCE_ID set) inherits
#   the same hook, which read the CORE's tasks/ directory — so a worker got
#   blocked by the core's OWN unrelated pending tasks, which are not the
#   worker's queue at all.
#
# Isolation follows check-pending-tasks-workspace.test.sh's pattern exactly
# (SUTANDO_TEST_MODE=1 + SUTANDO_WORKSPACE pinned to a temp dir, asserted
# before any write) — see that file for why the assertion is not optional.
#
# Run: bash tests/check-pending-tasks-worker-pool.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-wp.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  rm -rf "$TMPWS"
  exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  rm -rf "$TMPWS"
  exit 1
fi

record_delivery() {
  "$PYBIN" "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true
}

cleanup() { rm -rf "$TMPWS"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries"

WORKER=worker-abc
PROBE="task-wp-hooktest-$$"

# 1. A task with a live sentinel in a worker's own folder must NOT block the
#    core (#4338) — it was delegated, not orphaned.
mkdir -p "$WS/deliveries/$WORKER"
printf 'id: %s\ntask: routed\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
: > "$WS/deliveries/$WORKER/$PROBE.txt"
record_delivery
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "core does not block on a task delegated to a worker" ;;
  *) bad "core does not block on a task delegated to a worker" "got: ${OUT:0:160}" ;;
esac

# 2. ...same, but the worker has ACCEPTED it (.accepted suffix) — still not
#    the core's to report.
rm -f "$WS/deliveries/$WORKER/$PROBE.txt"
: > "$WS/deliveries/$WORKER/$PROBE.accepted"
record_delivery
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "core does not block on an accepted delegation either" ;;
  *) bad "core does not block on an accepted delegation either" "got: ${OUT:0:160}" ;;
esac
rm -f "$WS/deliveries/$WORKER/$PROBE.accepted" "$WS/tasks/$PROBE.txt"

# 3. THE CONTROL. A task with NO sentinel anywhere must still block the core —
#    proves case 1/2 measured delegation, not "the core never blocks".
printf 'id: %s\ntask: orphan\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
OUT="$(bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "core still blocks on a genuinely unclaimed task" ;;
  *) bad "core still blocks on a genuinely unclaimed task" "got: ${OUT:0:160}" ;;
esac
rm -f "$WS/tasks/$PROBE.txt"
record_delivery

# 4. Worker mode (#4281): the CORE's own unrelated pending task must not
#    block a worker session — it is not the worker's queue.
printf 'id: %s\ntask: core-only\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
OUT="$(SUTANDO_INSTANCE_ID="$WORKER" bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "worker is not blocked by the core's unrelated tasks/ queue" ;;
  *) bad "worker is not blocked by the core's unrelated tasks/ queue" "got: ${OUT:0:160}" ;;
esac

# 5. ...but a sentinel actually delivered to THIS worker's own folder DOES
#    block that worker (the delegation is real work it still owes a reply on).
mkdir -p "$WS/deliveries/$WORKER"
: > "$WS/deliveries/$WORKER/$PROBE.txt"
OUT="$(SUTANDO_INSTANCE_ID="$WORKER" bash "$HOOK" 2>&1)"
case "$OUT" in
  *'"decision":"block"'*) ok "worker blocks on its own unresolved sentinel" ;;
  *) bad "worker blocks on its own unresolved sentinel" "got: ${OUT:0:160}" ;;
esac
case "$OUT" in
  *"$PROBE"*) ok "worker's block payload names the task, read from the shared payload" ;;
  *) bad "worker's block payload names the task, read from the shared payload" "payload omits $PROBE" ;;
esac
case "$OUT" in
  *'deliveries/'"$WORKER"*) ok "worker's block reason names its own deliveries folder, not tasks/" ;;
  *) bad "worker's block reason names its own deliveries folder, not tasks/" "got: ${OUT:0:160}" ;;
esac

# 6. A ready result for that same task clears the worker's own block.
printf 'done\n' > "$WS/results/$PROBE.txt"
OUT="$(SUTANDO_INSTANCE_ID="$WORKER" bash "$HOOK" 2>&1)"
case "$OUT" in
  '{}') ok "worker's block clears once its result is ready" ;;
  *) bad "worker's block clears once its result is ready" "got: ${OUT:0:160}" ;;
esac
rm -f "$WS/deliveries/$WORKER/$PROBE.txt" "$WS/results/$PROBE.txt" "$WS/tasks/$PROBE.txt"
record_delivery

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
