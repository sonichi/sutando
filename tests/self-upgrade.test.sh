#!/usr/bin/env bash
# Behavioral tests for skills/self-upgrade/scripts/upgrade.sh — the mechanical
# half of the safe self-upgrade. Real git repos + a real bare remote + a stub
# restart.sh; no mocking of the code under test. Exercises the three behaviors
# that actually matter:
#   A. aborts (exit 2) on a dirty working tree — never clobbers uncommitted work
#   B. no-ops (exit 0) when already at latest — nothing to pull
#   C. runs the restart in durable tmux — the load-bearing fix. Proven by TIMING
#      and by inspecting the real tmux session: the
#      stub restart.sh blocks for 3s (simulating startup.sh's foreground hang);
#      if upgrade.sh hands it off, upgrade.sh returns immediately; if it runs
#      inline (the "stuck" bug) it takes 3s+. We assert it returned
#      fast AND that restart.sh actually ran (marker file).
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
SRC_SCRIPT="${SELF_UPGRADE_SCRIPT_UNDER_TEST:-$REPO/skills/self-upgrade/scripts/upgrade.sh}"
[ -f "$SRC_SCRIPT" ] || { echo "FAIL: upgrade.sh not found at $SRC_SCRIPT" >&2; exit 1; }
command -v tmux >/dev/null 2>&1 || { echo "FAIL: tmux is required for the self-upgrade handoff test" >&2; exit 1; }

TMPROOT="$(mktemp -d)"
TEST_TMUX_SOCKET="$TMPROOT/tmux.sock"
TEST_SOCKET_TAG="$(printf '%s' "$TEST_TMUX_SOCKET" | cksum | awk '{print $1}')"
TEST_DONE_MARKER="/tmp/sutando-self-upgrade-$TEST_SOCKET_TAG.done"
cleanup() {
  [ -n "${MARKER_SLEEP_PID:-}" ] && kill "$MARKER_SLEEP_PID" 2>/dev/null || true
  tmux -S "$TEST_TMUX_SOCKET" kill-server 2>/dev/null || true
  rm -rf "$TMPROOT"
}
trap cleanup EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }
ok()   { echo "  ok  $1"; }
command -v tmux >/dev/null 2>&1 || fail "tmux is required for the durable-restart test"

# git identity for the ephemeral fixture repos (CI runners have none configured)
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# Build a fixture "checkout" at $1 whose layout matches the real repo enough for
# upgrade.sh: it embeds a copy of upgrade.sh at skills/self-upgrade/scripts/ (so
# the script's REPO=dirname/../../.. resolves to the fixture root) and a stub
# src/restart.sh. Wires it to a fresh bare remote and returns with $NEW_REPO set.
make_fixture() {
  local root="$1"
  local remote="$root.remote.git"
  mkdir -p "$root/skills/self-upgrade/scripts" "$root/src" "$root/scripts" "$root/workspace/state/cores"
  # The witness-owed gate is part of the upgrade path; a fixture without it is not the shipped script.
  cp "$REPO/src/witness_owed.py" "$root/src/witness_owed.py"
  cp "$SRC_SCRIPT" "$root/skills/self-upgrade/scripts/upgrade.sh"
  cat > "$root/scripts/sutando-config.sh" <<EOF
#!/bin/bash
case "\${1:-}" in
  workspace) printf '%s\n' "$root/workspace" ;;
  python-bin) command -v python3 ;;
  host-label) printf '%s\n' testhost ;;
  tmux-socket) printf '%s\n' "$TEST_TMUX_SOCKET" ;;
esac
EOF
  chmod +x "$root/scripts/sutando-config.sh"
  # stub restart.sh: record that it ran, then BLOCK 3s (mimics startup.sh's
  # foreground hang). REPO is exported to it by upgrade.sh's cwd; write marker
  # into the fixture root via an absolute path passed through the env.
  cat > "$root/src/restart.sh" <<EOF
#!/bin/bash
echo "restart invoked" > "$root/restart-marker"
touch "$root/workspace/state/cores/test.alive"
printf '%s\n' "\$\$" > "$root/restart-pid"
exec sleep 3
EOF
  chmod +x "$root/src/restart.sh"
  cat > "$root/.gitignore" <<'EOF'
restart-marker
restart-pid
workspace/
EOF

  git init -q -b main "$root"
  ( cd "$root" && git add -A && git commit -qm "init" )
  git init -q -b main --bare "$remote"
  ( cd "$root" && git remote add origin "$remote" && git push -q -u origin main )
}

# Advance the bare remote by one commit so the fixture is "behind" by 1.
advance_remote() {
  local root="$1" work="$1.pusher"
  git clone -q -b main "$root.remote.git" "$work"
  ( cd "$work" && echo "upstream change" > CHANGELOG && git add -A && git commit -qm "upstream" && git push -q origin main )
  rm -rf "$work"
}

run_upgrade() { # args: repo, extra args...  -> sets RC and OUT
  local repo="$1"; shift
  set +e
  OUT="$(cd "$repo" && SUTANDO_TEST_MODE=1 SUTANDO_UPGRADE_VERIFY_TRIES=1 bash "$repo/skills/self-upgrade/scripts/upgrade.sh" "$@" 2>&1)"
  RC=$?
  set -e
}

# --- A. dirty tree aborts (exit 2), never fetches/pulls -----------------------
A="$TMPROOT/a"; make_fixture "$A"
echo "uncommitted" > "$A/dirty.txt"
run_upgrade "$A"
[ "$RC" -eq 2 ] || fail "dirty tree: expected exit 2, got $RC (out: $OUT)"
case "$OUT" in *"dirty"*) : ;; *) fail "dirty tree: expected 'dirty' in output, got: $OUT" ;; esac
[ ! -f "$A/restart-marker" ] || fail "dirty tree: restart.sh must NOT run when aborting"
ok "A: aborts on dirty tree (exit 2), restart never invoked"

# --- B. already latest → no-op (exit 0), no restart --------------------------
B="$TMPROOT/b"; make_fixture "$B"
run_upgrade "$B"
[ "$RC" -eq 0 ] || fail "already-latest: expected exit 0, got $RC (out: $OUT)"
case "$OUT" in *"already at latest"*) : ;; *) fail "already-latest: expected 'already at latest', got: $OUT" ;; esac
[ ! -f "$B/restart-marker" ] || fail "already-latest: restart.sh must NOT run when nothing to pull"
ok "B: no-ops when already at latest (exit 0), no restart"

# --- C. real upgrade → pulls + hands restart to durable tmux ------------------
C="$TMPROOT/c"; make_fixture "$C"
advance_remote "$C"                       # remote now 1 ahead → fixture is behind
# Start the tmux server before the simulated executor runs, just as sutando-core
# owns the production server before an upgrade task begins.
tmux -S "$TEST_TMUX_SOCKET" new-session -d -s fixture-core 'exec sleep 120'
before="$(cd "$C" && git rev-parse --short HEAD)"
start=$(date +%s)
run_upgrade "$C"
elapsed=$(( $(date +%s) - start ))
after="$(cd "$C" && git rev-parse --short HEAD)"

[ "$RC" -eq 0 ] || fail "upgrade: expected exit 0, got $RC (out: $OUT)"
[ "$after" != "$before" ] || fail "upgrade: HEAD did not advance ($before -> $after); pull didn't happen"
[ -f "$C/restart-marker" ] || fail "upgrade: restart.sh was never invoked (marker missing)"
case "$OUT" in *"core heartbeat advancing"*) : ;; *) fail "upgrade: heartbeat verification did not pass: $OUT" ;; esac
case "$OUT" in *"durable tmux session sutando-services"*) : ;; *) fail "upgrade: durable handoff was not reported: $OUT" ;; esac
tmux -S "$TEST_TMUX_SOCKET" has-session -t '=sutando-services' 2>/dev/null ||
  fail "upgrade: durable sutando-services tmux session did not survive executor return"
# The detach proof: restart.sh blocks 3s. Durable handoff returns immediately;
# inline (the bug) takes the full delay. Use a 2s threshold.
[ "$elapsed" -lt 2 ] || fail "upgrade: took ${elapsed}s — restart.sh was NOT handed off (would hang the core)"
# The detached fixture records its own PID immediately before exec'ing sleep.
# Read only that fixture-owned PID: a host-wide pgrep could select and kill an
# unrelated developer/CI process that happens to be running `sleep 15`.
MARKER_SLEEP_PID="$(cat "$C/restart-pid")"
case "$MARKER_SLEEP_PID" in *[!0-9]*|'') fail "upgrade: invalid fixture restart PID" ;; esac
kill -0 "$MARKER_SLEEP_PID" 2>/dev/null || fail "upgrade: fixture restart PID is not running"
pane_pid="$(tmux -S "$TEST_TMUX_SOCKET" list-panes -t '=sutando-services' -F '#{pane_pid}' | head -1)"
marker_ppid="$(ps -o ppid= -p "$MARKER_SLEEP_PID" | tr -d ' ')"
[ "$pane_pid" = "$marker_ppid" ] ||
  fail "upgrade: restart PID $MARKER_SLEEP_PID is not owned by tmux pane PID $pane_pid"
for _ in $(seq 1 10); do
  pane_pid="$(tmux -S "$TEST_TMUX_SOCKET" list-panes -t '=sutando-services' -F '#{pane_pid}' 2>/dev/null | head -1 || true)"
  completed_pid="$(cat "$TEST_DONE_MARKER" 2>/dev/null || true)"
  [ -n "$pane_pid" ] && [ "$completed_pid" = "$pane_pid" ] && break
  sleep 0.5
done
[ -n "$pane_pid" ] && [ "$completed_pid" = "$pane_pid" ] ||
  fail "upgrade: durable session never reached done state"
[ "$(tmux -S "$TEST_TMUX_SOCKET" list-panes -t '=sutando-services' -F '#{pane_current_command}' | head -1)" = "sleep" ] ||
  fail "upgrade: completed service session is not parked"
run_upgrade "$C"
[ "$RC" -eq 0 ] || fail "already-latest with parked service session: expected exit 0, got $RC (out: $OUT)"
tmux -S "$TEST_TMUX_SOCKET" has-session -t '=sutando-services' 2>/dev/null ||
  fail "already-latest check removed the parked service session"
ok "C: pulls to latest + hands restart to durable tmux (${elapsed}s < 2s, parked session + marker present)"

echo "PASS — self-upgrade behavioral suite (3/3)"
