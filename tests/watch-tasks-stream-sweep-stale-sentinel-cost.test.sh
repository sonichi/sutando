#!/bin/bash
# A stale sentinel (older than the race window, resolver says "no payload") costs
# the sweep one resolver call; a fresh one, or any other failure, keeps three.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
# Hermetic on any host: the polling stand-in for fswatch, first on PATH.
STUBBIN="$TMP/stubbin"; mkdir -p "$STUBBIN"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"; chmod +x "$STUBBIN/fswatch"

OLD="$(date -v-1H +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 hour ago' +%Y%m%d%H%M.%S)"
N=12
for i in $(seq 1 $N); do
  : > "$INBOX/task-stale$i.txt"
  touch -t "$OLD" "$INBOX/task-stale$i.txt"
done
# An old sentinel WITH a payload whose resolver fails once for its own reasons
# (rc 1, not the typed "no payload"): age alone must not make that final.
PAYLOAD="$WS/tasks/task-transient.txt"
printf 'id: task-transient\nsource: chat\ntask: still here\n' > "$PAYLOAD"
: > "$INBOX/task-transient.txt"
touch -t "$OLD" "$INBOX/task-transient.txt"

COUNT="$TMP/count"; TCOUNT="$TMP/tcount"
RESOLVER="$TMP/resolver.sh"
cat > "$RESOLVER" <<EOF
#!/bin/sh
n=0; [ -f '$COUNT' ] && n=\$(cat '$COUNT'); n=\$((n+1)); printf '%s' "\$n" > '$COUNT'
case "\$1" in
  */task-transient.txt)
    t=0; [ -f '$TCOUNT' ] && t=\$(cat '$TCOUNT'); t=\$((t+1)); printf '%s' "\$t" > '$TCOUNT'
    if [ "\$t" -lt 2 ]; then echo "resolver hiccup" >&2; exit 1; fi
    printf '%s\n' '$PAYLOAD'; exit 0 ;;
esac
echo "sentinel names no payload" >&2
exit 3
EOF
chmod +x "$RESOLVER"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
start=$(date +%s)
set -m
PATH="$STUBBIN:$PATH" SUTANDO_INBOX_RESOLVER="$RESOLVER" SUTANDO_WORKSPACE_DIR="$WS" \
  SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE=w-test SUTANDO_RESOLVE_RACE_WINDOW_S=10 \
  bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role session --inbox "$INBOX" > "$OUT" 2>"$ERR" &
pid=$!
set +m
# The session role stamps its sentinel only after the inbox answered a readiness
# probe, so the sentinel is proof the subscription is live.
for i in $(seq 1 150); do ls "$WS"/state/*.pid >/dev/null 2>&1 && break; sleep 0.1; done
ls "$WS"/state/*.pid >/dev/null 2>&1
check $? "the watcher subscribed to the inbox (readiness sentinel written)"
# The sweep is over once every stale sentinel was given up on and the old-but-real one dispatched.
for i in $(seq 1 120); do
  [ "$(grep -c 'names no payload and is' "$ERR" 2>/dev/null)" -ge "$N" ] && grep -q "TASK_FILE: $PAYLOAD" "$OUT" && break
  sleep 0.25
done
sweep_end=$(date +%s)
calls_after_sweep="$(cat "$COUNT" 2>/dev/null || echo 0)"
echo "  sweep over $N stale sentinels + 1 old real one: $calls_after_sweep resolver calls in $((sweep_end - start))s"
[ "$calls_after_sweep" = "$((N + 2))" ]
check $? "a stale sentinel costs the sweep exactly one resolver call (was three plus two sleeps)"
[ "$(grep -c 'names no payload and is .* old, past the 10s race window; not dispatching (1 attempt)' "$ERR")" = "$N" ]
check $? "...each give-up is logged as typed, aged, and one attempt"
grep -q "TASK_FILE: $PAYLOAD" "$OUT"
check $? "an old sentinel whose resolver failed once for its own reasons is retried and dispatched"
[ "$(cat "$TCOUNT" 2>/dev/null || echo 0)" = "2" ]
check $? "...on its second call (age never made a non-typed failure final)"

# A FRESH sentinel arriving after the sweep still gets the race-window retries.
: > "$INBOX/task-fresh.txt"
for i in $(seq 1 60); do
  grep -q 'task-fresh.txt after 3 attempts' "$ERR" 2>/dev/null && break
  sleep 0.25
done
calls_total="$(cat "$COUNT" 2>/dev/null || echo 0)"
echo "  after a fresh sentinel: $calls_total resolver calls in total"
[ "$calls_total" = "$((N + 2 + 3))" ]
check $? "a fresh sentinel still gets three attempts (the race window is kept)"
grep -q 'task-fresh.txt after 3 attempts' "$ERR"
check $? "...logged as three attempts for the fresh one"

kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
