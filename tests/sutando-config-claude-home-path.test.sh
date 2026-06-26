#!/bin/bash
# tests/sutando-config-claude-home-path.test.sh — E2E smoke for the shell
# `claude-home-path` subcommand on scripts/sutando-config.sh.
#
# Mirrors tests/util-paths-ccd-banner.test.py (the Python-side test for
# src/util_paths.py:claude_home_path). 4 cases, 12 assertions:
#
#   1. CLAUDE_CONFIG_DIR set       → no banner, resolves under CCD
#   2. CLAUDE_HOME set (CCD unset) → no banner, resolves under CLAUDE_HOME
#   3. Both unset                  → banner fires on stderr, falls back to ~/.claude/
#   4. SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 → silenced, still falls back
#
# Run: bash tests/sutando-config-claude-home-path.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/sutando-config.sh"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# -- Test 1: CCD set → no banner, resolves under CCD ------------------------
echo "[1] CLAUDE_CONFIG_DIR set → no banner, resolves under CCD"
T1="$(mktemp -d)"
out1="$(CLAUDE_CONFIG_DIR="$T1" bash "$SCRIPT" claude-home-path channels discord access.json 2>/dev/null)"
err1="$(CLAUDE_CONFIG_DIR="$T1" bash "$SCRIPT" claude-home-path channels discord access.json 2>&1 >/dev/null)"
[ "$out1" = "$T1/channels/discord/access.json" ]; report "$?" "resolves under CCD"
[ -z "$err1" ]; report "$?" "no fallback banner on stderr"
rm -rf "$T1"

# -- Test 2: CLAUDE_HOME set (CCD unset) → no banner ------------------------
echo "[2] CLAUDE_HOME set (CCD unset) → no banner, resolves under CLAUDE_HOME"
T2="$(mktemp -d)"
out2="$(env -u CLAUDE_CONFIG_DIR CLAUDE_HOME="$T2" bash "$SCRIPT" claude-home-path channels discord access.json 2>/dev/null)"
err2="$(env -u CLAUDE_CONFIG_DIR CLAUDE_HOME="$T2" bash "$SCRIPT" claude-home-path channels discord access.json 2>&1 >/dev/null)"
[ "$out2" = "$T2/channels/discord/access.json" ]; report "$?" "resolves under CLAUDE_HOME"
[ -z "$err2" ]; report "$?" "no banner — CLAUDE_HOME is a test override, not the deprecated path"
rm -rf "$T2"

# -- Test 3: Both unset → banner fires on stderr, falls back ----------------
echo "[3] CCD + CLAUDE_HOME unset → banner fires, falls back to ~/.claude/"
out3="$(env -u CLAUDE_CONFIG_DIR -u CLAUDE_HOME -u SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER bash "$SCRIPT" claude-home-path channels discord access.json 2>/dev/null)"
err3="$(env -u CLAUDE_CONFIG_DIR -u CLAUDE_HOME -u SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER bash "$SCRIPT" claude-home-path channels discord access.json 2>&1 >/dev/null)"
[ "$out3" = "$HOME/.claude/channels/discord/access.json" ]; report "$?" "resolves under ~/.claude/ default"
echo "$err3" | grep -q "CLAUDE_CONFIG_DIR not set"; report "$?" "fallback banner present on stderr"

# -- Test 4: SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 → silenced --------------
echo "[4] SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 → no banner, still falls back"
out4="$(env -u CLAUDE_CONFIG_DIR -u CLAUDE_HOME SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$SCRIPT" claude-home-path channels discord access.json 2>/dev/null)"
err4="$(env -u CLAUDE_CONFIG_DIR -u CLAUDE_HOME SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$SCRIPT" claude-home-path channels discord access.json 2>&1 >/dev/null)"
[ "$out4" = "$HOME/.claude/channels/discord/access.json" ]; report "$?" "resolves under ~/.claude/ default (suppression only silences banner)"
[ -z "$err4" ]; report "$?" "no banner with suppression env var set"

# -- Test 5: base only (no sub-path) ----------------------------------------
echo "[5] base only (no sub-path) → just \$CLAUDE_CONFIG_DIR"
T5="$(mktemp -d)"
out5="$(CLAUDE_CONFIG_DIR="$T5" bash "$SCRIPT" claude-home-path 2>/dev/null)"
[ "$out5" = "$T5" ]; report "$?" "base-only resolution returns just \$CLAUDE_CONFIG_DIR"
rm -rf "$T5"

# -- Test 6: multi-arg sub-path joining -------------------------------------
echo "[6] multi-arg sub-path joining"
T6="$(mktemp -d)"
out6="$(CLAUDE_CONFIG_DIR="$T6" bash "$SCRIPT" claude-home-path skills quota-tracker scripts read-quota.py 2>/dev/null)"
[ "$out6" = "$T6/skills/quota-tracker/scripts/read-quota.py" ]; report "$?" "multi-arg sub-path joins with /"
rm -rf "$T6"

# -- Test 7: single-arg sub-path with embedded slashes ----------------------
echo "[7] single-arg sub-path with embedded slashes"
T7="$(mktemp -d)"
out7="$(CLAUDE_CONFIG_DIR="$T7" bash "$SCRIPT" claude-home-path "skills/quota-tracker/scripts/read-quota.py" 2>/dev/null)"
[ "$out7" = "$T7/skills/quota-tracker/scripts/read-quota.py" ]; report "$?" "single-arg with embedded slashes preserves them"
rm -rf "$T7"

echo
echo "Results: $pass passed, $fail failed"
[ "$fail" = "0" ]
