#!/bin/bash
# A woken sentinel can be zero bytes with its real body elsewhere; the core runs
# a resolver it was handed and refuses rather than dispatch what it can't resolve.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

# shellcheck source=../src/inbox-resolve.sh
source "$REPO/src/inbox-resolve.sh"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
PAYLOAD="$WS/tasks/task-probe1.txt"
printf 'id: task-probe1\ntask: resolve me\n' > "$PAYLOAD"
: > "$INBOX/task-probe1.txt"          # the sentinel: zero bytes, by design

mk() { printf '%s\n' "$2" > "$TMP/$1"; chmod +x "$TMP/$1"; printf '%s' "$TMP/$1"; }
GOOD="$(mk good.sh "#!/bin/sh
printf '%s\\n' \"$PAYLOAD\"")"
BANNER="$(mk banner.sh "#!/bin/sh
echo 'sitecustomize: loaded'
printf '%s\\n' \"$PAYLOAD\"")"
ANGRY="$(mk angry.sh "#!/bin/sh
printf '%s\\n' \"$PAYLOAD\"
exit 1")"
GHOST="$(mk ghost.sh "#!/bin/sh
printf '%s\\n' '$WS/tasks/task-does-not-exist.txt'")"

# 1. The control that makes every case below a result: an unset resolver leaves
#    the entry exactly as it arrived, so a core that uses no pool is untouched.
unset SUTANDO_INBOX_RESOLVER
out="$(resolve_inbox_entry "$WS/tasks/task-probe1.txt")"; rc=$?
[ "$rc" = "0" ] && [ "$out" = "$WS/tasks/task-probe1.txt" ]
check $? "no resolver configured — the entry is dispatched unchanged"

# 2. The case this exists for: a sentinel becomes its payload.
export SUTANDO_INBOX_RESOLVER="$GOOD"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "0" ] && [ "$out" = "$PAYLOAD" ]
check $? "a sentinel resolves to the tasks/ payload"

# 2b. A relative answer is refused outright (this cwd has no matching file,
#     so bare `-f` would refuse it by luck too — 2c below is the real trap).
RELATIVE="$(mk relative.sh "#!/bin/sh
cd /
echo 'tasks/task-probe1.txt'")"
export SUTANDO_INBOX_RESOLVER="$RELATIVE"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]
check $? "a relative resolver answer is refused rather than misdispatched"

# 2c. The trap the bare-`-f` form misses: a relative answer that HAPPENS to
#     name a real file in the watcher's own cwd must still be refused.
COINCIDENCE="$(mk coincidence.sh "#!/bin/sh
printf '%s\\n' 'coincidental-name.txt'")"
: > "$TMP/coincidental-name.txt"
export SUTANDO_INBOX_RESOLVER="$COINCIDENCE"
( cd "$TMP" && out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
  [ "$rc" = "3" ] && [ -z "$out" ] )
check $? "a relative answer that coincidentally exists in the caller's cwd is still refused"

# 2c. Bounded: a resolver that never returns must not hang the watcher, on
#     whichever branch this host takes (GNU timeout or the hand-rolled watchdog).
SLOW="$(mk slow.sh "#!/bin/sh
sleep 30
printf '%s\\n' \"$PAYLOAD\"")"
export SUTANDO_INBOX_RESOLVER="$SLOW" SUTANDO_INBOX_RESOLVER_TIMEOUT=1
start=$(date +%s)
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
elapsed=$(( $(date +%s) - start ))
unset SUTANDO_INBOX_RESOLVER_TIMEOUT
[ "$rc" = "3" ] && [ -z "$out" ] && [ "$elapsed" -lt 10 ]
check $? "a hanging resolver is bounded, not left to block the watcher forever (elapsed ${elapsed}s)"

# 2d/2e. The hand-rolled watchdog, FORCED (a PATH holding only the tools the
#     resolver needs and no `timeout`), so both branches are measured on every
#     host -- a fallback exercised only where GNU timeout is absent is green on
#     CI and red on the real install (kewei, #4238 round 4).
NOBIN="$TMP/nobin"; mkdir -p "$NOBIN"
for t in sleep cat mktemp head rm printf sh; do b="$(command -v "$t" 2>/dev/null)"; [ -n "$b" ] && ln -s "$b" "$NOBIN/$t"; done
export SUTANDO_INBOX_RESOLVER="$GOOD" SUTANDO_INBOX_RESOLVER_TIMEOUT=3
start=$(date +%s)
out="$(PATH="$NOBIN" resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" = "0" ] && [ "$out" = "$PAYLOAD" ] && [ "$elapsed" -le 1 ]
check $? "fallback watchdog: a fast resolver returns at once, not at the deadline (elapsed ${elapsed}s of 3)"
STUBBORN="$(mk stubborn.sh "#!/bin/sh
trap '' TERM
sleep 30
printf '%s\\n' \"$PAYLOAD\"")"
export SUTANDO_INBOX_RESOLVER="$STUBBORN" SUTANDO_INBOX_RESOLVER_TIMEOUT=1
start=$(date +%s)
out="$(PATH="$NOBIN" resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
elapsed=$(( $(date +%s) - start ))
unset SUTANDO_INBOX_RESOLVER_TIMEOUT
[ "$rc" = "3" ] && [ -z "$out" ] && [ "$elapsed" -le 4 ]
check $? "fallback watchdog: a resolver that ignores TERM is KILLed at the deadline, not left running (elapsed ${elapsed}s of 1+1)"

# 3. A banner ahead of the answer is not the answer — the first line must BE a
#    file, or noise passes as a verdict.
export SUTANDO_INBOX_RESOLVER="$BANNER"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]
check $? "a resolver that prints a banner first is refused, not consumed"

# 4/5/6. Every other way of not answering is also a refusal, never a dispatch.
export SUTANDO_INBOX_RESOLVER="$ANGRY"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]; check $? "a resolver exiting non-zero is refused even though it printed a real path"
export SUTANDO_INBOX_RESOLVER="$GHOST"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]; check $? "a resolver naming a nonexistent file is refused"
export SUTANDO_INBOX_RESOLVER="$TMP/not-installed.sh"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]; check $? "a resolver that is not executable is refused"

# 7. The real watcher's initial sweep must announce the payload's name, not the
#    sentinel's. `set -m` isolates this runner's group from the watcher's `kill -TERM 0`.
run_sweep() {
  local resolver="${1:-}" outfile="$TMP/sweep.out" pid i
  : > "$outfile"
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
#    Sentinel and payload share a basename by construction, so a bare name can
#    not distinguish them — the announcement has to carry the path.
line="$(run_sweep "$GOOD")"
echo "  watcher emitted: ${line:-<nothing>}"
[ "$line" = "TASK_FILE: $PAYLOAD" ]
check $? "the sweep announces the payload by path, not the sentinel's name"

# What makes the check above meaningful: a refusing resolver leaves the sweep
# SILENT, so the emit above came from resolution, not the sentinel regardless.
line_refused="$(run_sweep "$GHOST")"
echo "  with a refusing resolver: ${line_refused:-<nothing>}"
[ -z "$line_refused" ]; check $? "an unresolvable sentinel is not surfaced to the agent at all"

# 8. The invariance control: with no resolver the wire format is exactly what
#    every existing core already consumes — a bare basename, no path.
line_plain="$(run_sweep "")"
echo "  with no resolver: ${line_plain:-<nothing>}"
[ "$line_plain" = "TASK_FILE: task-probe1.txt" ]
check $? "no resolver — the announcement is the bare basename, unchanged"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
