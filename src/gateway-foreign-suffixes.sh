#!/bin/bash
# Source AFTER the channel .env is loaded: the derivation reads the
# AG2_REMOTE_TOKEN_<INST> variables out of the environment, so order decides it.

_dfs_channels_root() {
  if [ -n "${GATEWAY_CHANNELS_DIR:-}" ]; then
    printf '%s' "$GATEWAY_CHANNELS_DIR"
    return 0
  fi
  local _dfs_repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  bash "$_dfs_repo/scripts/sutando-config.sh" claude-home-path channels 2>/dev/null
}

derive_foreign_suffixes() {
  if [ -n "${GATEWAY_FOREIGN_SUFFIXES:-}" ]; then
    printf '%s' "$GATEWAY_FOREIGN_SUFFIXES"
    return 0
  fi
  local _dfs_root
  _dfs_root="$(_dfs_channels_root)"
  local _dfs_out="" _dfs_var _dfs_inst _dfs_env _dfs_mxid _dfs_suffix
  # Iterate NAMES: parsing `env` output cannot tell a real variable from a
  # value carrying a newline that looks like an assignment.
  for _dfs_var in ${!AG2_REMOTE_TOKEN_@}; do
    _dfs_inst="${_dfs_var#AG2_REMOTE_TOKEN_}"
    # A bare AG2_REMOTE_TOKEN_ names no instance; :.ag2.space is not a lane.
    [ -n "$_dfs_inst" ] || continue
    _dfs_inst="$(printf '%s' "$_dfs_inst" | tr '[:upper:]' '[:lower:]')"
    # The instance name is a LABEL, not a homeserver: a lane's true suffix is
    # its identity's domain (AGENT_MXID), e.g. instance "local" -> :localhost.
    _dfs_suffix=""
    _dfs_env="$_dfs_root/${_dfs_inst}-ag2space/.env"
    if [ -n "$_dfs_root" ] && [ -f "$_dfs_env" ]; then
      _dfs_mxid="$(sed -n 's/^AGENT_MXID=//p' "$_dfs_env" | tail -1)"
      case "$_dfs_mxid" in
        *:*) _dfs_suffix=":${_dfs_mxid##*:}" ;;
      esac
    fi
    [ -n "$_dfs_suffix" ] || _dfs_suffix=":${_dfs_inst}.ag2.space"
    _dfs_out="${_dfs_out:+$_dfs_out,}${_dfs_suffix}"
  done
  printf '%s' "$_dfs_out"
}
