#!/bin/bash
# A recipient can be woken by a sentinel whose body lives elsewhere: the entry
# that appears in its inbox is zero bytes and the task text is in tasks/. The
# core must not learn that mapping (it belongs to whoever wrote the sentinel),
# so it runs a resolver it was handed — and refuses rather than dispatch an
# entry it could not resolve. With no resolver the behaviour is unchanged.
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

# 2b. A relative answer is refused, not misdispatched: `-f` is checked against
#     the CALLER's cwd, not the resolver's, so a bare relative name almost
#     never happens to name a real file there. Fails safe, silently — the
#     header comment says so; this pins that the refusal actually fires.
RELATIVE="$(mk relative.sh "#!/bin/sh
cd /
echo 'tasks/task-probe1.txt'")"
export SUTANDO_INBOX_RESOLVER="$RELATIVE"
out="$(resolve_inbox_entry "$INBOX/task-probe1.txt" 2>/dev/null)"; rc=$?
[ "$rc" = "3" ] && [ -z "$out" ]
check $? "a relative resolver answer is refused rather than misdispatched"

# 2c. Bounded: a resolver that never returns must not hang the watcher.
#     `SUTANDO_INBOX_RESOLVER_TIMEOUT=1` keeps this test itself fast; the
#     resolver sleeps far longer, so the ONLY way this returns quickly is if
#     the timeout actually fired.
if command -v timeout >/dev/null 2>&1; then
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
else
  echo '  skip a hanging resolver is bounded — no timeout binary on this host'
fi

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

# 7. The shipped path, not the module alone: the real watcher's initial sweep
#    over a sentinel inbox must announce the payload's name, never the sentinel's.
#    Same basename either way, so assert on the FILE the emitted name resolves to.
#    `set -m` is not optional: the watcher's cleanup ends in `kill -TERM 0`, so
#    sharing this runner's process group would kill the test that started it.
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

# The assertion that makes the one above a result: a refusing resolver leaves
# the sweep SILENT, so the emit came from resolution, not from the sentinel
# being surfaced regardless.
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
