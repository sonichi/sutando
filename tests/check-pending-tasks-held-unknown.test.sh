#!/bin/bash
# "No worker holds it" and "I could not ask" are different answers to the Stop
# hook's exemption question (john-the-dev's note on #4110).
#
# THE DEFECT. The hook ran `pool_delivery.py --held ... >/dev/null 2>&1` and
# read any non-zero exit as NOT HELD. A traceback exits non-zero too, and `2>&1`
# to /dev/null removed the only channel that told them apart — so a broken
# lookup reported a worker's in-flight task as unprocessed and the core was
# told to answer it. pool_delivery now exits 2 for "could not determine" and
# the hook treats that as UNKNOWN: not reported, and said on stderr.
#
# WHAT SEPARATES A FIX FROM THE BUG. A hook that exempted every task would also
# stop reporting this one, so the control is pinned: with the same workspace
# readable, an undelivered task still blocks. And silence is its own failure —
# the stderr line is asserted, not just the absence of a block.
#
# ISOLATION: SUTANDO_TEST_MODE=1 + SUTANDO_WORKSPACE with the resolved path
# ASSERTED before anything is written, as in check-pending-tasks-worker-held.
#
# Run: bash tests/check-pending-tasks-held-unknown.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-heldunknown.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  chmod -R u+rwX "$TMPWS" 2>/dev/null; rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."
  chmod -R u+rwX "$TMPWS" 2>/dev/null; rm -rf "$TMPWS"; exit 1
fi
trap 'chmod -R u+rwX "$TMPWS" 2>/dev/null; rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PROBE="task-zz-unknown-$$"
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries/worker-1"
printf 'id: %s\ntask: probe\n' "$PROBE" > "$WS/tasks/$PROBE.txt"

PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  printf '  skip: the resolver supplied no interpreter\n'; echo PASS; exit 0
fi

# 1. THE CONTROL that proves the probe is visible when the lookup works.
OUT="$(bash "$HOOK" 2>/dev/null)"
case "$OUT" in
  *'"decision":"block"'*) ok "a readable tree still blocks on an undelivered task" ;;
  *) bad "a readable tree still blocks on an undelivered task" "hook said: $OUT" ;;
esac

# 2. THE CASE. An unreadable deliveries tree: the lookup cannot answer, so the
#    task is neither reported to the core nor dropped without a word.
chmod 000 "$WS/deliveries"
if [ -r "$WS/deliveries" ]; then
  printf '  skip unknown case: the mode did not take effect (running as root?)\n'
else
  ERR="$(bash "$HOOK" 2>&1 >/dev/null)"
  OUT="$(bash "$HOOK" 2>/dev/null)"
  if [ "$OUT" = '{}' ]; then ok "an undeterminable hold is not reported to the core"
  else bad "an undeterminable hold is not reported to the core" "hook said: $OUT"; fi
  case "$ERR" in
    *"$PROBE"*could*not*say*) ok "the unknown is named on stderr" ;;
    *) bad "the unknown is named on stderr" "stderr was: $ERR" ;;
  esac
  # The owner of the grammar is what supplies the third answer.
  "$PYBIN" "$REPO/src/pool_delivery.py" --workspace "$WS" --held "$PROBE" >/dev/null 2>&1
  RC=$?
  if [ "$RC" -gt 1 ]; then ok "pool_delivery --held exits $RC, outside the 0/1 pair"
  else bad "pool_delivery --held exits outside the 0/1 pair" "exit was $RC"; fi
fi
chmod 755 "$WS/deliveries"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
