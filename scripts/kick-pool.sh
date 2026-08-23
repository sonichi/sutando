#!/usr/bin/env bash
# kick-pool.sh — detect + recover hung pool followers (#880 D1 recovery path).
#
# Walks the pool's tmux sessions. Per session: submit a staged
# `/proactive-loop-pool`, type one into an idle REPL, Escape out of an
# interactive menu, and never overwrite other staged input. Sessions showing
# "esc to interrupt" are mid-task and skipped, so it is safe on a healthy pool.
#
# Exit: 0 = at least one kick, 1 = nothing needed, 2 = error.
#
# Session naming and socket must match scripts/pool-core-wrapper.sh — the
# wrapper creates `core-N` on tmux's DEFAULT socket. An override is accepted
# for tests; SUTANDO_POOL_SOCKET set to a real socket path still works.

set -u

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
  pane=$(tmux_cmd capture-pane -t "$sess" -p -S -8 2>/dev/null)

  if echo "$pane" | grep -q "esc to interrupt"; then
    echo "$sess: BUSY (processing) — skip"
    continue
  fi

  if echo "$pane" | grep -qE "Esc to cancel|Enter to select"; then
    echo "$sess: in interactive menu → Escape"
    tmux_cmd send-keys -t "$sess" Escape
    sleep 1
    pane=$(tmux_cmd capture-pane -t "$sess" -p -S -8 2>/dev/null)
  fi

  if echo "$pane" | grep -qE '^❯ /proactive-loop-pool *$'; then
    echo "$sess: /proactive-loop-pool staged → Enter"
    tmux_cmd send-keys -t "$sess" Enter
    kicked=$((kicked+1))
    continue
  fi

  if echo "$pane" | grep -qE '^❯ .+$'; then
    staged=$(echo "$pane" | grep -E '^❯ ' | tail -1 | sed 's/^❯ //; s/ *$//')
    echo "$sess: HAS STAGED INPUT ('$staged') — skip (won't overwrite)"
    continue
  fi

  echo "$sess: idle REPL → type + send /proactive-loop-pool pass"
  tmux_cmd send-keys -t "$sess" "/proactive-loop-pool pass" Enter
  kicked=$((kicked+1))
done

echo "kick-pool: kicked $kicked core(s)"
[ "$kicked" -gt 0 ] && exit 0 || exit 1
