#!/bin/bash
# The core seat's own case, at the shipped default: a session watcher on <ws>/tasks whose fswatch
# answered the readiness probe and then went silent still announces a task dropped after that, within
# the default 30 s catch-up (sonichi/sutando#4590: a live core watcher sat on a task for 13 minutes).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/tests/fixtures/clean-watcher-env.sh"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

# Hermetic on a live host: no seat's workspace, interval or handler leaks in, so the default applies.
for v in $(compgen -e | grep '^SUTANDO_'); do unset "$v"; done
TMP="$(mktemp -d)"
WATCHER_PID=""
trap 'if [ -n "$WATCHER_PID" ]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null; fi; rm -rf "$TMP"' EXIT

DEFAULT=30
WS="$TMP/ws"; INBOX="$WS/tasks"
mkdir -p "$INBOX" "$WS/results" "$WS/state" "$TMP/bin" "$TMP/tmp"
# Reports the watcher's readiness probe, then never reports again: an fseventsd stall after subscribing.
cat > "$TMP/bin/fswatch" <<EOF
#!/bin/bash
while :; do
  for f in "$INBOX"/.ready-*; do
    [ -e "\$f" ] && { printf '%s\n' "\$f"; exec tail -f /dev/null; }
  done
  sleep 0.1
done
EOF
chmod +x "$TMP/bin/fswatch"

# Waiting before start: the startup sweep announces it, and that line marks the sweep as done.
printf 'id: task-early\naccess_tier: owner\ntask: swept\n' > "$INBOX/task-early.txt"
OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
PATH="$TMP/bin:$PATH" TMPDIR="$TMP/tmp" SUTANDO_WORKSPACE_DIR="$WS" SUTANDO_RESULTS_DIR="$WS/results" \
  bash "$REPO/src/watch-tasks-stream.sh" --role session --inbox "$INBOX" > "$OUT" 2>"$ERR" &
WATCHER_PID=$!

for _ in $(seq 1 300); do
  grep -qF "TASK_FILE: task-early.txt" "$OUT" && break
  sleep 0.1
done
compgen -G "$WS/state/watch-tasks-stream*.pid" > /dev/null && grep -qF "TASK_FILE: task-early.txt" "$OUT"
check $? "the session watcher passed its readiness round trip, stamped its sentinel and finished its startup sweep"
sleep 1

printf 'id: task-silent\naccess_tier: owner\ntask: find me\n' > "$INBOX/task-silent.txt"
dropped=$SECONDS
seen=""
for _ in $(seq 1 $(( (DEFAULT + 5) * 10 ))); do
  grep -qF "TASK_FILE: task-silent.txt" "$OUT" && { seen=$SECONDS; break; }
  sleep 0.1
done
took="never"; [ -z "$seen" ] || took="$(( seen - dropped ))s"
[ -n "$seen" ] && [ $(( seen - dropped )) -le $(( DEFAULT + 3 )) ]
check $? "a task no fswatch event reports is announced within the ${DEFAULT}s default + 3s (took $took)"
[ "$(grep -cF "TASK_FILE: task-silent.txt" "$OUT")" = 1 ]
check $? "...exactly once"
kill -0 "$WATCHER_PID" 2>/dev/null
check $? "...and the watcher is still running"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ] || { tail -5 "$ERR" | sed 's/^/  err: /'; exit 1; }
