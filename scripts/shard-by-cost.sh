#!/usr/bin/env bash
# usage: shard-by-cost.sh <shards> <shard> <cost-table> < files — prints that leg, heaviest first:
# each file goes onto the lightest leg so far; every file lands in exactly one leg; unlisted costs 1.
set -euo pipefail
SHARDS="$1"; SHARD="$2"; TABLE="$3"
[ "$SHARDS" -ge 1 ] && [ "$SHARD" -ge 1 ] && [ "$SHARD" -le "$SHARDS" ] || { echo "usage: $0 <shards> <shard> <cost-table>" >&2; exit 2; }
awk -v shards="$SHARDS" -v want="$SHARD" '
  NR == FNR { if ($0 !~ /^#/ && NF >= 2) cost[$2] = $1; next }
  # Floor at 1: a 0 never moves the lightest-leg pointer, so every sub-second
  # suite would land on the same leg.
  { n++; file[n] = $0; c[n] = ($0 in cost) ? cost[$0] + 0 : 1; if (c[n] < 1) c[n] = 1 }
  END {
    # Ties break on the sorted input order, so the assignment is deterministic.
    for (i = 1; i <= n; i++) order[i] = i
    for (i = 2; i <= n; i++) { k = order[i]; j = i - 1
      while (j >= 1 && (c[order[j]] < c[k] || (c[order[j]] == c[k] && file[order[j]] > file[k]))) { order[j + 1] = order[j]; j-- }
      order[j + 1] = k }
    for (s = 1; s <= shards; s++) load[s] = 0
    for (i = 1; i <= n; i++) { k = order[i]; best = 1
      for (s = 2; s <= shards; s++) if (load[s] < load[best]) best = s
      load[best] += c[k]; leg[k] = best }
    for (i = 1; i <= n; i++) { k = order[i]; if (leg[k] == want) print file[k] }
  }' "$TABLE" -
