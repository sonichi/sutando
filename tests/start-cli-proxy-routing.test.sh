#!/usr/bin/env bash
# Regression (#2417 review, qingyun P1): start-cli.sh's credential-proxy
# routing is a three-way launch policy — a live proxy LISTENer forwards
# ANTHROPIC_BASE_URL into the core env, a dead port omits it, and a
# caller-set value always wins. Prior suites never exercised
# ANTHROPIC_BASE_URL, so CI stayed green if the tmux forwarding was
# dropped or the listener guard inverted.
#
# Hermetic: lsof is stubbed via PATH (no real port state), and the
# --print-core-env probe exits before any tmux interaction — asserts run
# against the REAL assembled CORE_ENV_ARGS.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"

# lsof stubs: "live" exits 0 (a LISTENer holds the port), "dead" exits 1.
mkdir -p "$TMP/live-bin" "$TMP/dead-bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/live-bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/dead-bin/lsof"
chmod +x "$TMP/live-bin/lsof" "$TMP/dead-bin/lsof"

run_probe() {  # $1 = stub bin dir; remaining args = extra env KEY=VAL pairs
  local stub="$1"; shift
  env -i HOME="$HOME" PATH="$stub:/usr/bin:/bin:/usr/sbin:/sbin" "$@" \
      bash "$STARTCLI" --print-core-env 2>/dev/null
}

# 1. Live listener → the proxy URL is forwarded into the core env.
out="$(run_probe "$TMP/live-bin")"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://localhost:7846"
check $? "live proxy listener forwards ANTHROPIC_BASE_URL=http://localhost:7846"

# 2. Dead port → no ANTHROPIC_BASE_URL anywhere in the core env.
out="$(run_probe "$TMP/dead-bin")"
echo "$out" | grep -q "ANTHROPIC_BASE_URL"
[ $? -ne 0 ]
check $? "dead proxy port omits ANTHROPIC_BASE_URL (never point the core at a dead port)"

# 3. Caller preset → forwarded verbatim, not clobbered by the live-listener path.
out="$(run_probe "$TMP/live-bin" ANTHROPIC_BASE_URL=http://example.test:1)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://example.test:1"
check $? "caller-set ANTHROPIC_BASE_URL wins over the listener autowire"
echo "$out" | grep -q "ANTHROPIC_BASE_URL=http://localhost:7846"
[ $? -ne 0 ]
check $? "caller preset is not duplicated/overridden with the local proxy URL"

# 4. Baseline markers still forwarded in all modes (guard didn't eat the array).
echo "$out" | grep -q "SUTANDO_CORE_SESSION=1"
check $? "core-session marker still present in CORE_ENV_ARGS"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
