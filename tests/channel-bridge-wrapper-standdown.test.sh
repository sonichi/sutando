#!/bin/bash
# A stand-down is a DECLARED exit code, never an inferred one.
# single_instance.py exits 75 (EXIT_STANDDOWN) when a peer holds the lock;
# respawning that is the restart loop. Exit 0 is NOT a stand-down: a bridge
# whose main loop merely returns also exits 0, and standing down on that would
# leave it silently off with the owner alert suppressed.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
# resolve_python, not a bare `python3` — REVIEW.md:92-96 notes the scanner
# cannot see the bare-name form, which is the CLT stub on a clean macOS host.
# shellcheck source=../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the wrapper stand-down test")" || exit 1

run_case() {  # $1=exit code the stub bridge returns -> prints restart count
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts"
  printf '#!/bin/bash\necho "%s/none.env"\n' "$d" > "$d/scripts/sutando-config.sh"
  chmod +x "$d/scripts/sutando-config.sh"
  echo "import sys; sys.exit($1)" > "$d/src/discord-bridge.py"
  cp "$REPO/src/launchd/channel-bridge-wrapper.sh" "$d/src/launchd/"
  # The wrapper alerts via `osascript display notification`; unshimmed, running
  # this suite fires real desktop alerts at the owner.
  mkdir -p "$d/shims"; printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  chmod +x "$d/shims/osascript"
  ( cd "$d"; PATH="$d/shims:$PATH" DISCORD_BOT_TOKEN=t SUTANDO_CHANNEL_BRIDGE_PYTHON="$PY" \
      SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/channel-bridge-wrapper.sh discord > o.log 2>&1 & p=$!
    sleep 5; kill -TERM $p 2>/dev/null; wait $p 2>/dev/null )
  grep -c "automatically restarting" "$d/o.log"; rm -rf "$d"
}

standdown=$(run_case 75)
crash=$(run_case 1)
plain_zero=$(run_case 0)
echo "exit-75 child -> $standdown restart(s) (want 0)"
echo "exit-1  child -> $crash restart(s) (want >0, proves this test can fail)"
echo "exit-0  child -> $plain_zero restart(s) (want >0: a returned main loop is NOT a stand-down)"
[ "$standdown" -eq 0 ] || { echo "FAIL: declared stand-down was respawned"; fail=1; }
[ "$crash" -gt 0 ] || { echo "FAIL: crash was NOT respawned - guard is too broad"; fail=1; }
[ "$plain_zero" -gt 0 ] || { echo "FAIL: bare exit 0 stood down - bridge would sit down SILENTLY"; fail=1; }
[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
