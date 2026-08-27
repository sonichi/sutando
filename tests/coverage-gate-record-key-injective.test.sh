#!/usr/bin/env bash
# Workers must key records by sorted LINE INDEX: `tr "/." "__"` maps
# tests/a/b.test.py and tests/a_b.test.py alike, so a failing rc is overwritten.
set -uo pipefail
fail=0
here="$(cd "$(dirname "$0")/.." && pwd)"

# --- 1. The collision is real, and the index scheme is not vulnerable to it ---
k() { printf '%s' "$1" | tr '/.' '__'; }
if [ "$(k 'tests/a/b.test.py')" = "$(k 'tests/a_b.test.py')" ]; then
    echo "  OK: name-derived key collides on the two-path case (the defect)"
else
    echo "  FAIL: fixture no longer collides — the regression it guards is unreproducible"; fail=1
fi

# --- 2. Neither loop may reintroduce a name-derived key ---

# Strip comments first: both files describe the defect in prose, which a naive
# grep would flag as the defect itself.
for f in "$here/scripts/coverage-gate.sh" "$here/.github/workflows/ci.yml"; do
    code="$(sed 's/#.*$//' "$f")"
    if printf '%s' "$code" | grep -q 'tr "/\.\|tr .\/\.'; then
        echo "  FAIL: $(basename "$f") derives the record key from the path — not injective"; fail=1
    else
        echo "  OK: $(basename "$f") does not key records by path name (comments excluded)"
    fi
    printf '%s' "$code" | grep -q 'sed -n "${idx}p"' \
        || { echo "  FAIL: $(basename "$f") worker does not resolve its path by index"; fail=1; }
done

# --- 3. BEHAVIOURAL: colliding pair, one fails, one passes -> failure reported ---
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/tests/a"
printf 'import sys\nsys.exit(0)\n' > "$tmp/tests/a/b.test.py"       # passes
printf 'import sys\nsys.exit(1)\n' > "$tmp/tests/a_b.test.py"       # fails
RECDIR="$tmp/rec"; mkdir -p "$RECDIR"; export RECDIR
( cd "$tmp" && find tests -name '*.test.py' | sort > "$RECDIR/files" )
n="$(wc -l < "$RECDIR/files" | tr -d ' ')"

# Same worker shape as the loops under test: bare integers, since `xargs -I{}`
# rewrites a tab in the replaced string to a space.
( cd "$tmp" && seq 1 "$n" | xargs -P 4 -I{} bash -c '
    idx="$1"; f="$(sed -n "${idx}p" "$RECDIR/files")"
    rc=0; out="$(python3 "$f" 2>&1)" || rc=$?
    printf "%s" "$out" > "$RECDIR/$idx.out"
    printf "%s\n" "$rc" > "$RECDIR/$idx.rc"
' _ {} )

reported=0; i=0
while IFS= read -r f; do
    i=$((i + 1))
    rc="$(cat "$RECDIR/$i.rc" 2>/dev/null || echo 1)"
    [ "$rc" -ne 0 ] && { reported=$((reported + 1)); echo "     reported failure: $f (rc=$rc)"; }
done < "$RECDIR/files"

if [ "$reported" -eq 1 ]; then
    echo "  OK: exactly 1 of the colliding pair reported as failing"
else
    echo "  FAIL: expected exactly 1 reported failure, got $reported — a record was overwritten"; fail=1
fi

# Distinct records exist for both inputs — the property a name key cannot give.
if [ -f "$RECDIR/1.rc" ] && [ -f "$RECDIR/2.rc" ]; then
    echo "  OK: both colliding paths hold their own record"
else
    echo "  FAIL: the two paths did not get separate records"; fail=1
fi

[ "$fail" -eq 0 ] && echo "PASS: record keys are injective across both parallel loops"
exit "$fail"
