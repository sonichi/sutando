#!/usr/bin/env bash
# task-notifier-supervisor.sh with SUTANDO_STANDBY_MODE=alert (a Claude seat whose watcher the
# task-watch Mod hosts): the notifier never runs, so nothing is typed into the pane; after the
# alert grace with no ready session watcher the owner is alerted (a log line and a proactive
# result naming the seat and the waiting count, never a path), then reminded every repeat
# interval; a ready watcher ends the episode with no owner alert. Without the mode the same
# supervisor arms the typing notifier, as before.
#
# Part 1 sources the supervisor and drives run_alert_only on a fake clock and a scripted verdict,
# and the real alert text. Part 2 (tmux only; SKIP printed otherwise) runs the real supervisor in
# real tmux on a private socket, a stub notifier and a real session watcher on a fixture inbox.
# The tmux-free mode decision is tests/task-notifier-supervisor-alert-decision.test.py.
# Run: bash tests/task-notifier-supervisor-alert-only.test.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Hermetic, like tests/_helpers/hermetic_env.py: only PATH, LANG and TMPDIR cross, under a temp HOME.
if [ "${SUT_ALERT_TEST_HERMETIC:-}" != 1 ]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-sup-alert.XXXXXX")"; mkdir -p "$WORK/home"
  exec env -i PATH="$PATH" LANG="${LANG:-C}" TMPDIR="${TMPDIR:-/tmp}" HOME="$WORK/home" USER=hermetic \
    PYTHONNOUSERSITE=1 SUTANDO_TELEMETRY=0 DO_NOT_TRACK=1 SUT_ALERT_TEST_HERMETIC=1 SUT_ALERT_TEST_WORK="$WORK" \
    bash "$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")" "$@"
fi
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
WORK="$SUT_ALERT_TEST_WORK"
SOCK="$WORK/tmux.sock"
cleanup_all() {
  tmux -S "$SOCK" kill-server >/dev/null 2>&1 || true
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

echo "task-notifier-supervisor alert-only standby:"
leaked="$(env | cut -d= -f1 | grep -E '^SUTANDO_WORKSPACE|^SUTANDO_INSTANCE|TOKEN|SECRET|_KEY$|^ANTHROPIC|^AG2' || true)"
[ -z "$leaked" ] && r=0 || r=1; check "(0) runs in a hermetic environment" "$r" "$leaked"

# ---- Part 1: the episode rules, on a fake clock --------------------------------------------
# Each line of $VERDICTS is one poll; the clock advances ROLE_POLL per sleep.
unit() {  # unit <verdict per poll...>; prints the alerts and log lines it saw
  local verdicts="$WORK/verdicts" clock="$WORK/clock" alerts="$WORK/alerts" logs="$WORK/logs.txt"
  printf '%s\n' "$@" > "$verdicts"; echo 0 > "$clock"; : > "$alerts"; : > "$logs"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" \
      SUTANDO_NOTIFIER_ROLE_POLL="${UNIT_POLL:-5}" SUTANDO_NOTIFIER_ALERT_GRACE=300 \
      SUTANDO_NOTIFIER_ALERT_REPEAT="${UNIT_REPEAT:-1800}" SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_ALERT_REPEAT SUTANDO_NOTIFIER_SCRIPT
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    n=0
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    unwatched_alert() { echo "alert at $(now_s) after $1 waiting $2${3:+ $3}" >> "$alerts"; }
    supervisor_log() { echo "$*" >> "$logs"; }
    run_alert_only
  )
}
mkdir -p "$WORK/unit/tasks" "$WORK/unit/results"
printf 'id: task-1\ntask: a\n' > "$WORK/unit/tasks/task-1.txt"
printf 'id: task-2\ntask: b\n' > "$WORK/unit/tasks/task-2.txt"
printf 'task: bookkeeping\n' > "$WORK/unit/tasks/task-cron-3.txt"
nos() { local i; for i in $(seq 1 "$1"); do echo no; done; }

# 70 polls of "no" = 345 s: one alert, at the first poll at or past 300 s.
# shellcheck disable=SC2046
unit $(nos 70)
[ "$(wc -l < "$WORK/alerts" | tr -d ' ')" = 1 ] && r=0 || r=1; check "(1) one alert inside the repeat interval" "$r" "$(cat "$WORK/alerts")"
grep -qx "alert at 300 after 300 waiting 2" "$WORK/alerts" && r=0 || r=1; check "(1) ...raised once the grace has passed, not before, with the waiting count" "$r" "$(cat "$WORK/alerts")"

# 67 polls of 60 s = 4020 s: the alert at 300, reminders at 2100 and 3900.
# shellcheck disable=SC2046
UNIT_POLL=60 unit $(nos 67)
[ "$(printf '%s\n' "alert at 300 after 300 waiting 2" "alert at 2100 after 2100 waiting 2 reminder" \
      "alert at 3900 after 3900 waiting 2 reminder")" = "$(cat "$WORK/alerts")" ] && r=0 || r=1
check "(1r) a long episode is reminded every repeat interval, with the count" "$r" "$(cat "$WORK/alerts")"
# shellcheck disable=SC2046
UNIT_POLL=60 UNIT_REPEAT=0 unit $(nos 67)
[ "$(wc -l < "$WORK/alerts" | tr -d ' ')" = 1 ] && r=0 || r=1; check "(1r) ...and a repeat of 0 sends no reminder" "$r" "$(cat "$WORK/alerts")"
# Reminded every 600 s, then a watcher returns at 2400: the reminder due at 2700 is not sent
# (a new episode's grace runs to 2760), and the stand-down is a log line only.
# shellcheck disable=SC2046
UNIT_POLL=60 UNIT_REPEAT=600 unit $(nos 40) yes $(nos 5)
[ "$(wc -l < "$WORK/alerts" | tr -d ' ')" = 4 ] && grep -c 'stands down' "$WORK/logs.txt" | grep -qx 1 && r=0 || r=1
check "(1r) ...a returning watcher stops the reminders silently" "$r" "$(cat "$WORK/alerts" "$WORK/logs.txt")"

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

# The real alert text: the seat (judged from the inbox) and the count, never a path or session name.
W1=0123456789abcdef
mkdir -p "$WORK/unit/state" "$WORK/unit/deliveries/$W1" "$WORK/unit/deliveries/retired-one" "$WORK/scratch/tasks" "$WORK/scratch/results"
printf '{"workers": {"%s": {"state": "idle"}, "retired-one": {"state": "retired"}}}\n' "$W1" > "$WORK/unit/state/roster.json"
printf 'x\n' > "$WORK/unit/deliveries/$W1/task-7.txt"
printf 'id: task-8\ntask: s\n' > "$WORK/scratch/tasks/task-8.txt"
alert_text() {  # alert_text <inbox> <instance id or empty> [seconds]; the published alert, the notification, the log
  local out="$WORK/text"; rm -rf "$out"; mkdir -p "$out"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$1" SUTANDO_TMUX_SESSION=secret-session-name \
      SUTANDO_INSTANCE_ID="$2" SUTANDO_WORKSPACE_DIR="$WORK/unit"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_TMUX_SESSION SUTANDO_INSTANCE_ID SUTANDO_WORKSPACE_DIR
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    bash() { if [ "$1" = "$REPO/src/macos-notify.sh" ]; then printf '%s\n' "$2" > "$out/notify"; else command bash "$@"; fi; }
    supervisor_log() { echo "$*" >> "$out/log"; }
    unwatched_alert "${3:-600}" "$(waiting_count)"
  )
  cat "$WORK"/unit/results/proactive-standby-unwatched-*.txt "$WORK"/scratch/results/proactive-* > "$out/proactive" 2>/dev/null
  rm -f "$WORK"/unit/results/proactive-standby-unwatched-*.txt "$WORK"/scratch/results/proactive-*
}
alert_text "$WORK/unit/tasks" ""
for f in proactive notify; do
  grep -q "the core has had no task watcher for 10 min" "$WORK/text/$f" && grep -q "2 tasks are waiting" "$WORK/text/$f" && r=0 || r=1
  check "(9) the $f alert names the core and the waiting count" "$r" "$(cat "$WORK/text/$f" 2>/dev/null)"
  ! grep -qE "$WORK|/unit/tasks|secret-session-name" "$WORK/text/$f" && r=0 || r=1
  check "(9) ...and carries no local path or session name" "$r" "$(cat "$WORK/text/$f" 2>/dev/null)"
done
grep -q "$WORK/unit/tasks" "$WORK/text/log" && grep -q "secret-session-name" "$WORK/text/log" && r=0 || r=1
check "(9) the local log line keeps the detail" "$r" "$(cat "$WORK/text/log" 2>/dev/null)"
alert_text "$WORK/unit/tasks" "$W1"
grep -q "^Sutando: the core has had" "$WORK/text/proactive" && r=0 || r=1
check "(10) the core's inbox is the core even under a worker's instance id" "$r" "$(cat "$WORK/text/proactive" 2>/dev/null)"
alert_text "$WORK/unit/deliveries/$W1" ""
grep -q "^Sutando: worker 01234567 has had no task watcher" "$WORK/text/proactive" && grep -q "1 task is waiting" "$WORK/text/proactive" && r=0 || r=1
check "(10) a rostered worker's delivery inbox is that worker by its short id, with no instance id" "$r" "$(cat "$WORK/text/proactive" 2>/dev/null)"
for inbox in "$WORK/scratch/tasks" "$WORK/unit/deliveries/retired-one"; do
  alert_text "$inbox" ""
  [ ! -s "$WORK/text/proactive" ] && [ ! -e "$WORK/text/notify" ] && grep -q "ALERT: .*$inbox.*owner not paged" "$WORK/text/log" && r=0 || r=1
  check "(11) ${inbox#"$WORK"/}: no registered seat, so a local ALERT line only and no page" "$r" "$(cat "$WORK/text/proactive" "$WORK/text/log" 2>/dev/null)"
done
alert_text "$WORK/unit/tasks" "" 45
grep -q "no task watcher for under a minute," "$WORK/text/proactive" && ! grep -q "0 min" "$WORK/text/proactive" && r=0 || r=1
check "(12) a sub-minute duration reads 'under a minute', not '0 min'" "$r" "$(cat "$WORK/text/proactive" 2>/dev/null)"
alert_text "$WORK/unit/tasks" "" 60
grep -q "no task watcher for 1 min," "$WORK/text/proactive" && r=0 || r=1
check "(12) ...and 60 s reads '1 min'" "$r" "$(cat "$WORK/text/proactive" 2>/dev/null)"

# ---- Part 2: the real supervisor in tmux ---------------------------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  echo "  SKIP (6)-(8) part 2: tmux not available (the mode decision is pinned tmux-free elsewhere)"
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
      "env -u SUTANDO_INSTANCE_ID -u SUTANDO_WORKER_ID PATH=\$PATH SUTANDO_WORKSPACE_DIR=$2 \
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
  grep -q "the core has had no task watcher for under a minute, .*0 tasks are waiting, and nothing types them in" "$A"/results/proactive-standby-unwatched-*.txt 2>/dev/null \
    && ! grep -q "$A" "$A"/results/proactive-standby-unwatched-*.txt && r=0 || r=1
  check "(6) ...naming the core, its waiting count and that nothing is typed, and no path" "$r" "$(cat "$A"/results/proactive-* 2>/dev/null)"
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

if [ "$fail" = 0 ]; then echo "  ok  alert-only standby types nothing and alerts, then reminds, per episode"; else echo "  FAILED"; fi
exit "$fail"
