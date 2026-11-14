#!/usr/bin/env bash
# Two session starters on one fresh inbox at the same instant end as exactly one
# announcer, whichever interleaving; the per-inbox start lock serializes the scan
# through the sentinel stamp, and a lock left by a dead pid is taken over.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-startlock.XXXXXX")"
mkdir -p "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
# A mv that holds the FIRST takeover rename once ($SUTANDO_TEST_MV_ONCE names the
# once-flag) and logs every takeover rename: (b2) uses the hold to take the lock
# over as a live holder, so the held rename moves a LIVE lock every run.
printf '%s\n' '#!/bin/bash' \
  'case "$*" in *.dead.*) [ -n "${SUTANDO_TEST_MV_ONCE:-}" ] && mkdir "$SUTANDO_TEST_MV_ONCE" 2>/dev/null && sleep "${SUTANDO_TEST_MV_DELAY:-0.5}" ;; esac' \
  '[ -n "${SUTANDO_TEST_MV_LOG:-}" ] && printf "%s -> %s (pid in source: %s)\n" "$1" "$2" "$(cat "$1/pid" 2>/dev/null)" >> "$SUTANDO_TEST_MV_LOG"' \
  'exec /bin/mv "$@"' > "$WORK/stubbin/mv"
chmod +x "$WORK/stubbin/mv"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
# Each watcher in its own session (its cleanup runs `kill 0`), identity stripped.
start() {  # start <ws> <name> [extra args]; prints the pid
  local ws="$1" name="$2"; shift 2
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$ws/tasks" --role session --inbox "$ws/tasks" "$@" > "$ws/$name.out" 2> "$ws/$name.err" &
  echo $!
}
settle() {  # settle <ws> <pid> <pid>: wait until each is either exited or has stamped
  local ws="$1"; shift
  local i p done
  for i in $(seq 1 100); do
    done=1
    for p in "$@"; do
      if alive "$p" && [ "$(cat "$ws"/state/*.pid 2>/dev/null | head -1)" != "$p" ]; then done=0; fi
    done
    [ "$done" = 1 ] && return 0
    sleep 0.1
  done
  return 1
}

echo "watch-tasks-stream start lock:"
N=6; both_ran=0; none_ran=0; one_ran=0
for round in $(seq 1 $N); do
  WS="$WORK/r$round"; mkdir -p "$WS/tasks" "$WS/state"
  a="$(start "$WS" a)"; b="$(start "$WS" b)"; PIDS+=("$a" "$b")
  settle "$WS" "$a" "$b"
  live=0; alive "$a" && live=$((live+1)); alive "$b" && live=$((live+1))
  sentinels="$(ls "$WS"/state/*.pid 2>/dev/null | wc -l | tr -d ' ')"
  held="$(cat "$WS"/a.out "$WS"/b.out 2>/dev/null | grep -c '^WATCHER_HELD:')"
  case "$live" in 2) both_ran=$((both_ran+1)) ;; 0) none_ran=$((none_ran+1)) ;; 1) one_ran=$((one_ran+1)) ;; esac
  echo "  round $round: live=$live sentinels=$sentinels held-lines=$held"
  [ "$live" = 1 ] && [ "$sentinels" = 1 ] && [ "$held" = 1 ] || { echo "    a.err: $(tail -2 "$WS/a.err" | tr '\n' '|')"; echo "    b.err: $(tail -2 "$WS/b.err" | tr '\n' '|')"; }
  for p in "$a" "$b"; do kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; done
  sleep 0.3
done
check "(a) $N simultaneous pairs: one announcer every time (both ran: $both_ran, none ran: $none_ran)" "$([ "$one_ran" = "$N" ] && echo 0 || echo 1)"

# (b) A lock dir left by a dead pid is taken over, not waited out.
WS="$WORK/stale"; mkdir -p "$WS/tasks" "$WS/state"
key="$(printf '%s' "$(cd "$WS/tasks" && pwd -P)" | cksum | cut -d' ' -f1)"
LOCK="$WS/state/watch-tasks-stream.start-$key.lock"
mkdir -p "$LOCK"; echo 999999 > "$LOCK/pid"
t0=$(date +%s)
p="$(start "$WS" c)"; PIDS+=("$p")
for i in $(seq 1 100); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
took=$(( $(date +%s) - t0 ))
check "(b) a lock left by a dead pid is taken over: watcher stamped in ${took}s" "$([ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && [ "$took" -lt 5 ] && echo 0 || echo 1)" "$(tail -2 "$WS/c.err" | tr '\n' '|')"
grep -q 'left by dead pid 999999; taking it over' "$WS/c.err"; check "(b) ...and says so on stderr" $?
[ ! -d "$LOCK" ]; check "(b) ...and the lock is released after the stamp" $?
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3

# (b2) The steal window, forced: a starter judges the lock dead, and before its
#      rename lands (the mv shim holds it 1 s) the TEST takes the lock over as a
#      live holder. The delayed rename then moves a LIVE lock, which must be given
#      back by rename, and the starter must not stamp while the test holds it.
WS="$WORK/steal"; mkdir -p "$WS/tasks" "$WS/state"
key="$(printf '%s' "$(cd "$WS/tasks" && pwd -P)" | cksum | cut -d' ' -f1)"
LOCK="$WS/state/watch-tasks-stream.start-$key.lock"
mkdir -p "$LOCK"; echo 999999 > "$LOCK/pid"
export SUTANDO_TEST_MV_ONCE="$WS/mv-once" SUTANDO_TEST_MV_LOG="$WS/mv.log" SUTANDO_TEST_MV_DELAY=1
p="$(start "$WS" g)"; PIDS+=("$p")
for i in $(seq 1 50); do [ -d "$WS/mv-once" ] && break; sleep 0.1; done
[ -d "$WS/mv-once" ]; check "(b2) the starter judged the lock dead and its rename is held" $?
# The other taker: replace the dead lock with this test's own live lock.
/bin/mv "$LOCK" "$LOCK.gone" && rm -rf "$LOCK.gone"; mkdir "$LOCK"; echo $$ > "$LOCK/pid"
sleep 1.5
unset SUTANDO_TEST_MV_ONCE SUTANDO_TEST_MV_LOG SUTANDO_TEST_MV_DELAY
grep -E "\.lock -> .*\.dead\.$p \(pid in source: $$\)" "$WS/mv.log" >/dev/null; check "(b2) ...the delayed rename moved the test's LIVE lock" $? "$(sed "s#$WS/state/##g" "$WS/mv.log" | tr '\n' '|')"
grep -E "\.dead\.$p -> .*\.lock \(pid in source: $$\)" "$WS/mv.log" >/dev/null; check "(b2) ...and gave it back by rename (a mutant that deletes it fails here)" $? "$(sed "s#$WS/state/##g" "$WS/mv.log" | tr '\n' '|')"
[ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ]; check "(b2) ...so the test still holds the lock" $?
[ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ] && alive "$p"; check "(b2) ...and the starter has not stamped while the lock is held (waiting, alive)" $?
rm -rf "$LOCK"
for i in $(seq 1 100); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
[ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ]; check "(b2) ...and stamps once the lock is released" $?
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3

# (b3) A lock with no pid file (a winner died between mkdir and its pid write)
#      is reclaimed after a few seconds, not waited out for the full timeout.
WS="$WORK/nopid"; mkdir -p "$WS/tasks" "$WS/state"
key="$(printf '%s' "$(cd "$WS/tasks" && pwd -P)" | cksum | cut -d' ' -f1)"
LOCK="$WS/state/watch-tasks-stream.start-$key.lock"
mkdir -p "$LOCK"; touch -t "$(date -v-1M +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 minute ago' +%Y%m%d%H%M.%S)" "$LOCK"
t0=$(date +%s)
p="$(start "$WS" i)"; PIDS+=("$p")
for i in $(seq 1 150); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
took=$(( $(date +%s) - t0 ))
check "(b3) a pid-less lock older than a few seconds is reclaimed (stamped in ${took}s, not 30)" "$([ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && [ "$took" -lt 10 ] && echo 0 || echo 1)" "$(tail -2 "$WS/i.err" | tr '\n' '|')"
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3

# (b4) A brand-new pid-less lock (a winner between mkdir and its pid write) is
#      NOT reclaimed: with the takeover rename held, the test plants a fresh
#      pid-less lock in the window; the moved dir is young, so it is given back.
WS="$WORK/fresh"; mkdir -p "$WS/tasks" "$WS/state"
key="$(printf '%s' "$(cd "$WS/tasks" && pwd -P)" | cksum | cut -d' ' -f1)"
LOCK="$WS/state/watch-tasks-stream.start-$key.lock"
# The starter judges an OLD pid-less lock (so its "dead pid" is the empty string).
mkdir -p "$LOCK"; touch -t "$(date -v-1M +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 minute ago' +%Y%m%d%H%M.%S)" "$LOCK"
export SUTANDO_TEST_MV_ONCE="$WS/mv-once" SUTANDO_TEST_MV_LOG="$WS/mv.log" SUTANDO_TEST_MV_DELAY=1
p="$(start "$WS" j)"; PIDS+=("$p")
for i in $(seq 1 50); do [ -d "$WS/mv-once" ] && break; sleep 0.1; done
/bin/mv "$LOCK" "$LOCK.gone" && rm -rf "$LOCK.gone"; mkdir "$LOCK"   # a winner in its pid gap
sleep 1.5
unset SUTANDO_TEST_MV_ONCE SUTANDO_TEST_MV_LOG SUTANDO_TEST_MV_DELAY
grep -E "\.dead\.$p -> .*\.lock \(pid in source: \)" "$WS/mv.log" >/dev/null; check "(b4) a young pid-less lock moved in the window is given back, not reclaimed" $? "$(sed "s#$WS/state/##g" "$WS/mv.log" | tr '\n' '|')"
[ -d "$LOCK" ] && [ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ] && alive "$p"; check "(b4) ...the lock stands and the starter waits" $?
rm -rf "$LOCK"
for i in $(seq 1 100); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
[ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ]; check "(b4) ...and stamps once it is released" $?
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3

# (c) A lock held by a LIVE pid past the timeout does not strand the inbox: the
#     start proceeds without the lock and says so.
WS="$WORK/timeout"; mkdir -p "$WS/tasks" "$WS/state"
key="$(printf '%s' "$(cd "$WS/tasks" && pwd -P)" | cksum | cut -d' ' -f1)"
LOCK="$WS/state/watch-tasks-stream.start-$key.lock"
mkdir -p "$LOCK"; echo $$ > "$LOCK/pid"
export SUTANDO_WATCHER_START_LOCK_TIMEOUT_S=1
p="$(start "$WS" d)"; PIDS+=("$p")
unset SUTANDO_WATCHER_START_LOCK_TIMEOUT_S
for i in $(seq 1 100); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
check "(c) a lock held by a live pid past the timeout: the start proceeds without it" "$([ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && echo 0 || echo 1)" "$(tail -2 "$WS/d.err" | tr '\n' '|')"
grep -q 'held by pid .* for 1s; starting without it' "$WS/d.err"; check "(c) ...and says so on stderr" $?
[ -d "$LOCK" ] && [ "$(cat "$LOCK/pid")" = "$$" ]; check "(c) ...and leaves the other holder's lock alone" $?
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3
rm -rf "$LOCK"

# (d) A covered exit (second start over a stamped holder) leaves no lock behind.
WS="$WORK/covered"; mkdir -p "$WS/tasks" "$WS/state"
p="$(start "$WS" e)"; PIDS+=("$p")
for i in $(seq 1 100); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$p" ] && break; sleep 0.1; done
q="$(start "$WS" f)"; PIDS+=("$q")
for i in $(seq 1 100); do alive "$q" || break; sleep 0.1; done
check "(d) a second start over a stamped holder exits 0 as covered" "$(alive "$q" && echo 1 || echo 0)"
[ -z "$(ls -d "$WS"/state/watch-tasks-stream.start-*.lock 2>/dev/null)" ]; check "(d) ...and leaves no start lock behind" $?
alive "$p"; check "(d) ...and the holder is untouched" $?
kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; sleep 0.3

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi
