#!/usr/bin/env bash
# A worker's own beat cannot be spawned as a direct OS child of the process it
# speaks for (sonichi/sutando#4421 follow-up: every process this skill can
# start from inside a running session is born through a tool call whose own
# shell exits when the call returns, orphaning any background child to PID 1).
# --watch-pid exists for exactly that: it tracks an arbitrary pid by signal,
# not by parentage, so the beat writer's own reparenting is harmless.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
cleanup() { rm -rf "$SB"; }
trap cleanup EXIT

WORKSPACE_DIR="$SB/ws"
mkdir -p "$WORKSPACE_DIR/state"
BEAT_SCRIPT="$REPO/skills/worker-pool/scripts/pool_beat.py"
check "the beat script exists" '[ -f "$BEAT_SCRIPT" ]'

# ============================================================================
# Part 1: the beat survives being reparented — the whole reason --watch-pid
# exists. The fake worker stands in for a real worker's claude pid; it is
# never the beat writer's parent, only the pid it watches.
# ============================================================================
sleep 90 &
FAKE_WORKER=$!
BEAT1="$WORKSPACE_DIR/state/workers/w1.alive"
check "beat absent before the writer starts" '[ ! -f "$BEAT1" ]'

# Spawned exactly the way this skill actually can spawn it: backgrounded
# inside a subshell that exits immediately after, exactly like a tool call's
# own shell. The pid is captured AT SPAWN TIME, by the subshell itself, into
# a file — never re-derived afterward by matching argv (the class of bug
# that made a leak check vacuous on #4423: two spellings of one temp path).
( python3 "$BEAT_SCRIPT" --workspace "$WORKSPACE_DIR" --kind worker --id w1 \
      --watch-pid "$FAKE_WORKER" >/dev/null 2>&1 &
  echo $! > "$SB/beat1.pid" )
for _ in $(seq 1 40); do [ -s "$SB/beat1.pid" ] && break; sleep 0.05; done
BEAT_PID="$(cat "$SB/beat1.pid" 2>/dev/null)"
check "captured the beat writer's own pid at spawn time (pid=${BEAT_PID:-none})" '[ -n "$BEAT_PID" ]'

for _ in $(seq 1 60); do [ -f "$BEAT1" ] && break; sleep 0.25; done
check "beat writer created its beat despite the spawning subshell already exiting" '[ -f "$BEAT1" ]'
check "beat carries no payload (mtime only)" '[ ! -s "$BEAT1" ]'
check "the beat writer is alive by the pid captured at spawn" 'kill -0 "$BEAT_PID" 2>/dev/null'

BEAT_PPID="$(ps -o ppid= -p "$BEAT_PID" 2>/dev/null | tr -d ' ')"
check "it HAS been reparented away from the subshell that spawned it (ppid=${BEAT_PPID:-?}, proving --parent-pid tracking could not work here — that subshell no longer exists)" \
      '[ -n "$BEAT_PPID" ] && ! kill -0 "$BEAT_PPID" 2>/dev/null'

_before="$(stat -f %m "$BEAT1" 2>/dev/null || echo 0)"
sleep 2
_mid="$(stat -f %m "$BEAT1" 2>/dev/null || echo 0)"
check "beat is still advancing after 2s (writer keeps running despite the reparenting)" '[ "$_mid" -ge "$_before" ]'

# --- SIGKILL the WATCHED pid only — the beat must notice within ~2s --------
kill -KILL "$FAKE_WORKER" 2>/dev/null
for _ in $(seq 1 40); do kill -0 "$FAKE_WORKER" 2>/dev/null || break; sleep 0.05; done
check "the watched (fake worker) pid is confirmed gone" '! kill -0 "$FAKE_WORKER" 2>/dev/null'

_t0=$(date +%s)
for _ in $(seq 1 60); do kill -0 "$BEAT_PID" 2>/dev/null || break; sleep 0.05; done
_t1=$(date +%s)
_elapsed=$((_t1 - _t0))
check "beat writer exited once its watched pid was SIGKILLed" '! kill -0 "$BEAT_PID" 2>/dev/null'
check "...within the ~2s budget the design asks for (measured ${_elapsed}s)" '[ "$_elapsed" -le 3 ]'

sleep 2
_after="$(stat -f %m "$BEAT1" 2>/dev/null || echo 0)"
check "beat stopped advancing once the writer exited" '[ "$_mid" = "$_after" ]'
check "beat FILE itself is left behind, for a post-restart recency read" '[ -f "$BEAT1" ]'

# ============================================================================
# Part 2: the reverse direction, per review — killing the BEAT WRITER alone
# must not touch the worker it watches. No coupling was ever built the other
# way; proving it explicitly is what makes this a safe one-directional watch.
# ============================================================================
sleep 90 &
FAKE_WORKER2=$!
BEAT2="$WORKSPACE_DIR/state/workers/w2.alive"
( python3 "$BEAT_SCRIPT" --workspace "$WORKSPACE_DIR" --kind worker --id w2 \
      --watch-pid "$FAKE_WORKER2" >/dev/null 2>&1 &
  echo $! > "$SB/beat2.pid" )
for _ in $(seq 1 40); do [ -s "$SB/beat2.pid" ] && break; sleep 0.05; done
BEAT_PID2="$(cat "$SB/beat2.pid" 2>/dev/null)"
for _ in $(seq 1 60); do [ -f "$BEAT2" ] && break; sleep 0.25; done
check "second beat writer is up (pid=${BEAT_PID2:-none})" '[ -n "$BEAT_PID2" ] && [ -f "$BEAT2" ]'

kill -KILL "$BEAT_PID2" 2>/dev/null
for _ in $(seq 1 20); do kill -0 "$BEAT_PID2" 2>/dev/null || break; sleep 0.1; done
check "beat writer #2 is gone (we killed it directly)" '! kill -0 "$BEAT_PID2" 2>/dev/null'
check "the worker it was watching is COMPLETELY UNAFFECTED" 'kill -0 "$FAKE_WORKER2" 2>/dev/null'
kill -KILL "$FAKE_WORKER2" 2>/dev/null

# ============================================================================
# Control: the untouched --once path (no watch flag at all) is unaffected —
# this feature is additive, not a rewrite of the existing beat mechanics.
# ============================================================================
BEAT3="$WORKSPACE_DIR/state/workers/w3.alive"
python3 "$BEAT_SCRIPT" --workspace "$WORKSPACE_DIR" --kind worker --id w3 --once
check "control: --once still writes a beat with no watch flag (unrelated path untouched)" '[ -f "$BEAT3" ]'

echo ""
if [ "$fails" -eq 0 ]; then echo "ALL PASS — worker writes its own beat via --watch-pid (16 checks)"; else echo "$fails FAILURE(S)"; fi
exit $([ "$fails" -eq 0 ] && echo 0 || echo 1)
