#!/bin/bash
# A sentinel can be visible to fswatch before its payload's own separate write
# lands -- no ordering guarantee across the two files. dispatch_task() must
# retry a failed resolve briefly rather than silently drop the task forever,
# and must still give up (not hang) when the entry is permanently unresolvable.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
PAYLOAD="$WS/tasks/task-probe1.txt"
printf 'id: task-probe1\ntask: resolve me\n' > "$PAYLOAD"
: > "$INBOX/task-probe1.txt"          # the sentinel: zero bytes, by design

mk() { printf '%s\n' "$2" > "$TMP/$1"; chmod +x "$TMP/$1"; printf '%s' "$TMP/$1"; }

run_sweep() {
  local resolver="${1:-}" outfile="$TMP/sweep.out" pid i
  : > "$outfile"; : > "$TMP/sweep.err"
  set -m
  SUTANDO_INBOX_RESOLVER="$resolver" SUTANDO_WORKSPACE_DIR="$WS" \
    SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE=w-test \
    bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" > "$outfile" 2>"$TMP/sweep.err" &
  pid=$!
  set +m
  for i in $(seq 1 40); do grep -q 'TASK_FILE:' "$outfile" 2>/dev/null && break; sleep 0.25; done
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  grep 'TASK_FILE:' "$outfile" 2>/dev/null | head -1
}

# 1. Transient race: the resolver fails on its first two calls (payload "not
#    yet visible") and succeeds on the third. This is what a live-event
#    dispatch used to lose permanently -- one failed resolve, no retry.
COUNT1="$TMP/count1"
FLAKY="$(mk flaky.sh "#!/bin/sh
n=0
[ -f '$COUNT1' ] && n=\$(cat '$COUNT1')
n=\$((n+1))
printf '%s' \"\$n\" > '$COUNT1'
if [ \"\$n\" -lt 3 ]; then
  echo 'not yet' >&2
  exit 1
fi
printf '%s\\n' \"$PAYLOAD\"")"

start=$(date +%s.%N 2>/dev/null || date +%s)
line="$(run_sweep "$FLAKY")"
end=$(date +%s.%N 2>/dev/null || date +%s)
echo "  watcher emitted: ${line:-<nothing>}"
[ "$line" = "TASK_FILE: $PAYLOAD" ]
check $? "a resolver that succeeds on its 3rd call is retried until it resolves"

calls="$(cat "$COUNT1" 2>/dev/null || echo 0)"
[ "$calls" = "3" ]
check $? "...exactly 3 resolve attempts were made (retry actually fired, no more)"

elapsed="$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.2f", e-s}' 2>/dev/null || echo "?")"
echo "  elapsed: ${elapsed}s"
awk -v s="$start" -v e="$end" 'BEGIN{exit !(e-s >= 0.3)}' 2>/dev/null
check $? "...and the retries were spaced out (backoff), not a tight spin (elapsed ${elapsed}s)"

# 2. Permanent failure: a resolver that never succeeds must still give up --
#    bounded, not an infinite retry loop -- and the task stays undispatched
#    rather than being dispatched against a payload that was never resolved.
COUNT2="$TMP/count2"
NEVER="$(mk never.sh "#!/bin/sh
n=0
[ -f '$COUNT2' ] && n=\$(cat '$COUNT2')
n=\$((n+1))
printf '%s' \"\$n\" > '$COUNT2'
exit 1")"

start=$(date +%s)
line_never="$(run_sweep "$NEVER")"
elapsed_never=$(( $(date +%s) - start ))
echo "  with a permanently-refusing resolver: ${line_never:-<nothing>} (elapsed ${elapsed_never}s)"
[ -z "$line_never" ]
check $? "a permanently unresolvable sentinel is never dispatched"

calls2="$(cat "$COUNT2" 2>/dev/null || echo 0)"
[ "$calls2" = "3" ]
check $? "...it is retried exactly 3 times, then abandoned (bounded, not infinite)"

grep -q 'did not resolve.*after 3 attempts' "$TMP/sweep.err"
check $? "...and the give-up is logged, not a silent drop"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
