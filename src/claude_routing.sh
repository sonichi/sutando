#!/bin/sh
# Whether Claude sessions are routed through the local credential proxy or call Anthropic
# directly. POSIX: sourced by the bash launchers and by the `sh -c` wrapper a session starts under.

# Direct when QUOTA_STATUSLINE_ENABLED (a skill manifest `config` key, env first) is on: a session
# never carries the local proxy as ANTHROPIC_BASE_URL; any other base URL is the owner's, kept.

claude_proxy_url_is_local() {
  case "${1:-}" in
    http://localhost:7846|http://localhost:7846/|http://127.0.0.1:7846|http://127.0.0.1:7846/) return 0 ;;
    "http://[::1]:7846"|"http://[::1]:7846/") return 0 ;;
  esac
  return 1
}

# Prints switch $1: env first, else manifest config (reads REPO and PY; before forwarding).
claude_routing_switch() {
  if eval "[ -n \"\${$1+x}\" ]"; then
    eval "printf '%s' \"\${$1}\""
  elif command -v skill_manifest_config_pending > /dev/null 2>&1; then
    skill_manifest_config_pending "${REPO:-}" "${PY:-}" 2>/dev/null | tr '\0' '\n' \
      | sed -n "s/^$1=//p" | head -n 1
  fi
}

claude_routing_is_off() {
  case "$1" in 0|false|False|FALSE|no|No|NO|off|Off|OFF) return 0 ;; esac
  return 1
}

claude_direct_routing() {
  _cdr_v="$(claude_routing_switch QUOTA_STATUSLINE_ENABLED)"
  [ -n "$_cdr_v" ] && ! claude_routing_is_off "$_cdr_v"
}

# The owner's explicit "keep the proxy off"; unset or empty means the proxy is allowed.
claude_proxy_owner_off() {
  claude_routing_is_off "$(claude_routing_switch CREDENTIAL_PROXY_ENABLED)"
}

# Whether a session goes direct: tracking on, or the owner keeps the proxy off.
claude_routes_direct() {
  claude_direct_routing || claude_proxy_owner_off
}

# In the session's own environment (which tmux may take from its server, not the launcher).
claude_unset_local_proxy() {
  if claude_proxy_url_is_local "${ANTHROPIC_BASE_URL:-}"; then
    unset ANTHROPIC_BASE_URL
  fi
}
