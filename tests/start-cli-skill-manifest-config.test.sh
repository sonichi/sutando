#!/usr/bin/env bash
# Any installed skill's manifest.json "config" block reaches the launched core
# session -- generic and skill-agnostic: the launcher never names a skill, it
# only reads a key the manifest declares and forwards it like every other var.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
STARTCLI="$REPO/src/agent/claude/cli/start-cli.sh"

# A throwaway checkout: real repo files (start-cli.sh reads several by path),
# plus a fake skills/ carrying one manifest with a config block.
mkdir -p "$TMP/repo"
cp -R "$REPO"/. "$TMP/repo/" 2>/dev/null
rm -rf "$TMP/repo/skills"
mkdir -p "$TMP/repo/skills/fixture-skill"
cat > "$TMP/repo/skills/fixture-skill/manifest.json" <<'JSON'
{"config": {"SUTANDO_FIXTURE_CONFIG_VAR": "from-manifest"}}
JSON
# A malformed sibling manifest must not abort discovery of the good one.
mkdir -p "$TMP/repo/skills/broken-skill"
printf 'not json' > "$TMP/repo/skills/broken-skill/manifest.json"

mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STARTCLI_T="$TMP/repo/src/agent/claude/cli/start-cli.sh"

out="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI_T" --print-core-env 2>/dev/null)"
echo "$out" | grep -qx -- "SUTANDO_FIXTURE_CONFIG_VAR=from-manifest"
check $? "a skill's manifest config key reaches the launched core session"

# An already-set value (CLI/env) always wins -- never overridden by a manifest.
out2="$(env -i HOME="$HOME" PATH="$STUB_PATH" SUTANDO_FIXTURE_CONFIG_VAR=from-caller \
  bash "$STARTCLI_T" --print-core-env 2>/dev/null)"
echo "$out2" | grep -qx -- "SUTANDO_FIXTURE_CONFIG_VAR=from-caller"
check $? "a caller-set value is forwarded unchanged"
! echo "$out2" | grep -q "from-manifest"
check $? "the manifest value never overrides a caller-set value"

# No skills/ at all (a checkout that removed it, or none installed): no crash,
# nothing forwarded that wasn't already there.
rm -rf "$TMP/repo/skills"
out3="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI_T" --print-core-env 2>/dev/null)"
rc3=$?
[ "$rc3" -eq 0 ]
check $? "an install with no skills/ still launches (probe exits 0)"
! echo "$out3" | grep -q "SUTANDO_FIXTURE_CONFIG_VAR"
check $? "nothing manifest-sourced appears when there is no skills/ dir"

# Generic: the launcher itself must never name the fixture skill.
! grep -q "fixture-skill" "$STARTCLI"
check $? "the core's launcher names no concrete skill (it discovers manifests generically)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
