#!/usr/bin/env bash
# task-notifier-supervisor.sh with SUTANDO_STANDBY_MODE=alert (a Claude seat whose watcher the
# task-watch Mod hosts): the notifier never runs, so nothing is typed into the pane; after the
# alert grace with no ready session watcher the owner is alerted (a log line, a proactive result
# naming the seat, never a path, and the opt-in macOS notification), once per outage: no timed
# reminder unless one is opted into. A DM that was certainly not published is retried with a
# bounded back-off; the macOS notification fires once per outage. A ready watcher ends the outage
# with no owner alert. Without the mode the same supervisor arms the typing notifier, as before.
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
    SUT_ALERT_TEST_SKIP_TMUX="${SUT_ALERT_TEST_SKIP_TMUX:-}" \
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
# The outage record, on the fake clock: what src/standby_alert_once.py observe/end keep on disk.
stub_episodes() {
  : > "$WORK/episode"
  episode_observe() { [ -s "$WORK/episode" ] || echo "$1" > "$WORK/episode"; cat "$WORK/episode"; }
  episode_end() { : > "$WORK/episode"; }
}
unit() {  # unit <verdict per poll...>; prints the alerts and log lines it saw
  local verdicts="$WORK/verdicts" clock="$WORK/clock" alerts="$WORK/alerts" logs="$WORK/logs.txt"
  printf '%s\n' "$@" > "$verdicts"; echo 0 > "$clock"; : > "$alerts"; : > "$logs"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" \
      SUTANDO_NOTIFIER_ROLE_POLL="${UNIT_POLL:-5}" SUTANDO_NOTIFIER_ALERT_GRACE="${UNIT_GRACE:-300}" \
      SUTANDO_NOTIFIER_ALERT_REPEAT="${UNIT_REPEAT:-}" SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_ALERT_REPEAT SUTANDO_NOTIFIER_SCRIPT
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    unwatched_alert() { echo "alert at $(now_s) after $1${3:+ $3 $4}" >> "$alerts"; }
    supervisor_log() { echo "$*" >> "$logs"; }
    stub_episodes
    run_alert_only
  ) 2> "$WORK/unit.err"
}
mkdir -p "$WORK/unit/tasks" "$WORK/unit/results"
printf 'id: task-1\ntask: a\n' > "$WORK/unit/tasks/task-1.txt"
nos() { local i; for i in $(seq 1 "$1"); do echo no; done; }

# 70 polls of "no" = 345 s: one alert, at the first poll at or past 300 s.
# shellcheck disable=SC2046
unit $(nos 70)
[ "$(cat "$WORK/alerts")" = "alert at 300 after 300" ] && r=0 || r=1
check "(1) one alert, raised once the grace has passed, not before" "$r" "$(cat "$WORK/alerts")"
# The default: no reminder, however long the outage lasts (67 polls of 60 s = 4020 s).
# shellcheck disable=SC2046
UNIT_POLL=60 unit $(nos 67)
[ "$(cat "$WORK/alerts")" = "alert at 300 after 300" ] && r=0 || r=1
check "(1d) by default a long outage gets that one alert and nothing after it" "$r" "$(cat "$WORK/alerts")"
# shellcheck disable=SC2046
UNIT_POLL=60 UNIT_REPEAT=1800 unit $(nos 67)
[ "$(printf '%s\n' "alert at 300 after 300" "alert at 2100 after 2100 reminder 1" "alert at 3900 after 3900 reminder 2")" = "$(cat "$WORK/alerts")" ] && r=0 || r=1
check "(1r) an opted-in repeat reminds every interval, each reminder its own slot" "$r" "$(cat "$WORK/alerts")"
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

# The alert settings: whole seconds in range, else the default, logged once; never a shell error.
settings() {  # settings <grace value> <repeat value>: prints "<grace> <repeat> <warnings>"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh" \
      SUTANDO_NOTIFIER_ALERT_GRACE="$1" SUTANDO_NOTIFIER_ALERT_REPEAT="$2"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_NOTIFIER_SCRIPT SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_ALERT_REPEAT
    # shellcheck source=/dev/null
    source "$SUPERVISOR" 2> "$WORK/settings.err"
    printf '%s %s %s' "$ALERT_GRACE" "$ALERT_REPEAT" "$(grep -c '^task-notifier-supervisor: ignoring ' "$WORK/settings.err")"
  )
}
BIG=123456789012345678901234567890123456
r=0
for c in ":|300 0 0" "600:900|600 900 0" "abc:abc|300 0 2" "$BIG:$BIG|300 0 2" "0:0|300 0 1" \
         "007:010|7 10 0" "86400:86400|86400 86400 0" "86401:86401|300 0 2" "-5:1.5|300 0 2" " 60:60 |300 0 2"; do
  want="${c#*|}"; pair="${c%|*}"
  got="$(settings "${pair%%:*}" "${pair#*:}")"
  [ "$got" = "$want" ] || { r=1; echo "    settings ${pair}: got '$got', want '$want'"; }
done
check "(29) GRACE/REPEAT accept whole seconds in range (leading zeros decimal), else the default with one warning each" "$r"
# shellcheck disable=SC2046
UNIT_GRACE=abc unit $(nos 70)
[ "$(cat "$WORK/alerts")" = "alert at 300 after 300" ] && ! grep -q "expression expected" "$WORK/unit.err" && r=0 || r=1
check "(29) ...an invalid grace still alerts at the default, with no shell error on any poll" "$r" "$(cat "$WORK/alerts" "$WORK/unit.err")"
# shellcheck disable=SC2046
UNIT_GRACE=$BIG UNIT_POLL=60 UNIT_REPEAT=$BIG unit $(nos 67)
[ "$(cat "$WORK/alerts")" = "alert at 300 after 300" ] && ! grep -q "expression expected" "$WORK/unit.err" && r=0 || r=1
check "(29) ...and a 36-digit grace and repeat read as the defaults (one alert, no reminder)" "$r" "$(cat "$WORK/alerts" "$WORK/unit.err")"

# The real alert text: the seat (judged from the inbox), never a path or session name, and no promise.
W1=0123456789abcdef
mkdir -p "$WORK/unit/state" "$WORK/unit/deliveries/$W1" "$WORK/unit/deliveries/retired-one" "$WORK/scratch/tasks" "$WORK/scratch/results"
printf '{"workers": {"%s": {"state": "idle"}, "retired-one": {"state": "retired"}}}\n' "$W1" > "$WORK/unit/state/roster.json"
printf 'x\n' > "$WORK/unit/deliveries/$W1/task-7.txt"
printf 'id: task-8\ntask: s\n' > "$WORK/scratch/tasks/task-8.txt"
alert_text() {  # alert_text <inbox> <instance id or empty> [seconds] [kind]; the published alert, the notification, the log
  local out="$WORK/text"; rm -rf "$out" "$WORK/unit/state/standby-alert"; mkdir -p "$out"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$1" SUTANDO_TMUX_SESSION=secret-session-name \
      SUTANDO_INSTANCE_ID="$2" SUTANDO_WORKSPACE_DIR="$WORK/unit"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_TMUX_SESSION SUTANDO_INSTANCE_ID SUTANDO_WORKSPACE_DIR
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    bash() { if [ "$1" = "$REPO/src/macos-notify.sh" ]; then printf '%s\n' "$2" > "$out/notify"; else command bash "$@"; fi; }
    supervisor_log() { echo "$*" >> "$out/log"; }
    unwatched_alert "${3:-600}" 1000 ${4:+"$4" 1}
  )
  cat "$WORK"/unit/results/proactive-standby-unwatched-*.txt "$WORK"/scratch/results/proactive-* > "$out/proactive" 2>/dev/null
  rm -f "$WORK"/unit/results/proactive-standby-unwatched-*.txt "$WORK"/scratch/results/proactive-*
}
alert_text "$WORK/unit/tasks" ""
for f in proactive notify; do
  grep -qx "Sutando: the core has had no task watcher for 10 min, so it is not picking up its tasks, and nothing types them in for it. Restarting that session brings its watcher back." "$WORK/text/$f" && r=0 || r=1
  check "(9) the $f alert names the core and says what is wrong and what fixes it" "$r" "$(cat "$WORK/text/$f" 2>/dev/null)"
  ! grep -qE "$WORK|/unit/tasks|secret-session-name" "$WORK/text/$f" && r=0 || r=1
  check "(9) ...and carries no local path or session name" "$r" "$(cat "$WORK/text/$f" 2>/dev/null)"
  ! grep -qiE "last message|no further|not .* again|won't|will not|only if" "$WORK/text/$f" && r=0 || r=1
  check "(9) ...and promises nothing about later messages" "$r" "$(cat "$WORK/text/$f" 2>/dev/null)"
done
grep -q "$WORK/unit/tasks" "$WORK/text/log" && grep -q "secret-session-name" "$WORK/text/log" && r=0 || r=1
check "(9) the local log line keeps the detail" "$r" "$(cat "$WORK/text/log" 2>/dev/null)"
alert_text "$WORK/unit/tasks" "$W1"
grep -q "^Sutando: the core has had" "$WORK/text/proactive" && r=0 || r=1
check "(10) the core's inbox is the core even under a worker's instance id" "$r" "$(cat "$WORK/text/proactive" 2>/dev/null)"
alert_text "$WORK/unit/deliveries/$W1" ""
grep -q "^Sutando: worker 01234567 has had no task watcher" "$WORK/text/proactive" && r=0 || r=1
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

# The message names: one fixed name per message of an episode, so a retry reuses it.
names="$(SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh" SUTANDO_TASKS_DIR="$WORK/unit/tasks" \
  SUTANDO_WORKSPACE_DIR="$WORK/unit" bash -c 'source "$1"; for a in "1000" "1000" "1000 2" "2000"; do set -- $a
  alert_once stem --inbox "$TASKS_DIR" --episode "$1" ${2:+--slot "$2"}; done' _ "$SUPERVISOR" 2>/dev/null)"
n1="$(sed -n 1p <<<"$names")"; n2="$(sed -n 2p <<<"$names")"; n3="$(sed -n 3p <<<"$names")"; n4="$(sed -n 4p <<<"$names")"
[ "$n1" = "$n2" ] && [ "$n3" = "$n1-r2" ] && [ "$n4" != "$n1" ] && [[ "$n1" =~ ^proactive-standby-unwatched-[0-9a-f]{12}-1000$ ]] \
  && ! grep -q "$WORK" <<<"$names" && r=0 || r=1
check "(20) each message of an outage has one fixed name (a reminder its slot), with no path in it" "$r" "$names"

# The publish gate's answer decides: 0 published, 3 published but unsynced and 4 already published
# are sent; 1 (not published), a usage error or a kill are failures to retry.
# inbox_seat also runs through $PY (seat_inbox.py), so only the publish call is stubbed.
PUBPY="$WORK/pub-py"
printf '#!/bin/bash\ncase "$*" in *standby_alert_once.py\\ publish*) cat >/dev/null; exit "$PUB_RC" ;; *) exec python3 "$@" ;; esac\n' > "$PUBPY"
chmod +x "$PUBPY"
publish_rc() {  # publish_rc <publish exit>: unwatched_alert's own status
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" SUTANDO_WORKSPACE_DIR="$WORK/unit" \
      SUTANDO_NOTIFIER_PY="$PUBPY" PUB_RC="$1"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR SUTANDO_NOTIFIER_PY PUB_RC
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    bash() { if [ "$1" = "$REPO/src/macos-notify.sh" ]; then :; else command bash "$@"; fi; }
    supervisor_log() { echo "$*" >> "$WORK/pub.log"; }
    unwatched_alert 600 1000
  )
  printf '%s' "$?"
}
: > "$WORK/pub.log"
r=0
for c in 0:0 3:0 4:0 1:1 2:1 137:1; do [ "$(publish_rc "${c%:*}")" = "${c#*:}" ] || { r=1; echo "    exit ${c%:*} read wrong"; }; done
check "(22) published (0), unsynced (3) or already published (4) is sent; 1, 2 or a kill (137) is retried" "$r" "$(cat "$WORK/pub.log")"
grep -q "directory sync failed; not resent" "$WORK/pub.log" && grep -q "was already published; not resent" "$WORK/pub.log" \
  && grep -q "retried under the same name" "$WORK/pub.log" && r=0 || r=1
check "(22) ...and each is logged as such" "$r" "$(cat "$WORK/pub.log")"
rm -rf "$WORK/unit/state/standby-alert"
mkdir -p "$WORK/nores/state" "$WORK/nores/tasks"; cp "$WORK/unit/state/roster.json" "$WORK/nores/state/"
(
  SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/nores/tasks" SUTANDO_WORKSPACE_DIR="$WORK/nores"
  export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR
  # shellcheck source=/dev/null
  source "$SUPERVISOR"
  bash() { if [ "$1" = "$REPO/src/macos-notify.sh" ]; then :; else command bash "$@"; fi; }
  supervisor_log() { :; }
  unwatched_alert 600 1000
)
[ "$?" = 1 ] && r=0 || r=1; check "(22) ...and a seat with no results folder is a failure to retry, not a silent success" "$r"

# End to end: the real supervisor functions and the real publish gate on a fake clock. PUB_MODE
# makes the publisher's first call fail in one way, later calls work:
#   fail       exits 1 before writing anything        pre        killed (137) before its rename
#   post       killed (137) right after its rename    post-archive / post-quarantine
#              ...after the bridge already moved it into results/archive/ or results/undelivered/
KILLPUB="$WORK/killpub.py"
cat > "$KILLPUB" <<PYEOF
import os, sys, time
from pathlib import Path
sys.path.insert(0, "$REPO/src")
mode = os.environ["PUB_MODE"]
real = os.replace
def replace(src, dst):
    if mode == "pre":
        os._exit(137)
    real(src, dst)
    dst = Path(dst)
    if mode == "post-archive":
        month = dst.parent / "archive" / time.strftime("%Y-%m")
        month.mkdir(parents=True, exist_ok=True)
        real(dst, month / dst.name)
    elif mode == "post-quarantine":
        q = dst.parent / "undelivered"
        q.mkdir(parents=True, exist_ok=True)
        real(dst, q / (dst.stem + "-1700000000.txt"))
    os._exit(137)
os.replace = replace
import standby_alert_once
sys.exit(standby_alert_once.main(sys.argv[1:]))
PYEOF
MODEPY="$WORK/mode-py"
printf '#!/bin/bash\ncase "$*" in *standby_alert_once.py\\ publish*)\n  echo x >> "$PUB_CALLS"\n  if [ "$(wc -l < "$PUB_CALLS")" -eq 1 ] && [ -n "$PUB_MODE" ]; then\n    [ "$PUB_MODE" = fail ] && { cat >/dev/null; exit 1; }\n    shift; exec python3 "$KILLPUB" "$@"\n  fi ;;\nesac\nexec python3 "$@"\n' > "$MODEPY"
chmod +x "$MODEPY"
real_run() {  # real_run <tag> <clock start> <verdict per poll...>: one supervisor process on the shared fixture
  local tag="$1" verdicts="$WORK/$1.verdicts" clock="$WORK/$1.clock"; shift
  echo "$1" > "$clock"; shift
  printf '%s\n' "$@" > "$verdicts"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" SUTANDO_WORKSPACE_DIR="$WORK/unit" \
      SUTANDO_NOTIFIER_ROLE_POLL=5 SUTANDO_NOTIFIER_ALERT_GRACE=300 SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh" \
      SUTANDO_NOTIFIER_PY="$MODEPY" PUB_CALLS="$WORK/$tag.calls" KILLPUB="$KILLPUB" PUB_MODE="${PUB_MODE:-}"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_SCRIPT SUTANDO_NOTIFIER_PY PUB_CALLS KILLPUB PUB_MODE
    : > "$PUB_CALLS"
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    supervisor_log() { :; }
    bash() { if [ "$1" = "$REPO/src/macos-notify.sh" ]; then echo "$tag $(now_s)" >> "$WORK/notify.log"; else command bash "$@"; fi; }
    run_alert_only
  )
}
fresh() { rm -rf "$WORK/unit/state/standby-alert" "$WORK/unit/results" "$WORK/notify.log"; mkdir -p "$WORK/unit/results"; : > "$WORK/notify.log"; }
copies() { find "$WORK/unit/results" -name 'proactive-standby-unwatched-*.txt' | wc -l | tr -d ' '; }
live() { find "$WORK/unit/results" -maxdepth 1 -name 'proactive-standby-unwatched-*.txt' | wc -l | tr -d ' '; }
notes() { wc -l < "$WORK/notify.log" | tr -d ' '; }

fresh
# shellcheck disable=SC2046
PUB_MODE=fail real_run a 0 $(nos 70)
[ "$(wc -l < "$WORK/a.calls" | tr -d ' ')" = 2 ] && [ "$(copies)" = 1 ] && [ "$(notes)" = 1 ] && r=0 || r=1
check "(28) a DM that was not published is retried under the same name and lands once; macOS is notified once" "$r" \
  "calls $(wc -l < "$WORK/a.calls"), copies $(copies), notifications $(notes)"
# shellcheck disable=SC2046
real_run a 400 yes yes $(nos 70)
[ "$(copies)" = 2 ] && [ "$(notes)" = 2 ] && r=0 || r=1
check "(28) ...a returning watcher ends the outage, and the next one is alerted and notified again, once" "$r" "copies $(copies), notifications $(notes)"

for mode in post post-archive post-quarantine; do
  fresh
  # shellcheck disable=SC2046
  PUB_MODE=$mode real_run a 0 $(nos 70)
  [ "$(wc -l < "$WORK/a.calls" | tr -d ' ')" = 2 ] && [ "$(copies)" = 1 ] && [ "$(notes)" = 1 ] && r=0 || r=1
  check "(30) a publisher killed after its rename ($mode): the retry finds that copy, so exactly one alert exists" "$r" \
    "calls $(wc -l < "$WORK/a.calls"), copies $(copies), live $(live), notifications $(notes)"
done
[ "$(live)" = 0 ] && r=0 || r=1; check "(30) ...and no live copy is added beside one the bridge already took" "$r" "live $(live)"
fresh
# shellcheck disable=SC2046
PUB_MODE=pre real_run a 0 $(nos 70)
[ "$(wc -l < "$WORK/a.calls" | tr -d ' ')" = 2 ] && [ "$(live)" = 1 ] && [ "$(notes)" = 1 ] && r=0 || r=1
check "(31) a publisher killed before its rename wrote nothing, so the retry publishes it" "$r" \
  "calls $(wc -l < "$WORK/a.calls"), live $(live), notifications $(notes)"

# A supervisor restarted mid-outage: the outage and its alert are found, so nothing is resent.
fresh
# shellcheck disable=SC2046
real_run a 0 $(nos 70)
# shellcheck disable=SC2046
real_run b 360 $(nos 70)
[ "$(copies)" = 1 ] && [ "$(notes)" = 1 ] && [ "$(wc -l < "$WORK/b.calls" | tr -d ' ')" = 1 ] && r=0 || r=1
check "(32) a supervisor restarted mid-outage finds the outage and its alert, and publishes and notifies nothing" "$r" \
  "copies $(copies), notifications $(notes), restarted publish calls $(wc -l < "$WORK/b.calls")"
# Two supervisors of one inbox at once: one alert, one notification.
fresh
# shellcheck disable=SC2046
real_run c 0 $(nos 70) &
# shellcheck disable=SC2046
real_run d 0 $(nos 70) &
wait
[ "$(copies)" = 1 ] && [ "$(notes)" = 1 ] && r=0 || r=1
check "(33) two supervisors of one inbox at once publish one alert and notify once" "$r" "copies $(copies), notifications $(notes)"
fresh

# run_alert_only with a stubbed send: a failed first alert does not latch "alerted", so the very
# next poll retries it, rather than waiting a full ALERT_REPEAT (or forever, with reminders off).
retry_unit() {  # retry_unit <verdict per poll...>; prints each unwatched_alert attempt, succeeding from the 3rd
  local verdicts="$WORK/rverdicts" clock="$WORK/rclock" alerts="$WORK/ralerts"
  printf '%s\n' "$@" > "$verdicts"; echo 0 > "$clock"; : > "$alerts"; : > "$WORK/rcalls"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" SUTANDO_NOTIFIER_ROLE_POLL=5 \
      SUTANDO_NOTIFIER_ALERT_GRACE=300 SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_SCRIPT
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    supervisor_log() { :; }
    stub_episodes
    unwatched_alert() {
      echo x >> "$WORK/rcalls"
      local n; n="$(wc -l < "$WORK/rcalls" | tr -d ' ')"
      echo "attempt $n at $(now_s)" >> "$alerts"
      [ "$n" -ge 3 ]
    }
    run_alert_only
  )
}
# shellcheck disable=SC2046
retry_unit $(nos 70)
[ "$(printf '%s\n' "attempt 1 at 300" "attempt 2 at 305" "attempt 3 at 310")" = "$(cat "$WORK/ralerts")" ] && r=0 || r=1
check "(23) a failed alert is retried on the very next poll, not latched or delayed" "$r" "$(cat "$WORK/ralerts")"

# fail_unit: like unit, but the stubbed send fails for the kinds in FAIL_KINDS ("alert" is the first
# alert) until FAIL_UNTIL; every attempt is logged with its outcome.
fail_unit() {  # fail_unit <verdict per poll...>
  local verdicts="$WORK/fverdicts" clock="$WORK/fclock" alerts="$WORK/falerts"
  printf '%s\n' "$@" > "$verdicts"; echo 0 > "$clock"; : > "$alerts"
  (
    SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_TASKS_DIR="$WORK/unit/tasks" SUTANDO_NOTIFIER_ROLE_POLL="${UNIT_POLL:-5}" \
      SUTANDO_NOTIFIER_ALERT_GRACE=300 SUTANDO_NOTIFIER_ALERT_REPEAT="${UNIT_REPEAT:-}" \
      SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh"
    export SUTANDO_SUPERVISOR_SOURCE_ONLY SUTANDO_TASKS_DIR SUTANDO_NOTIFIER_ROLE_POLL \
      SUTANDO_NOTIFIER_ALERT_GRACE SUTANDO_NOTIFIER_ALERT_REPEAT SUTANDO_NOTIFIER_SCRIPT
    # shellcheck source=/dev/null
    source "$SUPERVISOR"
    target_alive() { [ -s "$verdicts" ]; }
    session_role_verdict() { head -n 1 "$verdicts"; tail -n +2 "$verdicts" > "$verdicts.n"; mv "$verdicts.n" "$verdicts"; }
    now_s() { cat "$clock"; }
    sleep() { echo $(( $(cat "$clock") + ${1%.*} )) > "$clock"; }
    supervisor_log() { :; }
    stub_episodes
    unwatched_alert() {
      local kind="${3:-alert}" t; t="$(now_s)"
      if [[ " ${FAIL_KINDS:-} " == *" $kind "* ]] && [ "$t" -lt "${FAIL_UNTIL:-999999}" ]; then
        echo "$t $kind failed" >> "$alerts"; return 1
      fi
      echo "$t $kind sent" >> "$alerts"
    }
    run_alert_only
  )
}
# An hour of 5 s polls with every send failing: retried at once, then backed off to every 300 s.
# shellcheck disable=SC2046
FAIL_KINDS=alert fail_unit $(nos 720)
tries="$(wc -l < "$WORK/falerts" | tr -d ' ')"
gap="$(tail -n 2 "$WORK/falerts" | cut -d' ' -f1 | tr '\n' ' ' | awk '{print $2 - $1}')"
[ "$(sed -n 2p "$WORK/falerts" | cut -d' ' -f1)" = 305 ] && [ "$tries" -le 25 ] && [ "$gap" -ge 300 ] && [ "$gap" -le 305 ] && r=0 || r=1
check "(24) a send that keeps failing is retried at once, then backed off to every 300 s, never every poll" "$r" "tries $tries, last gap $gap"
# An opted-in reminder that fails is retried on its own back-off and, once sent, keeps its interval.
# shellcheck disable=SC2046
UNIT_POLL=60 UNIT_REPEAT=600 FAIL_KINDS=reminder FAIL_UNTIL=1500 fail_unit $(nos 50)
dup="$(cut -d' ' -f1 "$WORK/falerts" | uniq -d)"
[ "$(grep ' sent$' "$WORK/falerts" | tr '\n' ';')" = "300 alert sent;1560 reminder sent;2100 reminder sent;2700 reminder sent;" ] \
  && grep -q "^900 reminder failed" "$WORK/falerts" && [ -z "$dup" ] && r=0 || r=1
check "(25) a failing opted-in reminder is retried, one attempt per poll, then keeps its schedule" "$r" "$(tr '\n' ';' < "$WORK/falerts")"

# The back-off itself: 1, 2, 4 ... capped at 300 s, and still 300 s after hours of failures (no overflow).
waits="$(SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_NOTIFIER_SCRIPT="$WORK/never-run.sh" bash -c \
  'source "$1"; for f in 1 2 3 9 10 100 1000; do printf "%s " "$(retry_wait "$f")"; done' _ "$SUPERVISOR" 2>/dev/null)"
[ "$waits" = "1 2 4 256 300 300 300 " ] && r=0 || r=1
check "(27) the retry back-off doubles from 1 s and stays capped at 300 s however long it fails" "$r" "$waits"

# ---- Part 2: the real supervisor in tmux ---------------------------------------------------
if [ "${SUT_ALERT_TEST_SKIP_TMUX:-}" = 1 ] || ! command -v tmux >/dev/null 2>&1; then
  echo "  SKIP (6)-(8) part 2: tmux not available or skipped (the mode decision is pinned tmux-free elsewhere)"
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
    local i=0; while [ "$i" -lt "$3" ]; do grep -q -- "$1" "$2" 2>/dev/null && return 0; sleep 0.1; i=$((i + 1)); done; return 1
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
  grep -q "the core has had no task watcher for under a minute, so it is not picking up its tasks, and nothing types them in" "$A"/results/proactive-standby-unwatched-*.txt 2>/dev/null \
    && ! grep -q "$A" "$A"/results/proactive-standby-unwatched-*.txt && r=0 || r=1
  check "(6) ...naming the core and that nothing is typed, and no path" "$r" "$(cat "$A"/results/proactive-* 2>/dev/null)"
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

if [ "$fail" = 0 ]; then echo "  ok  alert-only standby types nothing, alerts once per outage and retries a DM that did not publish"; else echo "  FAILED"; fi
exit "$fail"
