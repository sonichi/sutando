#!/usr/bin/env bash
# merge-voice-metrics.sh — merge two data/*.jsonl files by timestamp
# ascending, de-duplicating on a schema-aware identity key. Writes result
# atomically back to the local file.
#
# Works for any per-entry jsonl where each line has:
#   - a "timestamp" field (ISO 8601), AND
#   - one of: "callSid" (call-metrics), "sessionId" (voice-metrics),
#     "id" / "uuid" (generic). Falls back to timestamp-only dedup if
#     none are present.
#
# Covers today's data/ contents: voice-metrics.jsonl, call-metrics.jsonl,
# subtitle-metrics.jsonl — and any new .jsonl the cross-node-sync drops
# into the same pipeline.
#
# Invoked from cross-node-sync after rsync has staged the peer's copy.
# Safe to run standalone.
#
# Usage:
#   bash merge-voice-metrics.sh                      # default paths (voice-metrics)
#   bash merge-voice-metrics.sh LOCAL PEER           # explicit file paths
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
# Identity key: first non-null of callSid / sessionId / id / uuid, plus
# timestamp. Falls back to timestamp-only if no id-like field exists.
# Works for voice-metrics (sessionId), call-metrics (callSid),
# subtitle-metrics and generic jsonl.
ID_FIELDS = ("callSid", "sessionId", "id", "uuid")
entries = {}   # key=(id_val, timestamp) -> (timestamp, line)
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
                id_val = None
                for field in ID_FIELDS:
                    if d.get(field) is not None:
                        id_val = d[field]
                        break
                key = (id_val, d.get("timestamp"))
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
