#!/usr/bin/env bash
# merge-voice-metrics.sh — generic jsonl union-merger keyed on
# (id-like-field, timestamp). Writes result atomically back to the local file.
#
# Historical note: named "voice-metrics" because that was the original
# use case, but the implementation is and always was generic. As of
# #603 (sqlite migration), voice-metrics.jsonl + call-metrics.jsonl are
# frozen archives — new session rollups live in data/conversation.sqlite.
# This script remains in use for subtitle-metrics.jsonl + any future
# per-line jsonl files cross-node-sync drops into the same pipeline.
#
# Works for any per-entry jsonl where each line has:
#   - a "timestamp" field (ISO 8601), AND
#   - one of: "callSid", "sessionId", "id", "uuid" (any id-like field).
#     Falls back to timestamp-only dedup if none are present.
#
# Invoked from cross-node-sync after rsync has staged the peer's copy.
# Safe to run standalone.
#
# Usage:
#   bash merge-voice-metrics.sh LOCAL PEER           # explicit file paths required
#
# Per owner's 2026-04-17 direction: "merge in ascending order of time".

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 LOCAL_JSONL PEER_JSONL" >&2
    exit 2
fi
LOCAL="$1"
PEER="$2"

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
