#!/bin/bash
# Tests for src/agent/stop-core.sh — exact-selector + session-env contract
# (john-the-dev review on #2408: prefix matching killed sutando-core-debug;
# SUTANDO_TMUX_SESSION was ignored). Runs real tmux on an ISOLATED private
# socket; skips cleanly (exit 0) when tmux is unavailable.
#
# Run: bash tests/stop-core.test.sh   (exit 0 pass/skip, 1 fail)
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/src/agent/stop-core.sh"
command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

SOCK="$(mktemp -d)/t.sock"
fails=0
say() { echo "$1  $2"; [ "$1" = "FAIL" ] && fails=$((fails+1)); }
cleanup() { tmux -S "$SOCK" kill-server 2>/dev/null; }
trap cleanup EXIT

# --- 1. prefix-survival: only sutando-core-debug exists → script must NOT
#        match it, must report nothing-to-stop, and the session must survive.
tmux -S "$SOCK" new-session -d -s sutando-core-debug sleep 60
out="$(SUTANDO_TMUX_SOCKET="$SOCK" bash "$SCRIPT" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && echo "$out" | grep -q "nothing to stop" \
   && tmux -S "$SOCK" has-session -t "=sutando-core-debug" 2>/dev/null; then
  say ok "prefix session survives; script reports nothing-to-stop"
else
  say FAIL "prefix session: rc=$rc out=$out"
fi

# --- 2. exact stop: sutando-core AND sutando-core-debug both exist → script
#        kills exactly sutando-core; the debug sibling survives.
tmux -S "$SOCK" new-session -d -s sutando-core sleep 60
out="$(SUTANDO_TMUX_SOCKET="$SOCK" bash "$SCRIPT" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && ! tmux -S "$SOCK" has-session -t "=sutando-core" 2>/dev/null \
   && tmux -S "$SOCK" has-session -t "=sutando-core-debug" 2>/dev/null; then
  say ok "kills exactly sutando-core; debug sibling survives"
else
  say FAIL "exact stop: rc=$rc out=$out"
fi

# --- 3. SUTANDO_TMUX_SESSION honored (launcher contract parity): custom-core
#        configured → script stops it.
tmux -S "$SOCK" new-session -d -s custom-core sleep 60
out="$(SUTANDO_TMUX_SOCKET="$SOCK" SUTANDO_TMUX_SESSION=custom-core bash "$SCRIPT" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && ! tmux -S "$SOCK" has-session -t "=custom-core" 2>/dev/null; then
  say ok "SUTANDO_TMUX_SESSION=custom-core is stopped"
else
  say FAIL "custom session: rc=$rc out=$out"
fi

# --- 4. watcher sibling cleanup: sutando-core + sutando-core-watcher →
#        both killed; unrelated debug session still survives.
tmux -S "$SOCK" new-session -d -s sutando-core sleep 60
tmux -S "$SOCK" new-session -d -s sutando-core-watcher sleep 60
out="$(SUTANDO_TMUX_SOCKET="$SOCK" bash "$SCRIPT" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && ! tmux -S "$SOCK" has-session -t "=sutando-core" 2>/dev/null \
   && ! tmux -S "$SOCK" has-session -t "=sutando-core-watcher" 2>/dev/null \
   && tmux -S "$SOCK" has-session -t "=sutando-core-debug" 2>/dev/null; then
  say ok "watcher sibling killed with core; debug survives"
else
  say FAIL "watcher cleanup: rc=$rc out=$out"
fi

echo
if [ $fails -gt 0 ]; then echo "$fails test(s) FAILED"; exit 1; fi
echo "all tests passed — stop-core exact-selector contract"
