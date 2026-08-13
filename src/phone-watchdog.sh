#!/usr/bin/env bash
# Phone-stack watchdog — restart the phone stack when the PUBLIC webhook dies.
#
# Probes the PUBLIC url, not localhost: a wrong-domain or dead tunnel still
# answers on :3100, and Twilio only ever hits the public one.
# Recovery defaults to startup.sh because it is idempotent and owns bundled-mode
# launch; RECOVER_CMD overrides it for a phone-only restart.
#
# HEALTH_URL overrides the probed URL and DRY_RUN=1 prints the recovery action
# instead of running it, so tests never touch a real stack.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve the PUBLIC url Twilio posts to. Default: WEBHOOK_BASE_URL from .env
# (what startup.sh writes). No webhook configured → this host isn't a phone
# host, nothing to supervise.
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

# Identity by LISTENER, never by argv pattern: `pgrep -f` matches this script's
# own command line and any editor or grep that happens to mention the name.
port_listener_pids() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u
}

stop_wedged_stack() {
  local port pid stopped=""
  for port in "$PHONE_PORT" "$NGROK_API_PORT"; do
    for pid in $(port_listener_pids "$port"); do
      case "$pid" in ""|*[!0-9]*) continue ;; esac
      [ "$pid" = "$$" ] && continue
      kill -TERM "$pid" 2>/dev/null && stopped="$stopped $pid"
    done
  done
  [ -n "$stopped" ] || return 0
  echo "phone-watchdog: stopped wedged listener(s):$stopped" >&2
  # Give them a moment to release the ports, then insist, so startup.sh's
  # pgrep guard sees a clean slate rather than a dying process.
  sleep 2
  for port in "$PHONE_PORT" "$NGROK_API_PORT"; do
    for pid in $(port_listener_pids "$port"); do
      case "$pid" in ""|*[!0-9]*) continue ;; esac
      [ "$pid" = "$$" ] && continue
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
