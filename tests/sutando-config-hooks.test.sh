#!/bin/bash
# tests/sutando-config-hooks.test.sh — E2E smoke for scripts/sutando-config-hooks.sh
#
# Coverage: `install` / `detect-missing` are gone (Sutando hooks register at launch);
# migration-notice flags non-Sutando hooks while filtering Sutando-owned ones.
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

# Test 1: Sutando hooks register only at core launch; this helper writes none.
T="$(mktemp -d)"
echo '{}' > "$T/s.json"
for sub in install detect-missing; do
  bash "$SCRIPT" "$sub" "$T/s.json" >/dev/null 2>&1
  [ "$?" = "3" ] && [ "$(cat "$T/s.json")" = "{}" ]; report "$?" "$sub is gone (exit 3) and leaves the settings file untouched"
done

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

# Test 8: invalid subcommand exits 3
bash "$SCRIPT" bogus-subcommand >/dev/null 2>&1
[ "$?" = "3" ]; report "$?" "invalid subcommand exits 3"

# Test 11: malformed JSON in migration-notice — skip cleanly, exit 0
echo 'not valid json {{{' > "$T/malformed.json"
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
