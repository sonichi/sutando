#!/bin/bash
# Preconditions on the core's tmux socket, checked by the dispatcher before any launcher runs.

# Returns 0 when <tmux> sees no server on <socket> or the server is its own version, 2 when it
# reaches a server of another version, 1 when it cannot; sets _SUTANDO_TMUX_PROBLEM on non-zero.
_sutando_tmux_probe() {
  local bin="$1" socket="$2" out rc client server
  rc=0
  out="$("$bin" -S "$socket" list-sessions -F '#{version}' 2>&1)" || rc=$?
  if [ "$rc" != 0 ]; then
    case "$out" in
      ''|*"no server running"*|*"(No such file or directory)"*|*"(Connection refused)"*) return 0 ;;
    esac
    _SUTANDO_TMUX_PROBLEM="$bin cannot query the tmux server on $socket: ${out%%$'\n'*}"
    return 1
  fi
  client="$("$bin" -V 2>/dev/null || true)"
  client="${client#tmux }"
  server="${out%%$'\n'*}"
  if [ -n "$client" ] && [ -n "$server" ] && [ "$client" != "$server" ]; then
    _SUTANDO_TMUX_PROBLEM="$bin is tmux $client, but tmux $server owns $socket"
    return 2
  fi
}

# Refuses (returns 1) when no tmux can reach the server that owns <socket>, or when <socket> is a
# desktop install's and <repo> is not that install's engine checkout.
sutando_tmux_socket_guard() {
  local socket="$1" repo="$2" path_tmux exported bin rc chosen="" reachable="" mismatch=""
  local run_dir app_dir engine cands=()
  path_tmux="$(command -v tmux 2>/dev/null || true)"
  exported="${SUTANDO_TMUX_BIN:-}"
  _SUTANDO_TMUX_PROBLEM=""
  [ -n "$path_tmux" ] && cands+=("$path_tmux")
  if [ -n "$exported" ] && [ -x "$exported" ] && [ "$exported" != "$path_tmux" ]; then
    cands+=("$exported")
  fi
  for bin in ${cands[@]+"${cands[@]}"}; do
    rc=0
    _sutando_tmux_probe "$bin" "$socket" || rc=$?
    if [ "$rc" = 0 ]; then chosen="$bin"; break; fi
    if [ "$rc" = 2 ] && [ -z "$reachable" ]; then reachable="$bin"; mismatch="$_SUTANDO_TMUX_PROBLEM"; fi
  done
  if [ -z "$chosen" ] && [ -n "$reachable" ]; then
    echo "start-cli: warning: $mismatch; continuing with it, since it reaches that server." >&2
    chosen="$reachable"
  fi
  if [ -n "$chosen" ]; then
    # Launchers resolve tmux from PATH, so the client that reached the server goes first.
    if [ "$chosen" != "$path_tmux" ]; then
      PATH="$(dirname "$chosen"):$PATH"
      export PATH
    fi
  elif [ "${#cands[@]}" -gt 0 ]; then
    echo "start-cli: $_SUTANDO_TMUX_PROBLEM." >&2
    echo "start-cli: refusing to launch; a client that cannot reach the server reads a live core as absent. Put the tmux that owns that server on PATH." >&2
    return 1
  elif [ -S "$socket" ]; then
    echo "start-cli: a tmux server may own $socket, but no tmux is on PATH." >&2
    echo "start-cli: put the tmux that created it on PATH; installing another build would start a second, incompatible server." >&2
    return 1
  fi
  run_dir="$(dirname "$socket")"
  app_dir="$(dirname "$run_dir")"
  engine="$app_dir/engine/sutando"
  [ "${run_dir##*/}" = run ] && [ -d "$engine" ] || return 0
  [ "$(cd "$engine" && pwd -P)" = "$(cd "$repo" && pwd -P)" ] && return 0
  [ "${SUTANDO_ALLOW_FOREIGN_CORE_CHECKOUT:-}" = 1 ] && return 0
  echo "start-cli: $socket belongs to the desktop engine at $engine, not this checkout ($repo)." >&2
  echo "start-cli: set SUTANDO_ALLOW_FOREIGN_CORE_CHECKOUT=1 to replace the desktop's core with this checkout on purpose." >&2
  return 1
}
