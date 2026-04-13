#!/bin/bash
# BUG-318: Verify voice metrics disconnect bug (issue #319)
# Requires: voice agent running on port 9900
# Usage: bash scripts/verify-voice-metrics-bug.sh

METRICS=data/voice-metrics.jsonl
PORT=9900

# Count lines before
BEFORE=$(wc -l < "$METRICS" 2>/dev/null || echo 0)
echo "Metrics lines before: $BEFORE"

# Connect a WS client, wait 2s, then disconnect
echo "Connecting to voice agent on port $PORT..."
node -e "
const ws = new (require('ws'))('ws://localhost:$PORT');
ws.on('open', () => {
  console.log('Connected — waiting 2s then disconnecting...');
  setTimeout(() => { ws.close(); console.log('Disconnected.'); }, 2000);
});
ws.on('error', (e) => { console.error('WS error:', e.message); process.exit(1); });
ws.on('close', () => setTimeout(() => process.exit(0), 1000));
"

# Wait for any async flush
sleep 2

# Count lines after
AFTER=$(wc -l < "$METRICS" 2>/dev/null || echo 0)
echo "Metrics lines after:  $AFTER"

if [ "$AFTER" -gt "$BEFORE" ]; then
  echo "PASS — metrics were written on disconnect"
else
  echo "BUG CONFIRMED — no metrics written on client disconnect"
fi
