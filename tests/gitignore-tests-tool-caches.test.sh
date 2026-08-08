#!/usr/bin/env bash
# Regression: tool caches under tests/ must stay ignored, and real tests must not.
#
# `.gitignore` ignores `tests/*` and then re-includes `!tests/*/` so the tests
# tree can mirror src/ (tests/kernel/, tests/adapters/, …). That re-include is
# unqualified, so it also re-included every tool-generated directory — and it
# silently defeated the global `__pycache__/` rule declared earlier in the same
# file. Measured before the fix: all six directories below showed as `??`.
#
# It surfaced as a committed `tests/__pycache__/*.cpython-314.pyc` in a PR, which
# a reviewer had to catch by eye. The cause is not a stray `git add -A` — nothing
# was ignoring the path, so any contributor running the suite in-tree hits it.
#
# The positive half is load-bearing: a fix that over-denied (e.g. `tests/**/`)
# would re-hide relocated tests, which is exactly what the `!tests/*/` line was
# added to prevent. So this asserts both directions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

pass=0
fail=0
CREATED=()
cleanup() { for p in "${CREATED[@]:-}"; do rm -rf "$p"; done; }
trap cleanup EXIT

check_ignored() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "OK: $desc"; pass=$((pass + 1))
    else
        echo "FAIL: $desc — '$path' is NOT ignored, so \`add -A\` would stage it"
        fail=$((fail + 1))
    fi
}
check_tracked_ok() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "FAIL: $desc — '$path' IS ignored; the fix over-denied and hides real tests"
        fail=$((fail + 1))
    else
        echo "OK: $desc"; pass=$((pass + 1))
    fi
}

# --- negative: tool caches must be ignored, at top level and nested ----------
for d in __pycache__ .pytest_cache .mypy_cache .ruff_cache node_modules htmlcov; do
    mkdir -p "tests/$d"; CREATED+=("tests/$d")
    printf 'x' > "tests/$d/_probe"
    check_ignored "tests/$d/_probe" "tests/$d/ is ignored"
done

# nested one level deeper — the mirror-src layout the re-include exists for
mkdir -p "tests/kernel/__pycache__"; CREATED+=("tests/kernel")
printf 'x' > "tests/kernel/__pycache__/_probe.pyc"
check_ignored "tests/kernel/__pycache__/_probe.pyc" \
    "tests/kernel/__pycache__/ is ignored (nested, not just top level)"

# --- positive: real tests in a nested dir must STAY visible ------------------
printf 'x' > "tests/kernel/probe.test.py"
check_tracked_ok "tests/kernel/probe.test.py" \
    "a relocated test in tests/kernel/ is still trackable"
mkdir -p "tests/adapters"; CREATED+=("tests/adapters")
printf 'x' > "tests/adapters/probe.test.sh"
check_tracked_ok "tests/adapters/probe.test.sh" \
    "a relocated test in tests/adapters/ is still trackable"

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
