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
