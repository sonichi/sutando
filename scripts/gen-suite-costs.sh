#!/usr/bin/env bash
# Rebuilds a suite-cost table from a CI job log: every "→ <path> (<N>s)" line the
# replay prints becomes "<N> <path>". Feed it the job's raw log (gh api .../logs).
# usage: gen-suite-costs.sh <job-log> [run-id] > tests/python-suite-costs.txt
set -euo pipefail
LOG="$1"; RUN="${2:-unknown}"
echo "# seconds  path — measured on ubuntu-latest, run $RUN ($(date -u +%Y-%m-%d)), from the per-suite"
echo "# seconds CI prints. Regenerate: scripts/gen-suite-costs.sh <job-log> > tests/python-suite-costs.txt"
echo "# A missing file counts as 1s; the table only steers balance, never which files run."
sed 's/^[^Z]*Z //' "$LOG" | grep -oE '^→ tests/[^ ]+\.test\.py \([0-9]+s\)' \
  | sed -E 's/^→ (.*) \(([0-9]+)s\)$/\2 \1/' | sort -k2 -u
