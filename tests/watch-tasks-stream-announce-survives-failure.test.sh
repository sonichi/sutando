#!/bin/bash
# A resolved sentinel's payload path must survive a HANDLER FAILURE too, not
# just direct dispatch — a bare-basename fallback re-points the reader at the empty sentinel.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
PAYLOAD="$WS/tasks/task-probe1.txt"
printf 'id: task-probe1\naccess_tier: owner\ntask: resolve me\n' > "$PAYLOAD"
: > "$INBOX/task-probe1.txt"          # the sentinel: zero bytes, by design

RESOLVER="$TMP/resolver.sh"
printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$PAYLOAD" > "$RESOLVER"; chmod +x "$RESOLVER"

# Accepts the probe (queued as fallback-disposition), then FAILS the real
# run — finish_handler_task's rc!=0 fallback branch exists for this.
HANDLER="$TMP/handler.sh"
cat > "$HANDLER" << 'EOF'
#!/bin/sh
for a in "$@"; do [ "$a" = "--probe" ] && exit 0; done
exit 1
EOF
chmod +x "$HANDLER"

POLL_ITERS=0
run_sweep() {
  local outfile="$TMP/sweep.out" pid i
  : > "$outfile"
  set -m
  SUTANDO_INBOX_RESOLVER="$RESOLVER" SUTANDO_WORKSPACE_DIR="$WS" \
    SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE=w-test \
    SUTANDO_TASK_EVENT_HANDLER="$HANDLER" \
    bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$outfile" 2>"$TMP/sweep.err" &
  pid=$!
  set +m
  # A failed handler run takes a beat: probe, queue, spawn the real run, HANDLER_DONE.
  for i in $(seq 1 60); do grep -q 'TASK_FILE:' "$outfile" 2>/dev/null && break; sleep 0.25; done
  POLL_ITERS=$i
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  grep 'TASK_FILE:' "$outfile" 2>/dev/null | head -1
}

line="$(run_sweep)"
echo "  watcher emitted after handler failure: ${line:-<nothing>}"
[ "$line" = "TASK_FILE: $PAYLOAD" ]
rc=$?
check $rc "a failed handler's fallback emission still names the resolved payload path"
# 2>/dev/null on sed would make a missing file print as silently empty.
dump_file() { [ -f "$1" ] && sed 's/^/    /' "$1" || echo "    <file missing: $1>"; }
# Distinguishes never-emitted (POLL_ITERS=60) from a wrong path caught early.
if [ "$rc" != "0" ]; then
  echo "  poll iterations before kill: $POLL_ITERS/60"
  echo "  watcher stdout:"; dump_file "$TMP/sweep.out"
  echo "  watcher stderr:"; dump_file "$TMP/sweep.err"
fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
