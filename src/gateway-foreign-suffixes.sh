#!/bin/bash
# Source AFTER the channel .env is loaded: the derivation reads the
# AG2_REMOTE_TOKEN_<INST> variables out of the environment, so order decides it.

derive_foreign_suffixes() {
  if [ -n "${GATEWAY_FOREIGN_SUFFIXES:-}" ]; then
    printf '%s' "$GATEWAY_FOREIGN_SUFFIXES"
    return 0
  fi
  local _dfs_out="" _dfs_var _dfs_inst
  for _dfs_var in $(env | grep -o '^AG2_REMOTE_TOKEN_[A-Za-z0-9_][A-Za-z0-9_]*' || true); do
    _dfs_inst="$(printf '%s' "${_dfs_var#AG2_REMOTE_TOKEN_}" | tr '[:upper:]' '[:lower:]')"
    _dfs_out="${_dfs_out:+$_dfs_out,}:${_dfs_inst}.ag2.space"
  done
  printf '%s' "$_dfs_out"
}
