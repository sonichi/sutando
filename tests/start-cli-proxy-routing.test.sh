#!/usr/bin/env bash
# Pins start-cli's proxy-routing policy: caller-set ANTHROPIC_BASE_URL wins; a
# loaded proxy job polls bounded for the listener; a dead port is never wired.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"

# lsof stubs model port state; every dir also stubs launchctl (job loaded per
# case) so host launchd state cannot leak into the asserts.
mkdir -p "$TMP/live-bin" "$TMP/dead-bin" "$TMP/exp-late-bin" "$TMP/exp-never-bin" "$TMP/exp-dead-bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/live-bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/dead-bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/live-bin/launchctl"
printf '#!/bin/sh\nexit 1\n' > "$TMP/dead-bin/launchctl"
printf '#!/bin/sh\nexit 0\n' > "$TMP/exp-late-bin/launchctl"
printf '#!/bin/sh\nexit 0\n' > "$TMP/exp-never-bin/launchctl"
printf '#!/bin/sh\nexit 0\n' > "$TMP/exp-dead-bin/launchctl"
# exp-late: stateful lsof — the "listener" appears on the 4th probe (~1.5s into
# the poll), i.e. after launch, exactly the field-reported race shape.
printf '#!/bin/sh\nn=$(cat %s/probe-count 2>/dev/null || echo 0)\nn=$((n+1))\necho $n > %s/probe-count\n[ "$n" -ge 4 ] && exit 0\nexit 1\n' "$TMP" "$TMP" > "$TMP/exp-late-bin/lsof"
# exp-never: listener never appears; sleep is a no-op shim so the full 20-poll
# budget runs instantly (bounded-budget semantics, not wall-clock, is the pin).
printf '#!/bin/sh\nexit 1\n' > "$TMP/exp-never-bin/lsof"
printf '#!/bin/sh\nexit 0\n' > "$TMP/exp-never-bin/sleep"
# exp-dead: job loaded, port dead, REAL sleep — used to prove caller-preset
# launches skip the wait entirely (timing assert).
printf '#!/bin/sh\nexit 1\n' > "$TMP/exp-dead-bin/lsof"
chmod +x "$TMP"/live-bin/* "$TMP"/dead-bin/* "$TMP"/exp-late-bin/* "$TMP"/exp-never-bin/* "$TMP"/exp-dead-bin/*

run_probe() {  # $1 = stub bin dir; remaining args = extra env KEY=VAL pairs
  local stub="$1"; shift
  env -i HOME="$HOME" PATH="$stub:/usr/bin:/bin:/usr/sbin:/sbin" "$@" \
      bash "$STARTCLI" --print-core-env 2>"$TMP/stderr"
}

# 1. Live listener → the proxy URL is forwarded into the core env.
out="$(run_probe "$TMP/live-bin")"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://localhost:7846"
check $? "live proxy listener forwards ANTHROPIC_BASE_URL=http://localhost:7846"

# 2. Dead port, not expected → unwired AND no wait (real sleep on PATH; the
#    ≤5s elapsed bound trips if the 10s poll leaks onto this path).
_t0=$(date +%s)
out="$(run_probe "$TMP/dead-bin")"
_elapsed=$(( $(date +%s) - _t0 ))
echo "$out" | grep -q "ANTHROPIC_BASE_URL"
[ $? -ne 0 ]
check $? "dead proxy port omits ANTHROPIC_BASE_URL (never point the core at a dead port)"
[ "$_elapsed" -le 5 ]
check $? "proxy-less host does not wait (fast path, ${_elapsed}s elapsed)"

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

# 5. Expected + LATE listener (the launch-order race): the launchd job is
#    loaded but the listener only appears on the 4th poll — the bounded wait
#    must catch it and wire the proxy instead of launching unrouted for life.
rm -f "$TMP/probe-count"
_t0=$(date +%s)
out="$(run_probe "$TMP/exp-late-bin")"
_elapsed=$(( $(date +%s) - _t0 ))
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://localhost:7846"
check $? "expected + late listener → bounded wait catches it and wires the proxy"
[ "$_elapsed" -ge 1 ] && [ "$(cat "$TMP/probe-count" 2>/dev/null || echo 0)" -ge 4 ]
check $? "the wait actually re-polled (${_elapsed}s, $(cat "$TMP/probe-count" 2>/dev/null || echo 0) lsof probes)"

# 6. Expected + NEVER a listener: budget exhausts → proceed UNWIRED with a
#    one-line warning naming the consequence. sleep is shimmed, so this also
#    proves the poll is iteration-bounded (exactly 20 probes), not open-ended.
rm -f "$TMP/stderr"
out="$(run_probe "$TMP/exp-never-bin")"
echo "$out" | grep -q "ANTHROPIC_BASE_URL"
[ $? -ne 0 ]
check $? "expected + never-listener → budget exhausts, core env stays unwired (no dead port)"
grep -q "no proxy protection, no quota telemetry" "$TMP/stderr"
check $? "budget exhaustion warns with the consequence (no proxy protection / telemetry)"
echo "$out" | grep -q "SUTANDO_CORE_SESSION=1"
check $? "unwired launch still proceeds (core env intact after budget exhaustion)"

# 7. Caller preset + expected + dead port: the preset wins verbatim AND skips
#    the wait entirely (real sleep on PATH — a wait would blow the time bound).
_t0=$(date +%s)
out="$(run_probe "$TMP/exp-dead-bin" ANTHROPIC_BASE_URL=http://example.test:1)"
_elapsed=$(( $(date +%s) - _t0 ))
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://example.test:1"
check $? "caller preset still wins when the proxy is expected but down"
[ "$_elapsed" -le 5 ]
check $? "caller preset skips the wait (fast path, ${_elapsed}s elapsed)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
