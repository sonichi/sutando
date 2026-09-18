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
# Same base + suffix as watch-tasks-stream.sh's own CLAIMS_DIR (its
# SUTANDO_WORKSPACE_DIR override, honored here too, so both agree on the
# same directory under a test override) -- a task claimed must-handle by a
# required task-event handler must never reach this unrestricted live core,
# whichever unrelated task's wake triggered the rescan.
CLAIMS_DIR="${SUTANDO_WORKSPACE_DIR:-$(dirname "$TASKS_DIR")}/state/task-event-handler-claims"
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
  "$NOTIFIER_PY" "$DISPATCH_PY" next-pending "$TASKS_DIR" "$RESULTS_DIR" --claims-dir "$CLAIMS_DIR"
}

# "esc to interrupt" covers any in-flight turn; core-input-watch.py's gate
# signatures don't (a running turn isn't a gate), so this stays local.
core_pane_is_busy() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 0
  printf '%s\n' "$pane" | tail -12 | grep -Fq 'esc to interrupt'
}

# Delegates gate/idle-footer classification to core-input-watch.py's
# _is_idle_ready, the shared owner of Claude's pane-state patterns.
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

# Delegates to core-input-watch.py's _composer_is_empty -- idle-ready and an
# unsent owner draft in the composer are not mutually exclusive; typing over
# one would submit a mix of the draft and our prompt, or clobber the draft.
core_pane_composer_is_empty() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 1
  [ -n "$pane" ] || return 1
  printf '%s' "$pane" | "$NOTIFIER_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("core_input_watch", sys.argv[1])
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)
sys.exit(0 if ciw._composer_is_empty(sys.stdin.read()) else 1)
' "$REPO/src/core-input-watch.py"
}

# Trust core-status.json, pane only to catch a stale/wrong self-report.
# Both branches require POSITIVE idle-readiness -- "not busy" alone admits a gate.
core_is_idle() {
  local now status_ts
  [ -f "$CORE_STATUS_FILE" ] || return 1
  grep -Eq '"status"[[:space:]]*:[[:space:]]*"idle"' "$CORE_STATUS_FILE" 2>/dev/null \
    && core_pane_is_idle_ready && return 0
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
capture_tail() {
  tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null \
    | sed '/^[[:space:]]*$/d' | tail -20
}

# The tail suffix from the LAST ❯ prompt line onward -- the live composer.
# A stale copy of our own marker sitting in OLDER scrollback (above the
# current composer, e.g. an already-submitted prior attempt) must never be
# mistaken for freshly-staged content. No ❯ line at all returns the whole
# tail (conservative fallback). Mirrors core-input-watch.py's
# _composer_from_tail -- kept local in bash since it operates on a tail this
# script already captured, not on a live pane read.
composer_from_tail() {
  awk '
    /^[[:space:]]*❯/ { start = NR }
    { line[NR] = $0 }
    END { for (i = (start ? start : 1); i <= NR; i++) print line[i] }
  ' <<<"$1"
}

# Staged = OUR marker is in the LIVE COMPOSER (not merely somewhere in the
# tail — a stale copy already in scrollback above the composer must not
# count) AND the tail changed since $2, a baseline from before this
# episode's send-keys.
prompt_is_staged() {
  local tail="$1" baseline="$2"
  composer_from_tail "$tail" | grep -Fq "Sutando task ready: $3" && [ "$tail" != "$baseline" ]
}

# Type + verify staged, then C-m + verify submitted; both halves retry.
# A core that stays non-idle is a hard gate -- give up rather than type over a live turn.
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 attempt=0 waited staged=0
  local baseline staged_tail
  if ! wait_for_core_idle; then
    log_notifier "core did not become idle for $filename; leaving it queued"
    return 1
  fi
  while :; do
    # Require an empty composer before typing -- idle-ready and an unsent
    # owner draft are not mutually exclusive; typing over one mixes our
    # prompt into theirs. Re-checked on every retype, not just the first.
    if ! core_pane_composer_is_empty; then
      log_notifier "composer not empty for $filename; leaving it queued (failing closed, not typing over a draft)"
      return 1
    fi
    baseline="$(capture_tail)"
    tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" -l -- "$prompt"
    sleep "$POLL_INTERVAL"
    staged_tail="$(capture_tail)"
    if prompt_is_staged "$staged_tail" "$baseline" "$filename"; then staged=1; break; fi
    type_tries=$((type_tries + 1))
    [ "$type_tries" -ge 2 ] && break
    log_notifier "typed prompt for $filename did not stage; re-typing (2/2)"
    sleep "$POLL_INTERVAL"
  done
  # Nothing observably staged: never press Enter blind into a live session --
  # that would submit whatever the composer actually holds, ours or not.
  if [ "$staged" != 1 ]; then
    log_notifier "prompt for $filename never verifiably staged after $((type_tries + 1)) attempts; not pressing Enter (failing closed)"
    return 0
  fi
  [ "$type_tries" -gt 0 ] \
    && log_notifier "prompt staged for $filename after $((type_tries + 1)) attempts"
  # Re-verify the exact composer content immediately before THIS Enter too --
  # same discipline the retry loop below already applies to every later one.
  if [ "$(capture_tail)" != "$staged_tail" ]; then
    log_notifier "pane changed since $filename staged; not pressing Enter (failing closed, core may need attention)"
    return 0
  fi
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" C-m
  while :; do
    waited=0
    while [ "$waited" -lt "$SUBMIT_CONFIRM_TIMEOUT" ]; do
      # Confirmed by the pane going BUSY, never by the marker vanishing --
      # the submitted prompt stays visible as scrollback, so it never does.
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
    # Re-validate our prompt is STILL the exact composer content before
    # pressing C-m again -- a changed pane means this Enter is not ours.
    if [ "$(capture_tail)" != "$staged_tail" ]; then
      log_notifier "pane changed since $filename staged; not re-pressing C-m (failing closed, core may need attention)"
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
      # A wake signal, not queue order -- once idle, re-scan and pick by
      # priority so every task stays durable on disk until then.
      next_pending_task >/dev/null || continue
      wait_for_core_idle || exit 1
      filename="$(next_pending_task)" || continue
      submit_task "$filename"
      ;;
  esac
done < "$event_dir/events"
