#!/usr/bin/env bash
# Tests for skills/startup/SKILL.md
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SKILL="$REPO/skills/startup/SKILL.md"

pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass + 1)); }
fail() { echo "  ✗ $1"; fail=$((fail + 1)); }

echo "startup skill tests"

# Existence
[ -f "$SKILL" ] && ok "SKILL.md exists" || { fail "SKILL.md missing"; exit 1; }

# Frontmatter
grep -q '^name: startup$' "$SKILL"         && ok "name: startup" || fail "name field missing"
grep -q 'user-invocable: true' "$SKILL"    && ok "user-invocable: true" || fail "user-invocable missing"
grep -q 'canonical.*entry.*point\|entry.*point.*startup' "$SKILL" && ok "description mentions canonical entry point" || fail "description missing 'canonical entry point'"

# Sub-skill references
grep -q '/catchup-after-startup' "$SKILL"  && ok "references /catchup-after-startup" || fail "missing /catchup-after-startup"
grep -q '/task-orphan-check' "$SKILL"      && ok "references /task-orphan-check" || fail "missing /task-orphan-check"
grep -q '/schedule-crons' "$SKILL"         && ok "references /schedule-crons" || fail "missing /schedule-crons"

# Ordering — sentinel must come AFTER schedule-crons (step 4 after step 3)
step3_line=$(grep -n '/schedule-crons' "$SKILL" | head -1 | cut -d: -f1)
step4_line=$(grep -n 'Touch the sentinel\|touch.*sentinel\|sentinel.*touch' "$SKILL" | head -1 | cut -d: -f1)
[ -n "$step3_line" ] && [ -n "$step4_line" ] && [ "$step4_line" -gt "$step3_line" ] \
  && ok "sentinel touch (step 4) comes after schedule-crons (step 3)" \
  || fail "step ordering incorrect: sentinel should be after schedule-crons"

# Sentinel path uses SUTANDO_HOME (stando convention, not SUTANDO_WORKSPACE)
grep -q 'SUTANDO_HOME.*sentinel\|sentinel.*SUTANDO_HOME\|SUTANDO_HOME.*state' "$SKILL" \
  && ok "sentinel path uses SUTANDO_HOME" || fail "sentinel should use SUTANDO_HOME, not SUTANDO_WORKSPACE"

# Does NOT use SUTANDO_WORKSPACE for sentinel (stano-specific adaptation)
if grep -q 'SUTANDO_WORKSPACE.*sentinel\|sentinel.*SUTANDO_WORKSPACE' "$SKILL"; then
  fail "sentinel path must not use SUTANDO_WORKSPACE (use SUTANDO_HOME)"
else
  ok "sentinel path does not reference SUTANDO_WORKSPACE"
fi

# Idempotency note
grep -q 'idempotent\|no-op\|sentinel' "$SKILL" && ok "idempotency documented" || fail "idempotency not mentioned"

# Optional orphan check (guarded by skill presence)
grep -q 'not installed.*skip\|skip.*not installed' "$SKILL" \
  && ok "orphan-check is optional / gracefully skipped" || fail "orphan-check should be skip-guarded"

# Summary emit (step 5)
grep -q '/startup complete\|Emit.*summary\|one-line summary' "$SKILL" \
  && ok "step 5 summary emit present" || fail "summary emit step missing"

echo
echo "Results: $pass passed, $fail failed"
[ $fail -eq 0 ] && echo "All $pass startup skill tests passed." && exit 0 || exit 1
