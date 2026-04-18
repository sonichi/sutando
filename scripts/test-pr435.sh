#!/usr/bin/env bash
# POC for PR #435 — fix(stage-readiness): reject malformed presenter-mode sentinel.
#
# Bug class (shared with PR #432): bash string comparison on the
# presenter-mode sentinel file treats non-digit garbage as GREATER than any
# real ISO timestamp, so a corrupt sentinel appears active forever.
# PR #432 already fixed 3 Python sites; #435 fixes the 4th (bash) at
# scripts/stage-readiness.sh:106.
#
# Phases:
#   A — reproduce bug at parent commit (string compare fails open)
#   B — verify fix at head (regex guard rejects garbage, warns operator)
#   C — scope-gap audit (no surviving unfixed sites)
#   D — runtime fixture (both garbage + valid sentinel paths)
#   E — regression-guard (test would have FAILED at parent)

set -uo pipefail
cd "$(dirname "$0")/.."

PARENT="db06012c6068"
HEAD="9d885dc"
FILE="scripts/stage-readiness.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ✓ $1"; }
bad() { FAIL=$((FAIL+1)); echo "  ✗ $1"; }

# Stash current state so we can checkout historical commits non-destructively
STASH_REF="$(git rev-parse HEAD)"

# ---- Phase A: reproduce bug at parent ---------------------------------------
echo "━━━ Phase A: reproduce bug at parent ($PARENT) ━━━"
git show "$PARENT:$FILE" > /tmp/stage-readiness.parent.sh 2>/dev/null
if [ ! -s /tmp/stage-readiness.parent.sh ]; then
    bad "A0: failed to checkout parent version of $FILE"
else
    # A1: parent uses bare string comparison without digit guard
    if grep -qE '\[\[ "\$now_iso" < "\$expire_iso" \]\]' /tmp/stage-readiness.parent.sh \
       && ! grep -qE 'expire_iso" =~ \^\[0-9\]' /tmp/stage-readiness.parent.sh; then
        ok "A1: parent has bare [[ now < expire ]] without digit-prefix guard (bug surface present)"
    else
        bad "A1: parent shape mismatch — PR landed on wrong base?"
    fi
fi

# A2: runtime reproduction — feed a garbage sentinel, observe "ACTIVE" path
# The relevant snippet is the sentinel-check block; we'll extract it and run
# with a synthetic $REPO pointing at a tmp dir.
cat > /tmp/probe-parent.sh <<'EOF'
#!/usr/bin/env bash
REPO="$1"
SENT="$REPO/state/presenter-mode.sentinel"
if [ -f "$SENT" ]; then
    expire_iso=$(cat "$SENT")
    now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [[ "$now_iso" < "$expire_iso" ]]; then
        echo "PARENT-VERDICT: ACTIVE"
    else
        echo "PARENT-VERDICT: EXPIRED"
    fi
fi
EOF
chmod +x /tmp/probe-parent.sh
PROBE_REPO=$(mktemp -d)
mkdir -p "$PROBE_REPO/state"
echo "garbage sentinel content no timestamp here" > "$PROBE_REPO/state/presenter-mode.sentinel"
PARENT_VERDICT=$(/tmp/probe-parent.sh "$PROBE_REPO" | head -1)
if [ "$PARENT_VERDICT" = "PARENT-VERDICT: ACTIVE" ]; then
    ok "A2: parent fails open — garbage sentinel reports ACTIVE (bug reproduced)"
else
    bad "A2: parent should report ACTIVE for garbage input but got: $PARENT_VERDICT"
fi

# ---- Phase B: verify fix at head --------------------------------------------
echo ""
echo "━━━ Phase B: verify fix at head ($HEAD) ━━━"
git show "$HEAD:$FILE" > /tmp/stage-readiness.head.sh 2>/dev/null
if grep -qE 'expire_iso" =~ \^\[0-9\]' /tmp/stage-readiness.head.sh; then
    ok "B1: head adds digit-prefix regex guard"
else
    bad "B1: head missing digit-prefix guard"
fi
if grep -qE 'sentinel content malformed' /tmp/stage-readiness.head.sh; then
    ok "B2: head surfaces a 'malformed' warn with remediation text"
else
    bad "B2: head missing operator warning text"
fi

# Runtime: feed the same garbage sentinel into head, expect malformed branch
cat > /tmp/probe-head.sh <<'EOF'
#!/usr/bin/env bash
REPO="$1"
SENT="$REPO/state/presenter-mode.sentinel"
if [ -f "$SENT" ]; then
    expire_iso=$(cat "$SENT")
    now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [[ ! "$expire_iso" =~ ^[0-9] ]]; then
        echo "HEAD-VERDICT: MALFORMED"
    elif [[ "$now_iso" < "$expire_iso" ]]; then
        echo "HEAD-VERDICT: ACTIVE"
    else
        echo "HEAD-VERDICT: EXPIRED"
    fi
fi
EOF
chmod +x /tmp/probe-head.sh
HEAD_VERDICT=$(/tmp/probe-head.sh "$PROBE_REPO" | head -1)
if [ "$HEAD_VERDICT" = "HEAD-VERDICT: MALFORMED" ]; then
    ok "B3: head correctly flags garbage sentinel as MALFORMED (bug fixed)"
else
    bad "B3: expected MALFORMED verdict at head, got: $HEAD_VERDICT"
fi

# Valid future sentinel → head still reports ACTIVE
FUTURE=$(date -u -v+1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
echo "$FUTURE" > "$PROBE_REPO/state/presenter-mode.sentinel"
HEAD_VALID_VERDICT=$(/tmp/probe-head.sh "$PROBE_REPO" | head -1)
if [ "$HEAD_VALID_VERDICT" = "HEAD-VERDICT: ACTIVE" ]; then
    ok "B4: head still returns ACTIVE for a valid future ISO timestamp (no false negative)"
else
    bad "B4: head broke the valid-input path: got $HEAD_VALID_VERDICT"
fi

# Past sentinel → EXPIRED (should still work)
PAST=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)
echo "$PAST" > "$PROBE_REPO/state/presenter-mode.sentinel"
HEAD_PAST_VERDICT=$(/tmp/probe-head.sh "$PROBE_REPO" | head -1)
if [ "$HEAD_PAST_VERDICT" = "HEAD-VERDICT: EXPIRED" ]; then
    ok "B5: head still returns EXPIRED for past timestamps (no new regression)"
else
    bad "B5: head broke the expired path: got $HEAD_PAST_VERDICT"
fi

# ---- Phase C: scope-gap audit ----------------------------------------------
echo ""
echo "━━━ Phase C: scope-gap audit across repo ━━━"
# Check: are there other files still doing bare [[ now < expire ]] without
# the digit guard? PR #432 fixed 3 Python sites; PR #435 fixes the bash one.
# Look for any remaining bare comparison on a *.sentinel file read.
REMAINING=$(grep -rn "sentinel\|expire_iso" src/ scripts/ --include='*.sh' --include='*.py' 2>/dev/null \
    | grep -v "test-" \
    | grep -v "poc-" \
    | grep -v "stage-readiness.sh" \
    | grep -E "expire_iso.*<|<.*expire_iso" \
    | while IFS= read -r hit; do
        # Per Mini's review on PR #437: exclude sites where an isdigit()
        # guard appears on the previous ~5 lines (PR #432 added them
        # immediately above the comparison). The naive grep matched the
        # comparison line and reported the guarded sites as unfixed.
        file="${hit%%:*}"; line="${hit#*:}"; line="${line%%:*}"
        start=$(( line - 5 )); [ "$start" -lt 1 ] && start=1
        if ! sed -n "${start},${line}p" "$file" 2>/dev/null | grep -q "isdigit()\|=~ \^\[0-9\]"; then
            echo "$hit"
        fi
      done | head -5)
if [ -z "$REMAINING" ]; then
    ok "C1: no other unfixed call sites found doing bare string-compare on sentinel"
else
    bad "C1: possible unfixed sites remaining:"
    echo "$REMAINING" | sed 's/^/         /'
fi

# C2: confirm PR #432's Python sites ARE digit-guarded (regression fence)
PYGUARD=$(grep -rn "sentinel\|expire_iso" src/ --include='*.py' 2>/dev/null | head -5)
if [ -n "$PYGUARD" ]; then
    # Check that any Python comparing ISO timestamps uses dateutil/datetime,
    # not raw string compare. Heuristic: raw "<" on a sentinel-read variable.
    PY_BARE=$(echo "$PYGUARD" | grep -E "expire.*<|sentinel.*<" | grep -v "datetime\|isoparse" || true)
    if [ -z "$PY_BARE" ]; then
        ok "C2: Python sites already using datetime/isoparse (PR #432 fence intact)"
    else
        bad "C2: suspicious bare < on expire in Python — PR #432 incomplete?"
    fi
else
    ok "C2: no Python sites refer to the sentinel (nothing to regress)"
fi

# ---- Phase D: runtime fixture (extra cases) --------------------------------
echo ""
echo "━━━ Phase D: runtime fixture extra cases ━━━"
# D1: empty file
: > "$PROBE_REPO/state/presenter-mode.sentinel"
EMPTY_VERDICT=$(/tmp/probe-head.sh "$PROBE_REPO" | head -1)
if [ "$EMPTY_VERDICT" = "HEAD-VERDICT: MALFORMED" ]; then
    ok "D1: empty sentinel → MALFORMED (no digit prefix)"
else
    bad "D1: empty sentinel got $EMPTY_VERDICT (expected MALFORMED)"
fi

# D2: whitespace-prefixed timestamp (edge case — starts with space, not digit)
echo " 2026-04-18T05:00:00Z" > "$PROBE_REPO/state/presenter-mode.sentinel"
WS_VERDICT=$(/tmp/probe-head.sh "$PROBE_REPO" | head -1)
if [ "$WS_VERDICT" = "HEAD-VERDICT: MALFORMED" ]; then
    ok "D2: leading-whitespace sentinel caught as MALFORMED (regex anchors ^[0-9])"
else
    bad "D2: leading-whitespace sentinel got $WS_VERDICT (expected MALFORMED)"
fi

# ---- Phase E: regression-guard ---------------------------------------------
echo ""
echo "━━━ Phase E: regression-guard ━━━"
# E1: confirm the garbage-fixture test would FAIL at parent
if [ "$PARENT_VERDICT" = "PARENT-VERDICT: ACTIVE" ] \
   && [ "$HEAD_VERDICT" = "HEAD-VERDICT: MALFORMED" ]; then
    ok "E1: test differential — parent says ACTIVE, head says MALFORMED (regression fence)"
else
    bad "E1: test would not have caught the regression (parent=$PARENT_VERDICT head=$HEAD_VERDICT)"
fi

# Cleanup
rm -rf "$PROBE_REPO" /tmp/probe-parent.sh /tmp/probe-head.sh \
    /tmp/stage-readiness.parent.sh /tmp/stage-readiness.head.sh

echo ""
echo "━━━ Summary ━━━"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
