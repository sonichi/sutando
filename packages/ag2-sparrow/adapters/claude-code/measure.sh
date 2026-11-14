#!/bin/bash
# measure.sh <taskfile> <resultfile> [timeout_s] — waits for the result, prints mtime delta in ms
T=$1; R=$2; TO=${3:-180}
for ((i=0;i<TO*10;i++)); do [ -s "$R" ] && break; sleep 0.1; done
[ -s "$R" ] || { echo "TIMEOUT: no $R after ${TO}s"; exit 1; }
python3 - "$T" "$R" <<'PY'
import sys,os
t,r=sys.argv[1:3]
d=(os.stat(r).st_mtime_ns-os.stat(t).st_mtime_ns)/1e6
print(f"latency_ms={d:.0f} task={os.path.basename(t)} result={os.path.basename(r)}")
print("result body:", open(r).read().strip()[:200])
PY
