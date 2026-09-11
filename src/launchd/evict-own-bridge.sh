#!/bin/bash
# Evict a pre-existing BARE channel bridge that belongs to THIS checkout, before
# the launchd wrapper starts its own supervised child.
#
# CR #2068: the wrapper used `pkill -f "src/<channel>-bridge.py$"`, which matches
# the same bridge launched from ANY Sutando checkout on the host — so starting or
# upgrading one installation could kill another installation's live bridge.
# startup.sh launches slack/discord/telegram with a RELATIVE path
# (`python3 src/<channel>-bridge.py`, cwd = repo), so the command line alone is
# identical across checkouts; the only reliable discriminator is the process's
# resolved identity. This validates PID ownership: kill a candidate only if its
# command path is under THIS repo, OR its working directory IS this repo.
#
# Usage (sourced): `. evict-own-bridge.sh; evict_own_bridge <channel> <repo>`
# Usage (script, for tests): `evict-own-bridge.sh <channel> <repo> [inst-var] [inst-value]`

# Resolve a pid's working directory, cross-platform: /proc on Linux (CI), lsof on
# macOS (production). Empty string if it can't be determined.
# Read one env var from a running pid: /proc on Linux, `ps eww` on macOS. Empty
# when it cannot be determined — callers must treat that as UNKNOWN, not "unset".
# Echoes the value and returns 0 when the environment was READ (an unset var is an
# empty string); returns 1 when identity is INDETERMINATE, which must never kill.
_pid_env() {
  pid="$1"; name="$2"; raw=""
  if [ -r "/proc/$pid/environ" ]; then
    raw="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null)"
  else
    raw="$(ps eww -o command= -p "$pid" 2>/dev/null | tr ' ' '\n')"
  fi
  [ -n "$raw" ] || return 1
  printf '%s\n' "$raw" | sed -n "s/^$name=//p" | head -1
  return 0
}

_pid_cwd() {
  pid="$1"
  if [ -r "/proc/$pid/cwd" ]; then
    readlink "/proc/$pid/cwd" 2>/dev/null || true
  else
    lsof -a -d cwd -p "$pid" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
  fi
}

# _own_bridge_verdict <pid> <channel> <repo> [instance-var] [instance-value]
# Echoes OWN | FOREIGN | INDETERMINATE. The ONE identity decision shared by the
# evict (kill) and list (verify) entry points, so killer and verifier cannot drift.
_own_bridge_verdict() {
  pid="$1"; channel="$2"; repo="$3"; inst_var="${4:-}"; inst_val="${5:-}"
  rel="src/$channel-bridge.py"
  if [ -n "$inst_var" ]; then
    # Same script path serves every instance, so identity must come from the env.
    if ! got="$(_pid_env "$pid" "$inst_var")"; then
      echo INDETERMINATE; return 0
    fi
    if [ "$got" != "$inst_val" ]; then
      echo FOREIGN; return 0
    fi
  fi
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  case "$cmd" in
    *"$repo/$rel"*)
      # Absolute path under this checkout — unambiguously ours.
      echo OWN; return 0
      ;;
  esac
  # Relative launch: ours only if the process's cwd is this checkout. Compare
  # PHYSICAL paths so a symlinked checkout or macOS's /tmp -> /private/tmp
  # (lsof reports the resolved path) doesn't cause a false mismatch.
  cwd="$(_pid_cwd "$pid")"
  if [ -z "$cwd" ]; then
    echo INDETERMINATE; return 0
  fi
  cwd_p="$(cd "$cwd" 2>/dev/null && pwd -P || echo "$cwd")"
  repo_p="$(cd "$repo" 2>/dev/null && pwd -P || echo "$repo")"
  if [ "$cwd_p" = "$repo_p" ]; then echo OWN; else echo FOREIGN; fi
  return 0
}

# evict_own_bridge <channel> <repo> [instance-var] [instance-value]
# With an instance discriminator, a candidate is killed only when its own value of
# <instance-var> equals <instance-value>; indeterminate identity never kills.
# _own_bridge_candidates <channel>: echoes candidate pids; returns pgrep's rc.
# Candidates: any process whose command line ends with `src/<channel>-bridge.py`
# (matches both relative and absolute launches). rc 1 = clean no-match; any
# other nonzero rc = discovery FAILED, and callers must propagate it — an
# unreadable process table printed as emptiness reads as "already evicted".
_own_bridge_candidates() {
  pgrep -f "src/$1-bridge\.py\$" 2>/dev/null
}

evict_own_bridge() {
  channel="$1"; repo="$2"; inst_var="${3:-}"; inst_val="${4:-}"
  candidates="$(_own_bridge_candidates "$channel")" && _disc_rc=0 || _disc_rc=$?
  if [ "$_disc_rc" -gt 1 ]; then
    echo "evict_own_bridge: candidate discovery FAILED (pgrep rc=$_disc_rc); no eviction attempted" >&2
    return "$_disc_rc"
  fi
  for pid in $candidates; do
    [ "$pid" = "$$" ] && continue
    case "$(_own_bridge_verdict "$pid" "$channel" "$repo" "$inst_var" "$inst_val")" in
      OWN) kill "$pid" 2>/dev/null || true ;;
      INDETERMINATE) echo "evict_own_bridge: skip pid $pid (identity indeterminate; never killing)" >&2 ;;
    esac
  done
  return 0
}

# list_own_bridge <channel> <repo> [instance-var] [instance-value]
# Read-only verifier over the SAME identity decision: prints "OWN <pid>" for each
# live process of this checkout and "INDETERMINATE <pid>" for any it cannot
# classify (callers must fail closed on those). Foreign pids print nothing.
list_own_bridge() {
  channel="$1"; repo="$2"; inst_var="${3:-}"; inst_val="${4:-}"
  candidates="$(_own_bridge_candidates "$channel")" && _disc_rc=0 || _disc_rc=$?
  if [ "$_disc_rc" -gt 1 ]; then
    echo "list_own_bridge: candidate discovery FAILED (pgrep rc=$_disc_rc)" >&2
    return "$_disc_rc"
  fi
  for pid in $candidates; do
    [ "$pid" = "$$" ] && continue
    case "$(_own_bridge_verdict "$pid" "$channel" "$repo" "$inst_var" "$inst_val")" in
      OWN) echo "OWN $pid" ;;
      INDETERMINATE) echo "INDETERMINATE $pid" ;;
    esac
  done
  return 0
}

# Run directly when invoked as a script (tests / health-check), not when sourced.
# `--list` selects the read-only verifier; default is the evicting entry point.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  if [ "${1:-}" = "--list" ]; then
    shift
    list_own_bridge "$@"
  else
    evict_own_bridge "$@"
  fi
fi
