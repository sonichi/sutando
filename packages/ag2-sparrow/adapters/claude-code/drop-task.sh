#!/bin/bash
# drop-task.sh <inbox> <keyfile> <id-suffix> "<message>" — a Sparrow-shaped, HMAC-stamped,
# task-last file published by temp + fsync + rename, for exercising the mod by hand or in tests.
set -eu
INBOX=$1; KEY=$2; SUF=$3; MSG=$4
python3 - "$INBOX" "$KEY" "task-$SUF" "$MSG" <<'PY'
import datetime, hashlib, hmac, os, sys, uuid
inbox, key, tid, msg = sys.argv[1:5]
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
lines = [f"id: {tid}", f"timestamp: {ts}", "source: ag2space", "channel_id: !labroom:ag2.space",
         "interaction_type: message", "user_id: @tester:ag2.space", "sender_name: tester",
         f"source_message_id: $ev{uuid.uuid4().hex[:12]}", "access_tier: owner", f"task: {msg}"]
body = "\n".join(lines) + "\n"
# Same stamp as src/task_envelope.py: HMAC-SHA256 over the unstamped text, inserted after id:.
mac = hmac.new(bytes.fromhex(open(key).read().strip()), body.encode(), hashlib.sha256).hexdigest()
stamped = body.split("\n"); stamped.insert(1, "envelope_hmac: v1:" + mac)
dest = os.path.join(inbox, tid + ".txt")
tmp = os.path.join(inbox, f".{tid}.txt.{os.getpid()}.{uuid.uuid4().hex}.tmp")
with open(tmp, "w") as f:
    f.write("\n".join(stamped)); f.flush(); os.fsync(f.fileno())
os.replace(tmp, dest)
print(dest)
PY
