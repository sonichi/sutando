#!/bin/bash
# External task-file-injection notifier for the Claude Code core, matching
# Codex/agy's tmux-injection shape — a standby path alongside self-arm via Monitor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
  TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
else
  TASKS_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/tasks"
fi
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$(dirname "$TASKS_DIR")/results}"
CORE_STATUS_FILE="${SUTANDO_CORE_STATUS_FILE:-$(dirname "$TASKS_DIR")/state/core-status.json}"
CORE_STATUS_STALE_SEC=90
# shellcheck source=../../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
NOTIFIER_PY="$(require_python "$REPO" "resolve task priority and pane state")" || exit 1
DISPATCH_PY="$REPO/src/delivery/task_dispatch.py"
POLL_INTERVAL="${SUTANDO_NOTIFIER_POLL_INTERVAL:-0.5}"
COMPLETION_TIMEOUT="${SUTANDO_NOTIFIER_COMPLETION_TIMEOUT:-3600}"
CORE_READY_TIMEOUT="${SUTANDO_NOTIFIER_CORE_READY_TIMEOUT:-300}"
# Submit verification: re-press C-m while the prompt is still staged in the
# composer and no result has appeared. See deliver_prompt.
SUBMIT_RETRIES="${SUTANDO_NOTIFIER_SUBMIT_RETRIES:-6}"
SUBMIT_CONFIRM_TIMEOUT="${SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT:-5}"
watcher_pid=""
event_dir=""

stop_watcher() {
  [ -n "$watcher_pid" ] || return 0
  kill -TERM "-$watcher_pid" 2>/dev/null || kill -TERM "$watcher_pid" 2>/dev/null || true
  wait "$watcher_pid" 2>/dev/null || true
  watcher_pid=""
}

cleanup_notifier() {
  stop_watcher
  if [ -n "$event_dir" ]; then
    rm -f "$event_dir/events"
    rmdir "$event_dir" 2>/dev/null || true
  fi
}
trap cleanup_notifier EXIT
trap 'exit 0' HUP INT TERM

log_notifier() {
  local msg="task-notifier: $*" dir
  dir="$(dirname "$TASKS_DIR")/logs"
  [ -d "$dir" ] && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$msg" >>"$dir/claude-task-notifier.log" 2>/dev/null
  printf '%s\n' "$msg" >&2
}

# Completion detection and the priority-ordered pick are
# src/delivery/task_dispatch.py's contract, shared with Codex and agy.
has_result() {
  "$NOTIFIER_PY" "$DISPATCH_PY" has-result "$RESULTS_DIR" "$1"
}

next_pending_task() {
  "$NOTIFIER_PY" "$DISPATCH_PY" next-pending "$TASKS_DIR" "$RESULTS_DIR"
}

# Claude's footer adds "esc to interrupt" for any in-flight turn (tool or
# text); core-input-watch.py's gate signatures don't cover this (a running
# turn isn't a gate), so it stays this adapter's own signal.
core_pane_is_busy() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 0
  printf '%s\n' "$pane" | tail -12 | grep -Fq 'esc to interrupt'
}

# Delegates gate/idle-footer classification to core-input-watch.py's
# _is_idle_ready — the shared owner of Claude's pane-state patterns
# (trust/login/selection/permission gates) already maintained for the M0-M4
# core supervisor. Reimplementing that pattern list here would be exactly
# the duplicated-policy defect CLAUDE.md's architecture rules call out.
core_pane_is_idle_ready() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 1
  [ -n "$pane" ] || return 1
  core_pane_is_busy && return 1
  printf '%s' "$pane" | "$NOTIFIER_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("core_input_watch", sys.argv[1])
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)
sys.exit(0 if ciw._is_idle_ready(sys.stdin.read()) else 1)
' "$REPO/src/core-input-watch.py"
}

# Claude's live core self-reports to core-status.json (CLAUDE.md's "Work
# Status" convention); trust that first and use the pane only to catch a
# stale or wrong self-report, mirroring Codex's core_is_idle() exactly.
core_is_idle() {
  local now status_ts
  [ -f "$CORE_STATUS_FILE" ] || return 1
  grep -Eq '"status"[[:space:]]*:[[:space:]]*"idle"' "$CORE_STATUS_FILE" 2>/dev/null \
    && ! core_pane_is_busy && return 0
  grep -Eq '"status"[[:space:]]*:[[:space:]]*"running"' "$CORE_STATUS_FILE" 2>/dev/null \
    || return 1
  status_ts="$(sed -n 's/.*"ts"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CORE_STATUS_FILE" \
    | head -1)"
  [ -n "$status_ts" ] || return 1
  now="$(date +%s)"
  [ $((now - status_ts)) -gt "$CORE_STATUS_STALE_SEC" ] \
    && core_pane_is_idle_ready
}

wait_for_core_idle() {
  local started
  started="$(date +%s)"
  while ! core_is_idle; do
    if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
      return 1
    fi
    if [ $(( $(date +%s) - started )) -ge "$CORE_READY_TIMEOUT" ]; then
      log_notifier "core did not become idle within ${CORE_READY_TIMEOUT}s"
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# A wide tail: our newline-free prompt wraps across several rows at default
# pane width (agy caught this live — a narrow tail missed the marker).
prompt_is_staged() {
  local pane tail
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 1
  tail="$(printf '%s\n' "$pane" | sed '/^[[:space:]]*$/d' | tail -20)"
  printf '%s\n' "$tail" | grep -Fq "Sutando task ready: $1"
}

# Type + verify staged, then C-m + verify submitted; both halves retry.
# A core that stays non-idle is a hard gate — give up rather than risk
# typing over a live turn (see PR body for why this differs from Codex).
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 attempt=0 waited staged=0
  if ! wait_for_core_idle; then
    log_notifier "core did not become idle for $filename; leaving it queued"
    return 1
  fi
  while :; do
    tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" -l -- "$prompt"
    sleep "$POLL_INTERVAL"
    if prompt_is_staged "$filename"; then staged=1; break; fi
    type_tries=$((type_tries + 1))
    [ "$type_tries" -ge 2 ] && break
    log_notifier "typed prompt for $filename did not stage; re-typing (2/2)"
    sleep "$POLL_INTERVAL"
  done
  [ "$staged" = 1 ] && [ "$type_tries" -gt 0 ] \
    && log_notifier "prompt staged for $filename after $((type_tries + 1)) attempts"
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" C-m
  # Nothing observable staged: the submit is sent and unverifiable — never
  # re-press C-m blind into a live session.
  [ "$staged" = 1 ] || return 0
  while :; do
    waited=0
    while [ "$waited" -lt "$SUBMIT_CONFIRM_TIMEOUT" ]; do
      # Confirmed by the pane going BUSY, not by the marker vanishing — the
      # submitted prompt stays visible as scrollback, so that check always
      # read "still staged" (caught live: 3 spurious re-presses on a real
      # Claude session before this fix — see PR body).
      if has_result "$filename" || core_pane_is_busy; then
        [ "$attempt" -gt 0 ] && log_notifier "submit confirmed for $filename after $((attempt + 1)) attempts"
        return 0
      fi
      sleep 1
      waited=$((waited + 1))
    done
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$SUBMIT_RETRIES" ]; then
      log_notifier "submit NOT confirmed for $filename after $attempt attempts; prompt still staged (core may need attention)"
      return 0
    fi
    log_notifier "prompt still staged after C-m for $filename; re-pressing (attempt $((attempt + 1))/$SUBMIT_RETRIES)"
    tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" C-m
  done
}

submit_task() {
  local filename="$1" prompt started
  case "$filename" in
    ""|*/*|*..*) return 0 ;;
  esac
  has_result "$filename" && return 0
  prompt="Sutando task ready: $filename. Read $TASKS_DIR/$filename, follow CLAUDE.md, complete the task, and write the result to $RESULTS_DIR/$filename."
  if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
    log_notifier "no session $SESSION — dropping $filename"
    return 0
  fi
  deliver_prompt "$filename" "$prompt" || return 0
  started="$(date +%s)"
  while ! has_result "$filename"; do
    tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || return 0
    if [ $(( $(date +%s) - started )) -ge "$COMPLETION_TIMEOUT" ]; then
      log_notifier "timed out waiting for result: $filename"
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done
}

if [ "${1:-}" = "--event" ]; then
  [ -n "${2:-}" ] || { echo "task-notifier: --event requires a filename" >&2; exit 2; }
  submit_task "$2"
  exit 0
fi

event_dir="$(mktemp -d "${TMPDIR:-/tmp}/sutando-claude-task-notifier.XXXXXX")"
mkfifo "$event_dir/events"
"$NOTIFIER_PY" -c \
  'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1], sys.argv[2]])' \
  "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" > "$event_dir/events" &
watcher_pid=$!

while IFS= read -r event; do
  case "$event" in
    "TASK_FILE: "*)
      # Watcher output is a wake signal, not queue order. While the core is
      # busy, keep every task durable on disk; once idle, re-scan and pick
      # by priority (FIFO within a tier).
      next_pending_task >/dev/null || continue
      wait_for_core_idle || exit 1
      filename="$(next_pending_task)" || continue
      submit_task "$filename"
      ;;
  esac
done < "$event_dir/events"
