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

rm -rf "$T"
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" = "0" ]
