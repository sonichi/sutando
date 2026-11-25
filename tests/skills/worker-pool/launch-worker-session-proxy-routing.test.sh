#!/usr/bin/env bash
# A pool worker's launch follows the quota status line recorder's switch: on (the
# manifest default) it never forwards the local credential proxy as ANTHROPIC_BASE_URL;
# off, a live proxy is wired as before; an owner-set non-proxy base URL is always kept.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WORKERCLI="$REPO/skills/worker-pool/scripts/launch-worker-session.sh"
# A live proxy listener, no launchd job, no proxy process: nothing on the host leaks in.
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/pgrep"
printf '#!/bin/sh\necho '"'"'{"loggedIn": true}'"'"'\n' > "$TMP/bin/claude"  # signed in
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WS="$TMP/ws"; WID="w-routing"
mkdir -p "$WS/deliveries/$WID"
python3 - "$REPO" "$WS" "$WID" > "$TMP/plan-env" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/skills/worker-pool/scripts")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime="claude",
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
[ -s "$TMP/plan-env" ]
check $? "spawn_worker.plan() produced a session env"

probe() {  # extra KEY=VAL pairs override the plan's env
  # shellcheck disable=SC2046
  env -i HOME="$HOME" PATH="$STUB_PATH" $(grep -v '^ANTHROPIC_BASE_URL=\|^QUOTA_STATUSLINE_ENABLED=' "$TMP/plan-env") "$@" \
      bash "$WORKERCLI" --print-env 2>/dev/null
}

out="$(probe)"
echo "$out" | grep -q "SUTANDO_CORE_RUNTIME=claude"
check $? "control: the --print-env probe returned the assembled allowlist"
! echo "$out" | grep -q "^ANTHROPIC_BASE_URL="
check $? "recorder on (manifest default): a live proxy is not forwarded"

out="$(probe ANTHROPIC_BASE_URL=http://localhost:7846)"
! echo "$out" | grep -q "^ANTHROPIC_BASE_URL="
check $? "recorder on: an inherited local proxy URL is dropped"

out="$(probe ANTHROPIC_BASE_URL=http://example.test:1)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://example.test:1"
check $? "recorder on: a custom base URL is kept"

out="$(probe QUOTA_STATUSLINE_ENABLED=false)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://localhost:7846"
check $? "recorder off: the live proxy is forwarded again"

out="$(probe QUOTA_STATUSLINE_ENABLED=false ANTHROPIC_BASE_URL=http://example.test:1)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://example.test:1"
check $? "recorder off: a custom base URL is still kept"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
