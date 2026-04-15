#!/usr/bin/env bash
# Concurrency stress test for src/event_log.py — verifies atomicity under
# concurrent writes with varied payload sizes (including > PIPE_BUF).
#
# Usage:
#   bash scripts/test-event-log.sh                 # default: 50 writers × 50 events
#   bash scripts/test-event-log.sh 100 100         # 100 writers × 100 events
#
# Exits 0 on pass, 1 on fail. Prints a summary to stdout.
# Leaves a fresh logs/events-YYYY-MM-DD.jsonl behind — safe to re-run.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
N_WRITERS="${1:-50}"
M_EVENTS="${2:-50}"

python3 - <<PY
import subprocess, sys, time, json
from pathlib import Path
from datetime import date

REPO = Path("$REPO")
N = $N_WRITERS
M = $M_EVENTS
LOG = REPO / "logs" / f"events-{date.today()}.jsonl"

if LOG.exists():
    LOG.unlink()

CODE = f"""
import sys
sys.path.insert(0, '{REPO}/src')
from event_log import log_event
wid = int(sys.argv[1])
sizes = [100, 500, 1000, 3000, 4500]
for i in range({M}):
    sz = sizes[i % len(sizes)]
    log_event('stress.sized', writer=wid, seq=i, size=sz, payload='x'*sz)
"""

print(f"spawning {N} writers × {M} events (payloads 100-4500 bytes)...")
start = time.time()
procs = [subprocess.Popen([sys.executable, "-c", CODE, str(w)]) for w in range(N)]
for p in procs:
    p.wait()
elapsed = time.time() - start

lines = LOG.read_text().splitlines() if LOG.exists() else []
expected = N * M
valid = 0
invalid = 0
by_writer = {}
for line in lines:
    try:
        ev = json.loads(line)
        valid += 1
        w = ev.get("writer")
        if w is not None:
            by_writer[w] = by_writer.get(w, 0) + 1
    except json.JSONDecodeError:
        invalid += 1

print(f"elapsed: {elapsed:.2f}s")
print(f"expected: {expected}  actual: {len(lines)}  valid: {valid}  invalid: {invalid}")
print(f"writers: {len(by_writer)}/{N}")
if by_writer:
    print(f"events per writer: min={min(by_writer.values())} max={max(by_writer.values())} expected={M}")

ok = (valid == expected and invalid == 0 and len(by_writer) == N and all(v == M for v in by_writer.values()))
if ok:
    print("PASS — event_log atomicity holds under concurrency")
    sys.exit(0)
else:
    print("FAIL — atomicity broken; see output above")
    sys.exit(1)
PY
