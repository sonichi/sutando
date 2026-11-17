#!/usr/bin/env bash
# The core and worker launchers call resolve_claude_settings_args under `set -e`: a settings
# builder that exits non-zero must not abort the launch; it starts without --settings and says so.
set -uo pipefail
REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
command -v node >/dev/null 2>&1 || { echo "SKIP: node unavailable"; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TMP="$(cd "$TMP" && pwd -P)"
STUB="$TMP/repo"
mkdir -p "$STUB/src/agent/claude/cli" "$STUB/src/observability/claude/hooks"
CORE_BUILDER="$STUB/src/agent/claude/cli/build-core-settings.mjs"
OBS_BUILDER="$STUB/src/observability/claude/hooks/build-hook-settings.mjs"
stub_ok() { printf '%s\n' "process.stdout.write('$2');" > "$1"; }
stub_fail() { printf '%s\n' "process.stderr.write('boom\\n'); process.exit($2);" > "$1"; }

# Mirrors the launchers: `set -e`, call the resolver, then build the argv. Prints the argv, one per line.
launch() {
  ( set -e
    # shellcheck source=/dev/null
    source "$REAL_REPO/src/agent/claude/cli/session-launch.sh"
    REPO="$STUB"; unset SUTANDO_CLAUDE_WORKING_DIR
    resolve_claude_settings_args >/dev/null
    printf '%s\n' claude "${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"}" LAUNCHED
  ) >"$TMP/out" 2>"$TMP/err"; rc=$?
}

# Core builder exits non-zero.
unset SUTANDO_OBS_ENDPOINT
stub_fail "$CORE_BUILDER" 3
launch
[ "$rc" = 0 ] && grep -qx LAUNCHED "$TMP/out"
check $? "core builder exits 3 under set -e: the resolver returns and the launch continues"
! grep -qx -- --settings "$TMP/out"
check $? "core builder exits 3: the launch argv carries no --settings"
[ "$(grep -c 'settings builder failed (exit 3)' "$TMP/err")" = 1 ]
check $? "core builder exits 3: one stderr line names the builder failure and its exit code"
grep -q 'launch continues without --settings' "$TMP/err"
check $? "core builder exits 3: the line says the launch continues without it"
! grep -q 'NOT registered' "$TMP/err"
check $? "core builder exits 3: no second, generic diagnostic"

# Obs builder exits non-zero; the core builder still runs and its settings apply.
export SUTANDO_OBS_ENDPOINT="http://127.0.0.1:9"
stub_fail "$OBS_BUILDER" 5
stub_ok "$CORE_BUILDER" '{"hooks":{}}'
launch
[ "$rc" = 0 ] && grep -qx LAUNCHED "$TMP/out" && grep -qx -- --settings "$TMP/out"
check $? "obs builder exits 5 under set -e: the launch continues with the core --settings"
[ "$(grep -c 'obs hooks: settings builder failed (exit 5)' "$TMP/err")" = 1 ]
check $? "obs builder exits 5: one stderr line names the obs builder failure"
unset SUTANDO_OBS_ENDPOINT

# Unchanged paths: builder succeeds; builder succeeds but prints nothing.
stub_ok "$CORE_BUILDER" '{"hooks":{}}'
launch
[ "$rc" = 0 ] && grep -qx '{"hooks":{}}' "$TMP/out" && [ ! -s "$TMP/err" ]
check $? "core builder succeeds: --settings carries its JSON, nothing on stderr"
stub_ok "$CORE_BUILDER" ''
launch
[ "$rc" = 0 ] && ! grep -qx -- --settings "$TMP/out" && grep -q 'AskUserQuestion guard NOT registered' "$TMP/err"
check $? "core builder prints nothing: unchanged (no --settings, existing diagnostic)"

echo "session-launch-settings-builder-failure: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
