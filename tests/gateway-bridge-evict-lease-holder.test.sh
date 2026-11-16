#!/bin/bash
# #4360: a straggler bridge whose identity ps cannot show (INDETERMINATE) was never
# evicted, even when it held this bridge's lease, so the wrapper's own child deferred
# to it (rc 75) and the job ended unsupervised. evict_own_bridge now kills an
# indeterminate candidate only when it is the pid the caller proved from the lease
# (EVICT_PROVEN_PID), waits for it to exit (EVICT_WAIT_S), and `connections.py
# bridge-pid` prints that proven pid. Run: bash <this file> (exit 0/1)
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="$REPO_ROOT/src/launchd/evict-own-bridge.sh"
FAILED=0
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "SKIP: python3 not found"; exit 0; }

# shellcheck source=/dev/null
. "$HELPER"
# Identity is indeterminate wherever the instance env cannot be read (as on macOS for
# some interpreters); force it so the case runs on every platform.
_pid_env() { return 1; }

TMP="$(mktemp -d 2>/dev/null || mktemp -d -t evictlease)"
cleanup() { pkill -P $$ 2>/dev/null || true; rm -rf "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

assert_dead() { if kill -0 "$2" 2>/dev/null; then echo "  FAIL $1 (still alive)"; FAILED=1; else echo "  ok   $1"; fi; }
assert_alive() { if kill -0 "$2" 2>/dev/null; then echo "  ok   $1"; else echo "  FAIL $1 (was killed)"; FAILED=1; fi; }

# A channel name no real bridge uses, so pgrep can only find this test's stubs.
CH="evictlease"
mkdir -p "$TMP/repo/src"
A="$(cd "$TMP/repo" && pwd -P)"
printf 'import time\ntime.sleep(120)\n' > "$A/src/$CH-bridge.py"
# Exits a moment after SIGTERM, like a bridge finishing its outbound send.
mkdir -p "$TMP/slow/src"
printf 'import signal,sys,time\nsignal.signal(signal.SIGTERM, lambda *a: (time.sleep(1.5), sys.exit(0)))\ntime.sleep(120)\n' \
  > "$TMP/slow/src/$CH-bridge.py"

echo "### indeterminate candidates"
( exec "$PY" "$A/src/$CH-bridge.py" ) & HOLDER=$!
( exec "$PY" "$A/src/$CH-bridge.py" ) & OTHER=$!
sleep 0.6

EVICT_PROVEN_PID="" evict_own_bridge "$CH" "$A" GATEWAY_INSTANCE "" 2>/dev/null
sleep 0.3
assert_alive "no proven pid: an indeterminate candidate is never killed" "$HOLDER"

EVICT_PROVEN_PID="$HOLDER" evict_own_bridge "$CH" "$A" GATEWAY_INSTANCE "" 2>/dev/null
sleep 0.3
assert_dead  "the lease holder is evicted though ps cannot identify it" "$HOLDER"
assert_alive "an indeterminate pid that holds no lease survives" "$OTHER"
kill "$OTHER" 2>/dev/null || true

echo "### the eviction waits for the holder to exit"
( exec "$PY" "$TMP/slow/src/$CH-bridge.py" ) & SLOW=$!
sleep 0.6
EVICT_PROVEN_PID="$SLOW" EVICT_WAIT_S=10 evict_own_bridge "$CH" "$TMP/slow" GATEWAY_INSTANCE "" 2>/dev/null
assert_dead "evict_own_bridge returns only after the evicted pid exited" "$SLOW"

echo "### connections.py bridge-pid"
WS="$TMP/ws"; mkdir -p "$WS/state"
out="$("$PY" "$REPO_ROOT/scripts/connections.py" --workspace "$WS" bridge-pid)"; rc=$?
if [ "$rc" = 0 ] && [ -z "$out" ]; then echo "  ok   no lease: prints nothing, rc 0"
else echo "  FAIL no lease: rc=$rc out=[$out]"; FAILED=1; fi

"$PY" - "$REPO_ROOT/src" "$WS" <<'EOF' &
import sys, time
sys.path.insert(0, sys.argv[1])
import workspace_lock
r = workspace_lock.acquire("gateway-bridge", workspace_lock.Path(sys.argv[2]))
assert r.status != "deferred", r
time.sleep(60)
EOF
LEASED=$!
for _ in $(seq 1 50); do [ -s "$WS/state/locks/gateway-bridge.lock" ] && break; sleep 0.1; done
sleep 0.2
out="$("$PY" "$REPO_ROOT/scripts/connections.py" --workspace "$WS" bridge-pid "")"; rc=$?
if [ "$rc" = 0 ] && [ "$out" = "$LEASED" ]; then echo "  ok   prints the proven lease holder ($out)"
else echo "  FAIL lease holder: rc=$rc out=[$out] want $LEASED"; FAILED=1; fi
out="$("$PY" "$REPO_ROOT/scripts/connections.py" --workspace "$WS" bridge-pid other)"; rc=$?
if [ "$rc" = 0 ] && [ -z "$out" ]; then echo "  ok   another instance's role proves nothing here"
else echo "  FAIL other instance: rc=$rc out=[$out]"; FAILED=1; fi
kill "$LEASED" 2>/dev/null || true

[ "$FAILED" = 0 ] && { echo PASS; exit 0; }
echo FAIL; exit 1
