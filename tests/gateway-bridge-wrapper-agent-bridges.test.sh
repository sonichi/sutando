#!/usr/bin/env bash
# gateway-bridge-wrapper.sh keeps ONE agent-bridge supervisor (src/sparrowd.py
# --agent-bridges) alive beside the gateway bridge: the declarations under
# <workspace>/agents have no other supervisor on a launchd-wrapper install.
#
#   steady supervisor  -> started exactly once while the bridge runs; TERM to the
#                         wrapper reaches it; nothing of it survives the wrapper
#   exiting supervisor -> relaunched after the restart delay, never two at once
#   bridge rc 75       -> the supervisor is stopped with the wrapper's stand-down
#
# The stub records every start, every TERM and any overlap (a second instance
# finding the first alive). Run: bash tests/gateway-bridge-wrapper-agent-bridges.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway wrapper agent-bridge test")" || exit 1

# $1 = what the stub supervisor does: "steady" (runs until TERM) or "exit" (rc 3 at once)
# $2 = exit code of the stub bridge: 0 (relaunch path), 75 (stand-down), "steady" (runs until TERM)
# Prints: <starts> <terms> <overlaps> <wrapper-alive-at-end> <stub-alive-at-end> <argv-ok>
run_case() {
  local sup="$1" bridge="$2" d
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$d/none.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  if [ "$bridge" = steady ]; then
    echo "import time
while True: time.sleep(0.2)" > "$d/src/remote-gateway-bridge.py"
  else
    echo "import sys; sys.exit($bridge)" > "$d/src/remote-gateway-bridge.py"
  fi
  cat > "$d/src/sparrowd.py" <<PYS
import os, signal, sys, time
log = open("$d/sup.log", "a")
pidfile = "$d/sup.pid"
try:
    other = int(open(pidfile).read())
    os.kill(other, 0)
    log.write("OVERLAP\n"); log.flush()
except (OSError, ValueError):
    pass
open(pidfile, "w").write(str(os.getpid()))
log.write("START " + " ".join(sys.argv[1:]) + "\n"); log.flush()
if "$sup" == "exit":
    sys.exit(3)
def term(*_):
    log.write("TERM\n"); log.flush(); sys.exit(0)
signal.signal(signal.SIGTERM, term)
while True:
    time.sleep(0.2)
PYS
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"; cp "$REPO/src/result_publish.py" "$d/src/"
  printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  chmod +x "$d/shims/python3" "$d/shims/osascript"
  ( cd "$d"; env -u AG2_REMOTE_TOKEN PATH="$d/shims:$PATH" REMOTE_TASK_TOKEN=t \
      SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 & p=$!
    sleep 5
    if kill -0 $p 2>/dev/null; then alive=1; else alive=0; fi
    kill -TERM $p 2>/dev/null; wait $p 2>/dev/null
    sleep 0.5
    echo "$alive" > alive.txt )
  local starts terms overlaps alive stub_alive argv_ok
  # grep -c prints 0 on no match and exits 1; only a missing log prints nothing
  starts=$(grep -c '^START' "$d/sup.log" 2>/dev/null || true); starts=${starts:-0}
  terms=$(grep -c '^TERM' "$d/sup.log" 2>/dev/null || true); terms=${terms:-0}
  overlaps=$(grep -c '^OVERLAP' "$d/sup.log" 2>/dev/null || true); overlaps=${overlaps:-0}
  alive=$(cat "$d/alive.txt")
  if [ -f "$d/sup.pid" ] && kill -0 "$(cat "$d/sup.pid")" 2>/dev/null; then stub_alive=1; else stub_alive=0; fi
  if grep -q '^START --agent-bridges$' "$d/sup.log" 2>/dev/null; then argv_ok=1; else argv_ok=0; fi
  echo "$starts $terms $overlaps $alive $stub_alive $argv_ok"
  rm -rf "$d"
}

read -r s1 t1 o1 a1 sa1 ok1 <<<"$(run_case steady steady)"
read -r s2 _ o2 _ sa2 _ <<<"$(run_case exit steady)"
read -r s3 t3 _ a3 sa3 _ <<<"$(run_case steady 75)"
read -r s4 _ o4 _ _ _ <<<"$(run_case steady 0)"

echo "steady supervisor, steady bridge -> starts=$s1 terms=$t1 overlaps=$o1 wrapper-alive=$a1 stub-alive=$sa1 argv-ok=$ok1 (want 1 1 0 1 0 1)"
echo "exiting supervisor, steady bridge -> starts=$s2 overlaps=$o2 stub-alive=$sa2 (want >1 starts, 0 overlaps, 0 alive)"
echo "steady supervisor, bridge rc 75  -> starts=$s3 terms=$t3 wrapper-alive=$a3 stub-alive=$sa3 (want 1 1 0 0: stand-down takes it down)"
echo "steady supervisor, bridge exit 0 -> starts=$s4 overlaps=$o4 (want 1 start across the bridge's relaunches, 0 overlaps)"

[ "$s1" -eq 1 ] || { echo "FAIL: the agent-bridge supervisor was started $s1 times while the bridge ran (want exactly 1)"; fail=1; }
[ "$ok1" -eq 1 ] || { echo "FAIL: the supervisor was not started as 'sparrowd.py --agent-bridges'"; fail=1; }
[ "$t1" -eq 1 ] || { echo "FAIL: TERM to the wrapper did not reach the supervisor"; fail=1; }
[ "$a1" -eq 1 ] || { echo "FAIL: the wrapper did not stay up with a steady bridge"; fail=1; }
[ "$sa1" -eq 0 ] || { echo "FAIL: the supervisor outlived the wrapper"; fail=1; }
[ "$s2" -gt 1 ] || { echo "FAIL: an exited supervisor was not relaunched"; fail=1; }
[ "$o1$o2$o4" = "000" ] || { echo "FAIL: two supervisors ran at once (a declaration would run twice)"; fail=1; }
[ "$sa2" -eq 0 ] || { echo "FAIL: a relaunched supervisor outlived the wrapper"; fail=1; }
[ "$a3" -eq 0 ] || { echo "FAIL: wrapper stayed alive after the bridge's stand-down"; fail=1; }
[ "$s3" -eq 1 ] && [ "$t3" -eq 1 ] && [ "$sa3" -eq 0 ] || { echo "FAIL: the stand-down did not stop the supervisor (starts=$s3 terms=$t3 alive=$sa3)"; fail=1; }
[ "$s4" -eq 1 ] || { echo "FAIL: the bridge's relaunches restarted the supervisor ($s4 starts; want 1)"; fail=1; }

[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
