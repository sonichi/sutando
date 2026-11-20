#!/bin/bash
# evict_own_bridge kills an indeterminate straggler only when it is the lease holder the
# caller proved (EVICT_PROVEN_PID). Run: bash <this file> (exit 0/1)
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

echo "### a killed pid still alive at the deadline"
# Ignores SIGTERM, so it outlives any wait; a successor launched beside it would stand down.
mkdir -p "$TMP/stuck/src"
printf 'import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n' \
  > "$TMP/stuck/src/$CH-bridge.py"
( exec "$PY" "$TMP/stuck/src/$CH-bridge.py" ) & STUCK=$!
sleep 0.6
EVICT_PROVEN_PID="$STUCK" EVICT_WAIT_S=1 evict_own_bridge "$CH" "$TMP/stuck" GATEWAY_INSTANCE "" 2>/dev/null; rc=$?
if [ "$rc" = 4 ]; then echo "  ok   deadline exceeded: rc 4 (still running)"
else echo "  FAIL deadline exceeded: rc=$rc want 4"; FAILED=1; fi
assert_alive "no SIGKILL escalation: the straggler is left running" "$STUCK"
EVICT_PROVEN_PID="$STUCK" evict_own_bridge "$CH" "$TMP/stuck" GATEWAY_INSTANCE "" 2>/dev/null; rc=$?
if [ "$rc" = 0 ]; then echo "  ok   no EVICT_WAIT_S: rc 0 as before"
else echo "  FAIL no EVICT_WAIT_S: rc=$rc want 0"; FAILED=1; fi
kill -KILL "$STUCK" 2>/dev/null; wait "$STUCK" 2>/dev/null

echo "### the wrapper does not launch beside a straggler it could not evict"
# The straggler and the wrapper's child share the bridge script; STRAGGLER picks the role.
W="$TMP/wrap"; mkdir -p "$W/src/launchd" "$W/scripts" "$W/ws" "$W/shims"
W="$(cd "$W" && pwd -P)"
cp "$REPO_ROOT/src/launchd/gateway-bridge-wrapper.sh" "$REPO_ROOT/src/launchd/gateway-bridge-lib.sh" "$REPO_ROOT/src/launchd/evict-own-bridge.sh" "$W/src/launchd/"
printf 'import os,signal,sys,time\nif os.environ.get("STRAGGLER"):\n    signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)\nopen("%s/launched","w").close(); sys.exit(75)\n' \
  "$W" > "$W/src/remote-gateway-bridge.py"
printf 'import time\nwhile True: time.sleep(0.2)\n' > "$W/src/sparrowd.py"
printf '#!/bin/bash\ncase "${1:-}" in workspace) echo "%s/ws" ;; *) echo "%s/none.env" ;; esac\n' "$W" "$W" \
  > "$W/scripts/sutando-config.sh"
printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$W/shims/python3"
chmod +x "$W/scripts/sutando-config.sh" "$W/shims/python3"
( cd "$W" && exec env STRAGGLER=1 "$PY" "$W/src/remote-gateway-bridge.py" ) & STRAY=$!
sleep 0.6
# connections.py: bridge-pid proves the straggler; every other verb reports "no".
printf 'import sys\nif "bridge-pid" in sys.argv: print(%s); sys.exit(0)\nsys.exit(1)\n' "$STRAY" > "$W/scripts/connections.py"
( cd "$W" && exec env -u AG2_REMOTE_TOKEN -u GATEWAY_INSTANCE PATH="$W/shims:$PATH" REMOTE_TASK_TOKEN=t \
    SUTANDO_GATEWAY_EVICT_WAIT_S=1 SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
    bash src/launchd/gateway-bridge-wrapper.sh > "$W/o.log" 2>&1 ) & WRAP=$!
for _ in $(seq 1 300); do kill -0 "$WRAP" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$WRAP" 2>/dev/null; then kill -TERM "$WRAP" 2>/dev/null; wrc="still running"; fi
wait "$WRAP" 2>/dev/null; wrc="${wrc:-$?}"
if [ "$wrc" != "still running" ] && [ "$wrc" != 0 ]; then echo "  ok   wrapper exits non-zero (rc $wrc) so launchd retries"
else echo "  FAIL wrapper exit: rc=$wrc want non-zero"; FAILED=1; fi
if [ -e "$W/launched" ]; then echo "  FAIL wrapper launched its child beside the straggler"; FAILED=1
else echo "  ok   no child launched beside the straggler"; fi
assert_alive "wrapper did not SIGKILL the straggler" "$STRAY"
kill -KILL "$STRAY" 2>/dev/null; wait "$STRAY" 2>/dev/null

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
