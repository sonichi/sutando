#!/bin/bash
# Full logs per run — never tail a failing harness (truncated-log lesson).
for i in 1 2 3; do
  s=$(date +%s)
  CLAIM_MACHINE_EXAMPLES=800 CLAIM_MACHINE_STEPS=60 "$1/hypovenv/bin/python" \
    claim_machine_b.py > "run$i-full.log" 2>&1
  rc=$?
  echo "run $i: rc=$rc $(( $(date +%s) - s ))s — $(tail -1 run$i-full.log)"
done
