#!/bin/bash
# Shared by both runtime launchers: check the core CLI before a launch replaces a core.
# The verdict, repair plan and repair live in src/runtime_compat.py; this file only calls it.

# $1 repo, $2 runtime, then extra `ensure` args. Sets SUTANDO_CORE_CLI_BIN to the
# binary to launch. Returns non-zero (3 unusable, 4 login) when the core must not start.
sutando_runtime_compat_gate() {
  local repo="$1" runtime="$2" py="" wait_s out rc=0 first
  shift 2
  SUTANDO_CORE_CLI_BIN="$runtime"
  if [ "${SUTANDO_SKIP_RUNTIME_COMPAT:-0}" = "1" ]; then
    echo "runtime-compat: skipped (SUTANDO_SKIP_RUNTIME_COMPAT=1)" >&2
    return 0
  fi
  [ -f "$repo/src/runtime_compat.py" ] || return 0
  # Resolved in a child shell so the launcher's own resolver state stays untouched.
  if [ -r "$repo/scripts/python-binary.sh" ]; then
    py="$(bash -c '. "$1/scripts/python-binary.sh" && resolve_python "$1"' gate "$repo")"
  fi
  if [ -z "$py" ]; then
    echo "runtime-compat: no runnable python3, so the $runtime version check was skipped" >&2
    return 0
  fi
  # Off a terminal nobody can answer a prompt, so the owner is asked in their DM instead.
  local tty_args=(--notify-owner)
  if [ -t 0 ] && [ -t 2 ]; then tty_args=(); fi
  wait_s="${SUTANDO_RUNTIME_WAIT_S:-600}"
  out="$("$py" "$repo/src/runtime_compat.py" ensure --runtime "$runtime" --wait "$wait_s" \
    --repair --resume-hint "bash $repo/src/agent/start-cli.sh" ${tty_args[@]+"${tty_args[@]}"} "$@")" || rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  # Healthy hosts keep the exact command they launched before; only a fallback changes it.
  first="$(command -v "$runtime" 2>/dev/null || true)"
  if [ -n "$out" ] && [ "$out" != "$first" ]; then
    SUTANDO_CORE_CLI_BIN="$out"
  fi
  return 0
}
