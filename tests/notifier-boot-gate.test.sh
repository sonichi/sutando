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
out6="$(
  REPO="$FAKE_REPO"
  . "$MANIFEST_CONFIG_SRC"
  unset SUTANDO_POOL_BOOT_SWEEP
  if [ -z "${SUTANDO_POOL_BOOT_SWEEP:-}" ] && declare -F skill_manifest_config_pending >/dev/null; then
    while IFS= read -r -d '' _mcrec; do
      _mck=${_mcrec%%=*}
      [ "$_mck" = "SUTANDO_POOL_BOOT_SWEEP" ] || continue
      export SUTANDO_POOL_BOOT_SWEEP="${_mcrec#*=}"
      break
    done < <(skill_manifest_config_pending "$REPO" "$PY")
  fi
  echo "resolved=$SUTANDO_POOL_BOOT_SWEEP"
)"
check "adapter-level one-time resolution discovers a manifest-declared var" \
  "$out6" "resolved=$SWEEP"

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
