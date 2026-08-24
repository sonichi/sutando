#!/bin/bash
# A clean child exit is a deliberate stand-down, not a crash.
# single_instance.py exits 0 when a peer holds the lock; respawning that is the loop.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

run_case() {  # $1=exit code the stub bridge returns -> prints restart count
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts"
  printf '#!/bin/bash\necho "%s/none.env"\n' "$d" > "$d/scripts/sutando-config.sh"
  chmod +x "$d/scripts/sutando-config.sh"
  echo "import sys; sys.exit($1)" > "$d/src/discord-bridge.py"
  cp "$REPO/src/launchd/channel-bridge-wrapper.sh" "$d/src/launchd/"
  ( cd "$d"; DISCORD_BOT_TOKEN=t SUTANDO_CHANNEL_BRIDGE_PYTHON=/usr/bin/python3 \
      SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/channel-bridge-wrapper.sh discord > o.log 2>&1 & p=$!
    sleep 5; kill -TERM $p 2>/dev/null; wait $p 2>/dev/null )
  grep -c "automatically restarting" "$d/o.log"; rm -rf "$d"
}

clean=$(run_case 0)
crash=$(run_case 1)
echo "exit-0 child -> $clean restart(s) (want 0)"
echo "exit-1 child -> $crash restart(s) (want >0, proves this test can fail)"
[ "$clean" -eq 0 ] || { echo "FAIL: clean exit was respawned"; fail=1; }
[ "$crash" -gt 0 ] || { echo "FAIL: crash was NOT respawned - guard is too broad"; fail=1; }
[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
