#!/usr/bin/env bash
# An enabled skill's manifest "claude_plugin" directory becomes one --plugin-dir; nothing else does.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PY="$(command -v python3)"

# shellcheck source=/dev/null
source "$REPO/src/skill-manifest-config.sh"
# shellcheck source=/dev/null
source "$REPO/src/agent/claude/cli/session-launch.sh"

# $1 = manifest JSON; leaves SURFACE_ARGS and $err for the case.
launch() {
  rm -rf "$TMP/repo"; mkdir -p "$TMP/repo/skills/zz-skill/plugin" "$TMP/repo/skills/zz-skill/file"
  [ -n "${1:-}" ] && printf '%s' "$1" > "$TMP/repo/skills/zz-skill/manifest.json"
  mkdir -p "$TMP/repo/skills/outside"
  REPO="$TMP/repo"; SURFACE_ARGS=()
  add_skill_claude_plugins 2>"$TMP/err"; err="$(cat "$TMP/err")"
}

REAL="$(cd "$TMP" && pwd -P)/repo/skills/zz-skill/plugin"

launch '{"enabled": true, "claude_plugin": "./plugin"}'
[ "${SURFACE_ARGS[*]-}" = "--plugin-dir $REAL" ]
check $? "an enabled skill's in-skill directory becomes --plugin-dir <realpath>"

launch '{"enabled": false, "claude_plugin": "./plugin"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "enabled:false adds nothing"

launch '{"claude_plugin": "./plugin"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "a manifest without enabled:true adds nothing"

launch '{"enabled": true, "claude_plugin": "../outside"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ] && echo "$err" | grep -q "ignoring claude_plugin"
check $? "a claude_plugin escaping the skill is ignored and reported"

ln -s "$TMP/repo/skills/outside" "$TMP/repo/skills/zz-skill/link" 2>/dev/null
launch '{"enabled": true, "claude_plugin": "./link"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "a symlink leaving the skill is ignored"

launch '{"enabled": true, "claude_plugin": "./nope"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "a missing directory adds nothing"

launch '{"enabled": true, "claude_plugin": "./file/../../../outside"}'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "a dot-dot path that normalizes outside the skill is ignored"

launch 'not json'
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "a malformed manifest adds nothing"

rm -rf "$TMP/repo"; mkdir -p "$TMP/repo"
REPO="$TMP/repo"; SURFACE_ARGS=()
add_skill_claude_plugins
[ "${#SURFACE_ARGS[@]}" -eq 0 ]
check $? "no skills at all adds nothing"

echo "claude-plugin-launch-args: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
