#!/bin/bash
# Source AFTER the channel .env is loaded: the derivation reads the
# AG2_REMOTE_TOKEN_<INST> variables out of the environment, so order decides it.

derive_foreign_suffixes() {
  if [ -n "${GATEWAY_FOREIGN_SUFFIXES:-}" ]; then
    printf '%s' "$GATEWAY_FOREIGN_SUFFIXES"
    return 0
  fi
  local _dfs_out="" _dfs_var _dfs_inst
  # Iterate NAMES: parsing `env` output cannot tell a real variable from a
  # value carrying a newline that looks like an assignment.
  for _dfs_var in ${!AG2_REMOTE_TOKEN_@}; do
    _dfs_inst="${_dfs_var#AG2_REMOTE_TOKEN_}"
    # A bare AG2_REMOTE_TOKEN_ names no instance; :.ag2.space is not a lane.
    [ -n "$_dfs_inst" ] || continue
    _dfs_inst="$(printf '%s' "$_dfs_inst" | tr '[:upper:]' '[:lower:]')"
    _dfs_out="${_dfs_out:+$_dfs_out,}:${_dfs_inst}.ag2.space"
  done
  printf '%s' "$_dfs_out"
}
