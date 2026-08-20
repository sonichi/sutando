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

obs_collector_healthy() {
  local port="$1" response
  response="$(curl -fsS --max-time 2 "http://127.0.0.1:$port/health" 2>/dev/null)" || return 1
  case "$response" in
    *'"service":"sutando-observability-collector"'*) return 0 ;;
    *) return 1 ;;
  esac
}
