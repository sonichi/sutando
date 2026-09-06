#!/usr/bin/env bash
# Phone-stack watchdog. Probes the PUBLIC url, not localhost: a dead tunnel
# still answers on :3100, and Twilio only ever hits the public one.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Default: WEBHOOK_BASE_URL from .env. Unset => not a phone host, so there is
# nothing to supervise.
if [ -z "${HEALTH_URL:-}" ]; then
  base="$(grep -E '^WEBHOOK_BASE_URL=' "$REPO/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  [ -n "$base" ] || exit 0
  HEALTH_URL="${base%/}/health"
fi

# Probe the public health endpoint — the exact thing Twilio hits.
if curl -sf -m 10 "$HEALTH_URL" >/dev/null 2>&1; then
  exit 0   # healthy — fast common path
fi

# Unhealthy: the public webhook is unreachable. Recover.
recover_cmd="${RECOVER_CMD:-bash "$REPO/src/startup.sh"}"

# startup.sh starts each service only when `pgrep` finds none, so a WEDGED but
# resident process makes recovery a silent no-op. Free the ports first.
PHONE_PORT="${PHONE_PORT:-3100}"
NGROK_API_PORT="${NGROK_API_PORT:-4040}"

port_listener_pids() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u
}

# Holding the port is not authorization to kill: a WAN/DNS/tunnel failure fails
# the public probe while the local listener is healthy or someone else's.
PHONE_STACK_ARGV_MATCH="${PHONE_STACK_ARGV_MATCH:-conversation-server ngrok}"

# Reads the argv of ONE pid. `pgrep -f` would match this script and any grep
# naming it; `ps -p` cannot self-match because the pid is already chosen.
phone_stack_owns_pid() {
  local pid="$1" cmd needle
  cmd="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
  [ -n "$cmd" ] || return 1
  for needle in $PHONE_STACK_ARGV_MATCH; do
    case "$cmd" in *"$needle"*) return 0 ;; esac
  done
  return 1
}

owned_listener_pids() {
  local pid
  for pid in $(port_listener_pids "$1"); do
    case "$pid" in ""|*[!0-9]*) continue ;; esac
    [ "$pid" = "$$" ] && continue
    if phone_stack_owns_pid "$pid"; then
      echo "$pid"
    else
      echo "phone-watchdog: port $1 held by pid $pid, not the phone stack — leaving it alone" >&2
    fi
  done
}

stop_wedged_stack() {
  local port pid stopped=""
  for port in "$PHONE_PORT" "$NGROK_API_PORT"; do
    for pid in $(owned_listener_pids "$port"); do
      kill -TERM "$pid" 2>/dev/null && stopped="$stopped $pid"
    done
  done
  [ -n "$stopped" ] || return 0
  echo "phone-watchdog: stopped wedged listener(s):$stopped" >&2
  # Give them a moment to release the ports, then insist, so startup.sh's
  # pgrep guard sees a clean slate rather than a dying process.
  sleep 2
  for port in "$PHONE_PORT" "$NGROK_API_PORT"; do
    for pid in $(owned_listener_pids "$port"); do
      kill -KILL "$pid" 2>/dev/null || true
    done
  done
}

if [ "${DRY_RUN:-}" = "1" ]; then
  echo "phone-watchdog: $HEALTH_URL unreachable -> would stop listeners on $PHONE_PORT/$NGROK_API_PORT, then run: $recover_cmd"
  exit 0
fi
echo "phone-watchdog: $HEALTH_URL unreachable -> recovering: $recover_cmd"
stop_wedged_stack
# shellcheck disable=SC2086  # RECOVER_CMD is an intentional command string
eval $recover_cmd
