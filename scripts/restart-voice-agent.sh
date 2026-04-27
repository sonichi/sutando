#!/usr/bin/env bash
# restart-voice-agent.sh — kickstart com.sutando.voice-agent and verify the
# restart actually happened. Exists because `launchctl kickstart -k` is known
# to silently no-op (observed 2026-04-25, 9.5h of stale code mid-talk-prep).
#
# Verification:
#   1. Capture LISTEN PID on :9900 BEFORE kickstart (filters out connected
#      WebSocket clients that would otherwise pick up a non-listener PID).
#   2. `launchctl kickstart -k gui/<uid>/com.sutando.voice-agent`.
#   3. Poll up to MAX_DEADLINE_SECONDS for a fresh LISTEN PID. Don't fix-sleep
#      because cold tsx boots take variable time.
#   4. Accept the restart only when (etime <= MAX_ETIME_SECONDS), regardless
#      of whether NEW_PID matches OLD_PID — rare PID reuse + a fresh process
#      is still a successful restart, not a silent no-op.
#   5. Reject when (no listener appeared by the deadline) OR (NEW_PID == OLD_PID
#      AND etime > MAX_ETIME_SECONDS) — that's the silent-no-op signature.
#
# Exits 0 only when all checks pass. Non-zero with a one-line diagnostic
# pointing at which assertion failed.
#
# Usage: bash scripts/restart-voice-agent.sh

set -uo pipefail

UID_NUM="$(id -u)"
SERVICE="gui/${UID_NUM}/com.sutando.voice-agent"
PORT=9900
MAX_DEADLINE_SECONDS=30   # how long to wait for the new listener to come up
MAX_ETIME_SECONDS=30      # max age of an "accepted" listener (≤ deadline by design)

# Capture only the LISTENER PID — without `-sTCP:LISTEN`, lsof returns every
# pid bound to the port including connected WebSocket clients (browser, etc).
get_listener_pid() {
  lsof -nP -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -1
}

# Convert ps etime (SS, MM:SS, HH:MM:SS, DD-HH:MM:SS) to seconds.
# Forces base-10 arithmetic so leading-zero values like "08" don't get parsed
# as octal and fail bash arithmetic.
parse_etime_seconds() {
  local raw="$1"
  local days=0 hours=0 mins=0 secs=0
  if [[ "$raw" == *-* ]]; then
    days="${raw%%-*}"
    raw="${raw#*-}"
  fi
  local IFS=:
  read -r -a parts <<< "$raw"
  local n=${#parts[@]}
  case "$n" in
    1) secs="${parts[0]}" ;;
    2) mins="${parts[0]}"; secs="${parts[1]}" ;;
    3) hours="${parts[0]}"; mins="${parts[1]}"; secs="${parts[2]}" ;;
    *) echo ""; return 1 ;;
  esac
  # Use 10# prefix to defeat octal interpretation of values with leading 0s.
  echo $(( 10#$days*86400 + 10#$hours*3600 + 10#$mins*60 + 10#$secs ))
}

# --- 1. capture old listener PID ---
OLD_PID="$(get_listener_pid || true)"
if [ -z "${OLD_PID}" ]; then
  echo "WARN  no LISTEN process on :${PORT} before kickstart — may be normal if voice-agent was down"
fi

# --- 2. kickstart ---
echo "kickstart ${SERVICE} (old listener pid: ${OLD_PID:-none})"
if ! launchctl kickstart -k "${SERVICE}" 2>&1; then
  echo "FAIL  launchctl kickstart returned non-zero"
  exit 1
fi

# --- 3. poll for a fresh listener up to the deadline ---
DEADLINE_AT=$(( SECONDS + MAX_DEADLINE_SECONDS ))
NEW_PID=""
ETIME_RAW=""
ETIME_SECONDS=""
while [ "${SECONDS}" -lt "${DEADLINE_AT}" ]; do
  NEW_PID="$(get_listener_pid || true)"
  if [ -n "${NEW_PID}" ]; then
    ETIME_RAW="$(ps -p "${NEW_PID}" -o etime= 2>/dev/null | tr -d ' ')"
    if [ -n "${ETIME_RAW}" ]; then
      ETIME_SECONDS="$(parse_etime_seconds "${ETIME_RAW}" || true)"
      # Accept the moment we see a fresh listener (etime within deadline).
      # Don't gate on NEW_PID != OLD_PID — rare PID reuse + fresh etime is
      # still a real restart.
      if [ -n "${ETIME_SECONDS}" ] && [ "${ETIME_SECONDS}" -le "${MAX_ETIME_SECONDS}" ]; then
        break
      fi
    fi
  fi
  sleep 1
done

# --- 4. assertions ---
if [ -z "${NEW_PID}" ]; then
  echo "FAIL  no LISTEN process on :${PORT} within ${MAX_DEADLINE_SECONDS}s — voice-agent did not come back up"
  exit 2
fi

if [ -z "${ETIME_RAW}" ]; then
  echo "FAIL  could not read etime for new pid ${NEW_PID}"
  exit 4
fi

if [ -z "${ETIME_SECONDS}" ]; then
  echo "FAIL  could not parse etime '${ETIME_RAW}' for pid ${NEW_PID}"
  exit 5
fi

if [ "${ETIME_SECONDS}" -gt "${MAX_ETIME_SECONDS}" ]; then
  if [ -n "${OLD_PID}" ] && [ "${NEW_PID}" = "${OLD_PID}" ]; then
    echo "FAIL  listener pid unchanged (${NEW_PID}) and etime ${ETIME_RAW} >${MAX_ETIME_SECONDS}s — kickstart silently no-op'd"
    exit 3
  fi
  echo "FAIL  new listener pid ${NEW_PID} has etime ${ETIME_RAW} (>${MAX_ETIME_SECONDS}s) — looks like a stale process, not a fresh restart"
  exit 5
fi

echo "OK    voice-agent restarted: pid=${NEW_PID} etime=${ETIME_RAW} listening on :${PORT}"
exit 0
