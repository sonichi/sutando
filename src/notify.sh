#!/bin/bash
# Sutando: notify the user across available channels
# Usage: bash src/notify.sh "message"
#
# Channels (in order):
# 1. Voice (results/proactive-*.txt) — if voice client is connected
# 2. Discord DM — always
# 3. macOS notification — always (local only)

MSG="$1"
if [ -z "$MSG" ]; then echo "Usage: bash src/notify.sh 'message'"; exit 1; fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TS=$(date +%s%3N)

# 1. Voice — write proactive message if voice agent is up
if curl -s -o /dev/null -w "%{http_code}" http://localhost:9900 2>/dev/null | grep -q "426"; then
  _out="$REPO_DIR/results/proactive-$TS.txt"
  echo "$MSG" > "$_out.tmp-$$" && mv -f "$_out.tmp-$$" "$_out"
fi

# 2. Discord DM — via dm-result.py's send_dm, which owns token/owner
# resolution, chunking, and the send allowlist, and delivers through the
# shared DiscordRestClient (the one Discord POST chokepoint). Best-effort:
# a failed DM must not block the remaining channels.
SUTANDO_REPO_DIR="$REPO_DIR" python3 - "$MSG" <<'PY' || true
import importlib.util
import os
import sys

repo = os.environ["SUTANDO_REPO_DIR"]
sys.path.insert(0, os.path.join(repo, "src"))
spec = importlib.util.spec_from_file_location(
    "dm_result_notify", os.path.join(repo, "src", "dm-result.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.exit(0 if mod.send_dm(sys.argv[1]) else 1)
PY

# 3. macOS notification
osascript -e "display notification \"$MSG\" with title \"Sutando\"" 2>/dev/null
