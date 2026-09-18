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

# --- hostile manifests (review blockers B1/B2) -------------------------------
# Each case rebuilds skills/ so the fixtures cannot interact.
hostile() {  # $1 = manifest JSON, sets $hout / $hrc / $herr
  rm -rf "$TMP/repo/skills"; mkdir -p "$TMP/repo/skills/zz-hostile"
  printf '%s' "$1" > "$TMP/repo/skills/zz-hostile/manifest.json"
  hout="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI_T" --print-core-env 2>"$TMP/err")"
  hrc=$?; herr="$(cat "$TMP/err")"
}

# B1: a non-identifier key must not reach `export`, which aborts the launcher
# under `set -e` and takes down every restart path.
hostile '{"config": {"my-skill-key": "v", "ZZ_AFTER_BAD_KEY": "still-here"}}'
[ "$hrc" -eq 0 ]
check $? "a non-identifier config key does not abort the launch"
echo "$hout" | grep -qx -- "ZZ_AFTER_BAD_KEY=still-here"
check $? "keys after an invalid one are still forwarded"
! echo "$hout" | grep -q "my-skill-key"
check $? "the invalid key is not exported"
echo "$herr" | grep -q "not a shell identifier"
check $? "the skip is reported on stderr, not silent"

# A newline in a value must not forge a second assignment (records are NUL-framed).
hostile '{"config": {"ZZ_NL": "a\nZZ_INJECTED=yes", "ZZ_AFTER_NL": "still-here"}}'
! echo "$hout" | grep -q "^ZZ_INJECTED="
check $? "a newline in a value cannot inject a second env assignment"
! echo "$hout" | grep -q "^ZZ_NL="
check $? "the control-character value is skipped, not forwarded corrupted"
echo "$hout" | grep -qx -- "ZZ_AFTER_NL=still-here"
check $? "keys after a control-character value are still forwarded"
echo "$herr" | grep -q "control character in value"
check $? "the control-character skip is reported on stderr"

# Exec-hijack vectors are refused however they are declared.
hostile '{"config": {"PATH": "/tmp/evil", "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib", "ZZ_OK": "1"}}'
! echo "$hout" | grep -qx -- "PATH=/tmp/evil"
check $? "a manifest cannot set PATH"
! echo "$hout" | grep -q "DYLD_INSERT_LIBRARIES"
check $? "a manifest cannot set a loader-injection variable"
check $? "an ordinary key alongside a protected one still lands"

# Apple's suffix-less exported-function form: rejected by prefix, not by IDENT.
hostile '{"config": {"__BASH_FUNC_cd": "() { echo pwned; }", "ZZ_AFTER_FUNC": "1"}}'
! echo "$hout" | grep -q "__BASH_FUNC_cd"
check $? "a manifest cannot set an exported-bash-function variable"
echo "$hout" | grep -qx -- "ZZ_AFTER_FUNC=1"
check $? "an ordinary key alongside it still lands"
echo "$hout" | grep -qx -- "ZZ_OK=1"

# Duplicate keys across skills resolve deterministically to one record.
rm -rf "$TMP/repo/skills"
mkdir -p "$TMP/repo/skills/aa-dup" "$TMP/repo/skills/bb-dup"
printf '%s' '{"config": {"ZZ_DUP": "first"}}' > "$TMP/repo/skills/aa-dup/manifest.json"
printf '%s' '{"config": {"ZZ_DUP": "second"}}' > "$TMP/repo/skills/bb-dup/manifest.json"
dout="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI_T" --print-core-env 2>/dev/null)"
[ "$(echo "$dout" | grep -c '^ZZ_DUP=')" -eq 1 ]
check $? "a key declared by two skills is forwarded exactly once"
echo "$dout" | grep -qx -- "ZZ_DUP=first"
check $? "the duplicate resolves to the first skill in glob order"
derr="$(env -i HOME="$HOME" PATH="$STUB_PATH" bash "$STARTCLI_T" --print-core-env 2>&1 >/dev/null)"
echo "$derr" | grep -q "ZZ_DUP declared by more than one skill"
check $? "the dropped duplicate is reported on stderr, like every other rejection"

# B2: an explicitly-empty caller value is a disable, and must not be re-filled.
hostile '{"config": {"ZZ_DISABLED": "1"}}'
eout="$(env -i HOME="$HOME" PATH="$STUB_PATH" ZZ_DISABLED= bash "$STARTCLI_T" --print-core-env 2>/dev/null)"
! echo "$eout" | grep -qx -- "ZZ_DISABLED=1"
check $? "a manifest does not override a caller's explicit empty value"
[ "$(echo "$eout" | grep -c '^ZZ_DISABLED=')" -eq 1 ]
check $? "the explicitly-disabled variable is forwarded exactly once"

# Generic: the launcher itself must never name the fixture skill.
! grep -q "fixture-skill" "$STARTCLI"
check $? "the core's launcher names no concrete skill (it discovers manifests generically)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
