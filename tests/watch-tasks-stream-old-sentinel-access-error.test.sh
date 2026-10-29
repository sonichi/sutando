#!/bin/bash
# An old sentinel whose REAL payload is unreadable for the first resolver call
# (mode 000, restored right after) is retried and dispatched, never made final.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
if [ "$(id -u)" = "0" ]; then echo "SKIP: root can open a mode-000 file"; exit 0; fi

TMP="$(mktemp -d)"; trap 'chmod -R u+rwx "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
WS="$TMP/ws"; ID=w-test; INBOX="$WS/deliveries/$ID"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
STUBBIN="$TMP/stubbin"; mkdir -p "$STUBBIN"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"; chmod +x "$STUBBIN/fswatch"

PAYLOAD="$WS/tasks/task-locked.txt"
printf 'id: task-locked\nsource: chat\naccess_tier: owner\ntask: still here\n' > "$PAYLOAD"
chmod 000 "$PAYLOAD"
: > "$INBOX/task-locked.txt"
OLD="$(date -v-1H +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 hour ago' +%Y%m%d%H%M.%S)"
touch -t "$OLD" "$INBOX/task-locked.txt"

# The REAL pool resolver, wrapped only to count calls and to restore the payload
# after the first one: the access error is one-shot, as a transient one is.
COUNT="$TMP/count"; RESOLVER="$TMP/resolver.sh"
cat > "$RESOLVER" <<EOF
#!/bin/bash
n=0; [ -f '$COUNT' ] && n=\$(cat '$COUNT'); n=\$((n+1)); printf '%s' "\$n" > '$COUNT'
'$REPO/skills/worker-pool/scripts/resolve-inbox-entry' "\$@"; rc=\$?
[ "\$n" -eq 1 ] && chmod 600 '$PAYLOAD'
exit \$rc
EOF
chmod +x "$RESOLVER"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
set -m
PATH="$STUBBIN:$PATH" SUTANDO_INBOX_RESOLVER="$RESOLVER" SUTANDO_WORKSPACE_DIR="$WS" \
  SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE="$ID" SUTANDO_RESOLVE_RACE_WINDOW_S=10 \
  bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role session --inbox "$INBOX" > "$OUT" 2>"$ERR" &
pid=$!
set +m
for i in $(seq 1 200); do
  grep -q "TASK_FILE: $PAYLOAD" "$OUT" 2>/dev/null && break
  grep -q -E 'after [0-9]+ attempts; not dispatching|names no payload and is' "$ERR" 2>/dev/null && break
  sleep 0.1
done
calls="$(cat "$COUNT" 2>/dev/null || echo 0)"
echo "  resolver calls: $calls; stderr: $(grep -E 'resolve_inbox_entry|not dispatching' "$ERR" | head -2 | cut -c1-140 | tr '\n' '|')"
grep -q "TASK_FILE: $PAYLOAD" "$OUT"
check $? "an old sentinel whose payload was unreadable once is dispatched (not final on the access error)"
[ "$calls" = "2" ]
check $? "...on the second resolver call"
grep -q '(rc=1, first line: <empty>)' "$ERR"
check $? "...and the first call came back as the retryable rc 1, never the typed 3"
! grep -q 'names no payload' "$ERR"
check $? "...so the typed verdict was never given for a payload that exists"

kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
