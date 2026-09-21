#!/bin/bash
# notifier_boot_gate must apply the same fail-closed backfill boundary as
# core's own /startup Step 1.7, since a notifier starts its own watcher.
#
# Run: bash tests/notifier-boot-gate.test.sh
set -u
REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
GATE_SRC="$REAL_REPO/src/agent/notifier-boot-gate.sh"
MANIFEST_CONFIG_SRC="$REAL_REPO/src/skill-manifest-config.sh"
FAIL=0
check() {
  local desc="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    echo "  ok   $desc"
  else
    echo "  FAIL $desc (got '$got', want '$want')"
    FAIL=1
  fi
}

TD="$(mktemp -d)"
trap 'rm -rf "$TD"' EXIT

# A fake sweep script whose exit code is controlled by a sibling .rc file, so
# each test case doesn't need its own script.
SWEEP="$TD/fake-sweep.py"
cat > "$SWEEP" << 'PYEOF'
#!/usr/bin/env python3
import sys
rc_file = sys.argv[0] + ".rc"
try:
    rc = int(open(rc_file).read().strip())
except OSError:
    rc = 0
sys.exit(rc)
PYEOF
chmod +x "$SWEEP"
PY="$(command -v python3)"

# notifier_boot_gate shells out to "$REPO/scripts/sutando-config.sh workspace"
# -- fake REPO so that resolution never touches the real repo/workspace.
FAKE_REPO="$TD/fake-repo"
mkdir -p "$FAKE_REPO/scripts" "$TD/ws"
cat > "$FAKE_REPO/scripts/sutando-config.sh" << EOF
#!/bin/bash
case "\$1" in
  workspace) echo "$TD/ws";;
  *) echo "";;
esac
EOF
chmod +x "$FAKE_REPO/scripts/sutando-config.sh"
mkdir -p "$FAKE_REPO/src"
cp "$REAL_REPO/src/workspace_dir_resolve.sh" "$FAKE_REPO/src/workspace_dir_resolve.sh"

# --- Case 1: unset -- skip silently, same contract as Step 1.7's own "unset" case ---
out1="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  unset SUTANDO_POOL_BOOT_SWEEP
  notifier_boot_gate "$PY" 2>&1
  echo "rc=$?"
)"
check "unset SUTANDO_POOL_BOOT_SWEEP -> proceed (rc=0)" "$(grep -o 'rc=[0-9]*' <<<"$out1")" "rc=0"

# --- Case 2: set, sweep exits 0 -- proceed ---
echo 0 > "$SWEEP.rc"
out2="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  notifier_boot_gate "$PY" 2>&1
  echo "rc=$?"
)"
check "sweep exits 0 -> proceed (rc=0)" "$(grep -o 'rc=[0-9]*' <<<"$out2")" "rc=0"

# --- Case 3: set, sweep exits 3 (the documented HandlerPublishError code) -- refuse ---
echo 3 > "$SWEEP.rc"
out3="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  notifier_boot_gate "$PY" 2>&1
  echo "rc=$?"
)"
check "sweep exits 3 -> refuse (rc=1)" "$(grep -o 'rc=[0-9]*' <<<"$out3")" "rc=1"
check "sweep exits 3 -> prints a diagnostic" \
  "$(grep -c 'refusing to start the task notifier' <<<"$out3")" "1"

# --- Case 4: set, sweep exits 1 (ANY non-zero, not just 3) -- refuse ---
echo 1 > "$SWEEP.rc"
out4="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  notifier_boot_gate "$PY" 2>&1
  echo "rc=$?"
)"
check "sweep exits 1 (generic failure) -> refuse (rc=1)" "$(grep -o 'rc=[0-9]*' <<<"$out4")" "rc=1"

# --- Case 5: the gate is a PURE consumer -- it never resolves the manifest
# itself, so calling it repeatedly (Codex has three ensure_task_notifier()
# call sites) costs exactly one sweep invocation per call, never a re-glob of
# skills/*/manifest.json. Provider I/O (resolving the var) is each adapter's
# job, done once at its own edge -- see the top-level block in each launcher.
COUNTER="$TD/sweep-invocations"
: > "$COUNTER"
cat > "$SWEEP" << PYEOF
#!/usr/bin/env python3
import sys
open(sys.argv[0] + ".invocations", "a").write("1\n")
sys.exit(0)
PYEOF
chmod +x "$SWEEP"
out5="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  notifier_boot_gate "$PY"; notifier_boot_gate "$PY"; notifier_boot_gate "$PY"
  echo "rc=$?"
)"
check "three calls -> exactly three sweep invocations (no hidden re-resolution)" \
  "$(wc -l < "$SWEEP.invocations" | tr -d ' ')" "3"

# --- Case 6: the adapter-level one-time resolution pattern (Codex's own
# top-level block) actually discovers a manifest-declared var ---
MANIFEST_SKILL="$FAKE_REPO/skills/worker-pool"
mkdir -p "$MANIFEST_SKILL"
cat > "$MANIFEST_SKILL/manifest.json" << EOF
{"config": {"SUTANDO_POOL_BOOT_SWEEP": "$SWEEP"}}
EOF
adapter_resolve() {
  # Mirrors the REAL top-level block in codex/start-cli.sh exactly -- +x
  # (set-ness), not -z ...:- (emptiness), so this test breaks if that block
  # regresses to the old check.
  if [ -z "${SUTANDO_POOL_BOOT_SWEEP+x}" ] && declare -F skill_manifest_config_pending >/dev/null; then
    while IFS= read -r -d '' _mcrec; do
      _mck=${_mcrec%%=*}
      [ "$_mck" = "SUTANDO_POOL_BOOT_SWEEP" ] || continue
      export SUTANDO_POOL_BOOT_SWEEP="${_mcrec#*=}"
      break
    done < <(skill_manifest_config_pending "$REPO" "$PY")
  fi
}
out6="$(
  REPO="$FAKE_REPO"
  . "$MANIFEST_CONFIG_SRC"
  unset SUTANDO_POOL_BOOT_SWEEP
  adapter_resolve
  echo "resolved=$SUTANDO_POOL_BOOT_SWEEP"
)"
check "adapter-level one-time resolution discovers a manifest-declared var" \
  "$out6" "resolved=$SWEEP"

# --- Case 6b: an EXPLICIT EMPTY override must survive the manifest, at
# this bash-unit level too (the production-path Python test covers it
# separately). ---
out6b="$(
  REPO="$FAKE_REPO"
  . "$MANIFEST_CONFIG_SRC"
  export SUTANDO_POOL_BOOT_SWEEP=""
  adapter_resolve
  echo "resolved=[$SUTANDO_POOL_BOOT_SWEEP]"
)"
check "explicit empty override is not refilled from the manifest" "$out6b" "resolved=[]"

# A real process whose argv genuinely classifies as "watcher" per
# watcher_identity.py -- a bare `sleep` does not, so the identity-proof step
# would (correctly) refuse it. $1: dir to create the fake script in.
_make_fake_watcher_script() {
  local dir="$1"
  mkdir -p "$dir"
  cat > "$dir/watch-tasks-stream.sh" << 'SCRIPT'
#!/bin/bash
sleep 25
SCRIPT
  chmod +x "$dir/watch-tasks-stream.sh"
  printf '%s/watch-tasks-stream.sh' "$dir"
}

# --- Case 7: notifier_boot_gate_force_kill_watcher actually terminates the
# real process a launcher's tmux kill-session left alive -- the SAFETY
# OUTCOME (the watcher can no longer admit a task), not a diagnostic string.
# Uses REAL_REPO (needs the real util_paths.py/watcher_sentinel.sh). ---
WS7="$TD/ws7"
mkdir -p "$WS7/state"
FAKE_SCRIPT7="$(_make_fake_watcher_script "$TD/fw7")"
bash "$FAKE_SCRIPT7" > /dev/null 2>&1 &
FAKE_WATCHER_PID=$!
disown
SENTINEL7="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS7/state")"
echo "$FAKE_WATCHER_PID" > "$SENTINEL7"
out7="$(
  REPO="$REAL_REPO"
  . "$GATE_SRC"
  notifier_boot_gate_force_kill_watcher "$WS7" "$PY"
  echo "rc=$?"
)"
check "force-kill escalation reports success" "$(grep -o 'rc=[0-9]*' <<<"$out7")" "rc=0"
if kill -0 "$FAKE_WATCHER_PID" 2>/dev/null; then
  echo "  FAIL force-kill escalation actually terminates the process (still alive)"
  FAIL=1
  kill -9 "$FAKE_WATCHER_PID" 2>/dev/null
else
  echo "  ok   force-kill escalation actually terminates the process"
fi

# --- Case 7b: a genuinely non-watcher process at the sentinel pid (argv does
# not match watch-tasks-stream.sh) must be refused, not killed. ---
WS7b="$TD/ws7b"
mkdir -p "$WS7b/state"
nohup sleep 25 > /dev/null 2>&1 &
NOT_A_WATCHER_PID=$!
disown
SENTINEL7b="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS7b/state")"
echo "$NOT_A_WATCHER_PID" > "$SENTINEL7b"
out7b="$(
  REPO="$REAL_REPO"
  . "$GATE_SRC"
  notifier_boot_gate_force_kill_watcher "$WS7b" "$PY"
  echo "rc=$?"
)"
check "force-kill escalation refuses a non-watcher process" "$(grep -o 'rc=[0-9]*' <<<"$out7b")" "rc=1"
if kill -0 "$NOT_A_WATCHER_PID" 2>/dev/null; then
  echo "  ok   the non-watcher process was correctly left alone"
else
  echo "  FAIL the non-watcher process was killed anyway -- identity check bypassed"
  FAIL=1
fi
kill -9 "$NOT_A_WATCHER_PID" 2>/dev/null

# --- Case 8: negative control -- a REISSUED pid (the sentinel's mtime
# predates the live process's own start) must NEVER be killed, even though
# it IS a genuine watcher-classified process. ---
WS8="$TD/ws8"
mkdir -p "$WS8/state"
FAKE_SCRIPT8="$(_make_fake_watcher_script "$TD/fw8")"
bash "$FAKE_SCRIPT8" > /dev/null 2>&1 &
UNRELATED_PID=$!
disown
SENTINEL8="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS8/state")"
echo "$UNRELATED_PID" > "$SENTINEL8"
touch -t 202001010000 "$SENTINEL8"
out8="$(
  REPO="$REAL_REPO"
  . "$GATE_SRC"
  notifier_boot_gate_force_kill_watcher "$WS8" "$PY"
  echo "rc=$?"
)"
check "force-kill escalation refuses a reissued pid" "$(grep -o 'rc=[0-9]*' <<<"$out8")" "rc=1"
if kill -0 "$UNRELATED_PID" 2>/dev/null; then
  echo "  ok   the unrelated live process was correctly left alone"
else
  echo "  FAIL the unrelated live process was killed anyway -- ownership check bypassed"
  FAIL=1
fi
kill -9 "$UNRELATED_PID" 2>/dev/null

# --- Case 9: the boot sweep runs against the notifier's ACTUAL workspace,
# not the configured default, when SUTANDO_WORKSPACE_DIR or SUTANDO_TASKS_DIR
# overrides which tree the watcher will really admit tasks from. ---
OVERRIDE_WS="$TD/override-ws"
mkdir -p "$OVERRIDE_WS"
cat > "$SWEEP" << 'PYEOF'
#!/usr/bin/env python3
import sys
for i, a in enumerate(sys.argv):
    if a == "--workspace":
        open(sys.argv[0] + ".seen-workspace", "w").write(sys.argv[i + 1])
sys.exit(0)
PYEOF
chmod +x "$SWEEP"

out9="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  export SUTANDO_WORKSPACE_DIR="$OVERRIDE_WS"
  notifier_boot_gate "$PY"
  echo "rc=$?"
)"
check "SUTANDO_WORKSPACE_DIR override -> sweep sees that workspace" \
  "$(cat "$SWEEP.seen-workspace" 2>/dev/null)" "$OVERRIDE_WS"

rm -f "$SWEEP.seen-workspace"
out9b="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  export SUTANDO_POOL_BOOT_SWEEP="$SWEEP"
  unset SUTANDO_WORKSPACE_DIR
  export SUTANDO_TASKS_DIR="$OVERRIDE_WS/tasks"
  notifier_boot_gate "$PY"
  echo "rc=$?"
)"
check "SUTANDO_TASKS_DIR-only override -> sweep sees its dirname" \
  "$(cat "$SWEEP.seen-workspace" 2>/dev/null)" "$OVERRIDE_WS"

# --- Case 9c: the gate's wrapper must actually delegate to the shared
# resolver (workspace_dir_resolve.sh) rather than re-deriving the formula --
# calls the REAL function both ways, never a hand-typed replica of it (a
# replica is exactly what let the gate and the consumers drift apart before:
# every prior version of this case matched two copies of a formula, and
# neither copy could ever catch the other diverging from the real code). ---
GATE_WS9C="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  SUTANDO_WORKSPACE_DIR="/tmp/workspace-A" SUTANDO_TASKS_DIR="/tmp/workspace-B/tasks" \
    _notifier_boot_gate_workspace
)"
DIRECT_WS9C="$(
  . "$REAL_REPO/src/workspace_dir_resolve.sh"
  SUTANDO_WORKSPACE_DIR="/tmp/workspace-A" resolve_workspace_dir_from_tasks_dir "/tmp/workspace-B/tasks"
)"
check "gate's wrapper delegates to the real shared resolver, not a re-derived formula" \
  "$GATE_WS9C" "$DIRECT_WS9C"

check "start-cli.sh forwards SUTANDO_WORKSPACE_DIR into the notifier's env" \
  "$(grep -c 'NOTIFIER_ENV_ARGS+=(-e "SUTANDO_WORKSPACE_DIR=\$SUTANDO_WORKSPACE_DIR")' \
     "$REAL_REPO/src/agent/codex/cli/start-cli.sh")" "1"

# --- Case 9d: keweichen's exact leading-tilde control. A literal ~ in
# SUTANDO_TASKS_DIR is never shell-expanded inside a variable's value (only
# an unquoted literal word gets tilde expansion) -- the gate used to return
# it unexpanded while both consumers expanded it, so all three must agree
# here specifically. Runs task-notifier.sh's REAL pre-expansion snippet
# (its own tilde substitution, which happens before it ever calls the
# shared resolver) rather than skipping straight to the shared function. ---
GATE_WS9D="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  HOME=/tmp/h75home SUTANDO_TASKS_DIR='~/split/tasks' _notifier_boot_gate_workspace
)"
CODEX_WS9D="$(
  HOME=/tmp/h75home SUTANDO_TASKS_DIR='~/split/tasks' bash -c '
    TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
    . "'"$REAL_REPO"'/src/workspace_dir_resolve.sh"
    resolve_workspace_dir_from_tasks_dir "$TASKS_DIR"'
)"
check "gate resolves a leading-tilde SUTANDO_TASKS_DIR the same as the Codex consumer" \
  "$GATE_WS9D" "$CODEX_WS9D"
check "the leading-tilde control actually expanded (not left literal)" \
  "$GATE_WS9D" "/tmp/h75home/split"

# --- Case 9e: keweichen's SECOND control -- a leading-tilde
# SUTANDO_WORKSPACE_DIR itself (not SUTANDO_TASKS_DIR), checked against the
# REAL Claude notifier's own pre-expansion snippet
# (src/agent/claude/cli/task-notifier.sh), which was an omitted fourth
# policy reader: it never sourced workspace_dir_resolve.sh at all. ---
GATE_WS9E="$(
  REPO="$FAKE_REPO"
  . "$GATE_SRC"
  HOME=/tmp/h145home SUTANDO_WORKSPACE_DIR='~/split' SUTANDO_TASKS_DIR=/tmp/h145-tasks \
    _notifier_boot_gate_workspace
)"
CLAUDE_WS9E="$(
  HOME=/tmp/h145home SUTANDO_WORKSPACE_DIR='~/split' SUTANDO_TASKS_DIR=/tmp/h145-tasks bash -c '
    TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
    . "'"$REAL_REPO"'/src/workspace_dir_resolve.sh"
    resolve_workspace_dir_from_tasks_dir "$TASKS_DIR"'
)"
check "gate resolves a leading-tilde SUTANDO_WORKSPACE_DIR the same as the Claude notifier" \
  "$GATE_WS9E" "$CLAUDE_WS9E"
check "the Claude consumer actually sources the shared resolver, not its own formula" \
  "$(grep -c 'resolve_workspace_dir_from_tasks_dir' "$REAL_REPO/src/agent/claude/cli/task-notifier.sh")" "1"

# --- Case 10: the SUPERVISOR (the watcher session's own pane process) is
# killed too, not just the inner watcher -- a real task-notifier-supervisor.sh
# respawns the watcher on any exit, so killing only the sentinel pid is
# undone within ~1s in production. Uses a REAL private tmux socket + session
# so this is the actual shutdown unit, not a simulation of one.
#
# The pane runs a SUPERVISOR (its own long-lived shell) that spawns the
# watcher as a CHILD process, matching the real shape (task-notifier-
# supervisor.sh's pane != the watcher it wraps) -- the sentinel names the
# CHILD, never the pane, proving the helper still reaches and kills the
# supervisor once identity+ownership are proven. ---
if command -v tmux >/dev/null 2>&1; then
  T10_SOCK="$TD/tmux10.sock"
  T10_SESSION="fake-watcher-session-$$"
  WS10="$TD/ws10"
  mkdir -p "$WS10/state"
  FAKE_SCRIPT10="$(_make_fake_watcher_script "$TD/fw10")"
  FAKE_SUPERVISOR10="$TD/fake-supervisor10.sh"
  cat > "$FAKE_SUPERVISOR10" << SCRIPT
#!/bin/bash
bash "$FAKE_SCRIPT10" &
echo \$! > "$TD/fw10-child.pid"
wait
SCRIPT
  chmod +x "$FAKE_SUPERVISOR10"
  tmux -S "$T10_SOCK" new-session -d -s "$T10_SESSION" bash "$FAKE_SUPERVISOR10"
  SUPERVISOR_PID10="$(tmux -S "$T10_SOCK" list-panes -t "=$T10_SESSION" -F '#{pane_pid}' | head -1)"
  # Wait for the child to actually start and record its own pid.
  _tries=0
  while [ ! -s "$TD/fw10-child.pid" ] && [ "$_tries" -lt 40 ]; do
    sleep 0.05
    _tries=$((_tries + 1))
  done
  WATCHER_PID10="$(cat "$TD/fw10-child.pid" 2>/dev/null)"
  SENTINEL10="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS10/state")"
  echo "$WATCHER_PID10" > "$SENTINEL10"
  out10="$(
    REPO="$REAL_REPO"
    . "$GATE_SRC"
    TMUX_SOCKET="$T10_SOCK" WATCHER_SESSION="$T10_SESSION" \
      notifier_boot_gate_force_kill_watcher "$WS10" "$PY"
    echo "rc=$?"
  )"
  if kill -0 "$SUPERVISOR_PID10" 2>/dev/null; then
    echo "  FAIL the supervisor pane process survived force-kill (still alive) -- the watcher would respawn"
    FAIL=1
    kill -9 "$SUPERVISOR_PID10" 2>/dev/null
  else
    echo "  ok   the supervisor pane process was killed, closing the respawn loop"
  fi
  if kill -0 "$WATCHER_PID10" 2>/dev/null; then
    echo "  FAIL the inner watcher child also survived force-kill"
    FAIL=1
    kill -9 "$WATCHER_PID10" 2>/dev/null
  else
    echo "  ok   the inner watcher child (the sentinel-recorded pid) was killed"
  fi
  tmux -S "$T10_SOCK" kill-server 2>/dev/null || true
else
  echo "  skip Case 10 (no tmux on this host)"
fi

# --- Case 10b: the NEGATIVE of Case 10 -- a non-watcher pid sitting in the
# sentinel, sharing its pid with the tmux pane's own process. Nothing may be
# signaled: the pane process must survive, proving identity+ownership are
# checked before any kill. ---
if command -v tmux >/dev/null 2>&1; then
  T10B_SOCK="$TD/tmux10b.sock"
  T10B_SESSION="fake-nonwatcher-session-$$"
  WS10B="$TD/ws10b"
  mkdir -p "$WS10B/state"
  tmux -S "$T10B_SOCK" new-session -d -s "$T10B_SESSION" sleep 30
  PANE_PID10B="$(tmux -S "$T10B_SOCK" list-panes -t "=$T10B_SESSION" -F '#{pane_pid}' | head -1)"
  SENTINEL10B="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS10B/state")"
  echo "$PANE_PID10B" > "$SENTINEL10B"
  out10b="$(
    REPO="$REAL_REPO"
    . "$GATE_SRC"
    TMUX_SOCKET="$T10B_SOCK" WATCHER_SESSION="$T10B_SESSION" \
      notifier_boot_gate_force_kill_watcher "$WS10B" "$PY"
    echo "rc=$?"
  )"
  if kill -0 "$PANE_PID10B" 2>/dev/null; then
    echo "  ok   non-watcher sentinel+pane pid was correctly refused, nothing signaled"
  else
    echo "  FAIL non-watcher sentinel+pane pid was killed before the identity/ownership check refused it"
    FAIL=1
  fi
  tmux -S "$T10B_SOCK" kill-server 2>/dev/null || true
else
  echo "  skip Case 10b (no tmux on this host)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "notifier-boot-gate: ALL PASS"
else
  echo "notifier-boot-gate: FAILURES ABOVE"
  echo "--- out1 ---"; echo "$out1"
  echo "--- out2 ---"; echo "$out2"
  echo "--- out3 ---"; echo "$out3"
  echo "--- out4 ---"; echo "$out4"
  echo "--- out5 ---"; echo "$out5"
  echo "--- out6 ---"; echo "$out6"
fi
exit "$FAIL"
