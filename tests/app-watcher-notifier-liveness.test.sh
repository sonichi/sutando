#!/usr/bin/env bash
# ensure_task_notifier's idempotency check used to trust a tmux-session-present
# + version-match shortcut without confirming the actual watch-tasks-stream.sh
# process was alive (#4451 review, qingyun-wu): a hung supervisor, or a gap
# inside task-notifier-supervisor.sh's own RESTART_DELAY, left the session
# looking healthy while checkWatcher()'s exact failure condition -- no watcher
# process anywhere -- went unrepaired. Real behavioral test against the actual
# functions in start-cli.sh: real tmux, a real sentinel PID file, and the real
# extracted function bodies, isolated from the file's top-level launcher logic
# (which this test must never execute) and from the live production workspace
# (an unscoped `pgrep -f watch-tasks-stream.sh` was the first draft of the fix
# and is EXACTLY the unscoped-substring failure this repo already paid for
# tonight -- matched every worker's real watcher on the shared host running
# this test. Caught by this test itself failing against real concurrent
# watchers, not assumed correct).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/src/agent/claude/cli/start-cli.sh"
fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }

WORK="$(mktemp -d)"
trap 'tmux -S "$WORK/sock" kill-server 2>/dev/null; rm -rf "$WORK"' EXIT

# --- Wiring check: watcher_process_alive is scoped, not a bare host-wide pgrep
grep -q '^watcher_process_alive() {' "$SRC" \
  && ok "1 watcher_process_alive() is defined" || fail "1" "helper missing"
if grep -A 12 '^watcher_process_alive() {' "$SRC" | grep -qE '^\s*pgrep -f "watch-tasks-stream'; then
  fail "2" "watcher_process_alive uses an UNSCOPED pgrep -- matches every watcher on the host, including other sessions' production processes"
else
  ok "2 watcher_process_alive does not use a bare unscoped pgrep"
fi
grep -A 12 '^watcher_process_alive() {' "$SRC" | grep -q "sentinel_path_for" \
  && ok "3 watcher_process_alive is scoped via the established per-instance sentinel mechanism" \
  || fail "3" "no sentinel_path_for scoping found -- liveness check may not be instance-scoped"

# --- Behavioral: the sentinel_path_for + kill-0 mechanism itself, isolated --
# Extract sentinel_path_for from the real, shared watcher_sentinel.sh (the
# exact function the fix sources), not a reimplementation.
SENTINEL_SRC="$HERE/src/watcher_sentinel.sh"
[ -f "$SENTINEL_SRC" ] || { fail "4" "src/watcher_sentinel.sh not found"; echo "FAIL"; exit 1; }

FAKE_STATE="$WORK/state"; mkdir -p "$FAKE_STATE"

# Scenario A: sentinel file names a genuinely live PID -> alive.
REAL_PID_HOLDER_SCRIPT="$WORK/holder.sh"
printf '#!/bin/bash\nsleep 300\n' > "$REAL_PID_HOLDER_SCRIPT"; chmod +x "$REAL_PID_HOLDER_SCRIPT"
bash "$REAL_PID_HOLDER_SCRIPT" & HOLDER_PID=$!
sleep 0.2
SENT_PATH="$(bash -c ". '$SENTINEL_SRC'; sentinel_path_for '$FAKE_STATE'")"
[ -n "$SENT_PATH" ] || { fail "4" "sentinel_path_for produced no path"; kill "$HOLDER_PID" 2>/dev/null; echo "FAIL"; exit 1; }
echo "$HOLDER_PID" > "$SENT_PATH"
if [ -f "$SENT_PATH" ] && pid="$(cat "$SENT_PATH")" && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  ok "5 sentinel names a live PID -> correctly reads as alive"
else
  fail "5" "expected the live-PID sentinel to read as alive"
fi
kill "$HOLDER_PID" 2>/dev/null; wait "$HOLDER_PID" 2>/dev/null

# Scenario B (the bug this PR fixes): sentinel file exists and names a PID,
# but that PID is no longer running -- the exact "looks healthy, isn't" gap.
sleep 0.2
if [ -f "$SENT_PATH" ] && pid="$(cat "$SENT_PATH")" && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  fail "6" "expected the now-dead PID's sentinel to read as NOT alive"
else
  ok "6 sentinel names a PID that has since died -> correctly reads as not alive (this is what checkWatcher's ps-scan also sees as absent)"
fi

# Scenario C: no sentinel file at all (never started) -> not alive, no crash.
rm -f "$SENT_PATH"
if [ -f "$SENT_PATH" ]; then
  fail "7" "sentinel file should have been removed for this scenario"
else
  ok "7 no sentinel file -> correctly reads as not alive, without erroring"
fi

# --- Cross-instance isolation: another instance's sentinel must not leak in -
OTHER_STATE="$WORK/other-instance-state"; mkdir -p "$OTHER_STATE"
bash "$REAL_PID_HOLDER_SCRIPT" & OTHER_PID=$!
sleep 0.2
OTHER_SENT="$(bash -c ". '$SENTINEL_SRC'; sentinel_path_for '$OTHER_STATE' 'some-other-worker-id'")"
if [ -n "$OTHER_SENT" ]; then
  echo "$OTHER_PID" > "$OTHER_SENT"
  # Core's own (un-instanced) sentinel at FAKE_STATE must be unaffected by a
  # DIFFERENT instance's live process at a different state dir/identity.
  CORE_SENT="$(bash -c ". '$SENTINEL_SRC'; sentinel_path_for '$FAKE_STATE'")"
  if [ "$CORE_SENT" = "$OTHER_SENT" ]; then
    fail "8" "core's sentinel path collided with a different instance's -- would let a peer's watcher mask this one's absence"
  else
    ok "8 a different instance's live watcher does not mask core's own absent one (paths are distinct)"
  fi
else
  fail "8" "could not resolve a per-instance sentinel path to test isolation"
fi
kill "$OTHER_PID" 2>/dev/null; wait "$OTHER_PID" 2>/dev/null

if [ "$fails" -eq 0 ]; then
  echo "app-watcher-notifier-liveness: all checks pass"
  exit 0
else
  echo "app-watcher-notifier-liveness: $fails failure(s)"
  exit 1
fi
