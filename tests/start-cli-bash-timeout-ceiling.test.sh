#!/usr/bin/env bash
# Pins the core's foreground Bash ceiling: start-cli forwards BASH_MAX_TIMEOUT_MS
# (default 120000) so a long command auto-backgrounds instead of holding the turn.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"

# No proxy anywhere: lsof/launchctl/pgrep all report absent, so host state cannot leak in.
mkdir -p "$TMP/bin"
for t in lsof launchctl pgrep; do printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/$t"; done
chmod +x "$TMP"/bin/*

run_probe() {  # extra env KEY=VAL pairs
  env -i HOME="$HOME" PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@" \
      bash "$STARTCLI" --print-core-env 2>"$TMP/stderr"
}

# 1. Default: the ceiling is forwarded at 2 minutes.
out="$(run_probe)"
echo "$out" | grep -qx "BASH_MAX_TIMEOUT_MS=120000"
check $? "default core env forwards BASH_MAX_TIMEOUT_MS=120000"

# 2. Exactly one entry, so tmux cannot receive two conflicting values.
n="$(echo "$out" | grep -c '^BASH_MAX_TIMEOUT_MS=')"
rc=1; [ "$n" = "1" ] && rc=0
check "$rc" "BASH_MAX_TIMEOUT_MS appears once"

# 3. A caller-set value wins over the default.
out="$(run_probe BASH_MAX_TIMEOUT_MS=300000)"
echo "$out" | grep -qx "BASH_MAX_TIMEOUT_MS=300000"
check $? "caller-set BASH_MAX_TIMEOUT_MS=300000 is forwarded verbatim"
rc=0; echo "$out" | grep -qx "BASH_MAX_TIMEOUT_MS=120000" && rc=1
check "$rc" "caller preset is not overridden by the default"

# 4. The baseline core marker is still present (the append did not replace the array).
echo "$out" | grep -qx "SUTANDO_CORE_SESSION=1"
check $? "core-session marker still forwarded"

echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
