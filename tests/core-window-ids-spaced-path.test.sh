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

# The CLIs under a spaced path exist on this machine, as the real ones do.
A="$T/Library/Application Support/app"
mkdir -p "$A/engine/sutando/src/agent/codex/app-server" "$A/bin" "$T/my tools"
touch "$A/engine/sutando/src/agent/codex/app-server/codex-seat" "$A/bin/codex" "$T/my tools/claude"
SPACED="@0 0 \"$A/engine/sutando/src/agent/codex/app-server/codex-seat\" --host \"$A/state/host.json\" --seat sutando-core
@1 0 \"exec env -u AG2_IDENTITY_PINNED python3 gateway.py\"
@2 1 \"$A/bin/codex\" --yolo
@3 0 /usr/local/bin/claude --model opus
@4 0 \"$T/my tools/claude\""

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
R="$(TMUX_ROWS="$SPACED" ids live python3 env Support Application app-server host.json gateway.py)"
[ -z "$R" ] && ok "6 a later word of a quoted path or command line is never the command" || fail "6" "[$R]"
TMUX_FAIL=1 sutando_core_window_ids /sock sess live codex-seat > /dev/null 2>&1
[ $? -ne 0 ] && ok "7 unreadable panes still fail the lookup" || fail "7" "rc 0"
R="$(TMUX_ROWS='@5 0 "claude --model opus"
@6 0 "/usr/local/bin/codex --yolo"' ids live claude codex)"
[ "$R" = "@5 @6 " ] && ok "8 a command line tmux quoted as one string is read by its first word, as before" || fail "8" "[$R]"
# A restart closes the windows this names: a sibling whose command line only ENDS in a path to a core
# CLI (a wrapper, an editor, a tail on its log) is not that CLI and must never be named.
R="$(TMUX_ROWS="@7 0 \"python3 /opt/tools/wrap.py /usr/local/bin/codex\"
@8 0 \"tail -f $A/bin/codex\"
@9 0 \"vim /usr/local/bin/claude\"
@10 0 \"sh -c $A/engine/sutando/src/agent/codex/app-server/codex-seat\"" ids all codex claude codex-seat)"
[ -z "$R" ] && ok "9 a command line that ends in a core CLI's path is not that CLI" || fail "9" "[$R]"
R="$(TMUX_ROWS="@11 0 \"$T/no such dir/codex\" --yolo" ids live codex)"
[ -z "$R" ] && ok "10 a quoted path that is no file here is read by its first word, and matches nothing" || fail "10" "[$R]"

echo; [ $fails -eq 0 ] && echo "core-window-ids-spaced-path: all 10 checks pass" || { echo "core-window-ids-spaced-path: $fails FAILED"; exit 1; }
