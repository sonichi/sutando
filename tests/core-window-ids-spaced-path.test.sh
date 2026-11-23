#!/bin/bash
# sutando_core_window_ids finds the core's window when its CLI lives under a path with a space.
#
# Live 2026-11-23 (test VM, desktop-hosted Codex core): the seat's pane start command was
#   "/Users/x/Library/Application Support/<app>/engine/sutando/src/agent/codex/app-server/codex-seat" --host "..."
# The lookup split the row on spaces, took `"/Users/x/Library/Application` for the command, and
# found no seat, so switch-model.sh answered "no Codex core is running" (exit 13) for a core that
# was running, and a restart would not have found the window it was to replace.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; ok() { echo "ok - $1"; }; fail() { echo "FAIL - $1: $2"; fails=$((fails + 1)); }

mkdir -p "$T/bin"
cat > "$T/bin/tmux" <<'TMUX'
#!/bin/bash
[ -n "${TMUX_FAIL:-}" ] && exit 1
printf '%s\n' "$TMUX_ROWS"
TMUX
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH"
# shellcheck source=../src/agent/core-window.sh
. "$HERE/src/agent/core-window.sh"
ids() { sutando_core_window_ids /sock sess "$@" | tr '\n' ' '; }

SPACED='@0 0 "/Users/x/Library/Application Support/app/engine/sutando/src/agent/codex/app-server/codex-seat" --host "/Users/x/Library/Application Support/app/state/host.json" --seat sutando-core
@1 0 "exec env -u AG2_IDENTITY_PINNED python3 gateway.py"
@2 1 "/Users/x/Library/Application Support/app/bin/codex" --yolo
@3 0 /usr/local/bin/claude --model opus
@4 0 "/opt/my tools/claude"'

R="$(TMUX_ROWS="$SPACED" ids live codex-seat)"
[ "$R" = "@0 " ] && ok "1 a seat under a spaced path is found" || fail "1" "[$R]"
R="$(TMUX_ROWS="$SPACED" ids live codex)"
[ -z "$R" ] && ok "2 live: a dead Codex pane is not a running core" || fail "2" "[$R]"
R="$(TMUX_ROWS="$SPACED" ids all codex)"
[ "$R" = "@2 " ] && ok "3 all: the dead pane under a spaced path is still named" || fail "3" "[$R]"
R="$(TMUX_ROWS="$SPACED" ids live claude)"
[ "$R" = "@3 @4 " ] && ok "4 an unquoted command, and a quoted one with no arguments, are both read" || fail "4" "[$R]"
R="$(TMUX_ROWS="$SPACED" ids live codex-seat codex claude)"
[ "$R" = "@0 @3 @4 " ] && ok "5 several CLIs at once" || fail "5" "[$R]"
R="$(TMUX_ROWS="$SPACED" ids live python3 env Support app-server host.json)"
[ -z "$R" ] && ok "6 a later word of a quoted path or command line is never the command" || fail "6" "[$R]"
R="$(TMUX_ROWS='@5 0 "claude --model opus"
@6 0 "/usr/local/bin/codex --yolo"' ids live claude codex)"
[ "$R" = "@5 @6 " ] && ok "8 a command line tmux quoted as one string is read by its first word, as before" || fail "8" "[$R]"
TMUX_FAIL=1 sutando_core_window_ids /sock sess live codex-seat > /dev/null 2>&1
[ $? -ne 0 ] && ok "7 unreadable panes still fail the lookup" || fail "7" "rc 0"

echo; [ $fails -eq 0 ] && echo "core-window-ids-spaced-path: all 8 checks pass" || { echo "core-window-ids-spaced-path: $fails FAILED"; exit 1; }
