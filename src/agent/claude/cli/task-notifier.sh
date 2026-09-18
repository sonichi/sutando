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
# ONE canonical workspace root for everything workspace-owned (claims, receipts,
# status, the handler probe): a separate task inbox must never redefine it.
WORKSPACE_DIR="${SUTANDO_WORKSPACE_DIR:-$(dirname "$TASKS_DIR")}"
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$WORKSPACE_DIR/results}"
CORE_STATUS_FILE="${SUTANDO_CORE_STATUS_FILE:-$WORKSPACE_DIR/state/core-status.json}"
# Same base + suffix as watch-tasks-stream.sh's own CLAIMS_DIR.
CLAIMS_DIR="$WORKSPACE_DIR/state/task-event-handler-claims"
CORE_STATUS_STALE_SEC=90
# shellcheck source=../../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
NOTIFIER_PY="$(require_python "$REPO" "resolve task priority and pane state")" || exit 1
DISPATCH_PY="$REPO/src/delivery/task_dispatch.py"
TASK_HANDLER_FALLBACKS_DIR="$("$NOTIFIER_PY" "$REPO/src/util_paths.py" handler-fallbacks-dir "$WORKSPACE_DIR/state")" || {
  echo "task-notifier: could not resolve the fallback receipt dir" >&2
  exit 1
}
# A long single-line prompt wraps past the pane's own height, scrolling its
# leading marker into scrollback -- capture-pane -p alone never sees it.
CAPTURE_SCROLLBACK_LINES="${SUTANDO_NOTIFIER_CAPTURE_SCROLLBACK_LINES:-2000}"
POLL_INTERVAL="${SUTANDO_NOTIFIER_POLL_INTERVAL:-0.5}"
COMPLETION_TIMEOUT="${SUTANDO_NOTIFIER_COMPLETION_TIMEOUT:-3600}"
CORE_READY_TIMEOUT="${SUTANDO_NOTIFIER_CORE_READY_TIMEOUT:-300}"
# Submit verification: re-press C-m while the prompt is still staged in the
# composer and no result has appeared. See deliver_prompt.
SUBMIT_RETRIES="${SUTANDO_NOTIFIER_SUBMIT_RETRIES:-6}"
SUBMIT_CONFIRM_TIMEOUT="${SUTANDO_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT:-5}"
# A queued task with nothing left to re-trigger it (composer busy, staging
# failed) would otherwise wait forever for an unrelated wake. See the main loop.
RETRY_POLL_SEC="${SUTANDO_NOTIFIER_RETRY_POLL_SEC:-30}"
watcher_pid=""
event_dir=""

# A FRESH per-candidate probe, not a claims-dir file's existence, so a
# required handler that hasn't published its claim yet still blocks dispatch.
probe_optional_task_handler() {
  local filename="$1" rc
  [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ] || return 3
  [ -x "$SUTANDO_TASK_EVENT_HANDLER" ] || return 3
  "$SUTANDO_TASK_EVENT_HANDLER" \
    --runtime claude \
    --workspace "$WORKSPACE_DIR" \
    --task-file "$TASKS_DIR/$filename" \
    --results-dir "$RESULTS_DIR" \
    --repo "$REPO" \
    --probe >/dev/null
  rc=$?
  if [ "$rc" -eq 4 ]; then
    # Required Team handlers are watcher-owned and must never reach the live core.
    return 0
  fi
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
    echo "task-notifier: optional task handler probe failed for $filename (exit $rc); falling back to live core" >&2
    return 3
  fi
  return "$rc"
}

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
  dir="$WORKSPACE_DIR/logs"
  [ -d "$dir" ] && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$msg" >>"$dir/claude-task-notifier.log" 2>/dev/null
  printf '%s\n' "$msg" >&2
}

# Completion detection and the priority-ordered pick are
# src/delivery/task_dispatch.py's contract, shared with Codex and agy.
has_result() {
  local filename="$1"
  "$NOTIFIER_PY" "$DISPATCH_PY" has-result "$RESULTS_DIR" "$filename" || return 1
  rm -f "$TASK_HANDLER_FALLBACKS_DIR/$filename"
  return 0
}

# Priority/completion/live-claims come from task_dispatch; each candidate
# also gets a fresh probe here, since a claims-dir snapshot can't see a claim not yet published.
next_pending_task() {
  local candidate
  while IFS= read -r candidate; do
    if [ ! -f "$TASK_HANDLER_FALLBACKS_DIR/$candidate" ] \
        && probe_optional_task_handler "$candidate"; then
      # Its required handler hasn't claimed it yet; leave it durable on disk
      # and let that handler's own claim or fallback receipt wake us.
      continue
    fi
    printf '%s\n' "$candidate"
    return 0
  done < <(
    "$NOTIFIER_PY" "$DISPATCH_PY" pending-candidates "$TASKS_DIR" "$RESULTS_DIR" \
      --claims-dir "$CLAIMS_DIR"
  )
  return 1
}

# Every pane predicate has a TEXT form so one snapshot can be judged for busy,
# idle-ready and composer-empty at once -- three separate reads are three races.
# "esc to interrupt" covers any in-flight turn; the gate signatures don't.
pane_text_is_busy() {
  printf '%s\n' "$1" | tail -12 | grep -Fq 'esc to interrupt'
}

# core-input-watch.py owns Claude's pane-state patterns; $2 names the predicate.
pane_text_ciw() {
  printf '%s' "$1" | "$NOTIFIER_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("core_input_watch", sys.argv[1])
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)
sys.exit(0 if getattr(ciw, sys.argv[2])(sys.stdin.read()) else 1)
' "$REPO/src/core-input-watch.py" "$2"
}

pane_text_is_idle_ready() {
  [ -n "$1" ] || return 1
  pane_text_is_busy "$1" && return 1
  pane_text_ciw "$1" _is_idle_ready
}

pane_text_composer_is_empty() {
  [ -n "$1" ] || return 1
  pane_text_ciw "$1" _composer_is_empty
}

core_pane_is_busy() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 0
  pane_text_is_busy "$pane"
}

core_pane_is_idle_ready() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION:0" 2>/dev/null)" || return 1
  pane_text_is_idle_ready "$pane"
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

# The TARGET PANE's own limit/size (#{history_limit} is fixed at pane creation;
# the global option can move without it), never `show-options -g`. Empty on failure.
pane_history_field() {
  local v
  v="$(tmux -S "$TMUX_SOCKET" display-message -p -t "$SESSION:0" "#{$1}" 2>/dev/null)"
  case "$v" in ''|*[!0-9]*) printf '' ;; *) printf '%s' "$v" ;; esac
}

# min(our env cap, the pane's real retained limit); the env cap alone when unreadable.
effective_scrollback_lines() {
  local limit
  limit="$(pane_history_field history_limit)"
  if [ -n "$limit" ] && [ "$limit" -lt "$CAPTURE_SCROLLBACK_LINES" ]; then printf '%s' "$limit"
  else printf '%s' "$CAPTURE_SCROLLBACK_LINES"; fi
}

# Scrollback (-S), not just the visible screen: a wrapped prompt taller than
# the pane pushes its marker off-screen, past what any `tail` can recover.
capture_raw() {
  tmux -S "$TMUX_SOCKET" capture-pane -p -S "-$(effective_scrollback_lines)" -t "$SESSION:0" 2>/dev/null
}

capture_tail() {
  capture_raw | sed '/^[[:space:]]*$/d'
}

# Delegates to core-input-watch.py's _composer_text (dewrapped, footer/gate
# lines stripped) so exact-equality never sees the status bar or a stale marker.
composer_text() {
  printf '%s' "$1" | "$NOTIFIER_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("core_input_watch", sys.argv[1])
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)
print(ciw._composer_text(sys.stdin.read()) or "")
' "$REPO/src/core-input-watch.py"
}

# Staged = the composer holds EXACTLY our prompt (not merely our marker as a
# substring -- interleaved owner text would still match that) AND changed since baseline.
# Whitespace is ignored on both sides: the input box word-wraps at the pane width and
# indents continuation rows, so a dewrapped capture differs from the prompt only in spaces.
prompt_is_staged() {
  local tail="$1" baseline="$2" prompt="$3"
  [ "$(composer_text "$tail" | tr -d '[:space:]')" = "$(printf '%s' "$prompt" | tr -d '[:space:]')" ] \
    && [ "$tail" != "$baseline" ]
}

# No marker in a capture whose retained history (#{history_size}, else the RAW row
# count -- blank rows occupy history too) is at the cap: the env var alone cannot fix it.
# A visible empty marker is a marker; "no marker at all" is the only truncation shape.
composer_has_marker() {
  printf '%s' "$1" | "$NOTIFIER_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("core_input_watch", sys.argv[1])
ciw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ciw)
sys.exit(0 if ciw._composer_text(sys.stdin.read()) is not None else 1)
' "$REPO/src/core-input-watch.py"
}

capture_may_be_truncated() {
  local raw="$1" cap used
  composer_has_marker "$raw" && return 1
  cap="$(effective_scrollback_lines)"
  used="$(pane_history_field history_size)"
  [ -n "$used" ] || used="$(printf '%s\n' "$raw" | grep -c '')"
  [ "$used" -ge "$cap" ]
}

# Shared by every give-up site below -- each still logs its OWN reason too.
warn_if_capture_truncated() {
  local raw="$1" filename="$2"
  capture_may_be_truncated "$raw" || return 0
  log_notifier "prompt for $filename may exceed the capture window (effective cap $(effective_scrollback_lines) lines = min(CAPTURE_SCROLLBACK_LINES=$CAPTURE_SCROLLBACK_LINES, the pane's own #{history_limit}); pane #{history_size}=$(pane_history_field history_size)) -- no composer marker found; raising CAPTURE_SCROLLBACK_LINES will not help past the pane's own history-limit"
}

# Type + verify staged, then C-m + verify submitted; both halves retry.
# A core that stays non-idle is a hard gate -- give up rather than type over a live turn.
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 attempt=0 waited staged=0
  local baseline baseline_raw staged_tail staged_raw=""
  if ! wait_for_core_idle; then
    log_notifier "core did not become idle for $filename; leaving it queued"
    return 1
  fi
  while :; do
    # ONE snapshot is the last read before the paste and is judged whole:
    # positively idle-ready (a gate or a turn fails it) and composer empty
    # (re-checked every retype). Any later read would be a new race.
    baseline_raw="$(capture_raw)"
    baseline="$(printf '%s\n' "$baseline_raw" | sed '/^[[:space:]]*$/d')"
    if ! pane_text_is_idle_ready "$baseline_raw"; then
      log_notifier "core is not idle-ready at the paste for $filename (busy or a gate); leaving it queued (failing closed)"
      return 1
    fi
    if ! pane_text_composer_is_empty "$baseline_raw"; then
      warn_if_capture_truncated "$baseline_raw" "$filename"
      log_notifier "composer not empty for $filename; leaving it queued (failing closed, not typing over a draft)"
      return 1
    fi
    tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" -l -- "$prompt"
    sleep "$POLL_INTERVAL"
    staged_raw="$(capture_raw)"
    staged_tail="$(printf '%s\n' "$staged_raw" | sed '/^[[:space:]]*$/d')"
    if prompt_is_staged "$staged_tail" "$baseline" "$prompt"; then staged=1; break; fi
    type_tries=$((type_tries + 1))
    [ "$type_tries" -ge 2 ] && break
    log_notifier "typed prompt for $filename did not stage; re-typing (2/2)"
    sleep "$POLL_INTERVAL"
  done
  # Nothing observably staged: never press Enter blind into a live session.
  # Enter never sent -> the caller must leave this queued, not wait on a result.
  if [ "$staged" != 1 ]; then
    warn_if_capture_truncated "$staged_raw" "$filename"
    log_notifier "prompt for $filename never verifiably staged after $((type_tries + 1)) attempts; not pressing Enter (failing closed)"
    return 1
  fi
  [ "$type_tries" -gt 0 ] \
    && log_notifier "prompt staged for $filename after $((type_tries + 1)) attempts"
  # Re-verify the exact composer content AND idleness immediately before THIS
  # Enter too; Enter is still unsent here, so this is not-yet-submitted.
  if core_pane_is_busy || [ "$(capture_tail)" != "$staged_tail" ]; then
    log_notifier "pane changed or went busy since $filename staged; not pressing Enter (failing closed, core may need attention)"
    return 1
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

# A wake signal, not queue order -- once idle, re-scan and pick by priority
# so every task stays durable on disk until then.
attempt_highest_pending() {
  local filename
  next_pending_task >/dev/null || return 0
  wait_for_core_idle || exit 1
  filename="$(next_pending_task)" || return 0
  submit_task "$filename"
}

# `read || rc=$?`, NOT `if read; then ...; fi; rc=$?` -- the latter's own
# exit status is 0 whenever the then-branch never ran, erasing read's real one.
while :; do
  event="" rc=0
  IFS= read -r -t "$RETRY_POLL_SEC" event || rc=$?
  if [ "$rc" -eq 0 ]; then
    case "$event" in
      "TASK_FILE: "*) attempt_highest_pending ;;
    esac
    continue
  fi
  # macOS's own /bin/bash (3.2) returns 1 for a read TIMEOUT too, same as EOF
  # (unlike a modern bash's >128) -- ask if the watcher's alive instead.
  if kill -0 "$watcher_pid" 2>/dev/null; then
    attempt_highest_pending  # a queued task has no other trigger; retry it
    continue
  fi
  break   # the watcher died -- genuine EOF, stop the notifier
done < "$event_dir/events"
