#!/usr/bin/env bash
# Tool caches under tests/ stay ignored while relocated tests stay visible; an
# over-broad fix breaks the second half, so both directions are asserted.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE="$(mktemp -d -t gitignore-tool-cache-test.XXXXXX)"
trap 'rm -rf "$FIXTURE"' EXIT

# The fixture is a fresh repo; nothing here can outlive the trap or collide with
# the checkout, so `rm -rf` is safe in a way it is not on a shared tests/ dir.
git init -q "$FIXTURE"
cp "$REPO/.gitignore" "$FIXTURE/.gitignore"
cd "$FIXTURE"

pass=0
fail=0

check_ignored() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "OK: $desc"; pass=$((pass + 1))
    else
        echo "FAIL: $desc — '$path' is NOT ignored, so \`add -A\` would stage it"
        fail=$((fail + 1))
    fi
}
refute_ignored() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "FAIL: $desc — '$path' IS ignored; the fix over-denied and hides real tests"
        fail=$((fail + 1))
    else
        echo "OK: $desc"; pass=$((pass + 1))
    fi
}

# tool caches, top level under tests/ — the level the re-include exposed
for d in __pycache__ .pytest_cache .mypy_cache .ruff_cache node_modules htmlcov; do
    mkdir -p "tests/$d"
    printf 'x' > "tests/$d/_probe"
    check_ignored "tests/$d/_probe" "tests/$d/ is ignored"
done

# one level deeper: already covered by the global rule, which is why the hole
# looked narrower than it was
mkdir -p tests/kernel/__pycache__
printf 'x' > tests/kernel/__pycache__/_probe.pyc
check_ignored "tests/kernel/__pycache__/_probe.pyc" \
    "tests/kernel/__pycache__/ is ignored (nested, not just top level)"

# the positive direction the re-include exists for
printf 'x' > tests/kernel/probe.test.py
refute_ignored "tests/kernel/probe.test.py" \
    "a relocated test in tests/kernel/ is still trackable"
mkdir -p tests/adapters
printf 'x' > tests/adapters/probe.test.sh
refute_ignored "tests/adapters/probe.test.sh" \
    "a relocated test in tests/adapters/ is still trackable"

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
