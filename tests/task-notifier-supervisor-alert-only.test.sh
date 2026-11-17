#!/usr/bin/env bash
# task-notifier-supervisor.sh with SUTANDO_STANDBY_MODE=alert (a Claude seat whose watcher the
# task-watch Mod hosts): the notifier never runs, so nothing is typed into the pane; after the
# alert grace with no ready session watcher the owner is alerted once per episode (a log line and
# a proactive result); a ready watcher ends the episode with no owner alert. Without the mode the
# same supervisor arms the typing notifier, as before.
#
# Part 1 sources the supervisor and drives run_alert_only on a fake clock and a scripted verdict.
# Part 2 runs the real supervisor in real tmux on a private socket, a stub notifier and a real
# session watcher (watch-tasks-stream.sh) on a fixture inbox.
# Run: bash tests/task-notifier-supervisor-alert-only.test.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-sup-alert.XXXXXX")"
unset SUTANDO_WORKSPACE_DIR SUTANDO_INSTANCE_ID SUTANDO_WORKER_ID
SOCK="$WORK/tmux.sock"
cleanup_all() {
  tmux -S "$SOCK" kill-server >/dev/null 2>&1 || true
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

echo "task-notifier-supervisor alert-only standby:"

# ---- Part 1: the episode rules, on a fake clock --------------------------------------------
# Each line of $VERDICTS is one poll; the clock advances ROLE_POLL per sleep.
unit() {  # unit <verdict per poll...>; prints the alerts and log lines it saw
  local verdicts="$WORK/verdicts" clock="$WORK/clock" alerts="$WORK/alerts" logs="$WORK/logs.txt"
  printf '%s\n' "$@" > "$verdicts"; echo 0 > "$clock"; : > "$alerts"; : > "$logs"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" \
      SUTANDO_NOTIFIER_ROLE_POLL=5 SUTANDO_NOTIFIER_ALERT_GRACE=300 \
      SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_SCRIPT
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    n=0
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    unwatched_alert() { echo "alert at $(now_s) after $1" >> "$alerts"; }
    supervisor_log() { echo "$*" >> "$logs"; }
    run_alert_only
  )
}
mkdir -p "$WORK/unit/tasks"
nos() { local i; for i in $(seq 1 "$1"); do echo no; done; }

# 70 polls of "no" = 345 s: one alert, at the first poll at or past 300 s.
# shellcheck disable=SC2046
unit $(nos 70)
[ "$(wc -l < "$WORK/alerts" | tr -d ' ')" = 1 ] && r=0 || r=1; check "(1) one alert per episode, however long it lasts" "$r" "$(cat "$WORK/alerts")"
grep -qx "alert at 300 after 300" "$WORK/alerts" && r=0 || r=1; check "(1) ...raised once the grace has passed, not before" "$r" "$(cat "$WORK/alerts")"

# 59 polls of "no" end at 290 s: no alert inside the grace.
# shellcheck disable=SC2046
unit $(nos 59)
[ ! -s "$WORK/alerts" ] && r=0 || r=1; check "(2) no alert inside the grace (a boot or a restart)" "$r"

# A ready watcher inside the grace resets it: 50 no, 1 yes, 50 no -> no alert.
# shellcheck disable=SC2046
unit $(nos 50) yes $(nos 50)
[ ! -s "$WORK/alerts" ] && r=0 || r=1; check "(3) a watcher that comes back inside the grace resets it" "$r"

# Alert, stand down, then a second episode alerts again.
# shellcheck disable=SC2046
unit $(nos 62) yes yes $(nos 62)
[ "$(wc -l < "$WORK/alerts" | tr -d ' ')" = 2 ] && r=0 || r=1; check "(4) a new episode after a stand-down alerts again" "$r" "$(cat "$WORK/alerts")"
[ "$(grep -c 'stands down' "$WORK/logs.txt")" = 1 ] && r=0 || r=1; check "(4) ...and the stand-down is logged once, with no owner alert" "$r" "$(cat "$WORK/logs.txt")"

# "unknown" opens no episode.
# shellcheck disable=SC2046
unit $(for i in $(seq 1 70); do echo unknown; done)
[ ! -s "$WORK/alerts" ] && r=0 || r=1; check "(5) an unreadable process table never alerts" "$r"

# ---- Part 2: the real supervisor in tmux ---------------------------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  echo "  SKIP part 2: tmux not available"
else
  STUB="$WORK/stub-notifier.sh"
  printf '#!/bin/bash\necho "$$ $*" >> "$SUTANDO_STUB_MARKER"\ntrap "exit 0" TERM\nwhile true; do sleep 1; done\n' > "$STUB"
  chmod +x "$STUB"
  STUBBIN="$WORK/stubbin"; mkdir -p "$STUBBIN"
  cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"; chmod +x "$STUBBIN/fswatch"
  export PATH="$STUBBIN:$PATH"
  tmux -S "$SOCK" new-session -d -s target -c "$REPO" "sleep 600"

  start_supervisor() {  # start_supervisor <name> <ws> <mode>
    tmux -S "$SOCK" new-session -d -s "$1" -c "$REPO" \
      "env -u SUTANDO_INSTANCE_ID -u SUTANDO_WORKSPACE_DIR -u SUTANDO_WORKER_ID PATH=\$PATH \
         SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target SUTANDO_TASKS_DIR=$2/tasks \
         SUTANDO_NOTIFIER_SCRIPT=$STUB SUTANDO_STUB_MARKER=$2/notifier.marker \
         SUTANDO_NOTIFIER_GRACE_PERIOD=1 SUTANDO_NOTIFIER_ROLE_POLL=1 SUTANDO_NOTIFIER_ALERT_GRACE=3 \
         SUTANDO_STANDBY_MODE=$3 bash $SUPERVISOR > $2/sup.out 2>&1"
  }
  wait_grep() {  # wait_grep <pattern> <file> <tenths>
    local i; for i in $(seq 1 "$3"); do grep -q -- "$1" "$2" 2>/dev/null && return 0; sleep 0.1; done; return 1
  }

  A="$WORK/mod"; mkdir -p "$A/tasks" "$A/state" "$A/logs" "$A/results"
  start_supervisor sup-mod "$A" alert
  LOG="$A/logs/claude-task-notifier.log"
  wait_grep "ALERT" "$LOG" 150 && r=0 || r=1; check "(6) Mod seat: the alert is logged after the grace" "$r" "$(cat "$A/sup.out" 2>/dev/null)"
  sleep 4
  [ "$(grep -c 'ALERT' "$LOG")" = 1 ] && r=0 || r=1; check "(6) ...once" "$r" "$(cat "$LOG")"
  # shellcheck disable=SC2012
  [ "$(ls "$A/results" | grep -c '^proactive-standby-unwatched-.*\.txt$')" = 1 ] && r=0 || r=1
  check "(6) ...and the owner gets one proactive alert" "$r" "$(ls "$A/results")"
  grep -q "no task watcher has held .*/mod/tasks for 0 min, .*nothing types them in" "$A"/results/proactive-standby-unwatched-*.txt 2>/dev/null && r=0 || r=1
  check "(6) ...naming the inbox and saying nothing is typed" "$r"
  [ ! -e "$A/notifier.marker" ] && r=0 || r=1; check "(6) ...and the typing notifier never ran" "$r" "$(cat "$A/notifier.marker" 2>/dev/null)"
  tmux -S "$SOCK" new-session -d -s session-watcher -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID -u SUTANDO_WORKSPACE_DIR SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target \
       SUTANDO_WATCHER_READY_TIMEOUT=60 bash $WATCHER $A/tasks --role session --inbox $A/tasks > $A/watcher.out 2>&1"
  wait_grep "stands down" "$LOG" 300 && r=0 || r=1; check "(7) a ready session watcher ends the episode" "$r" "$(tail -3 "$LOG")"
  # shellcheck disable=SC2012
  [ "$(ls "$A/results" | grep -c '^proactive-')" = 1 ] && r=0 || r=1; check "(7) ...without a word to the owner" "$r"
  [ ! -e "$A/notifier.marker" ] && r=0 || r=1; check "(7) ...and still nothing typed" "$r"
  tmux -S "$SOCK" kill-session -t sup-mod >/dev/null 2>&1
  tmux -S "$SOCK" kill-session -t session-watcher >/dev/null 2>&1

  B="$WORK/plain"; mkdir -p "$B/tasks" "$B/state" "$B/logs" "$B/results"
  start_supervisor sup-plain "$B" type
  wait_grep "" "$B/notifier.marker" 150 && [ -s "$B/notifier.marker" ] && r=0 || r=1
  check "(8) without the Mod the same supervisor arms the typing notifier" "$r" "$(cat "$B/sup.out" 2>/dev/null)"
  # shellcheck disable=SC2012
  [ "$(ls "$B/results" | grep -c '^proactive-')" = 0 ] && ! grep -q ALERT "$B/logs/claude-task-notifier.log" 2>/dev/null && r=0 || r=1
  check "(8) ...and raises no unwatched-inbox alert" "$r"
fi

if [ "$fail" = 0 ]; then echo "  ok  alert-only standby types nothing and alerts once per episode"; else echo "  FAILED"; fi
exit "$fail"
