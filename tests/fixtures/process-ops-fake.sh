#!/bin/bash
# A recording, inert implementation of the src/process-ops.sh interface.
# Sourced INSTEAD of the real one via SUTANDO_PROCESS_OPS, so a restart.sh run
# under it cannot signal, kill, launchctl or tmux anything on the host.
#
# POPS_LOG           every call, one per line, in order
# POPS_ALIVE_PIDS    space-delimited pids that answer pops_alive
# POPS_ARGV_<pid>    the argv pops_argv reports for that pid
# POPS_ELAPSED_<pid> the `ps -o etime=` string for that pid (unset = unreadable)
# POPS_SIGNAL_RC     rc pops_signal returns (default 0 = delivered)
# POPS_SIGNAL_SURVIVORS  pids that keep answering pops_alive after a signal
# POPS_LAUNCHCTL_PRINT_RC   rc for `pops_launchctl print` (default 1 = no job)

_pops_rec() { printf '%s\n' "$*" >> "${POPS_LOG:-/dev/null}"; }

# A signalled pid stops answering pops_alive unless the fixture declares it a
# survivor — a stop that leaves the process up is what a failed stop looks like.
pops_signal()          { _pops_rec "signal $1 ${2:-TERM}"
                         [ "${POPS_SIGNAL_RC:-0}" = "0" ] || return "${POPS_SIGNAL_RC}"
                         case " ${POPS_SIGNAL_SURVIVORS:-} " in *" $1 "*) return 0 ;; esac
                         POPS_ALIVE_PIDS="$(printf '%s' " ${POPS_ALIVE_PIDS:-} " | sed "s/ $1 / /g")"
                         return 0; }
pops_alive()           { _pops_rec "alive $1"
                         case " ${POPS_ALIVE_PIDS:-} " in *" $1 "*) return 0 ;; esac; return 1; }
pops_argv()            { _pops_rec "argv $1"; eval "printf '%s' \"\${POPS_ARGV_$1:-}\""; }
pops_elapsed()         { _pops_rec "elapsed $1"; eval "printf '%s' \"\${POPS_ELAPSED_$1:-}\""; }
pops_grace_tick()      { _pops_rec "grace_tick"; }
pops_pattern_kill()    { _pops_rec "pattern_kill $1"; }
pops_pattern_running() { _pops_rec "pattern_running $1"; return 1; }
pops_name_running()    { _pops_rec "name_running $1"; return 1; }
pops_launchctl()       { _pops_rec "launchctl $*"
                         [ "$1" = "print" ] && return "${POPS_LAUNCHCTL_PRINT_RC:-1}"; return 0; }
pops_tmux()            { _pops_rec "tmux $*"; return 0; }
pops_port_listening()  { _pops_rec "port_listening $1"; return 0; }
