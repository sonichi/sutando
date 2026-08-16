#!/bin/bash
# The "Not measured" report must RENDER, not abort the gate.
#
# Its bullet format begins with "-", which bash's printf builtin parses as an
# option unless "--" ends option parsing. The formats are read out of the
# shipping script and executed, so this fails on the real source rather than on
# a copy of it.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE="$REPO/scripts/coverage-gate.sh"
fails=0

check() {  # name, expected, actual
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else
    echo "  FAIL $1: expected '$2', got '$3'"; fails=$((fails + 1)); fi
}

[ -f "$GATE" ] || { echo "FAIL: $GATE missing"; exit 1; }

# Every printf in the gate, executed exactly as written, with one argument.
n=0
while IFS= read -r line; do
  n=$((n + 1))
  err=$(eval "${line} probe" 2>&1 >/dev/null)
  check "printf #$n renders without a bash option error" "" "$err"
done < <(grep -oE "printf (--[[:space:]])?'[^']*'" "$GATE" | grep -v '%s'"'"' |' )

check "at least one printf was exercised" "yes" "$([ "$n" -gt 0 ] && echo yes || echo no)"

# The bullet-list format specifically: it is the one that starts with a dash.
bullet=$(grep -oE "printf (--[[:space:]])?'-[^']*'" "$GATE" | head -1)
check "the unmeasured bullet format exists" "yes" "$([ -n "$bullet" ] && echo yes || echo no)"
if [ -n "$bullet" ]; then
  out=$(eval "$bullet alpha beta" 2>/dev/null)
  check "it renders both arguments as bullets" "2" "$(printf '%s\n' "$out" | grep -c '^-')"
fi

if [ "$fails" -eq 0 ]; then echo "coverage-gate report: all checks passed"; else
  echo "FAILED ($fails)"; exit 1; fi
