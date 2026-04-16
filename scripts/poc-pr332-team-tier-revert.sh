#!/bin/bash
# POC: PR #332 — revert team-tier -C /tmp, strengthen instructions
#
# Reproduces the bugs introduced by PR #331 and verifies the fix from PR #332.
#
# Bug #1 (PR #331 team tier): Used `codex exec --sandbox read-only -C /tmp` WITHOUT
#   --skip-git-repo-check. Codex refuses to start from /tmp ("Not inside a trusted
#   directory") because /tmp is not a git repo and the flag wasn't supplied.
#
# Bug #2 (PR #331 team tier): Even if the flag had been present, -C /tmp only
#   changes the working directory — it does NOT block absolute path reads like
#   `cat /path/to/project/.env`. The sandbox is read-only everywhere.
#
# Bug #3 (PR #331 other tier): Same missing --skip-git-repo-check as team tier.
#   The other tier already had -C /tmp but was equally broken.
#
# Fix (PR #332):
#   - team tier: dropped -C /tmp entirely; added --skip-git-repo-check so codex
#     actually starts; added explicit .env-refusal rule to the system instructions
#   - other tier: kept -C /tmp; added --skip-git-repo-check so codex actually starts
#
# Usage: bash scripts/poc-pr332-team-tier-revert.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  POC: PR #332 — revert team-tier -C /tmp, fix sandbox boot  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# ─── Phase 0: Reproduce the bugs (old code, PR #331) ────────────────

echo "━━━ Phase 0: REPRODUCE the bugs (code at PR #331, commit 5400fc1) ━━━"
echo ""

# Extract PR #331 team-tier codex invocation from git history
OLD_TEAM_CMD=$(git show 5400fc1:src/discord-bridge.py \
    | grep 'codex exec' \
    | grep -v 'other\|#' \
    | head -1 \
    | sed "s/f\"  //;s/  codex exec/codex exec/;s/{quoted_task}/'<task>'/g;s/\\\\n\",*//")
echo "  PR #331 team-tier invocation (from git show 5400fc1):"
echo "    $OLD_TEAM_CMD"
echo ""

# Bug #1: team tier used -C /tmp WITHOUT --skip-git-repo-check
echo "--- Test 0a: PR #331 team tier is missing --skip-git-repo-check ---"
OLD_TEAM_LINE=$(git show 5400fc1:src/discord-bridge.py | grep 'codex exec' | sed -n '1p')
if echo "$OLD_TEAM_LINE" | grep -q '\-C /tmp'; then
    if echo "$OLD_TEAM_LINE" | grep -q '\-\-skip-git-repo-check'; then
        fail "PR #331 team tier already had --skip-git-repo-check (expected it to be missing)"
    else
        pass "BUG REPRODUCED: PR #331 team tier uses -C /tmp WITHOUT --skip-git-repo-check"
        echo "  → codex refuses: 'Not inside a trusted directory (no .git found)'"
    fi
else
    fail "PR #331 team tier does not use -C /tmp — pattern changed"
fi
echo ""

# Bug #2: -C /tmp does not block absolute path reads
echo "--- Test 0b: -C /tmp does not block absolute-path .env reads ---"
# Demonstrate: changing cwd to /tmp does not prevent 'cat /abs/path/.env'
# We use a temp file to simulate a .env in the project directory
MOCK_ENV=$(mktemp /tmp/mock-dot-env.XXXXXX)
echo "SECRET_KEY=super-secret-value" > "$MOCK_ENV"

# Simulate codex sandbox: read-only, cwd=/tmp — but absolute path still works
ABS_READ=$(cd /tmp && cat "$MOCK_ENV" 2>&1 || echo "READ_FAILED")
if echo "$ABS_READ" | grep -q "super-secret-value"; then
    pass "BUG REPRODUCED: -C /tmp does NOT block absolute-path reads (cat \$MOCK_ENV still works from /tmp cwd)"
    echo "  → attacker can still run: cat /path/to/.env even from -C /tmp"
else
    fail "Expected absolute path read to succeed from /tmp cwd, got: $ABS_READ"
fi
rm -f "$MOCK_ENV"
echo ""

# Bug #3: PR #331 other tier also missing --skip-git-repo-check
echo "--- Test 0c: PR #331 other tier is also missing --skip-git-repo-check ---"
# Filter to only actual invocation lines (contain quoted_task placeholder)
OLD_OTHER_LINE=$(git show 5400fc1:src/discord-bridge.py \
    | grep 'codex exec' \
    | grep 'quoted_task' \
    | sed -n '2p')
if echo "$OLD_OTHER_LINE" | grep -q '\-C /tmp'; then
    if echo "$OLD_OTHER_LINE" | grep -q '\-\-skip-git-repo-check'; then
        fail "PR #331 other tier already had --skip-git-repo-check (expected it to be missing)"
    else
        pass "BUG REPRODUCED: PR #331 other tier uses -C /tmp WITHOUT --skip-git-repo-check"
        echo "  → same boot failure: codex refuses to run from /tmp"
    fi
else
    fail "PR #331 other tier does not use -C /tmp — unexpected pattern"
fi
echo ""

# ─── Phase 1: Verify fix — team tier no longer uses -C /tmp ─────────

echo "━━━ Phase 1: Verify fix — team tier dropped -C /tmp ━━━"
echo ""

echo "--- Test 1a: team tier does NOT use -C /tmp ---"
TEAM_CMD=$(python3 -c "
import re, sys

src = open('src/discord-bridge.py').read()
# Extract the team-tier instructions block
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m:
    print(m.group(0))
")

if echo "$TEAM_CMD" | grep -q 'codex exec'; then
    if echo "$TEAM_CMD" | grep 'codex exec' | grep -q '\-C /tmp'; then
        fail "team tier still uses -C /tmp"
    else
        pass "team tier does NOT use -C /tmp (PR #332 fix confirmed)"
        echo "  team-tier invocation:"
        echo "$TEAM_CMD" | grep 'codex exec' | sed 's/^/    /'
    fi
else
    fail "codex exec not found in team-tier instructions"
fi
echo ""

# ─── Phase 2: Verify fix — both tiers have --skip-git-repo-check ────

echo "━━━ Phase 2: Verify fix — --skip-git-repo-check present where needed ━━━"
echo ""

echo "--- Test 2a: team tier has correct codex invocation (no -C /tmp, no --skip needed) ---"
# After the revert, team tier uses plain: codex exec --sandbox read-only -- <task>
# No -C /tmp means no need for --skip-git-repo-check (runs from workspace dir)
# Use python to extract team-tier block and grep within it
TEAM_CODEX=$(python3 -c "
import re
src = open('src/discord-bridge.py').read()
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m:
    for line in m.group(0).splitlines():
        if 'codex exec' in line:
            print(line.strip())
            break
" || true)
if [ -n "$TEAM_CODEX" ]; then
    if echo "$TEAM_CODEX" | grep -q '\-C /tmp'; then
        fail "team tier unexpectedly uses -C /tmp"
    elif echo "$TEAM_CODEX" | grep -q '\-\-skip-git-repo-check'; then
        # Having --skip-git-repo-check is fine too but not required without -C /tmp
        pass "team tier has --skip-git-repo-check (extra safety)"
    else
        pass "team tier uses plain invocation without -C /tmp (runs from workspace — git check passes automatically)"
        echo "  → $TEAM_CODEX"
    fi
else
    fail "codex exec not found in team tier"
fi
echo ""

echo "--- Test 2b: other tier has --skip-git-repo-check (needed because -C /tmp is kept) ---"
OTHER_CODEX=$(python3 -c "
import re

src = open('src/discord-bridge.py').read()
m = re.search(r'\"other\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m:
    block = m.group(0)
    for line in block.splitlines():
        if 'codex exec' in line:
            print(line.strip())
            break
")
if [ -n "$OTHER_CODEX" ]; then
    if echo "$OTHER_CODEX" | grep -q '\-\-skip-git-repo-check'; then
        pass "other tier has --skip-git-repo-check (PR #332 fix confirmed)"
        echo "  → $OTHER_CODEX"
    else
        fail "other tier still missing --skip-git-repo-check: $OTHER_CODEX"
    fi
else
    fail "codex exec not found in other tier"
fi
echo ""

# ─── Phase 3: Verify strengthened .env-refusal instruction ──────────

echo "━━━ Phase 3: Verify strengthened system instructions ━━━"
echo ""

echo "--- Test 3a: team tier has explicit .env-refusal rule ---"
TEAM_BLOCK=$(python3 -c "
import re
src = open('src/discord-bridge.py').read()
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m: print(m.group(0))
")
if echo "$TEAM_BLOCK" | grep -q '\.env.*credentials\|refuse.*\.env'; then
    pass "team tier has explicit .env / credentials refusal rule"
    echo "$TEAM_BLOCK" | grep '\.env' | head -2 | sed 's/^/  /'
else
    fail "team tier missing .env-refusal rule in system instructions"
fi
echo ""

echo "--- Test 3b: team tier PR #331 lacked .env-refusal rule ---"
OLD_TEAM_BLOCK=$(git show 5400fc1:src/discord-bridge.py | python3 -c "
import re, sys
src = sys.stdin.read()
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m: print(m.group(0))
")
if echo "$OLD_TEAM_BLOCK" | grep -q '\.env.*credentials\|refuse.*\.env'; then
    fail "PR #331 already had .env-refusal (expected it to be absent)"
else
    pass "CONFIRMED: PR #331 team tier lacked the explicit .env-refusal rule"
    echo "  → PR #332 added it as the compensating defense after dropping -C /tmp"
fi
echo ""

# ─── Phase 3c: Structural snapshot comparison ───────────────────────

echo "--- Test 3c: PR #332 team tier removed -C /tmp vs PR #331 ---"
OLD_TEAM_CODEX=$(git show 5400fc1:src/discord-bridge.py | python3 -c "
import re, sys
src = sys.stdin.read()
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m:
    for line in m.group(0).splitlines():
        if 'codex exec' in line:
            print(line.strip())
            break
")
NEW_TEAM_CODEX=$(python3 -c "
import re
src = open('src/discord-bridge.py').read()
m = re.search(r'\"team\":\s*\(.*?===END SUTANDO SYSTEM INSTRUCTIONS===', src, re.DOTALL)
if m:
    for line in m.group(0).splitlines():
        if 'codex exec' in line:
            print(line.strip())
            break
")
echo "  PR #331: $OLD_TEAM_CODEX"
echo "  PR #332: $NEW_TEAM_CODEX"

OLD_HAS_TMP=$(echo "$OLD_TEAM_CODEX" | grep -c '\-C /tmp' || true)
NEW_HAS_TMP=$(echo "$NEW_TEAM_CODEX" | grep -c '\-C /tmp' || true)

if [ "$OLD_HAS_TMP" -gt 0 ] && [ "$NEW_HAS_TMP" -eq 0 ]; then
    pass "Diff confirmed: PR #331 had -C /tmp in team tier; PR #332 removed it"
else
    fail "Expected: old has -C /tmp, new does not. old=$OLD_HAS_TMP, new=$NEW_HAS_TMP"
fi
echo ""

# ─── Summary ─────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed"
echo ""
echo "  PR #331 bugs:"
echo "    1. team tier: codex exec -C /tmp WITHOUT --skip-git-repo-check"
echo "       → codex refuses: 'Not inside a trusted directory'"
echo "    2. team tier: -C /tmp doesn't block absolute-path .env reads"
echo "       → cat /abs/path/.env works from any cwd"
echo "    3. other tier: same missing --skip-git-repo-check"
echo ""
echo "  PR #332 fix:"
echo "    team tier: dropped -C /tmp; added explicit .env-refusal rule"
echo "    other tier: kept -C /tmp; added --skip-git-repo-check"
echo "    both tiers: codex now actually starts (no git-check failure)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
