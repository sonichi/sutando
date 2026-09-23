#!/usr/bin/env bash
# A live session holder whose readiness sentinel is gone (or names another pid)
# is re-stamped by the next session start, which then yields as covered; and a
# named workspace must contain the inbox, or the start is refused.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-restamp.XXXXXX")"
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
start_inst() {  # start_inst <ws> <inbox> <instance> <name>; prints the pid
  local ws="$1" inbox="$2" inst="$3" name="$4"
  env -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_INSTANCE_ID="$inst" SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$inbox" --role session --inbox "$inbox" > "$WORK/$name.out" 2> "$WORK/$name.err" &
  echo $!
}
start() {  # start <ws-env> <inbox> <name> [args]; prints the pid
  local ws="$1" inbox="$2" name="$3"; shift 3
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      ${ws:+SUTANDO_WORKSPACE_DIR="$ws"} PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$inbox" --role session --inbox "$inbox" "$@" > "$WORK/$name.out" 2> "$WORK/$name.err" &
  echo $!
}
ready() { local i; for i in $(seq 1 150); do [ "$(cat "$1"/state/*.pid 2>/dev/null | head -1)" = "$2" ] && return 0; sleep 0.1; done; return 1; }
# "unknown" is an unobservable process table (a transient ps failure under load), not a verdict: ask again.
role_present() { local i v; for i in 1 2 3 4 5; do v="$(python3 "$REPO/src/watcher_identity.py" role-present session --inbox "$1/tasks" --ready "$1/state" 2>/dev/null)"; case "$v" in yes|no) printf '%s' "$v"; return 0 ;; esac; sleep 0.3; done; printf '%s' "${v:-unknown}"; }

echo "watch-tasks-stream unready holder:"
WS="$WORK/ws"; mkdir -p "$WS/tasks" "$WS/state"
h="$(start "$WS" "$WS/tasks" holder)"; PIDS+=("$h")
ready "$WS" "$h"; check "(setup) a session holder is ready (sentinel names pid $h)" $? "$(tail -2 "$WORK/holder.err" | tr '\n' '|')"
SENT="$(ls "$WS"/state/*.pid | head -1)"

# (a) the sentinel is gone (a test watcher took it with it): the probe says no
rm -f "$SENT"
rp="$(role_present "$WS")"
[ "$rp" = "no" ] && alive "$h"; check "(a) with its sentinel gone the live holder reads as unready" $? "role-present=$rp holder-alive=$(alive "$h" && echo yes || echo no) state=$(ls "$WS/state" | tr '\n' ' ')"
n="$(start "$WS" "$WS/tasks" second)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
! alive "$n"; check "(a) a new session start yields to the live holder (exits)" $?
grep -q '^WATCHER_HELD: ' "$WORK/second.out"; check "(a) ...as covered (WATCHER_HELD on stdout)" $?
[ "$(cat "$SENT" 2>/dev/null)" = "$h" ]; check "(a) ...and re-stamped the sentinel with the HOLDER's pid, not its own" $? "sentinel: $(cat "$SENT" 2>/dev/null)"
grep -q "re-stamped .*/$(basename "$SENT") for live holder pid $h (it named '<nothing>' before)" "$WORK/second.err"; check "(a) ...and said so on stderr" $? "$(tail -3 "$WORK/second.err" | tr '\n' '|')"
[ "$(role_present "$WS")" = "yes" ]; check "(a) ...so the probe answers yes again" $?
alive "$h"; check "(a) ...with the holder untouched" $?

# (b) the sentinel names a dead pid (a test watcher's): same re-stamp
echo 999999 > "$SENT"
[ "$(role_present "$WS")" = "no" ]; check "(b) a sentinel naming a dead pid reads as unready" $?
n="$(start "$WS" "$WS/tasks" third)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
[ "$(cat "$SENT" 2>/dev/null)" = "$h" ] && [ "$(role_present "$WS")" = "yes" ]; check "(b) the next session start re-stamps it with the holder's pid (probe yes)" $? "sentinel: $(cat "$SENT" 2>/dev/null); $(tail -2 "$WORK/third.err" | tr '\n' '|')"
grep -q "(it named '999999' before)" "$WORK/third.err"; check "(b) ...naming what it found" $?

# (c) a ready holder is left alone: no re-stamp line, sentinel unchanged
# Content, not mtime: a 1 s granularity clock makes an mtime comparison a race,
# and what must not change is WHOSE pid the sentinel names.
before="$(cat "$SENT")"
n="$(start "$WS" "$WS/tasks" fourth)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
[ "$(cat "$SENT")" = "$before" ] && [ "$before" = "$h" ] && ! grep -q 're-stamped' "$WORK/fourth.err"; check "(c) a ready holder's sentinel is not rewritten by a covered start" $? "before=$before now=$(cat "$SENT") | $(tail -2 "$WORK/fourth.err" | tr '\n' '|')"

# (c2) A sentinel naming a LIVE pid is never overwritten, even by its own seat.
n="$(start "$WS" "$WS/tasks" fifth)"; PIDS+=("$n")
for i in $(seq 1 150); do [ "$(cat "$SENT" 2>/dev/null)" != "$h" ] && break; alive "$n" || break; sleep 0.1; done
live_other="$(cat "$SENT" 2>/dev/null)"
echo "$$" > "$SENT"        # a live pid that is neither the holder nor a watcher
n="$(start "$WS" "$WS/tasks" sixth)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
[ "$(cat "$SENT")" = "$$" ]; check "(c2) a sentinel naming a live pid is left alone" $? "now=$(cat "$SENT") | $(tail -2 "$WORK/sixth.err" | tr '\n' '|')"
grep -q "not re-stamping .* it names live pid $$" "$WORK/sixth.err"; check "(c2) ...and says why" $? "$(tail -2 "$WORK/sixth.err" | tr '\n' '|')"
echo "$h" > "$SENT"

# (d) Only this seat's own sentinel is written: a start whose identity is not the
#     inbox's owner leaves the holder unready rather than mislabelling a sentinel.
WS2="$WORK/ws2"; mkdir -p "$WS2/deliveries/w1" "$WS2/state"
hw="$(start_inst "$WS2" "$WS2/deliveries/w1" w1 holderw)"; PIDS+=("$hw")
for i in $(seq 1 150); do [ -n "$(ls "$WS2"/state/*+w1.pid 2>/dev/null)" ] && break; sleep 0.1; done
SENT2="$(ls "$WS2"/state/*+w1.pid 2>/dev/null | head -1)"
[ -n "$SENT2" ] && [ "$(cat "$SENT2")" = "$hw" ]; check "(d) a worker holder is ready on its own inbox" $? "state=$(ls "$WS2/state" | tr '\n' ' ')"
rm -f "$SENT2"
n="$(start_inst "$WS2" "$WS2/deliveries/w1" w2 stranger)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
! alive "$n"; check "(d) a start from another identity still yields to the holder" $?
[ -z "$(ls "$WS2"/state/*+w2.pid 2>/dev/null)" ] && [ -z "$(ls "$WS2"/state/*+w1.pid 2>/dev/null)" ]; check "(d) ...and wrote no sentinel of its own for the holder's pid" $? "state=$(for f in "$WS2/state"/*.pid; do printf '%s=%s ' "$(basename "$f")" "$(cat "$f" 2>/dev/null)"; done)"
! grep -q 're-stamped' "$WORK/stranger.err"; check "(d) ...and said nothing about re-stamping" $? "$(tail -2 "$WORK/stranger.err" | tr '\n' '|')"
# ...while the inbox's own identity does re-stamp it.
n="$(start_inst "$WS2" "$WS2/deliveries/w1" w1 owner2)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
[ "$(cat "$WS2"/state/*+w1.pid 2>/dev/null)" = "$hw" ]; check "(d) the inbox's own identity re-stamps the holder's pid" $? "$(tail -2 "$WORK/owner2.err" | tr '\n' '|')"


echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi
