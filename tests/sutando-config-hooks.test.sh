#!/bin/bash
# tests/sutando-config-hooks.test.sh — E2E smoke for scripts/sutando-config-hooks.sh
#
# Coverage:
#   1. detect-missing returns 1 on empty settings, 0 after install
#   2. install is idempotent (re-run doesn't duplicate the entry)
#   3. install --with-project-hooks adds PreCompact + Stop entries
#   4. migration-notice flags non-Sutando hooks while filtering Sutando-owned
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
echo "Results: $pass passed, $fail failed"
[ "$fail" = "0" ]
