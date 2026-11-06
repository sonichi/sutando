#!/usr/bin/env bash
# The settle deadline is GLOBAL; the reported wait is PER-PORT. Per-port
# deadlines serialise, and a global wait misreports an already-ready service.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/src/startup.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "startup verify settle is global:"

BLOCK="$(awk '/^VERIFY_SETTLE_S=/,/^done$/' "$SRC")"
if [ -z "$BLOCK" ]; then
  bad "settle block extracted from src/startup.sh" "no VERIFY_SETTLE_S..done region"
  echo "  Total: 1 — pass: 0, fail: 1"; exit 1
fi
ok "settle block extracted from src/startup.sh"

# Three ports nothing listens on. Picked high and probed first so a real
# listener cannot make this pass by accident.
DEAD=""
for p in 59731 59733 59737; do
  lsof -i :"$p" >/dev/null 2>&1 && { bad "ports are free" "something listens on $p"; continue; }
  DEAD="$DEAD $p:svc$p"
done
[ "$fails" -eq 0 ] && ok "the three probe ports are free"

SETTLE=2
start=$(date +%s)
out=$(VERIFY_PORTS="$DEAD" VERIFY_SETTLE_S="$SETTLE" LOGS_DIR=/tmp bash -c "$BLOCK" 2>&1)
elapsed=$(( $(date +%s) - start ))

# Per-port would be 3 x 2 = 6s. Global is ~2s. The midpoint separates them
# without being tight enough to flake on a loaded machine.
if [ "$elapsed" -lt 4 ]; then
  ok "3 dead ports at settle=${SETTLE}s took ${elapsed}s (global, not 6s serial)"
else
  bad "3 dead ports at settle=${SETTLE}s took ${elapsed}s" "expected <4s; per-port serialisation is back"
fi

# The wait must still happen at all -- a zero-second run would pass the bound
# above while having removed the retry this block exists for.
if [ "$elapsed" -ge "$SETTLE" ]; then
  ok "the settle wait still occurs (${elapsed}s >= ${SETTLE}s)"
else
  bad "the settle wait still occurs" "${elapsed}s < ${SETTLE}s — retry was dropped, not made global"
fi

# All three must still be reported down; a global deadline must not skip ports.
n=$(printf '%s\n' "$out" | grep -c '✗')
if [ "$n" -eq 3 ]; then
  ok "all 3 dead ports still reported"
else
  bad "all 3 dead ports still reported" "got $n of 3"
fi

# A port that is ALREADY listening behind a dead one must report its own wait,
# not the dead port's. This is the case the all-dead run structurally cannot see.
LIVE_PORT=59741
(exec 9<>/dev/tcp/127.0.0.1/1 2>/dev/null) 2>/dev/null || true
python3 -c "
import socket,time,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',$LIVE_PORT)); s.listen(1); time.sleep(20)" &
listener=$!
sleep 1
if lsof -i :"$LIVE_PORT" >/dev/null 2>&1; then
  # Run repeatedly ACROSS a forced second boundary: a two-`date`-sample
  # implementation passes here only by luck, so one run is not a control.
  phantom=""
  for _ in 1 2 3 4; do
    python3 -c "import time;f=time.time()%1;time.sleep(max(0.0,0.985-f) if f<0.985 else 1.985-f)" 2>/dev/null
    out=$(VERIFY_PORTS="59731:dead $LIVE_PORT:ready" VERIFY_SETTLE_S=3 LOGS_DIR=/tmp bash -c "$BLOCK" 2>&1)
    printf '%s' "$out" | grep -qE "ready \(port $LIVE_PORT, after [1-9]" && phantom="$out"
  done
  if [ -z "$phantom" ]; then
    ok "a ready port behind a dead one reports no wait"
  else
    bad "a ready port behind a dead one reports no wait" \
        "got: $(printf '%s' "$phantom" | grep "$LIVE_PORT")"
  fi
else
  bad "helper listener bound" "could not bind $LIVE_PORT"
fi
kill "$listener" 2>/dev/null; wait "$listener" 2>/dev/null

# The timing arm cannot fail reliably, so assert the structural fact instead:
# the wait must count sleeps, never subtract two 1s-granularity `date` samples.
if printf '%s' "$BLOCK" | grep -qE 'waited=\$\(\( *\$\(date \+%s\) *- *[a-z_]+ *\)\)'; then
  bad "the reported wait counts sleeps, not two date samples" \
      "found a two-sample subtraction; it reports 1s across a second rollover"
elif printf '%s' "$BLOCK" | grep -q 'waited=$((waited + 1))'; then
  ok "the reported wait counts sleeps, not two date samples"
else
  bad "the reported wait counts sleeps, not two date samples" "neither form found"
fi

# A listener that becomes ready DURING the retry window must be reported ✓ with a
# nonzero wait — the arm neither the all-dead nor the already-ready case reaches.
SLOW_PORT=59743
if lsof -i :"$SLOW_PORT" >/dev/null 2>&1; then
  bad "slow-port probe is free" "something listens on $SLOW_PORT"
else
  python3 -c "
import socket,time
time.sleep(2)
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',$SLOW_PORT)); s.listen(1); time.sleep(20)" &
  slow=$!
  out=$(VERIFY_PORTS="$SLOW_PORT:slow" VERIFY_SETTLE_S=8 LOGS_DIR=/tmp bash -c "$BLOCK" 2>&1)
  case "$out" in
    *"✓ slow (port $SLOW_PORT, after "*)
      ok "a listener that appears during the window is ✓ with a nonzero wait" ;;
    *"✓ slow (port $SLOW_PORT)"*)
      bad "a listener that appears during the window is ✓ with a nonzero wait" \
          "reported ready with NO wait — the retry is not being counted" ;;
    *)
      bad "a listener that appears during the window is ✓ with a nonzero wait" \
          "got: $out" ;;
  esac
  kill "$slow" 2>/dev/null; wait "$slow" 2>/dev/null
fi

total=8
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1
