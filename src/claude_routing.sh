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

# Reads REPO and PY when the switch is not in the environment (manifest config, before forwarding).
claude_direct_routing() {
  _cdr_v="${QUOTA_STATUSLINE_ENABLED-}"
  if [ -z "${QUOTA_STATUSLINE_ENABLED+x}" ] && command -v skill_manifest_config_pending > /dev/null 2>&1; then
    _cdr_v="$(skill_manifest_config_pending "${REPO:-}" "${PY:-}" 2>/dev/null | tr '\0' '\n' \
      | sed -n 's/^QUOTA_STATUSLINE_ENABLED=//p' | head -n 1)"
  fi
  case "$_cdr_v" in
    ""|0|false|False|FALSE|no|No|NO|off|Off|OFF) return 1 ;;
  esac
  return 0
}

# In the session's own environment (which tmux may take from its server, not the launcher).
claude_unset_local_proxy() {
  if claude_proxy_url_is_local "${ANTHROPIC_BASE_URL:-}"; then
    unset ANTHROPIC_BASE_URL
  fi
}
