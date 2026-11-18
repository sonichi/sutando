#!/usr/bin/env bash
# A core the CLI re-exec'd as `claude.exe --resume <id>` (no --name) in a pane of the
# session is still that session's core: a restart must see it, or a second core starts.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; PIDS=""
SOCK="$T/tmux.sock"
trap 'tmux -S "$SOCK" kill-server 2>/dev/null; for p in $PIDS; do kill "$p" 2>/dev/null; done; rm -rf "$T"' EXIT
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
command -v tmux > /dev/null 2>&1 || { echo "session-launch-resumed-core: skipped (no tmux)"; exit 0; }
command -v cc > /dev/null 2>&1 || { echo "session-launch-resumed-core: skipped (no cc to build a process named claude)"; exit 0; }
BIN="$T/App Support/bin"; mkdir -p "$BIN"
printf '#include <unistd.h>\nint main(void){sleep(60);return 0;}\n' > "$T/c.c"
cc -o "$BIN/claude" "$T/c.c" && cp "$BIN/claude" "$BIN/claude.exe" || { fail "build" "cc failed"; exit 1; }

S="slr-$$"
tmux -S "$SOCK" new-session -d -s "$S" "$BIN/claude.exe" --resume 11111111-aaaa --settings x.json
tmux -S "$SOCK" new-window -d -t "$S" "$BIN/claude" --name "other-$S"
tmux -S "$SOCK" new-window -d -t "$S" sleep 60
tmux -S "$SOCK" new-session -d -s "$S-x" "$BIN/claude" --resume 22222222-bbbb
"$BIN/claude" --resume 33333333-cccc < /dev/null > /dev/null 2>&1 & PIDS="$PIDS $!"; outside=$!
sleep 0.5
resumed="$(tmux -S "$SOCK" list-panes -t "=$S:0" -F '#{pane_pid}')"
foreign="$(tmux -S "$SOCK" list-panes -t "=$S:1" -F '#{pane_pid}')"
other="$(tmux -S "$SOCK" list-panes -t "=$S-x:0" -F '#{pane_pid}')"

REPO="$HERE"; TMUX_SOCKET="$SOCK"; SESSION="$S"; . "$HERE/src/agent/claude/cli/session-launch.sh"
got=" $(claude_named_pids | tr '\n' ' ') "
case "$got" in *" $resumed "*) ok "1 the resumed core in the session's pane is the session's core" ;;
  *) fail "1" "resumed=$resumed got=$got" ;; esac
case "$got" in *" $foreign "*|*" $other "*|*" $outside "*) fail "2" "foreign=$foreign other=$other outside=$outside got=$got" ;;
  *) ok "2 a claude naming another session, another session's claude and one outside tmux are not" ;; esac
unset SUTANDO_TMUX_SOCKET
claude_named_session_running && ok "3 the session reads as running (shared socket): no heal beside it" || fail "3" "session read as dead"
SUTANDO_TMUX_SOCKET="$SOCK"
claude_named_session_running && ok "4 the session reads as running (private socket): no heal beside it" || fail "4" "session read as dead"
echo; [ $fails -eq 0 ] && echo "session-launch-resumed-core: all checks pass" || { echo "session-launch-resumed-core: $fails FAILED"; exit 1; }
