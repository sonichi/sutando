#!/bin/bash
# A silent default fswatch monitor (fseventsd stopped delivering) falls back to poll_monitor once, a monitor
# named in SUTANDO_FSWATCH_MONITOR is never replaced, and only the current round's probe proves readiness.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null; fi; rm -rf "$TMP"' EXIT

# Each launch logs its argv and pid; it reports events only under a monitor listed in <case>/working.
# <case>/default-mode makes the default monitor print the probes it saw when sent TERM (wedged: then ignore it).
make_case() {
  local dir="$TMP/$1"; shift
  mkdir -p "$dir/ws/tasks" "$dir/ws/results" "$dir/ws/state" "$dir/bin" "$dir/tmp"
  printf '%s\n' "$@" > "$dir/working"
  cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$dir/poll-stub.sh"
  cat > "$dir/bin/fswatch" <<EOF
#!/bin/bash
printf '%s\n' "\$\$ \$*" >> "$dir/launches"
monitor=default
prev=""
for a in "\$@"; do [ "\$prev" = "-m" ] && monitor="\$a"; prev="\$a"; done
grep -qxF "\$monitor" "$dir/working" && exec bash "$dir/poll-stub.sh" "\$@"
mode="\$(cat "$dir/default-mode" 2>/dev/null)"
if [ "\$monitor" = default ] && [ -n "\$mode" ]; then
  seen=""
  flush() { for f in \$seen; do printf '%s\n' "\$f"; done; }
  if [ "\$mode" = wedged ]; then trap flush TERM; else trap 'flush; exit 0' TERM; fi
  while :; do
    for f in "$dir/ws/tasks"/.ready-*; do
      [ -e "\$f" ] || continue
      case " \$seen " in *" \$f "*) ;; *) seen="\$seen \$f" ;; esac
    done
    sleep 0.1
  done
fi
exec tail -f /dev/null
EOF
  chmod +x "$dir/bin/fswatch"
  : > "$dir/launches"
}
start_watcher() {   # <case> [VAR=value ...]
  local dir="$TMP/$1"; shift
  env PATH="$dir/bin:$PATH" TMPDIR="$dir/tmp" SUTANDO_WORKSPACE_DIR="$dir/ws" SUTANDO_RESULTS_DIR="$dir/ws/results" \
    SUTANDO_WATCHER_READY_TIMEOUT=2 SUTANDO_WATCHER_CATCHUP_SECONDS=600 SUTANDO_STANDBY_STOP_TIMEOUT=1 "$@" \
    bash "$REPO/src/watch-tasks-stream.sh" --role session --inbox "$dir/ws/tasks" > "$dir/out" 2> "$dir/err" &
  WATCHER_PID=$!
}
wait_for() {   # <file> <fixed string> <tenths>
  local _
  for _ in $(seq 1 "$3"); do grep -qF -- "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done
  return 1
}
wait_exit() {   # sets RC to the watcher's status, or 999 if it is still running after <tenths>
  local _
  RC=999
  for _ in $(seq 1 "$1"); do
    kill -0 "$WATCHER_PID" 2>/dev/null || { wait "$WATCHER_PID"; RC=$?; WATCHER_PID=""; return; }
    sleep 0.1
  done
}
stop_watcher() {
  [ -n "$WATCHER_PID" ] || return 0
  kill -TERM "$WATCHER_PID" 2>/dev/null; wait "$WATCHER_PID" 2>/dev/null; WATCHER_PID=""
}
sentinel_stamped() { compgen -G "$TMP/$1/ws/state/watch-tasks-stream*.pid" > /dev/null; }
launch_count() { wc -l < "$TMP/$1/launches" | tr -d ' '; }

# --- a) the default monitor is silent ----------------------------------------
make_case a poll_monitor
start_watcher a
printf 'id: task-early\naccess_tier: owner\ntask: swept\n' > "$TMP/a/ws/tasks/task-early.txt"
wait_for "$TMP/a/out" "TASK_FILE: task-early.txt" 150
check $? "a) readiness passes after the fallback, and the startup sweep runs"
sentinel_stamped a
check $? "a) the sentinel is stamped"
grep -qF "relaunching fswatch with poll_monitor" "$TMP/a/err"
check $? "a) the watcher says it fell back to poll_monitor"
first="$(sed -n 1p "$TMP/a/launches")"; second="$(sed -n 2p "$TMP/a/launches")"
case "$first" in *"-m "*) false ;; *) true ;; esac
check $? "a) the first launch takes fswatch's default monitor ($first)"
case "$second" in *"-m poll_monitor "*) true ;; *) false ;; esac
check $? "a) the second launch names poll_monitor ($second)"
printf 'id: task-late\naccess_tier: owner\ntask: heard\n' > "$TMP/a/ws/tasks/task-late.txt"
wait_for "$TMP/a/out" "TASK_FILE: task-late.txt" 50
check $? "a) a task dropped after readiness is announced through events within 5 s (catch-up is 600 s)"
kill -TERM "${second%% *}" 2>/dev/null
wait_for "$TMP/a/err" "fswatch relaunched as pid" 100
third="$(sed -n 3p "$TMP/a/launches")"
case "$third" in *"-m poll_monitor "*) true ;; *) false ;; esac
check $? "a) a relaunch after fswatch dies keeps poll_monitor ($third)"
stop_watcher

# --- b) a named monitor is used from the start --------------------------------
make_case b kqueue_monitor
start_watcher b SUTANDO_FSWATCH_MONITOR=kqueue_monitor
printf 'id: task-early\naccess_tier: owner\ntask: swept\n' > "$TMP/b/ws/tasks/task-early.txt"
wait_for "$TMP/b/out" "TASK_FILE: task-early.txt" 150
check $? "b) readiness passes on the named monitor"
case "$(sed -n 1p "$TMP/b/launches")" in *"-m kqueue_monitor "*) true ;; *) false ;; esac
check $? "b) the first launch names kqueue_monitor"
[ "$(launch_count b)" = 1 ] && ! grep -qF "relaunching fswatch" "$TMP/b/err"
check $? "b) and there is no fallback"
stop_watcher

# --- c) the named monitor is silent: the owner's choice stands ------------------
make_case c poll_monitor
start_watcher c SUTANDO_FSWATCH_MONITOR=fsevents_monitor
wait_exit 100
[ "$RC" = 1 ]
check $? "c) the watcher exits 1 (rc $RC)"
grep -qF "fswatch monitor: fsevents_monitor" "$TMP/c/err"
check $? "c) its message names the monitor it was given"
[ "$(launch_count c)" = 1 ]
check $? "c) fswatch was launched once: a named monitor is not replaced"
! sentinel_stamped c
check $? "c) no sentinel is written"

# --- d) every monitor is silent -------------------------------------------------
make_case d
start_watcher d
wait_exit 100
[ "$RC" = 1 ]
check $? "d) the watcher exits 1 (rc $RC)"
grep -qF "fswatch monitor: poll_monitor" "$TMP/d/err"
check $? "d) its message names the fallback that also failed"
! sentinel_stamped d
check $? "d) no sentinel is written"

# --- e) a default monitor wedged on TERM, which then flushes the old probe -------
make_case e
echo wedged > "$TMP/e/default-mode"
start_watcher e
wait_exit 150
[ "$RC" = 1 ]
check $? "e) the watcher exits 1 rather than hanging on a monitor that ignores TERM (rc $RC)"
grep -qF "fswatch monitor: poll_monitor" "$TMP/e/err"
check $? "e) the round-1 probe flushed on TERM did not mark poll_monitor ready"
wedged="$(sed -n 1p "$TMP/e/launches")"; wedged="${wedged%% *}"
! kill -0 "$wedged" 2>/dev/null
check $? "e) the wedged fswatch ($wedged) was killed, not left running"
! sentinel_stamped e
check $? "e) no sentinel is written"

# --- f) the default monitor flushes the old probe on TERM and exits -------------
make_case f
echo flush > "$TMP/f/default-mode"
start_watcher f
wait_exit 100
[ "$RC" = 1 ] && ! sentinel_stamped f
check $? "f) a probe from the round before is no proof: exit 1 and no sentinel (rc $RC)"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ] || { for c in a b c d e f; do tail -3 "$TMP/$c/err" 2>/dev/null | sed "s/^/  err($c): /"; done; exit 1; }
