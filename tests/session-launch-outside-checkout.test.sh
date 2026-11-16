#!/usr/bin/env bash
# A Claude session whose cwd is not the checkout still gets the Stop hook and a pointer to the
# checkout's CLAUDE.md; a session in the checkout gets neither (its project settings carry them).
set -uo pipefail
REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
command -v node >/dev/null 2>&1 || { echo "SKIP: node unavailable"; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/project"

# shellcheck source=/dev/null
source "$REAL_REPO/src/agent/claude/cli/session-launch.sh"

# $1 = working dir ('' for none); leaves SETTINGS_ARGS.
settings_for() {
  REPO="$REAL_REPO"; unset SUTANDO_OBS_ENDPOINT
  if [ -n "$1" ]; then export SUTANDO_CLAUDE_WORKING_DIR="$1"; else unset SUTANDO_CLAUDE_WORKING_DIR; fi
  resolve_claude_settings_args >/dev/null 2>"$TMP/err"; rc=$?
}

settings_json() { local i; for ((i = 0; i < ${#SETTINGS_ARGS[@]}; i++)); do
  [ "${SETTINGS_ARGS[$i]}" = "--settings" ] && printf '%s' "${SETTINGS_ARGS[$((i + 1))]}"; done; }
prompt_arg() { local i; for ((i = 0; i < ${#SETTINGS_ARGS[@]}; i++)); do
  [ "${SETTINGS_ARGS[$i]}" = "--append-system-prompt" ] && printf '%s' "${SETTINGS_ARGS[$((i + 1))]}"; done; }
stop_commands() { settings_json | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const s=JSON.parse(d);for(const b of (s.hooks.Stop||[]))for(const h of b.hooks)console.log(h.command)})'; }

settings_for "$TMP/project"
[ "$rc" = 0 ]; check $? "outside the checkout: the function succeeds"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "outside the checkout: --settings carries exactly one Stop hook, check-pending-tasks.sh"
P="$(prompt_arg)"; case "$P" in *"Read $REAL_REPO/CLAUDE.md"*) true ;; *) false ;; esac
check $? "outside the checkout: --append-system-prompt points at the checkout's CLAUDE.md"
case "$P" in *"$REAL_REPO/src/result_publish.py"*) true ;; *) false ;; esac
check $? "outside the checkout: the pointer names result_publish.py"

settings_for "$REAL_REPO"
[ "$rc" = 0 ]; check $? "in the checkout: the function succeeds"
[ -z "$(stop_commands)" ]; check $? "in the checkout: no Stop hook in --settings (no duplicate of the project hook)"
[ -z "$(prompt_arg)" ]; check $? "in the checkout: no --append-system-prompt"

ln -s "$REAL_REPO" "$TMP/repo-link"
settings_for "$TMP/repo-link"
[ -z "$(stop_commands)" ] && [ -z "$(prompt_arg)" ]
check $? "a symlink to the checkout counts as the checkout"

settings_for ""
[ "$rc" = 0 ] && [ -z "$(stop_commands)" ] && [ -z "$(prompt_arg)" ]
check $? "no working dir (the default, cwd = checkout): unchanged"

echo "session-launch-outside-checkout: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
