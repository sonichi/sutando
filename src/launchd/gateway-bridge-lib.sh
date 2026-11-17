#!/bin/bash
# The legacy gateway bridge's launch policy, one copy for both launchers: sourced by the launchd wrapper,
# run as `gateway-bridge-lib.sh env|alert` by src/sparrow_core_bridge.py. Needs $REPO.

# What the process environment provided, kept apart from what the .env provides, so a re-read can
# drop a removed .env token without dropping an environment-provided one.
_ENV_REMOTE_TASK_TOKEN="${REMOTE_TASK_TOKEN:-}"; _ENV_AG2_REMOTE_TOKEN="${AG2_REMOTE_TOKEN:-}"
_ENV_REMOTE_TASK_TIER="${REMOTE_TASK_TIER:-}"; _ENV_AG2_REMOTE_TIER="${AG2_REMOTE_TIER:-}"
GW_LOG_TAG="${GW_LOG_TAG:-gateway-bridge-wrapper}"

# `.env` first (policy, shell-form credentials), then the file the shared resolver
# names as holding the token, unless the process environment supplied one.
_load_channel_env() {
    local dot="" tok=""
    dot="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/ag2space/.env 2>/dev/null)" || dot=""
    [ -n "$dot" ] && [ -f "$dot" ] && { set -a; . "$dot"; set +a; }
    if [ -z "${_ENV_REMOTE_TASK_TOKEN}${_ENV_AG2_REMOTE_TOKEN}" ]; then
        tok="$(bash "$REPO/scripts/channel-env.sh" ag2space 2>/dev/null)" || tok=""
        [ -n "$tok" ] && [ -f "$tok" ] && [ "$tok" != "$dot" ] && { set -a; . "$tok"; set +a; }
    fi
    # The bridge re-reads the file holding the token on an auth rejection, to pick up a rotated one.
    local held="$dot"; [ -n "$tok" ] && [ -f "$tok" ] && held="$tok"
    [ -n "$held" ] && [ -f "$held" ] && export REMOTE_TASK_TOKEN_FILE="$held"
    # Only the shared rule's "blank" verdict (rc 3) drops it; an unrunnable check keeps it.
    local v rc
    for v in REMOTE_TASK_TOKEN AG2_REMOTE_TOKEN; do
        rc=0
        printf '%s' "${!v:-}" | python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from channel_token import clean_relay_token as c; sys.exit(0 if c(sys.stdin.read()) else 3)' "$REPO/src" 2>/dev/null || rc=$?
        [ "$rc" -ne 3 ] || eval "$v="
    done
    return 0
}

# The bridge's environment, legacy names mapped. Tier "owner": a personal gateway pulls with the owner's
# bearer. Media marker `ag2space-media`: AG2 Space's tag, not the bridge's provider-neutral default.
gateway_legacy_env() {
    REMOTE_TASK_TOKEN="$_ENV_REMOTE_TASK_TOKEN"; AG2_REMOTE_TOKEN="$_ENV_AG2_REMOTE_TOKEN"
    REMOTE_TASK_TIER="$_ENV_REMOTE_TASK_TIER"; AG2_REMOTE_TIER="$_ENV_AG2_REMOTE_TIER"
    _load_channel_env
    REMOTE_TASK_TOKEN="${REMOTE_TASK_TOKEN:-${AG2_REMOTE_TOKEN:-}}"
    REMOTE_TASK_TIER="${REMOTE_TASK_TIER:-${AG2_REMOTE_TIER:-owner}}"
    REMOTE_MEDIA_MARKER="${REMOTE_MEDIA_MARKER:-ag2space-media}"
    export REMOTE_TASK_TOKEN REMOTE_TASK_TIER REMOTE_MEDIA_MARKER
}

_gw_age() { local t; t="$(cat "$1" 2>/dev/null || echo 0)"; echo $(( $(date +%s) - ${t:-0} )); }

# Tell the owner an unexpected restart happened (needs $WORKSPACE): quiet inside the deliberate-restart
# window and the alert cooldown.
gateway_restart_alert() {
  local dir="$WORKSPACE/state/channel-bridge-supervisor" now
  local deliberate="$dir/deliberate-restart" stamp="$dir/gateway.last-alert"
  now="$(date +%s)"
  echo "[$GW_LOG_TAG] previous process exited; automatically restarting" >&2
  if [ -f "$deliberate" ] && [ "$(_gw_age "$deliberate")" -lt "${SUTANDO_BRIDGE_DELIBERATE_WINDOW_S:-180}" ]; then
    echo "[$GW_LOG_TAG] exit within a deliberate restart window; no alert" >&2
    return 0
  fi
  if [ -f "$stamp" ] && [ "$(_gw_age "$stamp")" -lt "${SUTANDO_BRIDGE_ALERT_COOLDOWN_S:-600}" ]; then
    echo "[$GW_LOG_TAG] alert cooldown active; not repeating" >&2
    return 0
  fi
  mkdir -p "$dir" "$WORKSPACE/results" 2>/dev/null || true
  echo "$now" > "$stamp"
  printf '%s\n' "⚠️ The gateway bridge exited and was automatically restarted." \
    | python3 "$REPO/src/result_publish.py" "$WORKSPACE/results/proactive-gateway-bridge-restarted-$now.txt"
  bash "$REPO/src/macos-notify.sh" "The gateway bridge exited and was automatically restarted." || true
}

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  REPO="${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
  case "${1:-}" in
    env)
      gateway_legacy_env >&2
      exec python3 -c 'import json, os; os.environ.pop("_", None); print(json.dumps(dict(os.environ)))'
      ;;
    alert)
      WORKSPACE="${WORKSPACE:?WORKSPACE is required}"
      gateway_restart_alert
      ;;
    *) echo "usage: gateway-bridge-lib.sh env|alert" >&2; exit 2 ;;
  esac
fi
