#!/bin/bash
# Startup policy helpers for the local observability collector.

obs_collector_enabled() {
  [ "${SUTANDO_OBS_COLLECTOR:-1}" != "0" ]
}

obs_hooks_enabled() {
  # Preserve the old explicit opt-in for plaintext prompt/tool hook capture.
  # An unset value starts metrics-only collection; it does not enable hooks.
  [ "${SUTANDO_OBS_COLLECTOR:-}" = "1" ]
}

# 0 = only this account listens, 2 = a foreign account listens,
# 3 = nothing listens or ownership cannot be measured.
obs_listener_owned_by_me() {
  local port="$1" uids current_uid
  uids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fu 2>/dev/null \
    | sed -n 's/^u//p' | sort -u || true)"
  current_uid="$(id -u)"
  if [ -z "$uids" ]; then
    return 3
  fi
  if [ "$uids" = "$current_uid" ]; then
    return 0
  fi
  return 2
}

obs_collector_healthy() {
  local port="$1" response owner_rc
  obs_listener_owned_by_me "$port"
  owner_rc=$?
  [ "$owner_rc" -eq 0 ] || return "$owner_rc"
  response="$(curl -fsS --max-time 2 "http://127.0.0.1:$port/health" 2>/dev/null)" || return 1
  case "$response" in
    *'"service":"sutando-observability-collector"'*) return 0 ;;
    *) return 1 ;;
  esac
}
