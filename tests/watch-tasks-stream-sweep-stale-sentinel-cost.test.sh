#!/bin/bash
# A startup sweep over an inbox full of stale sentinels used to pay three resolver
# launches plus two sleeps per sentinel: the retry that protects a FRESH sentinel
# from racing its payload's write was applied to sentinels hours old too, so the
# sweep grew with the inbox's history (measured 0.7 s per sentinel, 337 → 3 min 48 s).
# A sentinel older than the race window gets one attempt; a fresh one keeps three.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; INBOX="$WS/deliveries/w-test"
mkdir -p "$WS/tasks" "$INBOX" "$WS/results" "$WS/state"
N=12
for i in $(seq 1 $N); do
  : > "$INBOX/task-stale$i.txt"
  # Backdated well past the race window: these payloads are long gone.
  touch -t "$(date -v-1H +%Y%m%d%H%M.%S 2>/dev/null || date -d '1 hour ago' +%Y%m%d%H%M.%S)" "$INBOX/task-stale$i.txt"
done

COUNT="$TMP/count"
REFUSE="$TMP/refuse.sh"
printf '%s\n' '#!/bin/sh' "n=0; [ -f '$COUNT' ] && n=\$(cat '$COUNT'); n=\$((n+1)); printf '%s' \"\$n\" > '$COUNT'" 'echo "no payload" >&2; exit 1' > "$REFUSE"
chmod +x "$REFUSE"

OUT="$TMP/out"; ERR="$TMP/err"; : > "$OUT"; : > "$ERR"
start=$(date +%s)
set -m
SUTANDO_INBOX_RESOLVER="$REFUSE" SUTANDO_WORKSPACE_DIR="$WS" SUTANDO_RESULTS_DIR="$WS/results" SUTANDO_INSTANCE=w-test \
  SUTANDO_RESOLVE_RACE_WINDOW_S=10 \
  bash "$REPO/src/watch-tasks-stream.sh" "$INBOX" --role standby --inbox "$INBOX" > "$OUT" 2>"$ERR" &
pid=$!
set +m
# The sweep is over once every stale sentinel has been given up on.
for i in $(seq 1 120); do
  [ "$(grep -c 'did not resolve' "$ERR" 2>/dev/null)" -ge "$N" ] && break
  sleep 0.25
done
sweep_end=$(date +%s)
calls_after_sweep="$(cat "$COUNT" 2>/dev/null || echo 0)"
echo "  sweep over $N stale sentinels: $calls_after_sweep resolver calls in $((sweep_end - start))s"
[ "$calls_after_sweep" = "$N" ]
check $? "a stale sentinel costs the sweep exactly one resolver call (was three plus two sleeps)"
grep -q 'did not resolve.*after 1 attempts' "$ERR"
check $? "...and the give-up is logged as one attempt, not a silent drop"

# A FRESH sentinel arriving after the sweep still gets the race-window retries.
: > "$INBOX/task-fresh.txt"
for i in $(seq 1 60); do
  [ "$(grep -c 'did not resolve' "$ERR" 2>/dev/null)" -ge $((N + 1)) ] && break
  sleep 0.25
done
calls_total="$(cat "$COUNT" 2>/dev/null || echo 0)"
echo "  after a fresh sentinel: $calls_total resolver calls in total"
[ "$calls_total" = "$((N + 3))" ]
check $? "a fresh sentinel still gets three attempts (the race window is kept)"
grep -q 'task-fresh.txt after 3 attempts' "$ERR"
check $? "...logged as three attempts for the fresh one"

kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null

echo
if [ "$fail" = "0" ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail of $((pass+fail)) checks failed"; exit 1; fi
