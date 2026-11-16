#!/usr/bin/env bash
# A Claude session whose cwd is not the checkout still gets the Stop hook and a pointer to the
# checkout's CLAUDE.md; a session in the checkout gets neither (its project settings carry them).
set -uo pipefail
REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
command -v node >/dev/null 2>&1 || { echo "SKIP: node unavailable"; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TMP="$(cd "$TMP" && pwd -P)"
# The launchers run from the checkout; a relative hook path must not resolve against it.
cd "$REAL_REPO" || exit 1
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


# start-cli.sh runs the real installer into an override dir first: --settings must not repeat it.
if command -v jq >/dev/null 2>&1; then
  mkdir -p "$TMP/core-override" "$TMP/home"
  HOME="$TMP/home" SUTANDO_CLAUDE_WORKING_DIR="$TMP/core-override" SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 \
    bash "$REAL_REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
  jq -e --arg c "src/check-pending-tasks.sh" '[.hooks.Stop[]?.hooks[]?.command | select(contains($c))] | length == 1' \
    "$TMP/core-override/.claude/settings.json" >/dev/null
  check $? "fixture: the real installer wrote this checkout's Stop hook into the override dir"
  settings_for "$TMP/core-override"
  [ -z "$(stop_commands)" ]
  check $? "an override dir holding this checkout's installed Stop hook: no second copy in --settings"
  P="$(prompt_arg)"; case "$P" in *"Read $REAL_REPO/CLAUDE.md"*) true ;; *) false ;; esac
  check $? "an override dir holding this checkout's installed Stop hook: the CLAUDE.md pointer stays"
else
  echo "  skip installer-fixture cases (jq unavailable)"
fi

# Another clone's hook watches that clone's workspace, never this worker's deliveries.
mkdir -p "$TMP/other-clone/.claude" "$TMP/elsewhere/sutando/src"
: > "$TMP/elsewhere/sutando/src/check-pending-tasks.sh"
printf '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"bash '"'"'%s/elsewhere/sutando/src/check-pending-tasks.sh'"'"'"}]}]}}' \
  "$TMP" > "$TMP/other-clone/.claude/settings.json"
settings_for "$TMP/other-clone"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "an override dir holding ANOTHER clone's Stop hook still gets this checkout's"

# A mention outside hooks.Stop (a permission entry) is not a registered hook.
mkdir -p "$TMP/perm-only/.claude"
printf '{"permissions":{"allow":["Bash(bash %s/src/check-pending-tasks.sh)"]}}' "$REAL_REPO" \
  > "$TMP/perm-only/.claude/settings.local.json"
settings_for "$TMP/perm-only"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "an override dir that only mentions the script in a permission still gets the Stop hook"

# A relative Stop command runs in the session's folder, so it resolves there, not in the checkout.
stop_hook_file() { mkdir -p "$1/.claude"; printf '{"hooks":{"%s":[{"hooks":[{"type":"command","command":"%s"}]}]}}' "$2" "$3" > "$1/.claude/settings.json"; }
stop_hook_file "$TMP/rel-missing" Stop "bash src/check-pending-tasks.sh"
settings_for "$TMP/rel-missing"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "a relative Stop command naming a script the folder does not have: the hook is added"

stop_hook_file "$TMP/rel-foreign" Stop "bash src/check-pending-tasks.sh"
mkdir -p "$TMP/rel-foreign/src"; : > "$TMP/rel-foreign/src/check-pending-tasks.sh"
settings_for "$TMP/rel-foreign"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "a relative Stop command naming the folder's own different script: the hook is added"

stop_hook_file "$TMP/rel-linked" Stop "bash src/check-pending-tasks.sh"
ln -s "$REAL_REPO/src" "$TMP/rel-linked/src"
settings_for "$TMP/rel-linked"
[ -z "$(stop_commands)" ]
check $? "a relative Stop command reaching this checkout's script through a symlink: counted, no second copy"

stop_hook_file "$TMP/other-event" PostToolUse "bash '$REAL_REPO/src/check-pending-tasks.sh'"
settings_for "$TMP/other-event"
[ "$(stop_commands)" = "bash '$REAL_REPO/src/check-pending-tasks.sh'" ]
check $? "the same command under another hook event is not a Stop hook: the hook is added"

# The helper itself: relative paths from the checkout or a symlink to it, and a bare spaced path.
HELPER="$REAL_REPO/src/agent/claude/cli/project-has-stop-hook.mjs"
stop_hook_file "$TMP/rel-settings" Stop "bash src/check-pending-tasks.sh"
node "$HELPER" "$REAL_REPO" "$REAL_REPO/src/check-pending-tasks.sh" "$TMP/rel-settings/.claude/settings.json"
check $? "helper: a relative command from this checkout's folder is this checkout's hook"
node "$HELPER" "$TMP/repo-link" "$REAL_REPO/src/check-pending-tasks.sh" "$TMP/rel-settings/.claude/settings.json"
check $? "helper: a relative command from a symlink to this checkout is this checkout's hook"
mkdir -p "$TMP/a b"; : > "$TMP/a b/hook.sh"
stop_hook_file "$TMP/spaced-bare" Stop "bash $TMP/a b/hook.sh"
! node "$HELPER" "$TMP" "$TMP/a b/hook.sh" "$TMP/spaced-bare/.claude/settings.json"
check $? "helper: an unquoted path with a space is split by bash, so it is not counted"
stop_hook_file "$TMP/spaced-quoted" Stop "bash '$TMP/a b/hook.sh'"
node "$HELPER" "$TMP" "$TMP/a b/hook.sh" "$TMP/spaced-quoted/.claude/settings.json"
check $? "helper: the same path single-quoted is counted"

echo "session-launch-outside-checkout: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
