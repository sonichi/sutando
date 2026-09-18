#!/usr/bin/env bash
# Sourceable helper: take the per-pane writer lock (scripts/tmux-pane-lock.sh owns the path)
# on a caller-chosen fd, so a whole multi-key transaction is one writer's.
#   pane_lock_take <socket> <session> <fd> [timeout_s]   # timeout_s omitted/empty = wait
# Returns 0 holding the lock on <fd>; non-zero means NOT held — the caller must defer, never send.
pane_lock_take() {
  local _sock="${1:?socket}" _session="${2:?session}" _fd="${3:?fd}" _to="${4:-}" _dir _lock _py
  case "$_fd" in [3-9]|[1-9][0-9]) ;; *) echo "pane_lock_take: fd must be a small integer >=3, got '$_fd'" >&2; return 2;; esac
  _dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  _lock="$(bash "$_dir/tmux-pane-lock.sh" "$_sock" "$_session")" || { echo "pane_lock_take: could not derive the pane lock for '$_session' on $_sock" >&2; return 7; }
  _py="$(bash "$_dir/sutando-config.sh" python-bin)"
  [ -x "$_py" ] || { echo "pane_lock_take: python interpreter not found ($_py) — cannot lock, not sending" >&2; return 7; }
  eval "exec $_fd>\"\$_lock\"" 2>/dev/null || { echo "pane_lock_take: could not open the pane lock ($_lock)" >&2; return 7; }
  # src/tmux_pane_lock.py owns the acquisition; this file owns only the fd plumbing,
  # which cannot move -- a shell transaction must hold the lock in its own shell.
  "$_py" "$_dir/../src/tmux_pane_lock.py" --flock-fd "$_fd" --timeout "$_to" \
    || { eval "exec $_fd>&-"; return 1; }
  return 0
}

# Release a lock taken by pane_lock_take: closing the fd drops it.
pane_lock_release() { local _fd="${1:?fd}"; eval "exec $_fd>&-" 2>/dev/null || true; }
