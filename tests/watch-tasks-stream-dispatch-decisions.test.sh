#!/bin/bash
# Every decision after the task-handler probe names itself on stderr (announced, handled, held with its
# owner), and a task still in the inbox a catch-up pass later that was never admitted is dispatched again.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

unset TMUX CLAUDE_CONFIG_DIR
for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
HOLDER_PID=""
cleanup() {
  [ -z "$WATCHER_PID" ] || { kill -TERM "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null; }
  [ -z "$HOLDER_PID" ] || kill "$HOLDER_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

CATCHUP=2
WS="$TMP/ws"; INBOX="$WS/tasks"
mkdir -p "$INBOX" "$WS/results" "$WS/state" "$TMP/bin" "$TMP/tmp"
# Alive and silent: every task here is found by catch-up, so the test needs no live event source.
printf '#!/bin/sh\nexec tail -f /dev/null\n' > "$TMP/bin/fswatch"
chmod +x "$TMP/bin/fswatch"
# The handler's probe and run exit codes come from files the cases set; each run is recorded.
HANDLER="$TMP/handler.sh"
cat > "$HANDLER" <<EOF
#!/bin/bash
for a in "\$@"; do [ "\$a" = --probe ] && exit "\$(cat "$TMP/probe-rc")"; done
echo run >> "$TMP/runs"
exit "\$(cat "$TMP/run-rc")"
EOF
chmod +x "$HANDLER"
echo 3 > "$TMP/probe-rc"; echo 0 > "$TMP/run-rc"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
PATH="$TMP/bin:$PATH" TMPDIR="$TMP/tmp" SUTANDO_WORKSPACE_DIR="$WS" SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_TASK_EVENT_HANDLER="$HANDLER" SUTANDO_WATCHER_CATCHUP_SECONDS="$CATCHUP" \
  bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$OUT" 2>"$ERR" &
WATCHER_PID=$!
for _ in $(seq 1 300); do compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null && break; sleep 0.1; done

drop() { printf 'id: %s\naccess_tier: owner\ntask: t\n' "$1" > "$INBOX/$1.txt"; }
wait_err() { for _ in $(seq 1 $(( (CATCHUP + 4) * 10 ))); do grep -qF -- "$1" "$ERR" && return 0; sleep 0.1; done; return 1; }

# probe rc 3: declined, announced.
drop task-declined
wait_err "dispatch task-declined.txt: probe rc=3: the handler declined it; announced to the live core"
check $? "probe rc=3 names the decline and the announcement"
grep -qF "TASK_FILE: task-declined.txt" "$OUT"; check $? "...and the task was announced"

# probe rc 0, run exit 0: handled, never announced.
echo 0 > "$TMP/probe-rc"
drop task-handled
wait_err "dispatch task-handled.txt: handled by the task handler (exit 0)"
check $? "a handled task says so"
grep -qF "dispatch task-handled.txt: probe rc=0: running the task handler" "$ERR"; check $? "...after naming the probe's rc"
! grep -qF "TASK_FILE: task-handled.txt" "$OUT"; check $? "...and is not announced"

# probe rc 0 while another live watcher holds the claim: held, naming that owner.
sleep 600 & HOLDER_PID=$!
CLAIMS="$WS/state/task-event-handler-claims"; mkdir -p "$CLAIMS"
printf '%s\nother\n%s\nfallback\n%s\n' "$HOLDER_PID" "$INBOX/task-claimed.txt" \
  "$(LC_ALL=C ps -o lstart= -p "$HOLDER_PID" | sed 's/^ *//;s/ *$//')" > "$CLAIMS/task-claimed.txt"
drop task-claimed
wait_err "dispatch task-claimed.txt: held: claimed by live watcher pid $HOLDER_PID"
check $? "a held task names the live watcher holding its claim"
kill "$HOLDER_PID" 2>/dev/null; wait "$HOLDER_PID" 2>/dev/null; HOLDER_PID=""

# A task dropped without a word (the shutdown gate) is dispatched again by a later pass once nothing stops it.
echo 3 > "$TMP/probe-rc"
: > "$WS/state/shutdown.sentinel"
drop task-stranded
sleep $(( CATCHUP * 3 ))
! grep -qF "TASK_FILE: task-stranded.txt" "$OUT"; check $? "(setup) nothing is announced while the shutdown gate stands"
rm -f "$WS/state/shutdown.sentinel"
wait_err "dispatch task-stranded.txt: re-dispatching: still in the inbox a catch-up pass later"
check $? "a task left unadmitted past its catch-up pass is re-dispatched, and says so"
for _ in $(seq 1 $(( (CATCHUP + 4) * 10 ))); do grep -qF "TASK_FILE: task-stranded.txt" "$OUT" && break; sleep 0.1; done
[ "$(grep -cF "TASK_FILE: task-stranded.txt" "$OUT")" = 1 ]; check $? "...and announced exactly once"
sleep $(( CATCHUP * 2 + 1 ))
[ "$(grep -cF "TASK_FILE: task-stranded.txt" "$OUT")" = 1 ] && [ "$(grep -cF "TASK_FILE: task-declined.txt" "$OUT")" = 1 ]
check $? "admitted tasks are never re-dispatched by later passes"
! grep -qF "dispatch task-handled.txt: re-dispatching" "$ERR"; check $? "...nor is a handled one"

# A worker watches delivery entries, not their resolved payloads. Silent fswatch
# makes each pass a catch-up-only path; neither a payload edit nor cksum failure
# may turn an admitted delivery into a new task.
kill -TERM "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null; WATCHER_PID=""
for checksum in normal unavailable; do
  WS="$TMP/worker-$checksum"; INBOX="$WS/deliveries/worker-test"
  mkdir -p "$INBOX" "$WS/tasks" "$WS/results" "$WS/state"
  OUT="$WS/out"; ERR="$WS/err"
  payload="$WS/tasks/task-payload.txt"
  printf 'id: task-payload\naccess_tier: owner\ntask: original\n' > "$payload"
  printf 'delivery entry\n' > "$INBOX/task-delivery.txt"
  cat > "$WS/resolve" <<EOF
#!/bin/bash
echo resolve >> "$WS/resolves"
printf '%s\n' "$payload"
EOF
  chmod +x "$WS/resolve"
  if [ "$checksum" = unavailable ]; then
    printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/cksum"; chmod +x "$TMP/bin/cksum"
  fi
  PATH="$TMP/bin:$PATH" TMPDIR="$TMP/tmp" SUTANDO_WORKSPACE_DIR="$WS" \
  SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE_ID=worker-test \
  SUTANDO_INBOX_RESOLVER="$WS/resolve" SUTANDO_WATCHER_CATCHUP_SECONDS="$CATCHUP" \
    bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$OUT" 2> "$ERR" &
  WATCHER_PID=$!
  for _ in $(seq 1 100); do grep -qF "TASK_FILE: $payload" "$OUT" && break; sleep 0.1; done
  [ "$(grep -cF "TASK_FILE: $payload" "$OUT")" = 1 ]; check $? "$checksum: resolved worker payload is admitted once"
  resolves="$(wc -l < "$WS/resolves" | tr -d ' ')"
  sleep $(( CATCHUP * 2 + 1 ))
  [ "$(wc -l < "$WS/resolves" | tr -d ' ')" = "$resolves" ]; check $? "$checksum: pending delivery does not rerun its resolver"
  printf 'payload edited while delivery is pending\n' >> "$payload"
  sleep $(( CATCHUP * 2 + 1 ))
  [ "$(grep -cF "TASK_FILE: $payload" "$OUT")" = 1 ]; check $? "$checksum: payload edit does not announce a second TASK_FILE"
  ! grep -qF 're-dispatching:' "$ERR"; check $? "$checksum: no false stranded decision is logged"
  if [ "$checksum" = unavailable ]; then
    grep -qF 'no usable checksum' "$ERR"; check $? "checksum failure exercised the admission fallback"
  fi
  kill -TERM "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null; WATCHER_PID=""
done

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ] || { tail -12 "$ERR" | sed 's/^/  err: /'; exit 1; }
