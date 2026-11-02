#!/usr/bin/env bash
# usage: gen-suite-costs.sh [-r <run-ids>] <job-log>... > tests/<python|shell>-suite-costs.txt
# Each "→ <path> (<N>s)" line is a sample; a file's cost is the median of its samples.
set -euo pipefail
RUNS="unknown"
if [ "${1:-}" = "-r" ]; then RUNS="$2"; shift 2; fi
[ "$#" -ge 1 ] || { echo "usage: $0 [-r <run-ids>] <job-log>..." >&2; exit 2; }
echo "# seconds  path — measured on ubuntu-latest: per-file median of the per-suite seconds CI"
echo "# printed in $# leg log(s) of run(s) $RUNS ($(date -u +%Y-%m-%d))."
echo "# Regenerate: scripts/gen-suite-costs.sh -r '<run-ids>' <leg-logs>... > tests/<python|shell>-suite-costs.txt"
echo "# A missing file counts as 1s; the table only steers balance, never which files run."
sed 's/^[^Z]*Z //' "$@" | grep -oE '^→ (tests|skills)/[^ ]+\.test\.(py|sh) \([0-9]+s\)' \
  | sed -E 's/^→ (.*) \(([0-9]+)s\)$/\1 \2/' | LC_ALL=C sort -k1,1 -k2,2n \
  | awk '
    $1 != f { flush(); f = $1; n = 0 }
    { v[++n] = $2 }
    END { flush() }
    # Samples arrive sorted per file; an even count takes the lower middle.
    function flush() { if (n) print v[int((n + 1) / 2)], f }'
