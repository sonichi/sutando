#!/bin/bash
# The test gate a release must pass: every package suite plus the src-drift guard.
# The tag publish and the PR job both run this file, on the same interpreter.
# usage: publish_gate.sh [<shards> <shard>] — no arguments runs it whole; leg 1 also runs the guard.
set -euo pipefail
cd "$(dirname "$0")/../../.."
SHARDS="${1:-1}"; SHARD="${2:-1}"
LEG="$(mktemp)"; trap 'rm -f "$LEG"' EXIT
ls packages/ag2-sparrow/tests/test_*.py \
  | bash scripts/shard-by-cost.sh "$SHARDS" "$SHARD" tests/sparrow-suite-costs.txt > "$LEG"
while IFS= read -r t; do
  s=$SECONDS; python3 "$t" < /dev/null; echo "→ $t ($((SECONDS - s))s)"
done < "$LEG"
if [ "$SHARD" = 1 ]; then python3 packages/ag2-sparrow/tools/test_no_drift.py; fi
