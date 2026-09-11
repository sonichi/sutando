#!/usr/bin/env bash
# Pins start-cli's handler wiring: a caller-set SUTANDO_TASK_EVENT_HANDLER is
# forwarded verbatim; absent, the repo's own router is the default; with neither,
# nothing is forwarded (a core booted without it routes nothing).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"
mkdir -p "$TMP/bin"; printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"; printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"; chmod +x "$TMP"/bin/*
run_probe() { local cli="$1"; shift; env -i HOME="$HOME" PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@" bash "$cli" --print-core-env 2>"$TMP/stderr"; }

# 1. Caller preset wins verbatim.
out="$(run_probe "$STARTCLI" SUTANDO_TASK_EVENT_HANDLER=/elsewhere/handler.py)"
printf '%s\n' "$out" | grep -qx 'SUTANDO_TASK_EVENT_HANDLER=/elsewhere/handler.py'
check $? "caller preset is forwarded verbatim"

# 2. No preset, repo router present -> the repo copy is the default.
[ -x "$REPO/src/pool_route_handler.py" ] || chmod +x "$REPO/src/pool_route_handler.py"
out="$(run_probe "$STARTCLI")"
printf '%s\n' "$out" | grep -qx "SUTANDO_TASK_EVENT_HANDLER=$REPO/src/pool_route_handler.py"
check $? "absent preset defaults to the repo's own router"

# 3. Neither: a mirror of the repo without the router forwards nothing. bash's
# logical cd keeps $0 under the mirror, so start-cli resolves REPO to it.
mkdir -p "$TMP/repo/src"
for e in "$REPO"/* "$REPO"/.[!.]*; do [ "$(basename "$e")" = src ] && continue; ln -s "$e" "$TMP/repo/$(basename "$e")"; done
for e in "$REPO"/src/*; do [ "$(basename "$e")" = pool_route_handler.py ] && continue; ln -s "$e" "$TMP/repo/src/$(basename "$e")"; done
out="$(run_probe "$TMP/repo/src/agent/claude/cli/start-cli.sh")"; rc=$?
[ "$rc" = 0 ] && ! printf '%s\n' "$out" | grep -q 'SUTANDO_TASK_EVENT_HANDLER='
check $? "no preset and no repo router -> nothing forwarded (probe rc=$rc; $(head -c 160 "$TMP/stderr" | tr '\n' ' '))"

echo "$pass passed, $fail failed"; [ "$fail" = 0 ]
