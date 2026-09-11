#!/usr/bin/env bash
# Pins start-cli's handler wiring: a caller-set SUTANDO_TASK_EVENT_HANDLER is
# forwarded verbatim; absent, nothing is forwarded — core never locates a handler
# by filename, even when the repo ships one.
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

# 2. No preset, router present in the repo -> nothing forwarded: core never
#    locates a handler by filename (the pool's install names it).
ROUTER="$REPO/skills/worker-pool/scripts/pool_route_handler.py"
[ -x "$ROUTER" ] || chmod +x "$ROUTER"
out="$(run_probe "$STARTCLI")"
! printf '%s\n' "$out" | grep -q 'SUTANDO_TASK_EVENT_HANDLER='
check $? "absent preset forwards nothing even though the repo ships a router"

# 3. Neither: a mirror without the worker-pool skill forwards nothing. bash's
# logical cd keeps $0 under the mirror, so start-cli resolves REPO to it.
mkdir -p "$TMP/repo/skills"
for e in "$REPO"/* "$REPO"/.[!.]*; do [ "$(basename "$e")" = skills ] && continue; ln -s "$e" "$TMP/repo/$(basename "$e")"; done
for e in "$REPO"/skills/*; do [ "$(basename "$e")" = worker-pool ] && continue; ln -s "$e" "$TMP/repo/skills/$(basename "$e")"; done
out="$(run_probe "$TMP/repo/src/agent/claude/cli/start-cli.sh")"; rc=$?
[ "$rc" = 0 ] && ! printf '%s\n' "$out" | grep -q 'SUTANDO_TASK_EVENT_HANDLER='
check $? "no preset and no router in the tree -> nothing forwarded (probe rc=$rc; $(head -c 160 "$TMP/stderr" | tr '\n' ' '))"

echo "$pass passed, $fail failed"; [ "$fail" = 0 ]
