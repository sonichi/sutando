#!/bin/bash
# Shared derivation of GATEWAY_FOREIGN_SUFFIXES, sourced by every gateway
# launch path so supervised and bare launches fence identically.
#
# Every configured named lane <inst> serves rooms on :<inst>.ag2.space (the
# same convention that maps <inst> to channels/<inst>-ag2space/), so those
# suffixes are foreign to the default lane. Operator-set
# GATEWAY_FOREIGN_SUFFIXES wins verbatim.
#
# Callers must source this AFTER loading the channel .env that carries the
# AG2_REMOTE_TOKEN_<INST> variables — the derivation reads them from the
# environment.

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
