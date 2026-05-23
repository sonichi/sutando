#!/usr/bin/env bash
# Tests for skills/task-orphan-check/SKILL.md
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SKILL="$REPO/skills/task-orphan-check/SKILL.md"

pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass + 1)); }
fail() { echo "  ✗ $1"; fail=$((fail + 1)); }

echo "task-orphan-check skill tests"

# Existence
[ -f "$SKILL" ] && ok "SKILL.md exists" || { fail "SKILL.md missing"; exit 1; }

# Frontmatter
grep -q '^name: task-orphan-check$' "$SKILL"   && ok "name: task-orphan-check" || fail "name field wrong"
grep -q 'user-invocable: true' "$SKILL"         && ok "user-invocable: true" || fail "user-invocable missing"
grep -q 'orphan\|classification' "$SKILL"       && ok "description mentions orphan classification" || fail "description missing orphan concept"

# Core steps present
grep -q 'Step 1.*List live tasks\|List live tasks' "$SKILL"    && ok "Step 1 list-tasks present" || fail "missing list-tasks step"
grep -q 'Step 2.*Classify\|Classify each task' "$SKILL"        && ok "Step 2 classify present" || fail "missing classify step"
grep -q 'Step 3.*Recover\|Recover orphan' "$SKILL"             && ok "Step 3 recover present" || fail "missing recover step"
grep -q 'Step 4.*archive\|archive.*directory\|Sanity check' "$SKILL" && ok "Step 4 archive sanity present" || fail "missing archive sanity step"
grep -q 'Step 5.*Emit summary\|summary' "$SKILL"               && ok "Step 5 summary present" || fail "missing summary step"

# DONE / FRESH / ORPHAN classification markers
grep -q 'DONE' "$SKILL"   && ok "DONE classification mentioned" || fail "DONE classification missing"
grep -q 'FRESH' "$SKILL"  && ok "FRESH classification mentioned" || fail "FRESH classification missing"
grep -q 'ORPHAN' "$SKILL" && ok "ORPHAN classification mentioned" || fail "ORPHAN classification missing"

# 5-minute fresh threshold
grep -q '300\|5 min' "$SKILL" && ok "5-minute fresh threshold present" || fail "5-minute threshold missing"

# Completion marker cross-reference
grep -q 'results/task-' "$SKILL"    && ok "result-file completion marker mentioned" || fail "result-file marker missing"
grep -q '\.sending\|sending' "$SKILL" && ok ".sending idempotency marker mentioned" || fail ".sending marker missing"

# Uses SUTANDO_HOME (stando convention), not SUTANDO_WORKSPACE
grep -q 'SUTANDO_HOME' "$SKILL" && ok "uses SUTANDO_HOME workspace var" || fail "should use SUTANDO_HOME not SUTANDO_WORKSPACE"
if grep -q 'SUTANDO_WORKSPACE' "$SKILL"; then
  fail "must not reference SUTANDO_WORKSPACE (use SUTANDO_HOME)"
else
  ok "does not reference SUTANDO_WORKSPACE"
fi

# Wired into /startup
grep -q '/startup\|startup.*step 2' "$SKILL" && ok "references /startup integration" || fail "/startup integration not documented"

# Safety: does not modify archive dir itself
grep -q 'DOES NOT touch.*archive\|archive.*graveyard\|never modified except' "$SKILL" \
  && ok "archive non-modification guarantee documented" || fail "missing archive safety guarantee"

echo
echo "Results: $pass passed, $fail failed"
[ $fail -eq 0 ] && echo "All $pass task-orphan-check tests passed." && exit 0 || exit 1
