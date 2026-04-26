#!/usr/bin/env bash
# restart-voice-agent.sh — kickstart com.sutando.voice-agent and verify the
# restart actually happened. Exists because `launchctl kickstart -k` is known
# to silently no-op (observed 2026-04-25, 9.5h of stale code mid-talk-prep).
#
# Verification:
#   1. Capture PID listening on :9900 BEFORE kickstart.
#   2. `launchctl kickstart -k gui/<uid>/com.sutando.voice-agent`.
#   3. Sleep 3s.
#   4. Capture PID listening on :9900 AFTER kickstart.
#   5. Assert new PID differs from old AND new PID's etime < 30s.
#   6. Confirm :9900 still listening.
#
# Exits 0 only when all checks pass. Non-zero with a one-line diagnostic
# pointing at which assertion failed.
#
# Usage: bash scripts/restart-voice-agent.sh

set -uo pipefail

UID_NUM="$(id -u)"
SERVICE="gui/${UID_NUM}/com.sutando.voice-agent"
PORT=9900
SLEEP_SECONDS=3
MAX_ETIME_SECONDS=30

# --- 1. capture old PID ---
OLD_PID="$(lsof -ti tcp:${PORT} 2>/dev/null | head -1 || true)"
if [ -z "${OLD_PID}" ]; then
  echo "WARN  no process on :${PORT} before kickstart — may be normal if voice-agent was down"
fi

# --- 2. kickstart ---
echo "kickstart ${SERVICE} (old pid: ${OLD_PID:-none})"
if ! launchctl kickstart -k "${SERVICE}" 2>&1; then
  echo "FAIL  launchctl kickstart returned non-zero"
  exit 1
fi

# --- 3. sleep ---
sleep "${SLEEP_SECONDS}"

# --- 4. capture new PID ---
NEW_PID="$(lsof -ti tcp:${PORT} 2>/dev/null | head -1 || true)"
if [ -z "${NEW_PID}" ]; then
  echo "FAIL  no process on :${PORT} ${SLEEP_SECONDS}s after kickstart — voice-agent did not come back up"
  exit 2
fi

# --- 5. assertions ---
if [ -n "${OLD_PID}" ] && [ "${NEW_PID}" = "${OLD_PID}" ]; then
  echo "FAIL  PID unchanged (${NEW_PID}) — kickstart silently no-op'd"
  exit 3
fi

# Read elapsed seconds via ps -o etime (formats: SS, MM:SS, HH:MM:SS, DD-HH:MM:SS).
ETIME_RAW="$(ps -p "${NEW_PID}" -o etime= 2>/dev/null | tr -d ' ')"
if [ -z "${ETIME_RAW}" ]; then
  echo "FAIL  could not read etime for new pid ${NEW_PID}"
  exit 4
fi

# Convert etime to seconds. Possible formats:
#   SS              (e.g. "07")
#   MM:SS           (e.g. "01:23")
#   HH:MM:SS        (e.g. "01:02:03")
#   DD-HH:MM:SS     (e.g. "1-02:03:04")
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
    *) echo 999999; return ;;
  esac
  echo $(( days*86400 + hours*3600 + mins*60 + secs ))
}

ETIME_SECONDS="$(parse_etime_seconds "${ETIME_RAW}")"
if [ "${ETIME_SECONDS}" -gt "${MAX_ETIME_SECONDS}" ]; then
  echo "FAIL  new pid ${NEW_PID} has etime ${ETIME_RAW} (>${MAX_ETIME_SECONDS}s) — looks like a stale process, not a fresh restart"
  exit 5
fi

# --- 6. confirm port still listening (paranoia: NEW_PID was non-empty above, but check again) ---
if ! lsof -i tcp:${PORT} -nP 2>/dev/null | grep -q LISTEN; then
  echo "FAIL  :${PORT} not in LISTEN state"
  exit 6
fi

echo "OK    voice-agent restarted: pid=${NEW_PID} etime=${ETIME_RAW} listening on :${PORT}"
exit 0
