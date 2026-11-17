#!/usr/bin/env bash
# The core and worker launchers call resolve_claude_settings_args under `set -e`: a settings
# builder that fails must not abort the launch, and the session still starts with its guards.
set -uo pipefail
REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
command -v node >/dev/null 2>&1 || { echo "SKIP: node unavailable"; exit 0; }
NODE="$(command -v node)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TMP="$(cd "$TMP" && pwd -P)"
STUB="$TMP/repo"
mkdir -p "$STUB/src/agent/claude/cli" "$STUB/src/observability/claude/hooks" "$TMP/nonode-bin"
CORE_BUILDER="$STUB/src/agent/claude/cli/build-core-settings.mjs"
OBS_BUILDER="$STUB/src/observability/claude/hooks/build-hook-settings.mjs"
# A core stub that succeeds and records the obs JSON it was handed.
stub_core_ok() { printf '%s\n' "import fs from 'node:fs'; fs.writeFileSync('$TMP/obs-arg', process.argv[3] || ''); process.stdout.write('$1');" > "$CORE_BUILDER"; }
stub_fail() { printf '%s\n' "process.stdout.write('$3'); process.stderr.write('boom\\n'); process.exit($2);" > "$1"; }

# Mirrors the launchers: `set -e`, call the resolver, then build the argv. Prints the argv, one per line.
launch() {
  ( set -e
    [ -z "${LAUNCH_PATH:-}" ] || PATH="$LAUNCH_PATH"
    # shellcheck source=/dev/null
    source "$REAL_REPO/src/agent/claude/cli/session-launch.sh"
    REPO="$STUB"; unset SUTANDO_CLAUDE_WORKING_DIR
    resolve_claude_settings_args >/dev/null
    printf '%s\n' claude "${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"}" LAUNCHED
  ) >"$TMP/out" 2>"$TMP/err"; rc=$?
}
settings_json() { grep -A1 -x -- --settings "$TMP/out" | tail -n +2; }
# The fallback the shell builds must equal the real builder's guards for the same repo.
expected_guards() {
  "$NODE" "$REAL_REPO/src/agent/claude/cli/build-core-settings.mjs" "$1/hooks/skip-ask-user-question.py" "" "" \
    "$1/hooks/gmail-write-guard.py" "$1/hooks/gdocs-write-guard.py" "$1/hooks/native-pim-guard.py" \
    "$1/hooks/shell-trap-guard.py" "$1/hooks/stale-head-post-guard.py" "${2:-}"
}
same_json() { "$NODE" -e 'const u=require("util");process.exit(u.isDeepStrictEqual(JSON.parse(process.argv[1]),JSON.parse(process.argv[2]))?0:1)' "$1" "$2" 2>/dev/null; }
has_ask_guard() { "$NODE" -e 'const s=JSON.parse(process.argv[1]);process.exit((s.hooks.PreToolUse||[]).some(e=>e.matcher==="AskUserQuestion"&&e.hooks.some(h=>/skip-ask-user-question\.py/.test(h.command)))?0:1)' "$1" 2>/dev/null; }

# Core builder prints a partial JSON, then exits non-zero.
unset SUTANDO_OBS_ENDPOINT
stub_fail "$CORE_BUILDER" 3 '{"hooks":{"PreToolUse":[{"matcher":"PARTIAL"'
launch
[ "$rc" = 0 ] && grep -qx LAUNCHED "$TMP/out"
check $? "core builder exits 3 under set -e: the resolver returns and the launch continues"
J="$(settings_json)"
! grep -q PARTIAL "$TMP/out"
check $? "core builder exits 3: its partial output is discarded"
same_json "$J" "$(expected_guards "$STUB")"
check $? "core builder exits 3: --settings is the fallback, equal to the builder's guards-only output"
has_ask_guard "$J"
check $? "core builder exits 3: the fallback registers the AskUserQuestion guard"
[ "$(grep -c 'settings builder failed (exit 3)' "$TMP/err")" = 1 ] && grep -q 'only the session guards registered' "$TMP/err"
check $? "core builder exits 3: one stderr line names the failure, its exit code, and guards-only"

# Obs builder prints a partial JSON, then exits non-zero; the core builder must not receive it.
export SUTANDO_OBS_ENDPOINT="http://127.0.0.1:9"
stub_fail "$OBS_BUILDER" 5 '{"hooks":{"PARTIAL_OBS":['
stub_core_ok '{"hooks":{"PreToolUse":[]}}'
launch
[ "$rc" = 0 ] && grep -qx '{"hooks":{"PreToolUse":\[\]}}' "$TMP/out"
check $? "obs builder exits 5 under set -e: the launch continues with the core builder's --settings"
[ -f "$TMP/obs-arg" ] && [ ! -s "$TMP/obs-arg" ]
check $? "obs builder exits 5: its partial output is not passed to the core builder"
[ "$(grep -c 'obs hooks: settings builder failed (exit 5)' "$TMP/err")" = 1 ]
check $? "obs builder exits 5: one stderr line names the obs builder failure"
unset SUTANDO_OBS_ENDPOINT

# Builder succeeds: unchanged.
stub_core_ok '{"hooks":{}}'
launch
[ "$rc" = 0 ] && grep -qx '{"hooks":{}}' "$TMP/out" && [ ! -s "$TMP/err" ]
check $? "core builder succeeds: --settings carries its JSON, nothing on stderr"

# Builder succeeds but prints nothing: the same guards-only fallback.
stub_core_ok ''
launch
[ "$rc" = 0 ] && same_json "$(settings_json)" "$(expected_guards "$STUB")" && grep -q 'printed nothing' "$TMP/err"
check $? "core builder prints nothing: guards-only fallback, with a diagnostic"

# No node on PATH: the same fallback, built without node.
LAUNCH_PATH="$TMP/nonode-bin" launch
[ "$rc" = 0 ] && grep -qx LAUNCHED "$TMP/out" && same_json "$(settings_json)" "$(expected_guards "$STUB")"
check $? "node unavailable: the launch continues with the guards-only fallback"
grep -q 'node unavailable — only the session guards are registered' "$TMP/err"
check $? "node unavailable: the stderr line says only the guards are registered"

# Parity with the real builder for awkward paths and with the outside-checkout Stop hook.
ODD="$TMP/o'dd \"re\\po\$x"
mkdir -p "$ODD"
got="$( REPO="$ODD"; source "$REAL_REPO/src/agent/claude/cli/session-launch.sh"; claude_guard_settings_json "$ODD/src/check-pending-tasks.sh")"
same_json "$got" "$(expected_guards "$ODD" "$ODD/src/check-pending-tasks.sh")"
check $? "fallback equals the builder's guards for a repo path with quotes, a backslash and a \$, plus the Stop hook"

echo "session-launch-settings-builder-failure: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
