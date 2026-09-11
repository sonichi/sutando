#!/bin/bash
# One sentinel grammar between the Stop hook and pool_delivery.
#
# THE DEFECT (@keweichen's P1 on #4110). src/pool_delivery.py deliberately still
# recognises the pre-rename `.claimed` acceptance during the pool rollout, but
# the hook spelled its own suffix set — `.txt` and `.accepted` only. A task a
# worker had accepted under the legacy name therefore read as UNPROCESSED, and
# the hook told the core to answer work already in flight: duplicate effects.
#
# The fix is not a third copy of the suffix list. The hook asks the owner of the
# grammar (`pool_delivery.py --held <id>`, exit 0/1), so the two cannot drift.
#
# WHAT SEPARATES A FIX FROM THE BUG. `.claimed` exempting the task is the case
# that fails at the parent commit. It alone is not enough: a hook that exempted
# EVERY task would also pass it, and so would one that exempted any directory
# under deliveries/. So both controls are pinned too — an undelivered task still
# blocks, and the CORE's own inbox is never a hold (the core declining is what
# put the task in front of this hook).
#
# ISOLATION: same pinned temp workspace as check-pending-tasks-workspace.test.sh
# — SUTANDO_TEST_MODE=1 + SUTANDO_WORKSPACE, with the resolved path ASSERTED
# before anything is written, so a regressed escape hatch aborts the run instead
# of writing into the live queue a watcher is consuming.
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

WORKER="worker-1"
PROBE="task-zz-held-$$"
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries/$WORKER" "$WS/deliveries/core"

# `decision` for one sentinel layout: allow (exempt) or block (the core's).
verdict() {   # $1 = recipient dir, $2 = sentinel suffix ("" for none)
  rm -f "$WS/deliveries/$WORKER/$PROBE".* "$WS/deliveries/core/$PROBE".*
  printf 'id: %s\ntask: probe\n' "$PROBE" > "$WS/tasks/$PROBE.txt"
  [ -n "$2" ] && : > "$WS/deliveries/$1/$PROBE$2"
  case "$(bash "$HOOK" 2>/dev/null)" in
    '{}') echo allow ;;
    *'"decision":"block"'*) echo block ;;
    *) echo other ;;
  esac
}

expect() {   # $1 = label, $2 = wanted, $3.. = verdict args
  local got; got="$(verdict "$3" "${4-}")"
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "wanted $2, got $got"; fi
}

# 1. THE CONTROL that proves the probe is visible at all.
expect "an undelivered task blocks"               block "$WORKER" ""

# 2. The two names the hook already knew.
expect "a worker's pending .txt exempts"          allow "$WORKER" ".txt"
expect "a worker's .accepted exempts"             allow "$WORKER" ".accepted"

# 3. THE CASE. Fails at the parent commit: the hook's private suffix list had
#    no `.claimed`, so legacy-accepted work was handed back to the core.
expect "a worker's legacy .claimed exempts"       allow "$WORKER" ".claimed"

# 4. The core's own inbox is not a hold, under every name — otherwise the hook
#    would exempt exactly the tasks it exists to report.
expect "the core's own .txt still blocks"         block core ".txt"
expect "the core's own .accepted still blocks"    block core ".accepted"
expect "the core's own .claimed still blocks"     block core ".claimed"

# 5. The owner of the grammar answers directly — the hook's exemption and this
#    exit code are the same decision, not two that agree today.
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -n "$PYBIN" ] && [ -x "$PYBIN" ]; then
  : > "$WS/deliveries/$WORKER/$PROBE.claimed"
  if "$PYBIN" "$REPO/src/pool_delivery.py" --workspace "$WS" --held "$PROBE" >/dev/null 2>&1; then
    ok "pool_delivery --held reports the legacy hold"
  else
    bad "pool_delivery --held reports the legacy hold" "exit was non-zero"
  fi
  rm -f "$WS/deliveries/$WORKER/$PROBE.claimed"
  if "$PYBIN" "$REPO/src/pool_delivery.py" --workspace "$WS" --held "$PROBE" >/dev/null 2>&1; then
    bad "pool_delivery --held reports no hold when there is none" "exit was zero"
  else
    ok "pool_delivery --held reports no hold when there is none"
  fi
else
  printf '  skip pool_delivery --held: the resolver supplied no interpreter\n'
fi

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
