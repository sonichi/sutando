#!/bin/bash
# tests/sutando-config-hooks.test.sh — E2E smoke for scripts/sutando-config-hooks.sh
#
# Coverage:
#   1. detect-missing returns 1 on empty settings, 0 after install
#   2. install is idempotent (re-run doesn't duplicate the entry)
#   3. install --with-project-hooks adds PreCompact + Stop entries
#   4. migration-notice flags non-Sutando hooks while filtering Sutando-owned
#   5. the two installers agree on the command string, so neither double-registers
#
# Run: bash tests/sutando-config-hooks.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/sutando-config-hooks.sh"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# Test 1: detect-missing on empty returns 1
T="$(mktemp -d)"
echo '{}' > "$T/s.json"
bash "$SCRIPT" detect-missing "$T/s.json" >/dev/null 2>&1
[ "$?" = "1" ]; report "$?" "detect-missing returns 1 on empty settings"

# Test 2: install adds the catchup hook
bash "$SCRIPT" install "$T/s.json" >/dev/null 2>&1
catchup_count="$(jq '[.hooks.SessionEnd[].hooks[] | select(.command | contains("session-handoff.sh"))] | length' "$T/s.json")"
[ "$catchup_count" -ge 1 ]; report "$?" "install adds SessionEnd catchup hook"

# Test 3: detect-missing returns 0 after install
bash "$SCRIPT" detect-missing "$T/s.json" >/dev/null 2>&1
[ "$?" = "0" ]; report "$?" "detect-missing returns 0 after install"

# Test 4: idempotent re-install (count stays at 1)
bash "$SCRIPT" install "$T/s.json" >/dev/null 2>&1
catchup_count_after="$(jq '[.hooks.SessionEnd[].hooks[] | select(.command | contains("session-handoff.sh"))] | length' "$T/s.json")"
[ "$catchup_count_after" = "$catchup_count" ]; report "$?" "install is idempotent (catchup count unchanged on re-run)"

# Test 5: --with-project-hooks adds PreCompact + Stop
bash "$SCRIPT" install "$T/s.json" --with-project-hooks >/dev/null 2>&1
precompact_count="$(jq '[.hooks.PreCompact[].hooks[]] | length' "$T/s.json" 2>/dev/null || echo 0)"
stop_count="$(jq '[.hooks.Stop[].hooks[]] | length' "$T/s.json" 2>/dev/null || echo 0)"
[ "$precompact_count" -ge 2 ] && [ "$stop_count" -ge 1 ]; report "$?" "--with-project-hooks adds PreCompact + Stop entries"

# Test 6: migration-notice filters Sutando hooks, flags third-party
cat > "$T/old.json" << 'EOJ'
{
  "hooks": {
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "bash $HOME/Documents/github/sutando/src/session-handoff.sh"}]},
      {"hooks": [{"type": "command", "command": "bash $HOME/.claude/hooks/third-party.sh"}]}
    ]
  }
}
EOJ
echo '{}' > "$T/new.json"
notice_out="$(bash "$SCRIPT" migration-notice "$T/old.json" "$T/new.json" 2>&1)"
echo "$notice_out" | grep -q "third-party.sh"; report "$?" "migration-notice flags third-party hook"
echo "$notice_out" | grep -qv "session-handoff.sh"; report "$?" "migration-notice filters out Sutando hook (session-handoff.sh)"

# Test 7: detect-missing on non-existent file returns 1
bash "$SCRIPT" detect-missing "$T/does-not-exist.json" >/dev/null 2>&1
[ "$?" = "1" ]; report "$?" "detect-missing returns 1 on missing file"

# Test 8: invalid subcommand exits 3
bash "$SCRIPT" bogus-subcommand >/dev/null 2>&1
[ "$?" = "3" ]; report "$?" "invalid subcommand exits 3"

# Test 9: malformed JSON in detect-missing — explicit error + exit 1
# (per Mini's PR #1500 review — previously this silently fell through)
echo 'not valid json {{{' > "$T/malformed.json"
err_out="$(bash "$SCRIPT" detect-missing "$T/malformed.json" 2>&1)"
rc="$?"
[ "$rc" = "1" ] && echo "$err_out" | grep -q "not valid JSON"
report "$?" "detect-missing emits explicit error + exit 1 on malformed JSON"

# Test 10: malformed JSON in install — refuses to edit
err_out2="$(bash "$SCRIPT" install "$T/malformed.json" 2>&1)"
rc2="$?"
[ "$rc2" = "1" ] && echo "$err_out2" | grep -q "not valid JSON"
report "$?" "install refuses to edit malformed JSON (exit 1)"

# Test 11: malformed JSON in migration-notice — skip cleanly, exit 0
err_out3="$(bash "$SCRIPT" migration-notice "$T/malformed.json" "$T/new.json" 2>&1)"
rc3="$?"
[ "$rc3" = "0" ] && echo "$err_out3" | grep -q "malformed"
report "$?" "migration-notice skips malformed input cleanly (exit 0 + warn)"

# Test 12: write-manifest + show-manifest round-trip
T12="$(mktemp -d)"
export CLAUDE_CONFIG_DIR="$T12"
bash "$SCRIPT" write-manifest "test-id" "src/custom-hook.sh" "src/installer.sh" >/dev/null 2>&1
manifest_content="$(bash "$SCRIPT" show-manifest 2>/dev/null)"
echo "$manifest_content" | jq -e '.sutando_owned_hooks | length >= 1' >/dev/null 2>&1
report "$?" "write-manifest creates manifest with 1 entry"

# Test 13: write-manifest is idempotent (same id twice → still 1 entry)
bash "$SCRIPT" write-manifest "test-id" "src/custom-hook.sh" "src/installer.sh" >/dev/null 2>&1
entry_count="$(bash "$SCRIPT" show-manifest 2>/dev/null | jq '.sutando_owned_hooks | length' 2>/dev/null || echo 0)"
[ "$entry_count" = "1" ]; report "$?" "write-manifest is idempotent (same id → count stays at 1)"

# Test 14: migration-notice uses manifest substrings when manifest exists
# Custom hook in manifest (not in hardcoded list) should be filtered out
bash "$SCRIPT" write-manifest "custom-corp-hook" "src/custom-hook.sh" "src/installer.sh" >/dev/null 2>&1
cat > "$T12/old.json" << 'EOJ'
{
  "hooks": {
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "bash /repo/src/custom-hook.sh --arg"}]},
      {"hooks": [{"type": "command", "command": "bash $HOME/.claude/hooks/unknown-third-party.sh"}]}
    ]
  }
}
EOJ
echo '{}' > "$T12/new.json"
notice14="$(bash "$SCRIPT" migration-notice "$T12/old.json" "$T12/new.json" 2>&1)"
echo "$notice14" | grep -q "unknown-third-party.sh"
report "$?" "migration-notice: manifest-registered hook flags unknown third-party"
echo "$notice14" | grep -qv "custom-hook.sh"
report "$?" "migration-notice: manifest-registered hook NOT flagged as dropped"
unset CLAUDE_CONFIG_DIR
rm -rf "$T12"

# Test 15: partial manifest still recognizes ALL hardcoded fallback substrings
# (regression guard for liususan091219's review on PR #1505: previous logic
# returned ONLY manifest entries when manifest was non-empty, so a host where
# only catchup-install had run would have migration-notice false-positively
# flag the project hooks as "dropped third-party".)
T15="$(mktemp -d)"
export CLAUDE_CONFIG_DIR="$T15"
# Manifest with ONLY catchup-session-end (simulating partial-install host).
bash "$SCRIPT" write-manifest "catchup-session-end" "src/session-handoff.sh" "skills/catchup-after-startup/scripts/install-hook.sh" >/dev/null 2>&1
# Build an old.json with a hardcoded-list hook + a real third-party hook.
cat > "$T15/old.json" << 'EOJ'
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command", "command": "bash /repo/src/check-pending-tasks.sh"}]},
      {"hooks": [{"type": "command", "command": "bash $HOME/.claude/hooks/random-corp-thing.sh"}]}
    ]
  }
}
EOJ
echo '{}' > "$T15/new.json"
notice15="$(bash "$SCRIPT" migration-notice "$T15/old.json" "$T15/new.json" 2>&1)"
# check-pending-tasks.sh IS in the hardcoded fallback — must NOT be flagged as dropped.
echo "$notice15" | grep -qv "check-pending-tasks.sh"
report "$?" "migration-notice: partial-manifest preserves hardcoded fallback recognition"
# random-corp-thing.sh IS NOT in either list — MUST be flagged.
echo "$notice15" | grep -q "random-corp-thing.sh"
report "$?" "migration-notice: partial-manifest still flags real third-party"
unset CLAUDE_CONFIG_DIR
rm -rf "$T15"

rm -rf "$T"
echo

# Tests 16-18: #4309 review round 6 (keweichen, 2026-09-16) — _installer_hook_command
# collapsed "installer absent" / "installer present but failed" / "hook
# intentionally omitted" into one signal, so callers guessed a fallback command
# in all three cases, defeating SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE and
# masking real resolver failures as quiet success.

# Test 16: installer GENUINELY ABSENT — fallback is used, and the fallback's
# archive path comes from the workspace RESOLVER, not a hardcoded
# $REPO_DIR/workspace guess (a relocated workspace must not be silently ignored).
T16="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooks-absent.XXXXXX")"
# Normalize through cd+pwd — macOS resolves /tmp (and some /var/folders
# paths) through a /private symlink, and Python's Path.resolve() (inside
# resolve_workspace) follows it while a raw mktemp string does not.
T16="$(cd "$T16" && pwd)"
mkdir -p "$T16/scripts" "$T16/src" "$T16/.claude"
cp "$SCRIPT" "$T16/scripts/"
cp "$REPO_DIR/scripts/sutando-config.sh" "$T16/scripts/"
cp "$REPO_DIR/scripts/python-binary.sh" "$T16/scripts/" 2>/dev/null || true
cp "$REPO_DIR/src/sutando_config.py" "$T16/src/"
cp "$REPO_DIR/sutando.config.json" "$T16/"
echo "{\"workspace\":{\"path\":\"$T16/custom-ws\"}}" > "$T16/sutando.config.local.json"
echo '{}' > "$T16/.claude/settings.json"
# Deliberately no src/install-claude-hooks.sh — genuinely absent installer.
( cd "$T16" && bash scripts/sutando-config-hooks.sh install "$T16/.claude/settings.json" --no-catchup-hook --with-project-hooks >/dev/null 2>&1 )
rc16=$?
[ "$rc16" = "0" ]; report "$?" "absent installer: install --with-project-hooks still succeeds (fallback)"
archive_cmd="$(jq -r '.hooks.PreCompact[0].hooks[0].command' "$T16/.claude/settings.json" 2>/dev/null)"
echo "$archive_cmd" | grep -qF "$T16/custom-ws/logs/conversations/"
report "$?" "absent installer: archive fallback path comes from the workspace RESOLVER"
echo "$archive_cmd" | grep -qF "$T16/workspace/logs/conversations/"
[ "$?" != "0" ]; report "$?" "absent installer: archive fallback does NOT hardcode \$REPO_DIR/workspace"
rm -rf "$T16"

# Test 17: installer PRESENT but FAILS (--print-hooks exits non-zero) — must
# PROPAGATE (exit non-zero, write nothing), never silently guess a fallback.
T17="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooks-resolver-fail.XXXXXX")"
mkdir -p "$T17/scripts" "$T17/src" "$T17/.claude"
cp "$SCRIPT" "$T17/scripts/"
printf '#!/bin/bash\necho "boom: resolver failed" >&2\nexit 1\n' > "$T17/src/install-claude-hooks.sh"
chmod +x "$T17/src/install-claude-hooks.sh"
echo '{}' > "$T17/.claude/settings.json"
err17="$(cd "$T17" && bash scripts/sutando-config-hooks.sh install "$T17/.claude/settings.json" --no-catchup-hook --with-project-hooks 2>&1)"
rc17=$?
[ "$rc17" != "0" ]; report "$?" "failing installer: install --with-project-hooks propagates (non-zero exit)"
echo "$err17" | grep -q "refusing to guess"
report "$?" "failing installer: error names the refusal to guess"
pc_count17="$(jq '[.hooks.PreCompact // [] | .[] | .hooks // [] | .[]] | length' "$T17/.claude/settings.json" 2>/dev/null || echo 0)"
[ "${pc_count17:-0}" = "0" ]; report "$?" "failing installer: no PreCompact hook was written"
rm -rf "$T17"

# Test 18: INTENTIONAL OMISSION (SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1) —
# the archive hook must be skipped silently (no fallback, no failure), while
# the other two project hooks still install normally.
T18="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooks-omit.XXXXXX")"
mkdir -p "$T18/.claude"
echo '{}' > "$T18/.claude/settings.json"
SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 \
  bash "$SCRIPT" install "$T18/.claude/settings.json" --no-catchup-hook --with-project-hooks >/dev/null 2>&1
rc18=$?
[ "$rc18" = "0" ]; report "$?" "omit flag: install --with-project-hooks still succeeds"
archive_count18="$(jq '[.hooks.PreCompact // [] | .[] | .hooks // [] | .[] | select(.command | contains("archive-transcript.sh"))] | length' "$T18/.claude/settings.json" 2>/dev/null || echo 0)"
[ "${archive_count18:-0}" = "0" ]; report "$?" "omit flag: no archive hook (real or fallback) was written"
stop_count18="$(jq '[.hooks.Stop // [] | .[] | .hooks // []] | flatten | length' "$T18/.claude/settings.json" 2>/dev/null || echo 0)"
[ "${stop_count18:-0}" -ge 1 ]; report "$?" "omit flag: the OTHER project hook (Stop) still installs"
rm -rf "$T18"

# Test 19-21: both installers own ONE command string per hook. They used to carry
# separate copies, and a SessionEnd handoff written two ways registered twice.
# Pin the repo both sides resolve against: $SCRIPT asks ${SUTANDO_REPO_DIR:-$REPO_DIR}
# internally, but the install-claude-hooks.sh call below always asks $REPO_DIR — an
# inherited SUTANDO_REPO_DIR pointing at another checkout diverges the two commands
# and this check measures the host's environment, not the code (qingyun-wu 2026-09-16).
unset SUTANDO_REPO_DIR
D="$(mktemp -d)"
mkdir -p "$D/workspace/.claude-sutando"
CORE_SETTINGS="$D/workspace/.claude-sutando/settings.json"
INSTALLER_SE="$(bash "$REPO_DIR/src/install-claude-hooks.sh" --print-hooks 2>/dev/null \
  | grep '^SessionEnd|src/session-handoff.sh|')"
INSTALLER_SE="${INSTALLER_SE#*|*|}"
[ -n "$INSTALLER_SE" ]; report "$?" "install-claude-hooks.sh --print-hooks emits the SessionEnd command"

echo '{}' > "$D/s.json"
bash "$SCRIPT" install "$D/s.json" >/dev/null 2>&1
CONFIG_SE="$(jq -r '[.hooks.SessionEnd[].hooks[] | select(.command | contains("session-handoff.sh")) | .command] | .[0] // ""' "$D/s.json")"
[ "$CONFIG_SE" = "$INSTALLER_SE" ]; report "$?" "sutando-config-hooks.sh writes the installer's exact command"

# Run BOTH against one settings file: the shapes must collapse to a single entry.
bash "$SCRIPT" install "$D/s.json" --with-project-hooks >/dev/null 2>&1
SE_COUNT="$(jq '[.hooks.SessionEnd[].hooks[] | select(.command | contains("session-handoff.sh"))] | length' "$D/s.json")"
[ "$SE_COUNT" = "1" ]; report "$?" "one SessionEnd handoff entry after both install paths, not two"
rm -rf "$D"

echo "Results: $pass passed, $fail failed"
[ "$fail" = "0" ]
