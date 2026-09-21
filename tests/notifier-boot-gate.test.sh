#!/bin/bash
# notifier_boot_gate (src/agent/notifier-boot-gate.sh) must apply the SAME
# synchronous, fail-closed backfill boundary as core's own /startup Step 1.7
# -- a managed task notifier starts its own watcher independent of whether
# Step 1.7 has run inside the core session yet, so it needs its own gate.
# keweichen's review on PR #4503, round 5.
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

# --- Case 6b: an EXPLICIT EMPTY override must survive the manifest --
# keweichen's review, round 9: the +x setness fix had no explicit-empty
# regression test at this level (only the production-path Python test did). ---
out6b="$(
  REPO="$FAKE_REPO"
  . "$MANIFEST_CONFIG_SRC"
  export SUTANDO_POOL_BOOT_SWEEP=""
  adapter_resolve
  echo "resolved=[$SUTANDO_POOL_BOOT_SWEEP]"
)"
check "explicit empty override is not refilled from the manifest" "$out6b" "resolved=[]"

# --- Case 7: notifier_boot_gate_force_kill_watcher actually terminates the
# real process a launcher's tmux kill-session left alive -- the SAFETY
# OUTCOME (the watcher can no longer admit a task), not a diagnostic string.
# Uses REAL_REPO (needs the real util_paths.py/watcher_sentinel.sh). ---
WS7="$TD/ws7"
mkdir -p "$WS7/state"
nohup sleep 25 > /dev/null 2>&1 &
FAKE_WATCHER_PID=$!
disown
SENTINEL7="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS7/state")"
echo "$FAKE_WATCHER_PID" > "$SENTINEL7"
out7="$(
  REPO="$REAL_REPO"
  . "$GATE_SRC"
  notifier_boot_gate_force_kill_watcher "$WS7"
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

# --- Case 8: negative control -- a REISSUED pid (the sentinel's mtime
# predates the live process's own start) must NEVER be killed. ---
WS8="$TD/ws8"
mkdir -p "$WS8/state"
nohup sleep 25 > /dev/null 2>&1 &
UNRELATED_PID=$!
disown
SENTINEL8="$(python3 "$REAL_REPO/src/util_paths.py" watcher-sentinel "$WS8/state")"
echo "$UNRELATED_PID" > "$SENTINEL8"
touch -t 202001010000 "$SENTINEL8"
out8="$(
  REPO="$REAL_REPO"
  . "$GATE_SRC"
  notifier_boot_gate_force_kill_watcher "$WS8"
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
# overrides which tree the watcher will really admit tasks from. keweichen's
# review, round 10: the gate approved admission without ever checking the
# override workspace's own pool declaration. ---
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
