#!/usr/bin/env bash
# startup's watch-tasks-stream reaper must only delete the sentinel it inspected.
#
# Unlinking a sentinel this reap did not inspect strands a live watcher untrackable.
# Case 3 makes that window deterministic: the `ps` shim re-stamps the file mid-reap.
#
# Run: bash tests/startup-watcher-reaper-ownership.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "startup watch-tasks-stream reaper ownership:"

# The function under test is the production one, sourced, not a copy.
# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh"

# Bailing on a missing helper would make this suite skip on base rather than fail,
# proving nothing; the wiring assertions below are what name the defect pre-fix.
have_fn=1
if declare -F reap_stale_task_watcher > /dev/null; then
  ok "reap_stale_task_watcher is defined in src/startup-runtime.sh"
else
  bad "reap_stale_task_watcher is defined in src/startup-runtime.sh" "not found"
  have_fn=0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ "$have_fn" -eq 1 ]; then

dead_pid() {
  # A pid that is certainly not running: spawn and reap one.
  local p
  bash -c 'exit 0' &
  p=$!
  wait "$p" 2>/dev/null
  echo "$p"
}

# --- case 1: sentinel names a dead pid -> removed (pre-existing behavior) -----
f="$TMP/case1.pid"
echo "$(dead_pid)" > "$f"
out="$(reap_stale_task_watcher "$f" 2>&1)"
if [ ! -e "$f" ]; then
  ok "dead pid: sentinel removed"
else
  bad "dead pid: sentinel removed" "still present ($out)"
fi

# --- case 2: sentinel names a LIVE watcher -> signaled, sentinel removed ------
# Real `ps` here: the process genuinely carries watch-tasks-stream in its argv.
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/watch-tasks-stream.sh" << 'SH'
#!/bin/bash
trap 'exit 0' TERM
sleep 30
SH
chmod +x "$TMP/fakebin/watch-tasks-stream.sh"
bash "$TMP/fakebin/watch-tasks-stream.sh" &
live=$!
f="$TMP/case2.pid"
echo "$live" > "$f"
# Give the child a moment to appear in the process table.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ps -p "$live" -o args= 2>/dev/null | grep -q watch-tasks-stream && break
  sleep 0.1
done
out="$(reap_stale_task_watcher "$f" 2>&1)"
wait "$live" 2>/dev/null
if [ ! -e "$f" ]; then
  ok "live stale watcher: sentinel removed"
else
  bad "live stale watcher: sentinel removed" "still present ($out)"
fi
if ! kill -0 "$live" 2>/dev/null; then
  ok "live stale watcher: signaled"
else
  kill -KILL "$live" 2>/dev/null
  bad "live stale watcher: signaled" "still running"
fi

# --- case 3 (the guard): a live watcher re-stamps DURING the reap -------------
# The `ps` shim re-stamps where a real watcher would: after the read, before delete.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/ps" << 'SH'
#!/bin/bash
printf '%s' "$SHIM_NEW_OWNER" > "$SHIM_PID_FILE"
exit 1
SH
chmod +x "$TMP/bin/ps"

f="$TMP/case3.pid"
stale="$(dead_pid)"
echo "$stale" > "$f"
export SHIM_PID_FILE="$f" SHIM_NEW_OWNER=424242
out="$(PATH="$TMP/bin:$PATH" reap_stale_task_watcher "$f" 2>&1)"
unset SHIM_PID_FILE SHIM_NEW_OWNER

if [ -e "$f" ]; then
  ok "re-stamped mid-reap: live watcher's sentinel survives"
else
  bad "re-stamped mid-reap: live watcher's sentinel survives" \
    "deleted a sentinel the reap never inspected ($out)"
fi
if [ "$(cat "$f" 2>/dev/null)" = "424242" ]; then
  ok "re-stamped mid-reap: sentinel still names the live watcher"
else
  bad "re-stamped mid-reap: sentinel still names the live watcher" \
    "content='$(cat "$f" 2>/dev/null)'"
fi

# --- case 4: no sentinel -> no-op, success ------------------------------------
if reap_stale_task_watcher "$TMP/absent.pid" > /dev/null 2>&1; then
  ok "absent sentinel: no-op, rc 0"
else
  bad "absent sentinel: no-op, rc 0" "non-zero rc"
fi
fi  # have_fn

# --- wiring: startup.sh must delegate, and reap only THIS instance -------------
# This used to assert `sentinel_paths_in` — the enumeration of EVERY instance's
# sentinel. The reaper deliberately kills a watcher that owns its sentinel, so
# that loop killed a peer's live watcher and removed its record.
if grep -q 'reap_stale_task_watcher "\$__sentinel"' "$REPO/src/startup.sh" \
   && grep -q 'sentinel_path_for "\$WORKSPACE/state"' "$REPO/src/startup.sh"; then
  ok "startup.sh delegates to the shared reaper for its OWN sentinel"
else
  bad "startup.sh delegates to the shared reaper" "call site not found"
fi
if grep -q 'sentinel_paths_in "\$WORKSPACE/state"' "$REPO/src/startup.sh"; then
  bad "startup.sh reaps only its own identity" "it still enumerates every instance's sentinel"
else
  ok "startup.sh reaps only its own identity"
fi

# --- peer survival: the property the enumeration assertion could not express ---
# LIMIT, measured: this case calls sentinel_path_for + the reaper DIRECTLY, so it
# proves the scoped shape is safe -- it does NOT re-fail if startup.sh regresses
# to the enumeration. The two greps above are what pin the wiring; restoring the
# loop failed exactly those two while this case still reported ok.
if [ -n "${have_fn:-}" ] && command -v sentinel_path_for >/dev/null 2>&1; then
  _pt="$(mktemp -d)"; mkdir -p "$_pt/state" "$_pt/src"
  printf '#!/bin/bash\nsleep 60\n' > "$_pt/src/watch-tasks-stream.sh"; chmod +x "$_pt/src/watch-tasks-stream.sh"
  bash "$_pt/src/watch-tasks-stream.sh" & _a=$!
  bash "$_pt/src/watch-tasks-stream.sh" & _b=$!
  sleep 2
  echo "$_a" > "$_pt/state/watch-tasks-stream.pid"
  echo "$_b" > "$_pt/state/watch-tasks-stream-peer-b+w2.pid"
  if kill -0 "$_a" 2>/dev/null && kill -0 "$_b" 2>/dev/null; then
    if _s="$(sentinel_path_for "$_pt/state")" && [ -n "$_s" ]; then
      reap_stale_task_watcher "$_s" >/dev/null 2>&1
    fi
    sleep 1
    if kill -0 "$_b" 2>/dev/null && [ -f "$_pt/state/watch-tasks-stream-peer-b+w2.pid" ]; then
      ok "a peer instance's live watcher and sentinel both survive this startup"
    else
      bad "a peer instance survives this startup" "the peer was killed or its sentinel removed"
    fi
    if kill -0 "$_a" 2>/dev/null; then
      bad "this instance's own stale watcher is still reaped" "it survived, so the reap did nothing"
    else
      ok "this instance's own stale watcher is still reaped"
    fi
  else
    bad "peer-survival fixture" "a fixture watcher was not alive; the case measured nothing"
  fi
  kill "$_a" "$_b" 2>/dev/null; rm -rf "$_pt"
fi
if grep -q 'rm -f "\$WATCHER_PID_FILE"' "$REPO/src/startup.sh"; then
  bad "startup.sh keeps no unguarded copy" "the inline rm -f is still there"
else
  ok "startup.sh keeps no unguarded copy"
fi

if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
fi
echo "FAILED ($fails)"
exit 1
