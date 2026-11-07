#!/bin/bash
# A worker watcher's startup sweep resolves only live sentinels: finished tasks' sentinels
# move to deliveries/<worker>/archive/ first (through the injected pool writer), and archive/ is never swept.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; W=w-test; INBOX="$WS/deliveries/$W"
mkdir -p "$WS/tasks/archive" "$WS/results/archive" "$INBOX/archive" "$WS/state"
STUBBIN="$TMP/stubbin"; mkdir -p "$STUBBIN"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"; chmod +x "$STUBBIN/fswatch"

N=20
for i in $(seq 1 $N); do
  printf 'id: task-done%s\ntask: x\n' "$i" > "$WS/tasks/archive/task-done$i.txt"
  printf 'the reply\n' > "$WS/results/archive/task-done$i.txt"
  : > "$INBOX/task-done$i.txt"
done
printf 'id: task-open\nsource: chat\ntask: still owed\n' > "$WS/tasks/task-open.txt"
: > "$INBOX/task-open.txt"
# Already in archive/ with a live payload: only the top level is ever swept.
printf 'id: task-old\nsource: chat\ntask: old\n' > "$WS/tasks/task-old.txt"
: > "$INBOX/archive/task-old.txt"

CALLS="$TMP/calls"; : > "$CALLS"
RESOLVER="$TMP/resolver.sh"
cat > "$RESOLVER" <<EOF
#!/bin/sh
printf '%s\n' "\$1" >> '$CALLS'
exec '$REPO/skills/worker-pool/scripts/resolve-inbox-entry' "\$@"
EOF
chmod +x "$RESOLVER"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
set -m
PATH="$STUBBIN:$PATH" SUTANDO_INBOX_RESOLVER="$RESOLVER" SUTANDO_WORKSPACE_DIR="$WS" \
  SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE=$W SUTANDO_INSTANCE_ID=$W \
  SUTANDO_POOL_DELIVERY_SCRIPT="$REPO/skills/worker-pool/scripts/pool_delivery.py" \
  bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role session --inbox "$INBOX" > "$OUT" 2>"$ERR" &
pid=$!
set +m
for i in $(seq 1 240); do grep -q "TASK_FILE: $WS/tasks/task-open.txt" "$OUT" && break; sleep 0.25; done

grep -q "TASK_FILE: $WS/tasks/task-open.txt" "$OUT"
check $? "the open delivery is dispatched"
[ "$(grep -c 'task-done' "$CALLS")" = "0" ]
check $? "no finished sentinel is resolved by the sweep (resolved $(grep -c 'task-done' "$CALLS") of $N)"
[ "$(ls "$INBOX"/archive | grep -c '^task-done[0-9]*\.txt$')" = "$N" ] && [ ! -e "$INBOX/task-done1.txt" ]
check $? "every finished sentinel moved into archive/"
[ -e "$INBOX/task-open.txt" ]
check $? "the open task keeps its live sentinel"
! grep -q 'task-old' "$OUT" "$CALLS"
check $? "an entry already in archive/ is never resolved or dispatched"

kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; sed -n 1,20p "$ERR"; exit 1; fi
