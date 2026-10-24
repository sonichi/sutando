#!/usr/bin/env bash
# Tests for src/schedule-crons-session-hint.sh and its registration in the core's
# launch settings (src/agent/claude/cli/build-core-settings.mjs --owned-hooks).
#
# Covers:
#   1. Hint script outputs valid JSON with expected structure, gated on the core marker
#   2. Launch settings register it exactly once, matcher "" (every SessionStart source)
#
# Run: bash tests/session-start-schedule-crons-hook.test.sh
# Exit: 0 = all pass, non-zero = failure

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
HINT="$REPO/src/schedule-crons-session-hint.sh"
BUILDER="$REPO/src/agent/claude/cli/build-core-settings.mjs"

TMPDIR_BASE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

pass=0; fail=0
ok()   { echo "  ok  $1"; pass=$((pass+1)); }
fail() { echo "FAIL: $1" >&2; fail=$((fail+1)); }

# ── 1. Hint script outputs valid JSON with expected structure ──────────────────
# The hint gates on SUTANDO_CORE_SESSION=1 (only the core bootstraps), so set it
# for the "fires" assertions below.
out="$(SUTANDO_CORE_SESSION=1 bash "$HINT")"
# Must be valid JSON
echo "$out" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
  && ok "hint script outputs valid JSON" \
  || fail "hint script output is not valid JSON: $out"

# Must contain hookSpecificOutput.additionalContext
ctx="$(echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d['hookSpecificOutput']['additionalContext'])
" 2>/dev/null)"
[ -n "$ctx" ] \
  && ok "hint script additionalContext is non-empty" \
  || fail "hint script missing hookSpecificOutput.additionalContext"

# Must mention /startup (the canonical bootstrap: orphan-recovery THEN crons)
case "$ctx" in
  */startup*) ok "hint script mentions /startup" ;;
  *) fail "hint script additionalContext does not mention /startup: $ctx" ;;
esac

# Scope gate: WITHOUT the core marker the hint must stay silent (no context) so
# ad-hoc sessions in the checkout don't trigger a redundant cron bootstrap.
out_nogate="$(env -u SUTANDO_CORE_SESSION bash "$HINT")"
[ -z "$out_nogate" ] \
  && ok "hint script is silent without SUTANDO_CORE_SESSION marker" \
  || fail "hint script emitted output without the core marker: $out_nogate"

# ── 2. Launch settings register it once, matcher "" ──────────────────────────
reg="$(node "$BUILDER" /x/guard.py "" --owned-hooks "$REPO" | HINT="$HINT" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
print([(g.get('matcher'), h['command']) for g in d['hooks'].get('SessionStart', [])
       for h in g.get('hooks', []) if 'schedule-crons-session-hint.sh' in h['command']])
")"
expect="$(HINT="$HINT" python3 -c "import os; h=os.environ['HINT']; print([('', 'bash ' + chr(39) + h.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39)) + chr(39))])")"
[ "$reg" = "$expect" ] \
  && ok "launch settings register the hint once, matcher \"\"" \
  || fail "launch-settings registration: got $reg, want $expect"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
