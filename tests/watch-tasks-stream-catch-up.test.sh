#!/bin/bash
# Regression for #4590: a task delivered while the event stream is silent must
# still be emitted. #4560 removed the directory sweep and the read timer, which
# were the only things that could notice a task fswatch never reported -- so the
# delivery was not late, it was lost (#4588).
#
# The stub fswatch below STAYS UP and emits nothing. That is the failure mode:
# a live event source that reports nothing is indistinguishable from a quiet
# inbox, so only a bounded catch-up pass can close the gap. A stub that exits
# would instead test EOF handling, which is a different contract (the watcher
# must terminate so its EXIT trap can release the sentinel).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
WATCHER_PID=""
unset SUTANDO_INBOX_RESOLVER
# NOT `kill -TERM -"$WATCHER_PID"`: with no job control the watcher shares this
# script's process group, so the negative form signals the test itself (exit 143).
# The watcher's own EXIT trap collects its fswatch child.
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; fi; rm -rf "$TMP"' EXIT

WS="$TMP/ws"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$TMP/bin"
# Alive, silent: never reports the task the test drops into the inbox.
cat > "$TMP/bin/fswatch" <<'STUB'
#!/bin/sh
exec tail -f /dev/null
STUB
chmod +x "$TMP/bin/fswatch"

OUT="$TMP/out"
PATH="$TMP/bin:$PATH" \
SUTANDO_WORKSPACE_DIR="$WS" \
SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_WATCHER_CATCHUP_SECONDS=1 \
bash "$REPO/src/watch-tasks-stream.sh" "$WS/tasks" --role standby --inbox "$WS/tasks" > "$OUT" 2>/dev/null &
WATCHER_PID=$!

# The task must land strictly AFTER the startup sweep, or the sweep is what
# finds it and the test proves nothing. The sentinel is stamped only once
# fswatch is confirmed up, which is after the sweep -- so it is the readiness
# edge to wait on. A fixed sleep is not: startup does real work (python
# resolution, holder probes) and routinely outruns it.
SENTINEL_GLOB="$WS/state/watch-tasks-stream*.pid"
for _ in $(seq 1 80); do
  compgen -G "$SENTINEL_GLOB" > /dev/null && break
  sleep 0.25
done
if ! compgen -G "$SENTINEL_GLOB" > /dev/null; then
  echo "FAIL — the watcher never stamped its sentinel; it was never ready, so nothing below would mean anything."
  exit 1
fi
printf 'id: catch-up\naccess_tier: owner\ntask: recover me\n' > "$WS/tasks/task-catch-up.txt"
for _ in $(seq 1 40); do
  grep -q 'TASK_FILE: task-catch-up.txt' "$OUT" 2>/dev/null && break
  sleep 0.25
done

count="$(grep -c 'TASK_FILE: task-catch-up.txt' "$OUT" 2>/dev/null || true)"
rc=0
if [ "$count" -ne 1 ]; then
  echo "FAIL — expected exactly one catch-up emission, got $count."
  rc=1
fi
# The floor must not cost the watcher its life: a catch-up pass is a safety net
# under a LIVE subscription, not a replacement for it.
if ! kill -0 "$WATCHER_PID" 2>/dev/null; then
  echo "FAIL — the watcher exited; the catch-up pass must not end a live subscription."
  rc=1
fi
# The sweep repeats every second; a task already dispatched must not be
# re-emitted on the next pass (fingerprint dedup). Only meaningful once
# something was emitted at all -- otherwise it just restates the failure above.
if [ "$count" -eq 1 ]; then
  sleep 2.5
  recount="$(grep -c 'TASK_FILE: task-catch-up.txt' "$OUT" 2>/dev/null || true)"
  if [ "$recount" -ne 1 ]; then
    echo "FAIL — catch-up re-emitted an already-dispatched task: $recount emissions after further passes."
    rc=1
  fi
fi

[ "$rc" -eq 0 ] && echo 'PASS — a silent event stream still yields exactly one catch-up emission, watcher still live.'
exit "$rc"
