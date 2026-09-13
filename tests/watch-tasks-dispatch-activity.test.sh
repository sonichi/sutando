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
q="$(printf '%s\n' "$body" | grep -cE 'queued_activity_row "\$[A-Za-z_]+"')"
[ "$q" -eq 1 ] && echo "PASS dispatch_task marks QUEUED exactly once" || { echo "FAIL QUEUED marked $q times"; fail=1; }
# The handler path: launching a worker is the pickup, so drain_dispatch_queue marks RUNNING there.
drain="$(awk '/^drain_dispatch_queue\(\) \{/,/^\}/' "$SRC/watch-tasks-stream.sh")"
printf '%s\n' "$drain" | grep -qE 'activity_transition RUNNING "\$[A-Za-z_]+"' && echo "PASS a launched handler marks RUNNING" || { echo "FAIL drain_dispatch_queue does not mark RUNNING on handler launch"; fail=1; }
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

# Behaviour, not text: a resolved entry's QUEUED transition must key on the
# real payload, not the sentinel basename would resolve to (kewei, #4238
# review) -- a structural grep for the argument NAME cannot tell a correct
# variable from a reverted one, since both are simple `"$var"` references.
tmp2="$(mktemp -d)"; log2="$tmp2/bus.log"
cat > "$tmp2/py" << PY
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$log2"
PY
chmod +x "$tmp2/py"
ws="$tmp2/ws"; inbox="$ws/deliveries/w-test"
mkdir -p "$ws/tasks" "$inbox" "$ws/results"
payload="$ws/tasks/task-probe1.txt"
printf 'id: task-probe1\naccess_tier: owner\ntask: resolve me\n' > "$payload"
: > "$inbox/task-probe1.txt"
resolver="$tmp2/resolver.sh"
printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$payload" > "$resolver"; chmod +x "$resolver"
outfile="$tmp2/sweep.out"
set -m
# SUTANDO_PY, not SUTANDO_PY_BIN: watch-tasks-stream.sh resolves its own
# SUTANDO_PY_BIN from require_python() at startup (line ~100) and overwrites
# whatever the caller exported, so that name is not the override channel for
# a live watcher process -- only resolve_python()'s SUTANDO_PY check is.
SUTANDO_INBOX_RESOLVER="$resolver" SUTANDO_WORKSPACE_DIR="$ws" \
  SUTANDO_RESULTS_DIR="$ws/results" SUTANDO_INSTANCE=w-test SUTANDO_PY="$tmp2/py" \
  bash "$SRC/watch-tasks-stream.sh" "$inbox" > "$outfile" 2>"$tmp2/sweep.err" &
sweep_pid=$!
set +m
for _ in $(seq 1 40); do grep -q 'TASK_FILE:' "$outfile" 2>/dev/null && break; sleep 0.25; done
for _ in $(seq 1 30); do grep -q 'transition QUEUED' "$log2" 2>/dev/null && break; sleep 0.1; done
kill -TERM -"$sweep_pid" 2>/dev/null || kill -TERM "$sweep_pid" 2>/dev/null
wait "$sweep_pid" 2>/dev/null
queued_line="$(grep 'transition QUEUED' "$log2" 2>/dev/null | head -1)"
echo "  QUEUED call: ${queued_line:-<none>}"
case "$queued_line" in
  *"--task-file $payload "*) echo "PASS QUEUED keys on the resolved payload, not the sentinel" ;;
  *) echo "FAIL QUEUED did not name the resolved payload path: ${queued_line:-<none>}"; fail=1 ;;
esac
rm -rf "$tmp2"

# Behaviour: a required handler that fails publishes the scheduler's FAILED, and
# that row must key on the resolved payload too -- publish_terminal_failure used
# to re-derive $TASKS_DIR/$filename, the sentinel (kewei, #4238 round 5).
tmp3="$(mktemp -d)"; log3="$tmp3/bus.log"
# A pass-through SPY, not a stub: the failure path needs the real interpreter for
# util_paths.py (the sentinel) and the claim helpers before it ever reaches
# activity_bus.py; a stub that swallows those never publishes a failure at all.
cat > "$tmp3/py" << PY
#!/usr/bin/env bash
case " \$* " in *activity_bus.py*) printf '%s\n' "\$*" >> "$log3"; exit 0 ;; esac
exec python3 "\$@"
PY
chmod +x "$tmp3/py"
# --probe: 4 = must-handle (never the live core); any run: exit 1 = the handler failed.
cat > "$tmp3/handler.sh" << 'H'
#!/usr/bin/env bash
case " $* " in *" --probe "*) exit 4 ;; esac
exit 1
H
chmod +x "$tmp3/handler.sh"
ws3="$tmp3/ws"; inbox3="$ws3/deliveries/w-test"
mkdir -p "$ws3/tasks" "$inbox3" "$ws3/results"
payload3="$ws3/tasks/task-probe3.txt"
printf 'id: task-probe3\naccess_tier: team\ntask: fail me\n' > "$payload3"
: > "$inbox3/task-probe3.txt"
resolver3="$tmp3/resolver.sh"
printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$payload3" > "$resolver3"; chmod +x "$resolver3"
set -m
SUTANDO_INBOX_RESOLVER="$resolver3" SUTANDO_TASK_EVENT_HANDLER="$tmp3/handler.sh" SUTANDO_WORKSPACE_DIR="$ws3" \
  SUTANDO_RESULTS_DIR="$ws3/results" SUTANDO_INSTANCE=w-test SUTANDO_PY="$tmp3/py" \
  bash "$SRC/watch-tasks-stream.sh" "$inbox3" > "$tmp3/sweep.out" 2>"$tmp3/sweep.err" &
sweep3=$!
set +m
for _ in $(seq 1 60); do grep -q 'transition FAILED' "$log3" 2>/dev/null && break; sleep 0.25; done
kill -TERM -"$sweep3" 2>/dev/null || kill -TERM "$sweep3" 2>/dev/null
wait "$sweep3" 2>/dev/null
failed_line="$(grep 'transition FAILED' "$log3" 2>/dev/null | head -1)"
echo "  FAILED call: ${failed_line:-<none>}"
case "$failed_line" in
  *"--task-file $payload3 "*) echo "PASS FAILED keys on the resolved payload, not the sentinel" ;;
  *) echo "FAIL FAILED did not name the resolved payload path: ${failed_line:-<none>} (stderr: $(tail -2 "$tmp3/sweep.err" 2>/dev/null))"; fail=1 ;;
esac
rm -rf "$tmp3"

[ "$fail" -eq 0 ] && echo "watch-tasks-dispatch-activity: PASS" || { echo "watch-tasks-dispatch-activity: FAIL"; exit 1; }
