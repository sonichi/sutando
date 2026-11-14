#!/usr/bin/env bash
# A worker's identity must survive the tmux boundary: a session gets the SERVER's
# env plus only the launcher's -e list, so the spawner's env alone never reaches it.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
WS="$TMP/ws"; WID="w-testworker"; mkdir -p "$WS/deliveries/$WID"

for runtime in claude codex; do
  python3 - "$REPO" "$WS" "$WID" "$runtime" > "$TMP/plan-$runtime" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/skills/worker-pool/scripts")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime=sys.argv[4],
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
  launcher="$REPO/skills/worker-pool/scripts/launch-worker-session.sh"
  [ "$runtime" = codex ] && launcher="$REPO/skills/worker-pool/scripts/launch-codex-worker-session.sh"
  # shellcheck disable=SC2046
  env -i HOME="$HOME" PATH="$STUB_PATH" $(cat "$TMP/plan-$runtime") \
      bash "$launcher" --print-env > "$TMP/fwd-$runtime" 2>"$TMP/err-$runtime"
  grep -q "^SUTANDO_INSTANCE_ID=$WID\$" "$TMP/fwd-$runtime"
  check $? "$runtime: the --print-env probe returned the allowlist"
  grep -qx -- "SUTANDO_WORKER_ID=$WID" "$TMP/fwd-$runtime"
  check $? "$runtime: SUTANDO_WORKER_ID=$WID crosses into the session"
  for k in SUTANDO_CORE_ID SUTANDO_CORE_POOL_SIZE SUTANDO_WORKER_SEAT; do
    grep -qx -- "$k=" "$TMP/fwd-$runtime"
    check $? "$runtime: $k is explicit-empty, so a server-global core value cannot return"
  done
done

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
