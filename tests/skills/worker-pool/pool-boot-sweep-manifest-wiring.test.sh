#!/usr/bin/env bash
# The real worker-pool skills/manifest.json (not a fixture) reaches the core
# launcher's env as $SUTANDO_POOL_BOOT_SWEEP, so skills/startup/SKILL.md's
# Step 1.7 can invoke pool_supervise.py's --sweep without naming the skill by
# a literal path (core-never-names-a-concrete-skill,
# tests/skills/worker-pool/spawn-worker-launcher.test.py's
# test_the_core_startup_skill_names_the_env_not_a_path). Reviewed by
# qingyun-wu (Qingyun's Personal Codex) on PR #4503: "add a guaranteed
# startup/upgrade backfill ... with a startup-path regression."
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

MANIFEST="$REPO/skills/worker-pool/manifest.json"
[ -f "$MANIFEST" ]
check $? "skills/worker-pool/manifest.json exists"

DECLARED="$(python3 -c "import json; print(json.load(open('$MANIFEST'))['config']['SUTANDO_POOL_BOOT_SWEEP'])" 2>/dev/null)"
[ -n "$DECLARED" ]
check $? "the manifest declares SUTANDO_POOL_BOOT_SWEEP"

[ -f "$REPO/$DECLARED" ]
check $? "the declared path ($DECLARED) actually exists"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"

out="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI" --print-core-env 2>/dev/null)"
echo "$out" | grep -qx -- "SUTANDO_POOL_BOOT_SWEEP=$DECLARED"
check $? "the real manifest's declaration reaches the launched core session's env, unchanged"

# A caller-set value (a product deployment pinning something else) must still
# win -- the manifest is a default, never an override.
out2="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_POOL_BOOT_SWEEP=/opt/pinned-sweep.py \
  bash "$STARTCLI" --print-core-env 2>/dev/null)"
echo "$out2" | grep -qx -- "SUTANDO_POOL_BOOT_SWEEP=/opt/pinned-sweep.py"
check $? "an explicit caller value is not overridden by the manifest default"

echo ""
if [ "$fail" -eq 0 ]; then
  echo "PASS — $pass check(s) green"
  exit 0
else
  echo "FAIL — $fail of $((pass+fail)) check(s) red"
  exit 1
fi
