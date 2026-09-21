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

# The QUEUE line: the TASK_FILE line stays byte-identical, and a second line
# `QUEUE: <n> pending after this` follows only when other owner tasks are waiting.
tmpq="$(mktemp -d)"; mkdir -p "$tmpq/ws/tasks" "$tmpq/ws/state"
printf 'id: task-q1\ntask: first\n' > "$tmpq/ws/tasks/task-q1.txt"
out1="$(TASKS_DIR="$tmpq/ws/tasks" bash -c 'source "$1"; emit_dispatch_task_file task-q1.txt' _ "$SRC/task-emit.sh" 2>/dev/null)"
[ "$out1" = "TASK_FILE: task-q1.txt" ] && echo "PASS alone in the queue: exactly the TASK_FILE line" || { echo "FAIL alone in the queue, stdout was: $out1"; fail=1; }
sleep 1
printf 'id: task-q2\ntask: second\n' > "$tmpq/ws/tasks/task-q2.txt"
printf 'id: task-cron-x\ntask: bookkeeping\n' > "$tmpq/ws/tasks/task-cron-x.txt"
out2="$(TASKS_DIR="$tmpq/ws/tasks" bash -c 'source "$1"; emit_dispatch_task_file task-q2.txt' _ "$SRC/task-emit.sh" 2>/dev/null)"
[ "$(printf '%s\n' "$out2" | head -1)" = "TASK_FILE: task-q2.txt" ] && echo "PASS the first line is byte-identical with a queue" || { echo "FAIL first line changed: $out2"; fail=1; }
[ "$(printf '%s\n' "$out2" | sed -n 2p)" = "QUEUE: 1 pending after this" ] && echo "PASS QUEUE names the one other owner task (bookkeeping excluded)" || { echo "FAIL QUEUE line: $out2"; fail=1; }
[ "$(printf '%s\n' "$out2" | wc -l | tr -d ' ')" = "2" ] && echo "PASS nothing after the QUEUE line" || { echo "FAIL extra lines: $out2"; fail=1; }
grep -q '"depth": 2' "$tmpq/ws/state/task-queue.json" 2>/dev/null && echo "PASS the snapshot is refreshed on dispatch" || { echo "FAIL no fresh state/task-queue.json: $(cat "$tmpq/ws/state/task-queue.json" 2>/dev/null)"; fail=1; }
# A stubbed interpreter that prints nothing: no line it cannot vouch for.
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmpq/py"; chmod +x "$tmpq/py"
out3="$(TASKS_DIR="$tmpq/ws/tasks" SUTANDO_PY_BIN="$tmpq/py" bash -c 'source "$1"; emit_dispatch_task_file task-q2.txt' _ "$SRC/task-emit.sh" 2>/dev/null)"
[ "$out3" = "TASK_FILE: task-q2.txt" ] && echo "PASS no counter, no QUEUE line" || { echo "FAIL a QUEUE line without a count: $out3"; fail=1; }
# tasks/ that cannot be listed (x-only: the task file still opens by path): no QUEUE line, not
# "QUEUE: 0", and the snapshot on disk is left exactly as it was. Root lists anything: skipped there.
if [ "$(id -u)" = "0" ]; then
	echo "SKIP unreadable tasks/ (root)"
else
	snap_before="$(cat "$tmpq/ws/state/task-queue.json")"
	chmod 100 "$tmpq/ws/tasks"
	out4="$(TASKS_DIR="$tmpq/ws/tasks" bash -c 'source "$1"; emit_dispatch_task_file task-q2.txt' _ "$SRC/task-emit.sh" 2>/dev/null)"
	chmod 755 "$tmpq/ws/tasks"
	[ "$out4" = "TASK_FILE: task-q2.txt" ] && echo "PASS unreadable tasks/: the dispatch goes on with no QUEUE line" || { echo "FAIL unreadable tasks/, stdout was: $out4"; fail=1; }
	[ "$(cat "$tmpq/ws/state/task-queue.json")" = "$snap_before" ] && echo "PASS unreadable tasks/: the previous snapshot is untouched" || { echo "FAIL the snapshot changed: $(cat "$tmpq/ws/state/task-queue.json")"; fail=1; }
fi
rm -rf "$tmpq"

# Behaviour, not text: a grep for the argument NAME cannot tell the resolved
# variable from a reverted one -- both are plain `"$var"`. Assert the row.
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
# SUTANDO_PY, not SUTANDO_PY_BIN: the watcher re-resolves SUTANDO_PY_BIN at
# startup and overwrites the caller's, so only SUTANDO_PY overrides it.
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

# A failing required handler publishes FAILED, and that row must name the
# resolved payload: the failure path used to re-derive the sentinel instead.
tmp3="$(mktemp -d)"; log3="$tmp3/bus.log"
# A pass-through spy, not a stub: the failure path needs the real interpreter
# first, and a stub that swallows those calls never publishes a failure at all.
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
