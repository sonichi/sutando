#!/bin/bash
# The ONE owner of "which Python test files does CI run".
#
# Both runners (ci.yml, coverage-gate.sh) call this instead of writing their own
# `find`, and the guard that asserts coverage calls it too rather than parsing
# shell text out of them. Four review rounds of parser edge cases -- unioned
# roots, comments, reset-vs-append order, assignments after the consumer -- were
# all artifacts of reading a declaration instead of running the thing.
set -euo pipefail

# `skills` is optional: a skill owns its own tests/, so a suite that moves into
# one stays discovered; a checkout without skills/ still works.
roots=(tests)
[ -d skills ] && roots+=(skills)

out=$(find "${roots[@]}" -name '*.test.py' -not -path '*/node_modules/*' | sort)

# Zero discovered tests is a broken checkout or a broken root list, never a
# legitimate "nothing to run" -- a consumer handed an empty list exits green.
if [ -z "$out" ]; then
  echo "discover-python-tests: found NO test files under ${roots[*]} -- refusing to" >&2
  echo "  hand a consumer an empty list; a runner given zero tests exits green." >&2
  exit 3
fi

printf '%s\n' "$out"
