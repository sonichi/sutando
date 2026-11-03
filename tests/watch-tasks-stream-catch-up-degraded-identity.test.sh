#!/bin/bash
# When cksum fails, an admission is still recorded (inode + mtime), so a task the
# event stream announced is not announced again when catch-up examines it.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null; fi; rm -rf "$TMP"' EXIT

CATCHUP=2
WS="$TMP/ws"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$TMP/bin"
# A live event stream (the polling stand-in), so every new file is reported AND
# examined by the next catch-up pass.
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$TMP/bin/fswatch"; chmod +x "$TMP/bin/fswatch"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/cksum"; chmod +x "$TMP/bin/cksum"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
PATH="$TMP/bin:$PATH" SUTANDO_WORKSPACE_DIR="$WS" SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_WATCHER_CATCHUP_SECONDS="$CATCHUP" \
bash "$REPO/src/watch-tasks-stream.sh" "$WS/tasks" --role standby --inbox "$WS/tasks" > "$OUT" 2>"$ERR" &
WATCHER_PID=$!
for _ in $(seq 1 100); do
  compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null && break
  sleep 0.1
done
compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null
check $? "the watcher subscribed"

printf 'id: task-nocksum\naccess_tier: owner\ntask: once\n' > "$WS/tasks/task-nocksum.txt"
for _ in $(seq 1 50); do
  grep -q 'TASK_FILE: task-nocksum.txt' "$OUT" && break
  sleep 0.1
done
grep -q 'TASK_FILE: task-nocksum.txt' "$OUT"
check $? "the event stream announced the task"
grep -q 'no usable checksum for task-nocksum.txt; deduping on inode and mtime' "$ERR"
check $? "...with a degraded identity, since cksum failed"
sleep $(( CATCHUP * 3 + 1 ))
[ "$(grep -c 'TASK_FILE: task-nocksum.txt' "$OUT")" = "1" ]
check $? "three catch-up passes later it is still announced once (got $(grep -c 'TASK_FILE: task-nocksum.txt' "$OUT"))"

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
