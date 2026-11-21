#!/usr/bin/env bash
# The core launchers forward an embedder's SUTANDO_WORKSPACE_DIR into the core
# tmux session, which otherwise takes the (possibly older) tmux server's env.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CLAUDE="$REPO/src/agent/claude/cli/start-cli.sh"
WS="$TMP/embedder workspace"

out="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_WORKSPACE_DIR="$WS" \
  bash "$CLAUDE" --print-core-env 2>/dev/null)"
echo "$out" | grep -qxF -- "SUTANDO_WORKSPACE_DIR=$WS"
check $? "claude core: a set SUTANDO_WORKSPACE_DIR is forwarded, spaces intact"

out="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$CLAUDE" --print-core-env 2>/dev/null)"
! echo "$out" | grep -q "^SUTANDO_WORKSPACE_DIR="
check $? "claude core: unset stays unset (OSS installs unchanged)"

out="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_WORKSPACE_DIR= \
  bash "$CLAUDE" --print-core-env 2>/dev/null)"
! echo "$out" | grep -q "^SUTANDO_WORKSPACE_DIR="
check $? "claude core: an empty value is not forwarded"

# The codex half is tested by behaviour in tests/codex-core-launcher.test.py.

echo "start-cli forwards workspace dir: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
