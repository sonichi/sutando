#!/usr/bin/env bash
# The watcher's ordinary dispatch path reaches the lifecycle owner: dispatch_task marks QUEUED once,
# tells the live core, then marks RUNNING — for every branch that prints TASK_FILE.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/../src"
fail=0
# No raw TASK_FILE printf survives inside dispatch_task: every emit goes through the owner.
body="$(awk '/^dispatch_task\(\) \{/,/^\}/' "$SRC/watch-tasks-stream.sh")"
raw="$(printf '%s\n' "$body" | grep -c "printf 'TASK_FILE")"
[ "$raw" -eq 0 ] && echo "PASS dispatch_task has no raw TASK_FILE printf" || { echo "FAIL dispatch_task still prints TASK_FILE directly ($raw)"; fail=1; }
via="$(printf '%s\n' "$body" | grep -c 'emit_dispatch_task_file')"
[ "$via" -ge 4 ] && echo "PASS dispatch_task emits through emit_dispatch_task_file ($via sites)" || { echo "FAIL expected >=4 owner emits, got $via"; fail=1; }
q="$(printf '%s\n' "$body" | grep -c 'queued_activity_row "\$filename"')"
[ "$q" -eq 1 ] && echo "PASS dispatch_task marks QUEUED exactly once" || { echo "FAIL QUEUED marked $q times"; fail=1; }
# The handler path: launching a worker is the pickup, so drain_dispatch_queue marks RUNNING there.
drain="$(awk '/^drain_dispatch_queue\(\) \{/,/^\}/' "$SRC/watch-tasks-stream.sh")"
printf '%s\n' "$drain" | grep -q 'activity_transition RUNNING "$(basename "$marker")"' && echo "PASS a launched handler marks RUNNING" || { echo "FAIL drain_dispatch_queue does not mark RUNNING on handler launch"; fail=1; }
# Behaviour: emit_dispatch_task_file prints the line and marks RUNNING through the bus (stubbed).
tmp="$(mktemp -d)"; log="$tmp/bus.log"
cat > "$tmp/py" << PY
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$log"
PY
chmod +x "$tmp/py"
mkdir -p "$tmp/tasks"; printf 'id: task-abc\ntask: hi\n' > "$tmp/tasks/task-abc.txt"
out="$(TASKS_DIR="$tmp/tasks" SUTANDO_PY_BIN="$tmp/py" bash -c 'source "$1"; emit_dispatch_task_file task-abc.txt' _ "$SRC/task-emit.sh" 2>/dev/null)"
for _ in $(seq 1 30); do grep -q "transition RUNNING" "$log" 2>/dev/null && break; sleep 0.1; done  # the transition is fire-and-forget
[ "$out" = "TASK_FILE: task-abc.txt" ] && echo "PASS the live core is told" || { echo "FAIL stdout was: $out"; fail=1; }
grep -q "transition RUNNING" "$log" 2>/dev/null && echo "PASS RUNNING follows the emit" || { echo "FAIL no RUNNING transition recorded: $(cat "$log" 2>/dev/null)"; fail=1; }
grep -q "transition QUEUED" "$log" 2>/dev/null && { echo "FAIL emit_dispatch_task_file must not re-mark QUEUED"; fail=1; } || echo "PASS QUEUED is dispatch_task's, not the emitter's"
rm -rf "$tmp"
[ "$fail" -eq 0 ] && echo "watch-tasks-dispatch-activity: PASS" || { echo "watch-tasks-dispatch-activity: FAIL"; exit 1; }
