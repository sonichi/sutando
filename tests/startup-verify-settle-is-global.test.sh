#!/usr/bin/env bash
# The settle deadline is GLOBAL, not per-port. Per-port it serialises to
# ports x VERIFY_SETTLE_S of dead time before the core launches.
#
# Run: bash tests/startup-verify-settle-is-global.test.sh
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

total=5
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1
