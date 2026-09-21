#!/bin/bash
# External task-file-injection notifier for the Claude Code core, matching
# Codex/agy's tmux-injection shape — a standby path alongside self-arm via Monitor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
# The core's window in that session; a heal may place it off index 0.
CORE_WINDOW="${SUTANDO_TMUX_WINDOW:-0}"
# The pane itself when the launcher named it: an index can be reused, a pane id cannot.
TARGET="${SUTANDO_TMUX_PANE:-$SESSION:$CORE_WINDOW}"
if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
  TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
else
  TASKS_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/tasks"
fi
# ONE canonical workspace root for everything workspace-owned (receipts,
# claims): a separate task inbox must never redefine it.
WORKSPACE_DIR="${SUTANDO_WORKSPACE_DIR:-$(dirname "$TASKS_DIR")}"
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$WORKSPACE_DIR/results}"
# Same base + suffix as watch-tasks-stream.sh's own CLAIMS_DIR; only
# staged_prompt_is_ambiguous() still reads it, to disambiguate a staged prompt.
CLAIMS_DIR="$WORKSPACE_DIR/state/task-event-handler-claims"
# The pool router's hand-off sentinels (task_dispatch.worker_holds); a routed task stays in tasks/.
DELIVERIES_DIR="$WORKSPACE_DIR/deliveries"
# Durable at-most-once record of a submitted prompt, per core incarnation.
INFLIGHT_DIR="$WORKSPACE_DIR/state/task-notifier-inflight"
# shellcheck source=../../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
NOTIFIER_PY="$(require_python "$REPO" "resolve task priority and pane state")" || exit 1
DISPATCH_PY="$REPO/src/delivery/task_dispatch.py"
PANE_GATE_PY="$REPO/src/delivery/pane_gate.py"
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
# FIFO of announced-but-unresolved filenames, as marker files (oldest mtime =
# head) -- not a bash array, since bash 3.2's `set -u` errors on an empty one.
queue_dir=""

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
    rm -rf "$event_dir/queue" 2>/dev/null || true
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

# Completion detection is src/delivery/task_dispatch.py's contract, shared
# with Codex and agy.
has_result() {
  local filename="$1"
  "$NOTIFIER_PY" "$DISPATCH_PY" has-result "$RESULTS_DIR" "$filename" || return 1
  "$NOTIFIER_PY" "$DISPATCH_PY" inflight-clear "$INFLIGHT_DIR" "$filename" || true
  return 0
}

# Every pane predicate has a TEXT form so one snapshot can be judged for healthy
# and composer-empty at once -- two separate reads are two races.

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

# Healthy = the pane accepts input. One verdict from src/delivery/pane_gate.py,
# the gate every notifier shares: an abnormal banner (parked or retrying, via
# cli_wedge) or a dialog holds; a running turn still accepts (it queues).
pane_text_is_healthy() {
  [ -n "$1" ] || return 1
  printf '%s' "$1" | "$NOTIFIER_PY" "$PANE_GATE_PY" healthy --runtime claude >/dev/null 2>&1
}

pane_text_composer_is_empty() {
  [ -n "$1" ] || return 1
  pane_text_ciw "$1" _composer_is_empty
}

core_pane_is_healthy() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -J -t "$TARGET" 2>/dev/null)" || return 1
  pane_text_is_healthy "$pane"
}

# The pane is the only witness; the core's own status file is a self-report that
# is stale or absent in exactly the moments this path serves.
core_is_healthy() {
  core_pane_is_healthy
}

# The exact pane, not the session: a sibling window can outlive the core. tmux
# answers a dead pane in a live session with rc 0 and a BLANK id, so the id must
# be non-empty and, when a pane was declared, that exact pane.
target_pane_is_live() {
  local id
  id="$(tmux -S "$TMUX_SOCKET" display-message -p -t "$TARGET" '#{pane_id}' 2>/dev/null)" || return 1
  [ -n "$id" ] || return 1
  [ -z "${SUTANDO_TMUX_PANE:-}" ] || [ "$id" = "$SUTANDO_TMUX_PANE" ]
}

wait_for_core_healthy() {
  local started
  started="$(date +%s)"
  while ! core_is_healthy; do
    target_pane_is_live || return 1
    if [ $(( $(date +%s) - started )) -ge "$CORE_READY_TIMEOUT" ]; then
      log_notifier "core did not become healthy within ${CORE_READY_TIMEOUT}s"
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# The TARGET PANE's own limit/size (#{history_limit} is fixed at pane creation;
# the global option can move without it), never `show-options -g`. Empty on failure.
pane_history_field() {
  local v
  v="$(tmux -S "$TMUX_SOCKET" display-message -p -t "$TARGET" "#{$1}" 2>/dev/null)"
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
  tmux -S "$TMUX_SOCKET" capture-pane -p -J -S "-$(effective_scrollback_lines)" -t "$TARGET" 2>/dev/null
}

capture_tail() {
  capture_raw | sed '/^[[:space:]]*$/d'
}

# The visible screen only: a banner is live when it is on screen, and an error
# that scrolled off is history however small the pane.
capture_view_esc() {
  tmux -S "$TMUX_SOCKET" capture-pane -p -e -J -t "$TARGET" 2>/dev/null
}

strip_sgr() {
  LC_ALL=C sed $'s#\x1b\\[[0-9;?]*[ -/]*[@-~]##g'
}

# The prompt row's own text, dewrapped -- src/delivery/pane_gate.py's
# composer_text() owns the parse (glyph, box frame, footer/tip rows) so
# exact-equality never sees the status bar or a stale marker.
composer_text() {
  printf '%s' "$1" | "$NOTIFIER_PY" "$PANE_GATE_PY" composer-text --runtime claude 2>/dev/null || true
}

# Staged = the composer holds EXACTLY our prompt (not merely our marker as a
# substring -- interleaved owner text would still match that).
# Whitespace is ignored on both sides: the input box word-wraps at the pane width and
# indents continuation rows, so a dewrapped capture differs from the prompt only in spaces.
prompt_is_staged() {
  local raw="$1" prompt="$2"
  [ "$(composer_text "$raw" | tr -d '[:space:]')" = "$(printf '%s' "$prompt" | tr -d '[:space:]')" ]
}

# The composer still carries our prompt at all (exactly, or with owner text
# mixed in). False once it left: submitted, or queued behind a running turn.
composer_holds_prompt() {
  local raw="$1" prompt="$2"
  case "$(composer_text "$raw" | tr -d '[:space:]')" in
    *"$(printf '%s' "$prompt" | tr -d '[:space:]')"*) return 0 ;;
  esac
  return 1
}

# No marker in a capture whose retained history (#{history_size}, else the RAW row
# count -- blank rows occupy history too) is at the cap: the env var alone cannot fix it.
# A visible empty marker is a marker; "no marker at all" is the only truncation shape.
# pane_gate's "composer-text" exits EXIT_UNSAFE only when no glyph line was found
# at all; an empty-but-present composer still exits 0.
composer_has_marker() {
  printf '%s' "$1" | "$NOTIFIER_PY" "$PANE_GATE_PY" composer-text --runtime claude >/dev/null 2>&1
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
# A running turn is not a gate: the line queues behind it, as the Monitor
# tool's own notification does. Only an unhealthy pane or a draft holds.
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 staged=0
  local baseline_esc baseline_raw staged_raw="" incarnation=""
  if ! wait_for_core_healthy; then
    log_notifier "core did not become healthy for $filename; leaving it queued"
    return 1
  fi
  # The wait can outlast the task: another path may have answered it meanwhile.
  if has_result "$filename"; then
    log_notifier "result for $filename appeared while waiting for the core; not delivering"
    return 0
  fi
  while :; do
    # ONE snapshot is the last read before the paste and is judged whole:
    # healthy (an abnormal banner or a gate fails it) and composer empty
    # (re-checked every retype). Any later read would be a new race.
    baseline_esc="$(capture_view_esc)"
    baseline_raw="$(printf '%s\n' "$baseline_esc" | strip_sgr)"
    # The incarnation the prompt is typed INTO: sampled here, before the paste,
    # never after the Enter, when a restart could already have replaced the pane.
    incarnation="$(core_incarnation)"
    if [ -z "$incarnation" ]; then
      log_notifier "core incarnation unreadable at the paste for $filename; leaving it queued (failing closed)"
      return 1
    fi
    if ! pane_text_is_healthy "$baseline_raw"; then
      log_notifier "core is not healthy at the paste for $filename (abnormal or a gate); leaving it queued (failing closed)"
      return 1
    fi
    if ! pane_text_composer_is_empty "$baseline_esc"; then
      warn_if_capture_truncated "$baseline_raw" "$filename"
      log_notifier "composer not empty for $filename; leaving it queued (failing closed, not typing over a draft)"
      return 1
    fi
    tmux -S "$TMUX_SOCKET" send-keys -t "$TARGET" -l -- "$prompt"
    sleep "$POLL_INTERVAL"
    staged_raw="$(capture_raw)"
    if prompt_is_staged "$staged_raw" "$prompt"; then staged=1; break; fi
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
  # The composer must hold EXACTLY our prompt at this Enter; the rest of the
  # pane may move. Owner text mixed in means this Enter would not be ours.
  if ! prompt_is_staged "$(capture_raw)" "$prompt"; then
    log_notifier "composer changed since $filename staged; not pressing Enter (failing closed, core may need attention)"
    return 1
  fi
  press_enter_and_confirm "$filename" "$prompt" "$incarnation"
}

# Which core is running: a marker from another incarnation is stale, and the
# turn it recorded died with that core.
core_incarnation() {
  pane_history_field pane_pid
}

# Marker first, then C-m, then confirm the prompt LEFT the composer (submitted, or
# queued behind a running turn); re-press while it is still exactly ours. The marker
# precedes the Enter so no crash window exists in which the prompt was submitted
# and nothing recorded it; a marker beside a still-staged prompt means resume.
press_enter_and_confirm() {
  local filename="$1" prompt="$2" incarnation="$3" attempt=0 waited cap
  has_result "$filename" && return 0
  if ! "$NOTIFIER_PY" "$DISPATCH_PY" inflight-mark "$INFLIGHT_DIR" "$filename" "$incarnation"; then
    log_notifier "could not record the in-flight marker for $filename; not pressing Enter (failing closed)"
    return 1
  fi
  tmux -S "$TMUX_SOCKET" send-keys -t "$TARGET" C-m
  while :; do
    waited=0
    while [ "$waited" -lt "$SUBMIT_CONFIRM_TIMEOUT" ]; do
      # Confirmed when the prompt has LEFT the composer: submitted, or queued
      # behind a running turn. A busy footer proves nothing about our line.
      if has_result "$filename"; then
        return 0
      fi
      # A failed capture says nothing about the composer; only a read that
      # shows the prompt gone confirms.
      if cap="$(capture_raw)" && ! composer_holds_prompt "$cap" "$prompt"; then
        [ "$attempt" -gt 0 ] && log_notifier "submit confirmed for $filename after $((attempt + 1)) attempts"
        return 0
      fi
      sleep 1
      waited=$((waited + 1))
    done
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$SUBMIT_RETRIES" ]; then
      log_notifier "submit NOT confirmed for $filename after $attempt attempts; prompt still staged, the next pick resumes it (core may need attention)"
      return 1
    fi
    # Re-press only while the composer is STILL exactly our prompt -- with
    # owner text mixed in, this Enter would not be ours.
    if ! prompt_is_staged "$(capture_raw)" "$prompt"; then
      log_notifier "composer changed since $filename staged; not re-pressing C-m (failing closed, core may need attention)"
      return 1
    fi
    log_notifier "prompt still staged after C-m for $filename; re-pressing (attempt $((attempt + 1))/$SUBMIT_RETRIES)"
    tmux -S "$TMUX_SOCKET" send-keys -t "$TARGET" C-m
  done
}

task_prompt() {
  printf 'Sutando task ready: %s. Read %s/%s, follow CLAUDE.md, complete the task, and write the result to %s/%s.' \
    "$1" "$TASKS_DIR" "$1" "$RESULTS_DIR" "$1"
}

# Whitespace is not identity in a wrapped composer: when another pending task's
# prompt also reads as staged, the composer cannot say whose it is.
staged_prompt_is_ambiguous() {
  local raw="$1" filename="$2" other
  while IFS= read -r other; do
    [ "$other" = "$filename" ] && continue
    prompt_is_staged "$raw" "$(task_prompt "$other")" && return 0
  done < <("$NOTIFIER_PY" "$DISPATCH_PY" pending-candidates "$TASKS_DIR" "$RESULTS_DIR" --claims-dir "$CLAIMS_DIR" --deliveries-dir "$DELIVERIES_DIR" 2>/dev/null || true)
  return 1
}

submit_task() {
  local filename="$1" prompt started raw incarnation live_rc
  case "$filename" in
    ""|*/*|*..*) return 0 ;;
  esac
  has_result "$filename" && return 0
  prompt="$(task_prompt "$filename")"
  if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
    log_notifier "no session $SESSION — dropping $filename"
    return 0
  fi
  # A capture can fail (the pane is gone); the liveness wait below is what decides that.
  raw="$(capture_raw)" || raw=""
  incarnation="$(core_incarnation)"
  # 0 live, 1 not live, anything else undecidable (an unreadable marker): a
  # marker that cannot be read is not evidence the prompt was never submitted.
  live_rc=0
  "$NOTIFIER_PY" "$DISPATCH_PY" inflight-live "$INFLIGHT_DIR" "$filename" "$incarnation" || live_rc=$?
  if [ "$live_rc" -ne 0 ] && [ "$live_rc" -ne 1 ]; then
    log_notifier "cannot decide whether $filename is already in flight (marker read failed, rc $live_rc); leaving it queued (failing closed)"
    return 0
  fi
  if prompt_is_staged "$raw" "$prompt"; then
    # Typed but never confirmed sent (a swallowed Enter, a crash or restart between
    # the paste and C-m): the composer is exactly ours, so resume at the Enter. This
    # precedes the marker: a marker beside a staged prompt records an Enter that never landed.
    if staged_prompt_is_ambiguous "$raw" "$filename"; then
      log_notifier "composer holds a prompt that reads as $filename's and another pending task's; leaving it queued (failing closed, core may need attention)"
      return 0
    fi
    if [ -z "$incarnation" ]; then
      log_notifier "core incarnation unreadable while $filename is staged; not pressing Enter (failing closed)"
      return 0
    fi
    log_notifier "prompt for $filename is staged but unsent; resuming its submission"
    press_enter_and_confirm "$filename" "$prompt" "$incarnation" || return 0
  elif [ "$live_rc" -eq 0 ]; then
    log_notifier "prompt for $filename was already submitted to this core; awaiting its result, not re-typing"
  elif composer_holds_prompt "$raw" "$prompt"; then
    log_notifier "composer holds $filename's prompt with other text; leaving it queued (failing closed, core may need attention)"
    return 0
  else
    deliver_prompt "$filename" "$prompt" || return 0
  fi
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
queue_dir="$event_dir/queue"
mkdir -p "$queue_dir"
"$NOTIFIER_PY" -c \
  'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1], sys.argv[2]])' \
  "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" > "$event_dir/events" &
watcher_pid=$!

# The watcher is the sole decider: enqueue exactly the filename it announced,
# in announce order. No rescan, no priority pick, no handler probe here.
enqueue_announced_task() {
  local filename="$1"
  case "$filename" in ""|*/*|*..*) return 0 ;; esac
  has_result "$filename" && return 0
  [ -e "$queue_dir/$filename" ] && return 0
  : > "$queue_dir/$filename"
}

# Oldest marker = the head (mtime order == announce order: each marker is
# created once and never touched again).
queue_head() {
  ls -1tr "$queue_dir" 2>/dev/null | tail -1
}

# A narrower net than the watcher's own routing, for a worker claim that
# outlives its handler declaration (the pool de-registers mid-flight).
filename_is_worker_held() {
  local filename="$1" rc=0
  "$NOTIFIER_PY" "$DISPATCH_PY" worker-holds "$DELIVERIES_DIR" "$filename" || rc=$?
  [ "$rc" -ne 1 ]
}

# Same narrower net, for a watcher-owned claim (CLAIMS_DIR); staleness
# stays the watcher's own call (acquire_task_claim), never re-derived here.
filename_is_claimed() {
  [ -e "$CLAIMS_DIR/$1" ]
}

# Retry the SAME head task on every wake until it resolves; never re-pick.
process_announced_queue() {
  local filename
  while :; do
    filename="$(queue_head)"
    [ -n "$filename" ] || return 0
    if has_result "$filename"; then
      rm -f "$queue_dir/$filename"
      continue
    fi
    if filename_is_worker_held "$filename"; then
      log_notifier "$filename is worker-held per deliveries/; leaving it queued, not typing into the core"
      return 0
    fi
    if filename_is_claimed "$filename"; then
      log_notifier "$filename has a live task-event-handler claim; leaving it queued, not typing into the core"
      return 0
    fi
    if ! wait_for_core_healthy; then
      # A busy core is not a dead one: the task stays queued for the next poll.
      tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || exit 1
      return 0
    fi
    submit_task "$filename"
    if has_result "$filename"; then
      rm -f "$queue_dir/$filename"
      continue
    fi
    return 0
  done
}

# `read || rc=$?`, NOT `if read; then ...; fi; rc=$?` -- the latter's own
# exit status is 0 whenever the then-branch never ran, erasing read's real one.
while :; do
  event="" rc=0
  IFS= read -r -t "$RETRY_POLL_SEC" event || rc=$?
  if [ "$rc" -eq 0 ]; then
    case "$event" in
      "TASK_FILE: "*)
        enqueue_announced_task "${event#TASK_FILE: }"
        process_announced_queue
        ;;
    esac
    continue
  fi
  # macOS's own /bin/bash (3.2) returns 1 for a read TIMEOUT too, same as EOF
  # (unlike a modern bash's >128) -- ask if the watcher's alive instead.
  if kill -0 "$watcher_pid" 2>/dev/null; then
    process_announced_queue  # retry the same announced task; never rescans
    continue
  fi
  break   # the watcher died -- genuine EOF, stop the notifier
done < "$event_dir/events"
