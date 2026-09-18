#!/usr/bin/env bash
# The watcher refreshes `state/watchers/<id>.alive` while it runs, and stops when it
# exits — a beat that outlives its writer is the defect #4213 records in the core's.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
WATCHER_PID=""
# The watcher gets its OWN group (below), so this group TERM reaps it and its
# children; sharing a group would make the watcher's own cleanup kill this test.
cleanup() { [ -n "$WATCHER_PID" ] && kill -TERM "-$WATCHER_PID" 2>/dev/null; sleep 0.4; rm -rf "$SB"; }
trap cleanup EXIT

export WORKSPACE_DIR="$SB/ws"
mkdir -p "$WORKSPACE_DIR/tasks" "$WORKSPACE_DIR/state"
BEAT="$WORKSPACE_DIR/state/watchers/core.alive"

check "beat is absent before the watcher starts" '[ ! -f "$BEAT" ]'

# INJECTED: the core must not locate the skill itself, so the test hands it the
# path exactly as a spawner would (tests/ may name the skill; src/ may not).
export SUTANDO_WATCHER_BEAT="$REPO/skills/worker-pool/scripts/pool_beat.py"
check "the injected beat script exists" '[ -f "$SUTANDO_WATCHER_BEAT" ]'

# Explicit tasks dir: without it the watcher resolves a DIFFERENT workspace and the
# test silently measures nothing.

# Own process group (no setsid on macOS): the watcher's cleanup sends a GROUP
# TERM, which kills this test instead of the watcher if they share one.
python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    bash "$REPO/src/watch-tasks-stream.sh" "$WORKSPACE_DIR/tasks" >"$SB/out" 2>"$SB/err" &
WATCHER_PID=$!

for _ in $(seq 1 60); do [ -f "$BEAT" ] && break; sleep 0.25; done
check "watcher created its beat" '[ -f "$BEAT" ]'
check "beat carries no payload (mtime only)" '[ ! -s "$BEAT" ]'
check "watcher still alive (beat did not come from a crash path)" 'kill -0 "$WATCHER_PID" 2>/dev/null'

_before="$(stat -f %m "$BEAT" 2>/dev/null || echo 0)"
kill -TERM "-$WATCHER_PID" 2>/dev/null
for _ in $(seq 1 40); do kill -0 "$WATCHER_PID" 2>/dev/null || break; sleep 0.25; done
check "watcher exited" '! kill -0 "$WATCHER_PID" 2>/dev/null'

# The beat FILE remains (that is what makes a post-restart recency read possible);
# what must stop is its refreshing.
sleep 2
_after="$(stat -f %m "$BEAT" 2>/dev/null || echo 0)"
check "beat stopped advancing once the watcher exited" '[ "$_before" = "$_after" ]'
check "beat file itself is left behind for the recency read" '[ -f "$BEAT" ]'

# No leaked beat writers: the child must die with its parent.
_leaked="$(pgrep -f "pool_beat.py --workspace $WORKSPACE_DIR" 2>/dev/null | wc -l | tr -d ' ')"
check "no pool_beat child survived the watcher (leaked=$_leaked)" '[ "$_leaked" -eq 0 ]'

WATCHER_PID=""

# ...and with the variable UNSET the core writes no beat at all, which is what
# keeps a host without the pool skill unaffected.
SB2="$(mktemp -d)"
export WORKSPACE_DIR="$SB2/ws"
mkdir -p "$WORKSPACE_DIR/tasks" "$WORKSPACE_DIR/state"
unset SUTANDO_WATCHER_BEAT
python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    bash "$REPO/src/watch-tasks-stream.sh" "$WORKSPACE_DIR/tasks" >/dev/null 2>&1 &
W2=$!
sleep 3
check "no beat when SUTANDO_WATCHER_BEAT is unset" '[ ! -d "$WORKSPACE_DIR/state/watchers" ]'
check "the watcher itself still runs unaffected" 'kill -0 "$W2" 2>/dev/null'
kill -TERM "-$W2" 2>/dev/null; sleep 0.5; rm -rf "$SB2"

echo ""
if [ "$fails" -eq 0 ]; then echo "ALL PASS — watcher writes its beat (11 checks)"; else echo "$fails FAILURE(S)"; fi
exit $([ "$fails" -eq 0 ] && echo 0 || echo 1)
