#!/bin/bash
# The ONE seam through which restart.sh touches a process. Every signal, pattern
# kill, liveness probe, argv read, launchctl and tmux call goes through a
# `pops_*` function here and nowhere else, so a test can replace the whole layer
# at once instead of hoping a PATH stub shadows the right binary.
#
# SUTANDO_PROCESS_OPS=<file> sources that file INSTEAD of the real
# implementations below. A fake records calls and touches nothing; a caller that
# reaches past this interface escapes the fake, which is why
# tests/restart-process-ops-interface.test.sh greps the shipped restart.sh for
# bare process verbs and requires zero hits.

if [ -n "${SUTANDO_PROCESS_OPS:-}" ]; then
  if [ ! -r "$SUTANDO_PROCESS_OPS" ]; then
    echo "process-ops: SUTANDO_PROCESS_OPS=$SUTANDO_PROCESS_OPS is unreadable" >&2
    return 1 2>/dev/null || exit 1
  fi
  # shellcheck disable=SC1090
  . "$SUTANDO_PROCESS_OPS"
  return 0 2>/dev/null || exit 0
fi

# Send <sig> (default TERM) to exactly one pid.
pops_signal() { kill -"${2:-TERM}" "$1" 2>/dev/null; }

# EXISTENCE and permission only. A process answering this is not thereby ours:
# pids are reissued, so the caller must establish ownership by other means.
pops_alive() { kill -0 "$1" 2>/dev/null; }

# The full argv of one pid, empty when it cannot be read.
pops_argv() { ps -p "$1" -o args= 2>/dev/null; }

# Elapsed run time of one pid, `ps` format ([[DD-]HH:]MM:SS), empty when unread.
# src/watcher_sentinel.sh: a process younger than the sentinel did not write it.
pops_elapsed() { ps -p "$1" -o etime= 2>/dev/null; }

# One tick of the grace a signalled process gets to exit. Absolute, because a
# stubbed `sleep` on PATH would turn the wait into no wait at all.
pops_grace_tick() { /bin/sleep "${1:-0.1}"; }

# Host-wide pattern kill. Matches processes of every instance on the machine,
# so only an all-scope caller may use it, and never for a watcher.
pops_pattern_kill() { pkill -f "$1" 2>/dev/null; }

# True when any process matches <pattern> anywhere on the host.
pops_pattern_running() { pgrep -f "$1" >/dev/null 2>&1; }

# True when a process is named exactly <name> (never -f, which matches argv).
pops_name_running() { pgrep -x "$1" >/dev/null 2>&1; }

pops_launchctl() { launchctl "$@"; }

pops_tmux() { tmux "$@"; }

# True when something is LISTENing on <port> — a transient client connection on
# the same port is not a rebound server.
pops_port_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
