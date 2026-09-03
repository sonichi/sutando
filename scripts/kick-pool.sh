#!/usr/bin/env bash
# kick-pool.sh — detect + recover hung pool followers (#880 D1 recovery path).
#
# Walks the pool's tmux sessions. Per session: resolve that core's runtime from
# its installed plist, then hand the pane to pool-runtime-drive.sh, which owns
# every marker and keystroke (submit a staged pool entry, type one into an idle
# REPL, dismiss a menu, never overwrite other staged input, skip a busy core).
# This script keeps only session enumeration, launchd revival and tmux binding.
#
# Exit: 0 = at least one kick, 1 = nothing needed, 2 = error.
#
# Session naming and socket must match scripts/pool-core-wrapper.sh — the
# wrapper creates `core-N` on tmux's DEFAULT socket. An override is accepted
# for tests; SUTANDO_POOL_SOCKET set to a real socket path still works.

set -u

# The staged copy is a sibling. Refuse rather than run with no driving policy:
# an install staged before the library existed would type nothing, silently.
DRIVE_LIB="$(dirname "$0")/pool-runtime-drive.sh"
if ! [ -r "$DRIVE_LIB" ]; then
  echo "kick-pool: missing $DRIVE_LIB — re-run scripts/install-core-pool.sh" >&2
  exit 2
fi
# shellcheck source=./pool-runtime-drive.sh
. "$DRIVE_LIB"

# Resolved from PATH, never a literal prefix: the wrapper injects
# POOL_TMUX_BIN, and the launchd job sets a PATH that carries the real one.
TMUX_BIN="${TMUX_BIN:-${POOL_TMUX_BIN:-$(command -v tmux || true)}}"
SESSION_PREFIX="${SUTANDO_POOL_SESSION_PREFIX:-core-}"
# Empty (the default) = tmux's default socket, which is what the wrapper uses.
SOCKET="${SUTANDO_POOL_SOCKET:-}"

if ! [ -x "$TMUX_BIN" ]; then
  echo "kick-pool: $TMUX_BIN not found or not executable" >&2
  exit 2
fi

tmux_cmd() {
  if [ -n "$SOCKET" ]; then "$TMUX_BIN" -S "$SOCKET" "$@"; else "$TMUX_BIN" "$@"; fi
}

if [ -n "$SOCKET" ] && ! [ -S "$SOCKET" ]; then
  echo "kick-pool: socket $SOCKET not present — pool may not be running" >&2
  exit 2
fi

sessions=$(tmux_cmd list-sessions -F '#S' 2>/dev/null | grep "^${SESSION_PREFIX}")

kicked=0

# A DEAD core has no session, so the pane walk below cannot see it. launchd
# does not reliably revive one either: a KeepAlive job that exits 0 gets
# "pended nondemand spawn", observed sitting dead indefinitely. Kickstart is
# the arm that actually works, so drive it from the installed plists.
for plist in "$HOME/Library/LaunchAgents/com.sutando.${SESSION_PREFIX}"*.plist; do
  [ -e "$plist" ] || continue
  label=$(basename "$plist" .plist)
  inst="${label#com.sutando.}"
  if echo "$sessions" | grep -qx "$inst"; then
    continue
  fi
  echo "$inst: NO SESSION (dead) → launchctl kickstart"
  if launchctl kickstart "gui/$(id -u)/${label}" >/dev/null 2>&1; then
    kicked=$((kicked+1))
  else
    echo "$inst: kickstart FAILED" >&2
  fi
done
for sess in ${sessions:-}; do
  # The core's own plist is the only authority on which runtime it runs. An
  # unreadable or unknown one yields the empty string, which fails closed below.
  runtime=$(pool_runtime_from_plist \
    "$HOME/Library/LaunchAgents/com.sutando.${sess}.plist" || true)
  if pool_drive_kick "$runtime" "$sess" tmux_cmd "${sess#"$SESSION_PREFIX"}"; then
    kicked=$((kicked+1))
  fi
done

echo "kick-pool: kicked $kicked core(s)"
[ "$kicked" -gt 0 ] && exit 0 || exit 1
