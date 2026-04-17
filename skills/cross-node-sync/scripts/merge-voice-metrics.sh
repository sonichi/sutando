#!/usr/bin/env bash
# merge-voice-metrics.sh — merge two voice-metrics.jsonl files by timestamp
# ascending, de-duplicating on (sessionId, timestamp) pairs. Writes result
# atomically back to the local file.
#
# Invoked from cross-node-sync after rsync has staged the peer's file at
# data/voice-metrics.peer.jsonl. Safe to run standalone.
#
# Usage:
#   bash merge-voice-metrics.sh                      # default paths
#   bash merge-voice-metrics.sh LOCAL PEER           # explicit
#
# Per owner's 2026-04-17 direction: "merge in ascending order of time".

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LOCAL="${1:-$REPO_ROOT/data/voice-metrics.jsonl}"
PEER="${2:-$REPO_ROOT/data/voice-metrics.peer.jsonl}"

[ -f "$LOCAL" ] || { mkdir -p "$(dirname "$LOCAL")"; : > "$LOCAL"; }
[ -f "$PEER" ]  || { echo "merge-voice-metrics: no peer file at $PEER (nothing to merge)"; exit 0; }

python3 - "$LOCAL" "$PEER" <<'PY'
import json, sys, os, tempfile
local_path, peer_path = sys.argv[1], sys.argv[2]
entries = {}   # key=(sessionId, timestamp) -> (timestamp, line)
for path in (local_path, peer_path):
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line: continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                key = (d.get("sessionId"), d.get("timestamp"))
                if key in entries: continue
                entries[key] = (d.get("timestamp", ""), line)
    except FileNotFoundError:
        continue
ordered = [line for _, line in sorted(entries.values(), key=lambda x: x[0])]
tmp_fd, tmp_path = tempfile.mkstemp(prefix=".voice-metrics.merge.", dir=os.path.dirname(local_path))
try:
    with os.fdopen(tmp_fd, "w") as f:
        f.write("\n".join(ordered))
        if ordered: f.write("\n")
    os.replace(tmp_path, local_path)
except Exception:
    os.unlink(tmp_path)
    raise
print(f"merge-voice-metrics: merged {len(ordered)} entries into {local_path}")
PY

# Clean up the peer-staging file after successful merge so subsequent
# syncs don't re-merge stale state.
rm -f "$PEER"
