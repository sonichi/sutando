#!/usr/bin/env bash
# The task-watch Mod is installed only on an explicit per-launcher opt-in and a Claude Code at
# or above the Mod's own floor; the session marker says which watcher path the session is on.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2${3:+ -- $3}"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
MOD="$REPO/src/agent/claude/task-watch-mod"
MIN="$(sed -n "s/^export const MIN_CLAUDE_VERSION = '\([0-9.]*\)'.*/\1/p" "$MOD/hooks/version.ts")"
[ -n "$MIN" ]; check $? "the launchers can read the Mod's floor from hooks/version.ts ($MIN)"
grep -q "MIN_CLAUDE_VERSION" "$MOD/hooks/register.ts"; check $? "the Mod checks the same constant itself"

# A stub `claude` answering --version with $STUB_VERSION; lsof/launchctl/pgrep report nothing.
mkdir -p "$TMP/bin"
printf '#!/bin/sh\n[ "$1" = --version ] && echo "$STUB_VERSION (Claude Code)"\nexit 0\n' > "$TMP/bin/claude"
for t in lsof launchctl pgrep; do printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/$t"; done
# A stub tmux: its global PATH is $STUB_PANE_PATH when set, and new-session records its argv.
cat > "$TMP/bin/tmux" <<'SH'
#!/bin/sh
case " $* " in
  *" show-environment -g PATH "*) [ -n "$STUB_PANE_PATH" ] && { echo "PATH=$STUB_PANE_PATH"; exit 0; } ;;
  *" new-session "*) printf '%s\n' "$@" > "$STUB_TMUX_LOG"; exit 0 ;;
esac
exit 1
SH
chmod +x "$TMP"/bin/*
# An older claude the tmux pane's PATH finds first, while the caller's PATH finds the newer stub.
mkdir -p "$TMP/old"
printf '#!/bin/sh\n[ "$1" = --version ] && echo "2.1.200 (Claude Code)"\nexit 0\n' > "$TMP/old/claude"
chmod +x "$TMP/old/claude"
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# add_task_watch_mod <flag>, with STUB_VERSION; prints "<TASK_WATCH_MOD>|<SURFACE_ARGS>" and stderr to $TMP/err.
helper() {
  env -i HOME="$HOME" PATH="$STUB_PATH" STUB_VERSION="$1" REPO="$REPO" FLAG="$2" bash -c '
    source "$REPO/src/agent/claude/cli/session-launch.sh"
    SURFACE_ARGS=(--x)
    add_task_watch_mod "$FLAG"
    printf "%s|%s" "$TASK_WATCH_MOD" "${SURFACE_ARGS[*]}"' 2>"$TMP/err"
}

[ "$(helper 9.9.9 "")" = "|--x" ]; check $? "no flag: nothing installed, marker empty"
[ "$(helper 9.9.9 0)" = "|--x" ]; check $? "flag 0: nothing installed"
[ "$(helper "$MIN" 1)" = "1|--x --plugin-dir $MOD" ]; check $? "flag 1 at the floor: the Mod's --plugin-dir and marker 1"
[ "$(helper 2.1.302 1)" = "|--x" ] && grep -q "keeps the Monitor watcher" "$TMP/err"
check $? "flag 1 below the floor: nothing installed, one line says the Monitor stays" "$(cat "$TMP/err")"
[ "$(helper "" 1)" = "|--x" ]; check $? "flag 1 with no readable version: nothing installed"

# The gate checks the claude the session runs, not the caller's: the pane's PATH decides.
pane() {  # pane <pane PATH>; prints "<TASK_WATCH_MOD>|<CLAUDE_BIN>|<tmux new-session argv>"
  env -i HOME="$HOME" PATH="$STUB_PATH" STUB_VERSION=9.9.9 STUB_PANE_PATH="$1" STUB_TMUX_LOG="$TMP/tmux.log" \
    REPO="$REPO" bash -c '
    source "$REPO/src/agent/claude/cli/session-launch.sh"
    TMUX_SOCKET="$HOME/no.sock"; SESSION=sutando-test; SURFACE_ARGS=(); BOOT_PROMPT=/startup
    claude_named_session_running() { return 0; }
    rm -f "$STUB_TMUX_LOG"
    add_task_watch_mod 1
    launch_claude_session >/dev/null 2>&1
    printf "%s|%s|%s" "$TASK_WATCH_MOD" "${CLAUDE_BIN:-}" "$(tr "\n" " " < "$STUB_TMUX_LOG" 2>/dev/null)"' 2>/dev/null
}
out="$(pane "$TMP/old:$TMP/bin:/usr/bin:/bin")"
[ "${out%%|*}" = "" ]; check $? "an older claude first on the pane's PATH keeps the Monitor, though the caller's claude is new" "$out"
case "$out" in *"|$TMP/old/claude|"*" $TMP/old/claude --name sutando-test "*) r=0 ;; *) r=1 ;; esac
check "$r" "the gate and the launch use the same absolute claude, the one the pane resolves" "$out"
out="$(pane "$TMP/bin:$TMP/old:/usr/bin:/bin")"
case "$out" in "1|$TMP/bin/claude|"*" $TMP/bin/claude --name sutando-test "*"--plugin-dir $MOD "*) r=0 ;; *) r=1 ;; esac
check "$r" "a new claude first on the pane's PATH installs the Mod and is the one launched" "$out"

# The worker launcher's env probe.
WORKER_ENV=(SUTANDO_INSTANCE_ID=w-test SUTANDO_TMUX_SESSION=sutando-worker-w-test)
worker_env() { env -i HOME="$HOME" PATH="$STUB_PATH" STUB_VERSION=9.9.9 "${WORKER_ENV[@]}" "$@" \
  bash "$REPO/skills/worker-pool/scripts/launch-worker-session.sh" --print-env 2>/dev/null; }
out="$(worker_env)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD="; check $? "a worker is launched with the marker explicitly empty by default"
out="$(worker_env SUTANDO_TASK_WATCH_MOD_WORKER=1)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD=1"; check $? "SUTANDO_TASK_WATCH_MOD_WORKER=1 marks the worker's session"
out="$(worker_env SUTANDO_TASK_WATCH_MOD_CORE=1)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD="; check $? "the core's flag does not switch a worker"
[ "$(echo "$out" | grep -c '^SUTANDO_TASK_WATCH_MOD=')" = 1 ]; check $? "the worker marker appears once"

# The core launcher's env probe.
core_env() { env -i HOME="$HOME" PATH="$STUB_PATH" STUB_VERSION=9.9.9 "$@" \
  bash "$REPO/src/agent/claude/cli/start-cli.sh" --print-core-env 2>/dev/null; }
out="$(core_env)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD="; check $? "the core is off by default, marker explicitly empty"
out="$(core_env SUTANDO_TASK_WATCH_MOD_WORKER=1)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD="; check $? "the worker flag does not switch the core"
out="$(core_env SUTANDO_TASK_WATCH_MOD_CORE=1)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD=1"; check $? "only SUTANDO_TASK_WATCH_MOD_CORE=1 switches the core"
out="$(core_env SUTANDO_TASK_WATCH_MOD_CORE=1 STUB_VERSION=2.1.0)"
echo "$out" | grep -qx "SUTANDO_TASK_WATCH_MOD="; check $? "the core keeps the Monitor below the floor"

echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
