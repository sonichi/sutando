# shellcheck shell=sh
# Sourced first by a fake tmux: turns src/tmux-pane-keys.sh's ticket-guarded send back into argv.
# Claims the ticket as tmux would, replays the mode exit as its own call, takes the send branch of the
# in-mode check (a fake pane is never in a mode), then restores each key exactly as it was passed.
if [ "${1:-}" = -S ] && [ "${3:-}" = if-shell ]; then
  _tfu_sock="$2"
  _tfu_mode="${5%% ; *}"
  _tfu_cmd="${5#copy-mode -q -t * ; }"
  sh -c "$4" || exit 0
  eval "set -- $_tfu_mode"
  "$0" -S "$_tfu_sock" "$@"
  eval "set -- $_tfu_cmd"
  eval "_tfu_cmd=\${$#}"
  eval "set -- $_tfu_cmd"
  _tfu_out=""
  for _tfu_k in "$@"; do
    case "$_tfu_k" in *';') _tfu_k="${_tfu_k%;}\\;" ;; esac
    _tfu_out="$_tfu_out '$(printf '%s' "$_tfu_k" | sed "s/'/'\\\\''/g")'"
  done
  eval "set -- -S \"\$_tfu_sock\" $_tfu_out"
fi
