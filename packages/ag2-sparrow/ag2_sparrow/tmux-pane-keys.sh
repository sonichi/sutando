#!/usr/bin/env bash
# tmux-pane-keys.sh [--tmux BIN] --python PY -S SOCKET -t TARGET [--timeout SECS] -- <send-keys args...>
# tmux-pane-keys.sh [--tmux BIN] --python PY -S SOCKET -t PANE [--timeout SECS] --fence PREDICATE ACTION
# Leaves pane mode and sends in one tmux step, bounded; or runs ACTION only while the format PREDICATE
# holds for PANE, else orphans the ticket (exit 1, nothing sent). Exit: status, 124 revoked timeout,
# 125 uncertain, 75 busy, 2 usage. The interpreter for the guard is the caller's, never a repo helper.

# Copy-mode letters can open a command-prompt that blocks the sending client.
set -u
WORK=""
if [ "${1:-}" = --guarded ]; then WORK="$2"; shift 2; fi
ARGS=("$@")
TMUX_BIN=tmux; SOCK=""; TARGET=""; TIMEOUT=5; PY=""; FENCED=""; FENCE_PRED=""; FENCE_ACTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tmux) TMUX_BIN="${2:?}"; shift 2 ;;
    --python) PY="${2:?}"; shift 2 ;;
    -S) SOCK="${2:?}"; shift 2 ;;
    -t) TARGET="${2:?}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?}"; shift 2 ;;
    --fence) FENCE_PRED="${2:?}"; FENCE_ACTION="${3:?}"; FENCED=1; shift 3 ;;
    --) shift; break ;;
    *) echo "tmux-pane-keys: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$SOCK" ] && [ -n "$TARGET" ] && { [ $# -gt 0 ] || [ -n "$FENCED" ]; } \
  || { echo "tmux-pane-keys: need -S, -t and keys after -- (or --fence PREDICATE ACTION)" >&2; exit 2; }
[ -z "$FENCED" ] || [ $# -eq 0 ] || { echo "tmux-pane-keys: --fence takes no keys" >&2; exit 2; }
case "$TIMEOUT" in ''|*[!0-9]*) echo "tmux-pane-keys: --timeout takes whole seconds" >&2; exit 2 ;; esac

[ "$((10#$TIMEOUT))" -gt 0 ] || { echo "tmux-pane-keys: --timeout must be positive" >&2; exit 2; }
source "$(dirname "${BASH_SOURCE[0]}")/bounded-wait.sh"
if [ -z "$WORK" ]; then
  [ -n "$PY" ] || { echo "tmux-pane-keys: need --python <interpreter> for the guard" >&2; exit 2; }
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  exec "$PY" "$DIR/tmux_pane_keys.py" run "$SOCK" "${ARGS[@]}"
fi
TIMEOUT_FLAG="$WORK/timed-out"

# Output goes to files, never to a pipe anyone waits on: a client killed while its server is
# stopped leaves its fds with that server, which would hold a pipe open past the bound.
bounded() {
  local rc
  rm -f "$TIMEOUT_FLAG"
  run_bounded "$TIMEOUT" "$TIMEOUT_FLAG" -- "$TMUX_BIN" -u -S "$SOCK" "$@" > "$WORK/out" 2> "$WORK/err" < /dev/null
  rc=$?
  if [ -e "$TIMEOUT_FLAG" ]; then
    echo "tmux-pane-keys: $1 to $TARGET timed out after ${TIMEOUT}s" >&2
    return 124
  fi
  return "$rc"
}

# tmux single-quoted word: literal; an embedded ' closes, is double-quoted, reopens.
tmux_quote() { local sq="'\"'\"'"; printf "'%s'" "${1//\'/$sq}"; }

# A queued request outlives its client: it sends only if it claims this one-time ticket and the guard ($PPID,
# same start time read in a fixed locale and zone, not a zombie) is alive after the claim; else orphaned.
TICKET="$WORK/ticket"
STARTED="$(LC_ALL=C TZ=UTC ps -o lstart= -p "$PPID" 2>/dev/null)"; STARTED="${STARTED%"${STARTED##*[! ]}"}"
[ -n "$STARTED" ] || { echo "tmux-pane-keys: cannot read the guard's start time" >&2; exit 1; }
ORPHAN="mv $(tmux_quote "$TICKET.claimed") $(tmux_quote "$TICKET.orphaned")"
CLAIM="mv $(tmux_quote "$TICKET") $(tmux_quote "$TICKET.claimed") && case \"\$(LC_ALL=C TZ=UTC ps -o lstart=,stat= -p $PPID 2>/dev/null)\" in \"$STARTED\"*Z*) $ORPHAN; false ;; \"$STARTED\"*) true ;; *) $ORPHAN; false ;; esac"

if [ -n "$FENCED" ]; then
  # The compare and the action are one server command: the predicate is evaluated against the pane
  # -t selects, and a false predicate orphans the ticket and fails, so nothing reaches a replacement.
  SEND="if-shell -F -t $(tmux_quote "$TARGET") $(tmux_quote "$FENCE_PRED") $(tmux_quote "$FENCE_ACTION") $(tmux_quote "run-shell $(tmux_quote "$ORPHAN")")"
else
  # Hooks may run between the commands of a list, so the send is withheld (and its claim orphaned)
  # when the pane is back in a mode at the moment of sending.
  KEYS="send-keys -t $(tmux_quote "$TARGET")"
  for key in "$@"; do
    # As argv, a trailing ';' ends the command and a trailing '\;' is a literal ';'.
    case "$key" in *'\;') key="${key%\\;};" ;; *';') key="${key%;}" ;; esac
    KEYS="$KEYS $(tmux_quote "$key")"
  done
  SEND="copy-mode -q -t $(tmux_quote "$TARGET") ; if-shell -F -t $(tmux_quote "$TARGET") '#{pane_in_mode}' $(tmux_quote "run-shell $(tmux_quote "$ORPHAN")") $(tmux_quote "$KEYS")"
fi
printf '%s\n' "$SEND" > "$WORK/cmd"
bounded if-shell "$CLAIM" "$SEND"; rc=$?
# Withheld is read from the orphaned ticket: a failing run-shell would put the pane in view mode (tmux < 3.5).
[ -z "$FENCED" ] || [ "$rc" != 0 ] || [ ! -e "$TICKET.orphaned" ] || rc=1
cat "$WORK/out"; cat "$WORK/err" >&2
exit "$rc"
