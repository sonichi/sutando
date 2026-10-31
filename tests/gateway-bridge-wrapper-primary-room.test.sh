#!/usr/bin/env bash
# gateway-bridge-wrapper.sh launches the legacy unsuffixed bridge WITHOUT REMOTE_PROACTIVE_ROOM
# once a named connection row holds the primary (connections.py primary prints its slug), so one
# bridge posts nudges; with the legacy row primary, or no record, the room reaches the bridge.
# The room may come from the plist environment or from the channel .env the wrapper sources.
# Run: bash tests/gateway-bridge-wrapper-primary-room.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway wrapper primary-room test")" || exit 1

# $1 = what `connections.py primary` answers: "none" (exit 2), "" (legacy row primary) or a slug
# $2 = where the room comes from: "env", "dotenv", or "device" (only in the desktop's device env,
#      which the wrapper never sources: the child must see the variable UNSET, not empty, so the
#      bridge still reads it from there)
# Prints the REMOTE_PROACTIVE_ROOM the stub bridge saw, or "<missing>".
run_case() {
  local primary="$1" via="$2" d room_env=""
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  case "$via" in
    dotenv) printf 'REMOTE_PROACTIVE_ROOM=!owner:prod.example\n' > "$d/channel.env";;
    env) room_env="!owner:prod.example";;
    device) printf 'REMOTE_PROACTIVE_ROOM=!owner:prod.example\n' > "$d/device.env";;
  esac
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$d/channel.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  cat > "$d/scripts/connections.py" <<PYS
import sys
if sys.argv[1:] == ["primary"]:
    if "$primary" == "none":
        sys.exit(2)
    print("$primary"); sys.exit(0)
sys.exit(1)
PYS
  # The stub bridge records its room and stands down (rc 75), which ends the wrapper cleanly.
  cat > "$d/src/remote-gateway-bridge.py" <<PYS
import os, sys
open("$d/seen.txt", "w").write(os.environ.get("REMOTE_PROACTIVE_ROOM", "<missing>"))
sys.exit(75)
PYS
  echo "import time
while True: time.sleep(0.2)" > "$d/src/sparrowd.py"
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"; cp "$REPO/src/result_publish.py" "$d/src/"
  printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  chmod +x "$d/shims/python3" "$d/shims/osascript"
  ( cd "$d" || exit 1; env -u AG2_REMOTE_TOKEN -u REMOTE_PROACTIVE_ROOM PATH="$d/shims:$PATH" REMOTE_TASK_TOKEN=t \
      ${room_env:+REMOTE_PROACTIVE_ROOM="$room_env"} SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 & p=$!
    for _ in $(seq 1 50); do [ -f seen.txt ] && break; sleep 0.2; done
    kill -TERM $p 2>/dev/null; wait $p 2>/dev/null )
  cat "$d/seen.txt" 2>/dev/null || echo "<missing>"
  rm -rf "$d"
}

check() {  # $1 label, $2 got, $3 want
  if [ "$2" = "$3" ]; then echo "  ok   $1 -> '$2'"; else echo "  FAIL $1 -> '$2' (want '$3')"; fail=1; fi
}
check "no record, room from env: legacy bridge keeps the room"            "$(run_case none env)"    "!owner:prod.example"
check "legacy row primary, room from env: keeps the room"                 "$(run_case "" env)"      "!owner:prod.example"
check "named row primary, room from env: launched with the room empty"    "$(run_case s-space env)" ""
check "named row primary, room from the channel .env: also empty"         "$(run_case s-space dotenv)" ""
check "no record, room from the channel .env: keeps the room"             "$(run_case none dotenv)" "!owner:prod.example"
check "no record, room only in the device env: left UNSET for the bridge" "$(run_case none device)" "<missing>"
check "legacy row primary, room only in the device env: left UNSET"       "$(run_case "" device)"   "<missing>"
check "named row primary, room only in the device env: set empty"         "$(run_case s-space device)" ""
exit $fail
