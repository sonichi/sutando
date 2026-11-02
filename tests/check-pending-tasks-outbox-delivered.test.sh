#!/bin/bash
# A reply the gateway outbox delivered must read as answered to the Stop hook,
# unanswered-tasks.py and task_disposition.py alike.
#
# Delivery through the outbox retires results/<id>.txt, and the settle path can
# archive the task with no archive copy of the result left behind. The outbox
# item (results/.outbox[.<instance>]/.items/*.json, status DELIVERED) is then the
# only trace of the reply; a predicate that reads only result files re-blocks
# every turn end on a task that was answered long ago.
#
# The receipts are written by the producer (src/outbox.py:record_delivered).
# Isolation follows check-pending-tasks-workspace.test.sh.
#
# Run: bash tests/check-pending-tasks-outbox-delivered.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-ob.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
export SUTANDO_CORE_SESSION=1
export SUTANDO_STOP_HOOK_WATCHER_GATE=0

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
trap 'rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

record_delivery() {
  "$PYBIN" "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true
}
# receipt <outbox dir name> <item id>: the outbox's own writer marks the item DELIVERED.
receipt() {
  SUTANDO_SRC="$REPO/src" "$PYBIN" -c 'import os, sys
sys.path.insert(0, os.environ["SUTANDO_SRC"])
from pathlib import Path
from outbox import record_delivered
record_delivered(Path(sys.argv[1]), sys.argv[2], provider="ag2space")' "$WS/results/$1" "$2"
}
hook() { record_delivery; bash "$HOOK" 2>/dev/null; }

mkdir -p "$WS/tasks/archive" "$WS/results" "$WS/deliveries"
WORKER=worker-ob
PROBE="task-ob-hooktest-$$"

# 1. CONTROL: a worker's sentinel with no result and no receipt blocks the worker.
mkdir -p "$WS/deliveries/$WORKER"
: > "$WS/deliveries/$WORKER/$PROBE.txt"
printf 'id: %s\ntask: answered via the outbox\n' "$PROBE" > "$WS/tasks/archive/$PROBE.txt"
OUT="$(record_delivery; SUTANDO_INSTANCE_ID="$WORKER" bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  *'"decision":"block"'*) ok "CONTROL: a worker with an unanswered sentinel is blocked" ;;
  *) bad "CONTROL: a worker with an unanswered sentinel is blocked" "got: ${OUT:0:160}" ;;
esac

# 2. THE LOOP: delivered through the outbox, live result retired, task archived,
#    no archive copy. The worker's stale sentinel must not block it again.
receipt .outbox "$PROBE"
OUT="$(record_delivery; SUTANDO_INSTANCE_ID="$WORKER" bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  '{}') ok "a worker whose reply the outbox delivered is not blocked" ;;
  *) bad "a worker whose reply the outbox delivered is not blocked" "got: ${OUT:0:160}" ;;
esac
rm -rf "$WS/deliveries/$WORKER" "$WS/results/.outbox"

# 3. Core mode, a named gateway instance's id: the receipt lives in that
#    instance's outbox under the broker id.
LANE_ID="task-lane1~task-ob$$"
printf 'id: %s\ntask: lane task\n' "$LANE_ID" > "$WS/tasks/$LANE_ID.txt"
OUT="$(hook)"
case "$OUT" in
  *'"decision":"block"'*) ok "CONTROL: core blocks on a lane task with no receipt" ;;
  *) bad "CONTROL: core blocks on a lane task with no receipt" "got: ${OUT:0:160}" ;;
esac
receipt .outbox.lane1 "task-ob$$"
OUT="$(hook)"
case "$OUT" in
  '{}') ok "core is not blocked by a lane task its instance's outbox delivered" ;;
  *) bad "core is not blocked by a lane task its instance's outbox delivered" "got: ${OUT:0:160}" ;;
esac

# 4. unanswered-tasks.py and task_disposition.py agree with the hook.
old="$(( $(date +%s) - 600 ))"
touch -t "$(date -r "$old" +%Y%m%d%H%M.%S)" "$WS/tasks/$LANE_ID.txt"
UA="$("$PYBIN" "$REPO/scripts/unanswered-tasks.py" --workspace "$WS" 2>/dev/null)"; UA_RC=$?
[ "$UA_RC" -eq 0 ] && ok "unanswered-tasks.py reads the delivered task as answered" \
  || bad "unanswered-tasks.py reads the delivered task as answered" "rc $UA_RC: $UA"
DISP="$("$PYBIN" "$REPO/src/task_disposition.py" "$LANE_ID" --workspace "$WS" 2>/dev/null)"
[ "$DISP" = answered ] && ok "task_disposition.py says answered" \
  || bad "task_disposition.py says answered" "got: $DISP"
rm -rf "$WS/results/.outbox.lane1"
UA="$("$PYBIN" "$REPO/scripts/unanswered-tasks.py" --workspace "$WS" 2>/dev/null)"; UA_RC=$?
[ "$UA_RC" -eq 1 ] && ok "CONTROL: unanswered-tasks.py still fires without the receipt" \
  || bad "CONTROL: unanswered-tasks.py still fires without the receipt" "rc $UA_RC: $UA"
rm -f "$WS/tasks/$LANE_ID.txt"

# 5. A receipt that is not DELIVERED, or another id's, settles nothing.
printf 'id: %s\ntask: queued\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
SUTANDO_SRC="$REPO/src" "$PYBIN" -c 'import os, sys
sys.path.insert(0, os.environ["SUTANDO_SRC"])
from pathlib import Path
from outbox import park_item
park_item(Path(sys.argv[1]), sys.argv[2], "test")' "$WS/results/.outbox" "$PROBE"
receipt .outbox "${PROBE}-2"
OUT="$(hook)"
case "$OUT" in
  *'"decision":"block"'*) ok "a PARKED item or a sibling id's receipt does not clear the block" ;;
  *) bad "a PARKED item or a sibling id's receipt does not clear the block" "got: ${OUT:0:160}" ;;
esac
rm -rf "$WS/results/.outbox"

# 6. A wake turn of a delivered id is a new turn: the first run's receipt does not answer it.
printf 'id: %s\nwake_episode: 2\ntask: woken\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
receipt .outbox "$PROBE"
OUT="$(hook)"
case "$OUT" in
  *'"decision":"block"'*) ok "a wake turn is not answered by the earlier receipt" ;;
  *) bad "a wake turn is not answered by the earlier receipt" "got: ${OUT:0:160}" ;;
esac
rm -rf "$WS/results/.outbox" "$WS/tasks/$PROBE.txt"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
