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

# --- 5. stop tombstone end-to-end (#2160, qingyun review): the PRODUCTION stop
#        path must publish the durable graceful-stop state, and the recover
#        path — with its DEFAULT production wiring, no injected stopped_fn —
#        must read it and refuse to relaunch. Crash control: same recover run
#        without the tombstone restarts, so the gate is proven able to fail.
WS="$(mktemp -d)"
HB_ENV=(SUTANDO_TEST_MODE=1 "SUTANDO_WORKSPACE=$WS" SUTANDO_HOST_LABEL=stoptest)
tmux -S "$SOCK" new-session -d -s sutando-core sleep 60
out="$(env "${HB_ENV[@]}" SUTANDO_TMUX_SOCKET="$SOCK" bash "$SCRIPT" 2>&1)"; rc=$?
TOMB="$WS/state/cores/stoptest.stopped"
if [ $rc -eq 0 ] && [ -f "$TOMB" ]; then
  say ok "stop-core publishes the graceful-stop tombstone"
else
  say FAIL "tombstone publish: rc=$rc tomb-present=$([ -f "$TOMB" ] && echo y || echo n) out=$out"
fi

recover_action() {  # runs recover twice (observe -> past confirm) with production stopped_fn
  env "${HB_ENV[@]}" python3 - "$WS" <<'PY'
import importlib.util, sys, tempfile
from pathlib import Path
repo = Path(__file__).resolve()  # unused; module path from argv
spec = importlib.util.spec_from_file_location("hc", str(Path("src/health-check.py").resolve()))
hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)
hc.RECOVER_CONFIRM_SEC = 120
state = Path(tempfile.mkdtemp()) / "rec.json"
kw = dict(state_file=state, alive_fn=lambda: False,
          oldest_task_fn=lambda: ("t1", 5000), status_ts_fn=lambda: None,
          just_booted_fn=lambda: False, restart_fn=lambda: True,
          sender=lambda t: True)  # stopped_fn deliberately NOT injected
hc.recover_core_if_wedged(now=1_000_000, **kw)
r = hc.recover_core_if_wedged(now=1_000_200, **kw)
print((r or {}).get("action"))
PY
}
act="$(recover_action)"
if [ "$act" = "deliberate-stop" ]; then
  say ok "recover (production stopped_fn) refuses to relaunch a stopped core"
else
  say FAIL "recover with tombstone: action=$act (want deliberate-stop)"
fi
rm -f "$TOMB"
act="$(recover_action)"
if [ "$act" = "restarted" ]; then
  say ok "crash control: without the tombstone the same recover run relaunches"
else
  say FAIL "crash control: action=$act (want restarted)"
fi

# --- 6. no-session early exit must NOT write a tombstone: probing a crashed
#        core with stop-core must not mask it from recovery.
WS2="$(mktemp -d)"
out="$(env SUTANDO_TEST_MODE=1 "SUTANDO_WORKSPACE=$WS2" SUTANDO_HOST_LABEL=stoptest \
       SUTANDO_TMUX_SOCKET="$SOCK" SUTANDO_TMUX_SESSION=absent-core bash "$SCRIPT" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ ! -f "$WS2/state/cores/stoptest.stopped" ]; then
  say ok "nothing-to-stop path writes no tombstone"
else
  say FAIL "no-session tombstone guard: rc=$rc out=$out"
fi

echo
if [ $fails -gt 0 ]; then echo "$fails test(s) FAILED"; exit 1; fi
echo "all tests passed — stop-core exact-selector contract"
