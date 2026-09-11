#!/bin/bash
# A recording, inert implementation of the src/process-ops.sh interface.
# Sourced INSTEAD of the real one via SUTANDO_PROCESS_OPS, so a restart.sh run
# under it cannot signal, kill, launchctl or tmux anything on the host.
#
# POPS_LOG           every call, one per line, in order
# POPS_ALIVE_PIDS    space-delimited pids that answer pops_alive
# POPS_ARGV_<pid>    the argv pops_argv reports for that pid
# POPS_LAUNCHCTL_PRINT_RC   rc for `pops_launchctl print` (default 1 = no job)

_pops_rec() { printf '%s\n' "$*" >> "${POPS_LOG:-/dev/null}"; }

pops_signal()          { _pops_rec "signal $1 ${2:-TERM}"; }
pops_alive()           { _pops_rec "alive $1"
                         case " ${POPS_ALIVE_PIDS:-} " in *" $1 "*) return 0 ;; esac; return 1; }
pops_argv()            { _pops_rec "argv $1"; eval "printf '%s' \"\${POPS_ARGV_$1:-}\""; }
pops_pattern_kill()    { _pops_rec "pattern_kill $1"; }
pops_pattern_running() { _pops_rec "pattern_running $1"; return 1; }
pops_name_running()    { _pops_rec "name_running $1"; return 1; }
pops_launchctl()       { _pops_rec "launchctl $*"
                         [ "$1" = "print" ] && return "${POPS_LAUNCHCTL_PRINT_RC:-1}"; return 0; }
pops_tmux()            { _pops_rec "tmux $*"; return 0; }
pops_port_listening()  { _pops_rec "port_listening $1"; return 0; }
