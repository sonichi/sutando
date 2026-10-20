#!/bin/bash
# A pointer fswatch (alive, silent: an fseventsd stall) never reports is announced by catch-up
# once, also when renamed in with an old mtime, never if answered; a stale inbox does not slow it.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

# Hermetic on a pool host: no live seat's resolver, handler or workspace leaks in.
for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
# Not `kill -TERM -$PID`: without job control the watcher shares this test's group.
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null; fi; rm -rf "$TMP"' EXIT

CATCHUP=2
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$INBOX" "$TMP/bin" "$TMP/tmp"
# Alive and blocked, reporting nothing: what a stalled fseventsd looks like from here.
printf '#!/bin/sh\nexec tail -f /dev/null\n' > "$TMP/bin/fswatch"
if [ "${1:-}" = "--noisy" ]; then
  printf '#!/bin/sh\nwhile :; do printf "/unrelated-event\\n"; sleep 0.1; done\n' > "$TMP/bin/fswatch"
fi
chmod +x "$TMP/bin/fswatch"
# Once armed, a cleanup step outlasts any catch-up read timeout still pending.
SLOW="$TMP/slow-cleanup"
printf '#!/bin/sh\n[ -e "%s" ] && sleep %s\nexec %s "$@"\n' "$SLOW" "$(( CATCHUP + 1 ))" "$(command -v pgrep)" > "$TMP/bin/pgrep"
chmod +x "$TMP/bin/pgrep"
# A pointer's payload is the same-named file in tasks/; rc 3 is "no payload".
RESOLVER="$TMP/resolver.sh"
cat > "$RESOLVER" <<EOF
#!/bin/sh
p='$WS/tasks/'"\$(basename "\$1")"
[ -f "\$p" ] || exit 3
printf '%s\n' "\$p"
EOF
chmod +x "$RESOLVER"
# A long-lived inbox: answered pointers nothing retired. The startup sweep pays
# for them once; a catch-up pass must not pay for them again.
STALE=40
OLD="$(date -v-1H +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 hour ago' +%Y%m%d%H%M.%S)"
for i in $(seq 1 "$STALE"); do
  printf 'id: task-stale%s\ntask: old\n' "$i" > "$WS/tasks/task-stale$i.txt"
  printf 'done.\n' > "$WS/results/task-stale$i.txt"
  : > "$INBOX/task-stale$i.txt"
  touch -t "$OLD" "$INBOX/task-stale$i.txt"
done

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
PATH="$TMP/bin:$PATH" TMPDIR="$TMP/tmp" \
SUTANDO_WORKSPACE_DIR="$WS" \
SUTANDO_RESULTS_DIR="$WS/results" \
SUTANDO_INBOX_RESOLVER="$RESOLVER" \
SUTANDO_WATCHER_CATCHUP_SECONDS="$CATCHUP" \
bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$OUT" 2>"$ERR" &
WATCHER_PID=$!

# The sentinel is stamped after the startup sweep, so anything dropped after it
# can only be found by catch-up.
t0=$SECONDS
for _ in $(seq 1 1200); do
  compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null && break
  sleep 0.1
done
compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null
check $? "the watcher subscribed (sentinel stamped after the startup sweep of $STALE stale pointers, $(( SECONDS - t0 ))s)"
stale_refusals="$(grep -c 'already answered, not dispatching again: .*task-stale' "$ERR")"

# An answered task: its result is live before its pointer lands.
printf 'id: task-answered\naccess_tier: owner\ntask: already done\n' > "$WS/tasks/task-answered.txt"
printf 'done.\n' > "$WS/results/task-answered.txt"
: > "$INBOX/task-answered.txt"
# A live task: payload first, then the pointer, as the pool writer delivers.
printf 'id: task-catch-up\naccess_tier: owner\ntask: recover me\n' > "$WS/tasks/task-catch-up.txt"
dropped=$(date +%s)
: > "$INBOX/task-catch-up.txt"

LIVE="TASK_FILE: $WS/tasks/task-catch-up.txt"
seen=""
for _ in $(seq 1 $(( (CATCHUP + 4) * 10 ))); do
  grep -qF "$LIVE" "$OUT" && { seen=$(date +%s); break; }
  sleep 0.1
done
took="never"; [ -z "$seen" ] || took="$(( seen - dropped ))s"
[ -n "$seen" ] && [ $(( seen - dropped )) -le $(( CATCHUP + 3 )) ]
check $? "a pointer fswatch never reported is announced within ${CATCHUP}s + 3s margin (took $took)"

# A pointer written elsewhere an hour ago and renamed into the inbox: its mtime is old,
# only its ctime says it just arrived.
printf 'id: task-renamed\naccess_tier: owner\ntask: moved in\n' > "$WS/tasks/task-renamed.txt"
: > "$TMP/task-renamed.txt"
touch -t "$OLD" "$TMP/task-renamed.txt"
dropped=$(date +%s)
mv "$TMP/task-renamed.txt" "$INBOX/task-renamed.txt"
RENAMED="TASK_FILE: $WS/tasks/task-renamed.txt"
seen=""
for _ in $(seq 1 $(( (CATCHUP + 4) * 10 ))); do
  grep -qF "$RENAMED" "$OUT" && { seen=$(date +%s); break; }
  sleep 0.1
done
took="never"; [ -z "$seen" ] || took="$(( seen - dropped ))s"
[ -n "$seen" ] && [ $(( seen - dropped )) -le $(( CATCHUP + 3 )) ]
check $? "a pointer renamed into the inbox with an hour-old mtime is announced within ${CATCHUP}s + 3s margin (took $took)"

# Three more catch-up intervals: each re-sweeps, none may re-announce.
sleep $(( CATCHUP * 3 + 1 ))
[ "$(grep -cF "$LIVE" "$OUT")" = "1" ]
check $? "it is announced exactly once across further catch-up sweeps (got $(grep -cF "$LIVE" "$OUT"))"
! grep -q 'task-answered' "$OUT"
check $? "a pointer whose task already has a result is never announced"
[ "$(grep -c 'already answered, not dispatching again: .*task-answered' "$ERR")" = "1" ]
check $? "...the sweep reached it, refused it, and never re-examined it (refusals: $(grep -c 'task-answered' "$ERR"))"
# Each stale pointer cost the startup sweep two subprocesses; the catch-up
# passes since must have cost nothing for them.
[ "$(grep -c 'already answered, not dispatching again: .*task-stale' "$ERR")" = "$stale_refusals" ]
check $? "catch-up never re-examines the $STALE stale pointers the startup sweep already refused (refusals: $stale_refusals then $(grep -c 'task-stale' "$ERR"))"
kill -0 "$WATCHER_PID" 2>/dev/null
check $? "the watcher is still live (catch-up runs under the subscription, not instead of it)"

# bash 3.2's read -t alarm must not cut the TERM trap short and leave the watcher running.
: > "$SLOW"
kill -TERM "$WATCHER_PID"
for _ in $(seq 1 $(( (CATCHUP + 1) * 3 * 10 ))); do kill -0 "$WATCHER_PID" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  pkill -9 -P "$WATCHER_PID" 2>/dev/null; kill -9 "$WATCHER_PID" 2>/dev/null; trc=137
else
  wait "$WATCHER_PID"; trc=$?
fi
WATCHER_PID=""
[ "$trc" = "0" ]
check $? "TERM during a pending catch-up read still stops the watcher through its trap (exit $trc)"
! compgen -G "$TMP/tmp/sutando-task-watch.*" > /dev/null
check $? "the watcher's runtime dir is gone after exit (left: $(ls -d "$TMP/tmp"/sutando-task-watch.* 2>/dev/null | wc -l | tr -d ' '))"

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
