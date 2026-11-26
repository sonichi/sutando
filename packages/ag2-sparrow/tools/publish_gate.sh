#!/bin/bash
# The test gate a release must pass: every package suite plus the src-drift guard.
# The tag publish and the PR job both run this file, on the same interpreter.
# usage: publish_gate.sh [<shards> <shard>] — every suite at HEAD (commit first), heaviest first, one per core,
#        each in its own worktree; with <shards> <shard>, only that cost-balanced leg of them.
#        publish_gate.sh --list — prints every suite the gate runs, and runs nothing.
set -euo pipefail
cd "$(dirname "$0")/../../.."
DRIFT=packages/ag2-sparrow/tools/test_no_drift.py
if [ "${1:-}" = --list ]; then ls packages/ag2-sparrow/tests/test_*.py; echo "$DRIFT"; exit 0; fi
SHARDS="${1:-1}"; SHARD="${2:-1}"
WORKERS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
RECDIR="$(mktemp -d)"; trap 'rm -rf "$RECDIR"' EXIT
ls packages/ag2-sparrow/tests/test_*.py \
  | bash scripts/shard-by-cost.sh "$SHARDS" "$SHARD" tests/sparrow-suite-costs.txt > "$RECDIR/files"
bash scripts/parallel-suite-lane.sh "$WORKERS" "$RECDIR/files" "$RECDIR" python3
failed=()
idx=0
while IFS= read -r t; do
  idx=$((idx + 1))
  rc="$(cat "$RECDIR/$idx.rc" 2>/dev/null || echo 1)"
  echo "→ $t ($(cat "$RECDIR/$idx.time" 2>/dev/null || echo '?')s)"
  if [ "$rc" -ne 0 ]; then
    echo "::error::FAILED: $t (exit $rc)"
    cat "$RECDIR/$idx.out" 2>/dev/null || true
    failed+=("$t")
  fi
done < "$RECDIR/files"
python3 "$DRIFT"
if [ "${#failed[@]}" -ne 0 ]; then
  printf 'publish gate: %s suite(s) failed:\n' "${#failed[@]}"
  printf '  %s\n' "${failed[@]}"
  exit 1
fi
