#!/usr/bin/env bash
# Ties the worker launcher's tmux env allowlist to its caller: every variable
# the spawner sets AND the watcher reads must reach the worker's session, or
# its results/ land under deliveries/ where no drain looks. Derived from both
# files, so the next such variable is covered too. Was start-cli.sh's job
# (tested as start-cli-worker-env-forwarded.test.sh); a worker's own launch
# now lives in skills/worker-pool/scripts/launch-worker-session.sh.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WORKERCLI="$REPO/skills/worker-pool/scripts/launch-worker-session.sh"
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"
WATCHER="$REPO/src/watch-tasks-stream.sh"
# No proxy, no launchd job: the probe must not poll or wire a base URL here.
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WS="$TMP/ws"; WID="w-testworker"; INBOX="$WS/deliveries/$WID"
mkdir -p "$INBOX"

# The plan the spawner would hand the launcher, as KEY=VALUE lines.
python3 - "$REPO" "$WS" "$WID" > "$TMP/plan-env" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/skills/worker-pool/scripts")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime="claude",
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
[ -s "$TMP/plan-env" ]
check $? "spawn_worker.plan() produced a session env ($(wc -l < "$TMP/plan-env" | tr -d ' ') vars)"

# What the worker launcher forwards into tmux, under exactly that env.
# shellcheck disable=SC2046
env -i HOME="$HOME" PATH="$STUB_PATH" $(cat "$TMP/plan-env") \
    bash "$WORKERCLI" --print-env > "$TMP/fwd" 2>/dev/null
grep -q "SUTANDO_CORE_RUNTIME=claude" "$TMP/fwd"
check $? "the --print-env probe returned the assembled allowlist"
# Not the core marker: a worker launch carries the marker explicit-EMPTY, not
# omitted (omitting -e leaves a stale server-global value in place) and never
# the canonical core's "=1".
grep -qx -- "SUTANDO_CORE_SESSION=" "$TMP/fwd"
check $? "a worker session carries the core-session marker explicit-empty"
! grep -q "SUTANDO_CORE_SESSION=1" "$TMP/fwd"
check $? "a worker session is not handed the canonical core's marker"

# The required set is the INTERSECTION of what the spawner sets and what the
# watcher reads from env — neither file alone names it, so neither can drift.
WATCHER_READS="$(grep -oE '\$\{SUTANDO_[A-Z_]+:' "$WATCHER" | tr -d '${:' | sort -u)"
echo "  watcher reads from env: $(echo "$WATCHER_READS" | tr '\n' ' ')"
missing=""
while IFS='=' read -r k v; do
  echo "$WATCHER_READS" | grep -qx "$k" || continue
  grep -qx -- "$k=$v" "$TMP/fwd" || missing="$missing $k"
done < "$TMP/plan-env"
[ -z "$missing" ]
check $? "every var the spawner sets and the watcher reads is forwarded${missing:+ — MISSING:$missing}"

# The control for the check above: this is the line that fails at 5080eaf8f.
grep -qx -- "SUTANDO_WORKSPACE_DIR=$WS" "$TMP/fwd"
check $? "SUTANDO_WORKSPACE_DIR=$WS reaches the session"

# The worker gate is the pool skill's file; `/startup --worker` runs whatever
# this names, so the launcher must carry it without ever knowing the path.
BOOT="$(grep -m1 '^SUTANDO_WORKER_BOOTSTRAP=' "$TMP/plan-env" | cut -d= -f2-)"
[ -n "$BOOT" ] && grep -qx -- "SUTANDO_WORKER_BOOTSTRAP=$BOOT" "$TMP/fwd"
check $? "the worker gate the spawner names reaches the session"
# A negative needs a live instrument: the same pattern IS in this file, which
# reaches the spawner by path because it is the caller under test.
grep -q "skills/worker-pool" "${BASH_SOURCE[0]}"
check $? "control: the pool-path scan finds a real occurrence"
# The placement rule cuts the other way now: the WORKER launcher lives inside
# the skill and is expected to reference its own siblings freely; it's
# core/src/'s launcher that must never name a pool path (it arrives in env).
! grep -q "skills/worker-pool" "$STARTCLI"
check $? "the core's launcher names no pool path (it arrives in env)"

# Behaviour, not spelling: run the watcher's own resolution under the forwarded
# env and assert results/ lands in the shared workspace, not under deliveries/.
# shellcheck disable=SC2046
resolved="$(env -i HOME="$HOME" PATH="/usr/bin:/bin" \
  $(grep -E '^SUTANDO_(TASKS|WORKSPACE|RESULTS)_DIR=' "$TMP/fwd") \
  bash -c '
    TASKS_DIR="$SUTANDO_TASKS_DIR"; mkdir -p "$TASKS_DIR"
    TASKS_DIR_ABS="$(cd "$TASKS_DIR" && pwd -P)"
    WORKSPACE_DIR="${SUTANDO_WORKSPACE_DIR:-$(dirname "$TASKS_DIR_ABS")}"
    echo "${SUTANDO_RESULTS_DIR:-$WORKSPACE_DIR/results}"')"
[ "$(cd "$(dirname "$resolved")" && pwd -P)" = "$(cd "$WS" && pwd -P)" ]
check $? "the worker resolves RESULTS_DIR to the shared workspace (got $resolved)"
case "$resolved" in */deliveries/*) false ;; *) true ;; esac
check $? "the worker's results/ is not under deliveries/"

# The worker launcher REFUSES outright with no SUTANDO_TMUX_SESSION/INSTANCE_ID
# (spawn_worker.py always sets both) rather than silently launching a
# core-shaped session under a guessed name.
env -i HOME="$HOME" PATH="$STUB_PATH" bash "$WORKERCLI" --print-env > "$TMP/fwd-bare" 2>"$TMP/fwd-bare.err"
[ $? -ne 0 ]
check $? "an install with no worker env refuses rather than guessing a session name"

# A worker with an instance ID must have a usable delivery writer. The probe
# above can inspect incomplete env, but an actual launch must fail before tmux
# creates a session that cannot acknowledge pending tasks.
cat > "$TMP/bin/tmux" <<'SH'
#!/bin/sh
touch "$SUTANDO_TEST_TMUX_CALLED"
exit 99
SH
chmod +x "$TMP/bin/tmux"
for writer in unset "$TMP/missing-pool-delivery.py"; do
  if [ "$writer" = unset ]; then
    env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_TEST_TMUX_CALLED="$TMP/tmux-called" \
      SUTANDO_TMUX_SESSION="sutando-test-worker" SUTANDO_INSTANCE_ID="$WID" \
      bash "$WORKERCLI" > "$TMP/launch-without-writer.out" 2>&1
  else
    env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_TEST_TMUX_CALLED="$TMP/tmux-called" \
      SUTANDO_TMUX_SESSION="sutando-test-worker" SUTANDO_INSTANCE_ID="$WID" \
      SUTANDO_POOL_DELIVERY_SCRIPT="$writer" \
      bash "$WORKERCLI" > "$TMP/launch-without-writer.out" 2>&1
  fi
  rc=$?
  [ "$rc" -eq 2 ] && grep -q 'SUTANDO_POOL_DELIVERY_SCRIPT.*readable file' "$TMP/launch-without-writer.out" \
    && [ ! -e "$TMP/tmux-called" ]
  check $? "an actual worker launch refuses a $writer delivery writer before tmux"
done

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
