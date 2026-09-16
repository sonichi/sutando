#!/bin/bash
# External task-file-injection notifier for the agy (Antigravity CLI) core.
# agy notifies on subprocess completion only, not per-line, so it can't self-arm a never-exiting watcher — this injects tasks into the pane externally instead.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_AGY_TMUX_SOCKET:-${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}}"
SESSION="${SUTANDO_AGY_TMUX_SESSION:-sutando-agy}"
# agy is not selected in place of the running Claude/Codex core (see
# src/agent/agy/README.md) — its watcher runs ALONGSIDE the canonical one, not
# instead of it. Defaulting to the shared workspace/tasks + results dirs would
# put two independent watchers on the same queue, so every task (including an
# irreversible one) gets processed twice. Default to a separately-owned agy
# inbox instead; an explicit SUTANDO_TASKS_DIR/SUTANDO_RESULTS_DIR override
# (as every test here uses) still works exactly as before.
if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
  TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
else
  TASKS_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/tasks-agy"
fi
if [ -n "${SUTANDO_RESULTS_DIR:-}" ]; then
  RESULTS_DIR="${SUTANDO_RESULTS_DIR/#\~/$HOME}"
elif [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
  # An explicit TASKS_DIR override without an explicit RESULTS_DIR derives the
  # sibling results/ dir, same convention every other consumer follows.
  RESULTS_DIR="$(dirname "$TASKS_DIR")/results"
else
  RESULTS_DIR="$(dirname "$TASKS_DIR")/results-agy"
fi
POLL_INTERVAL="${SUTANDO_AGY_NOTIFIER_POLL_INTERVAL:-0.5}"
COMPLETION_TIMEOUT="${SUTANDO_AGY_NOTIFIER_COMPLETION_TIMEOUT:-3600}"
CORE_READY_TIMEOUT="${SUTANDO_AGY_NOTIFIER_CORE_READY_TIMEOUT:-300}"
# How long to wait after Enter before re-pressing once (not Codex's 6x-retry loop).
SUBMIT_CONFIRM_TIMEOUT="${SUTANDO_AGY_NOTIFIER_SUBMIT_CONFIRM_TIMEOUT:-5}"
# Ticks (of POLL_INTERVAL) to wait for a paste to visibly stage before retyping.
TYPE_CONFIRM_TIMEOUT_TICKS="${SUTANDO_AGY_NOTIFIER_TYPE_CONFIRM_TICKS:-8}"

# shellcheck source=../../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
NOTIFIER_PY="$(resolve_python "$REPO")"
if [ -z "$NOTIFIER_PY" ]; then
  echo "agy-task-notifier: no runnable python3 — cannot resolve task priority" >&2
  exit 1
fi

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
  local msg="agy-task-notifier: $*" dir
  dir="$(dirname "$TASKS_DIR")/logs"
  [ -d "$dir" ] && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$msg" >>"$dir/agy-task-notifier.log" 2>/dev/null
  printf '%s\n' "$msg" >&2
}

DISPATCH_PY="$REPO/src/delivery/task_dispatch.py"

# Completion-detection and priority-selection are owned by
# src/delivery/task_dispatch.py — see its header for why the bash copy this
# used to be was a defect, not just a duplicate (sonichi#4303 review).
has_result() {
  "$NOTIFIER_PY" "$DISPATCH_PY" has-result "$RESULTS_DIR" "$1"
}

next_pending_task() {
  "$NOTIFIER_PY" "$DISPATCH_PY" next-pending "$TASKS_DIR" "$RESULTS_DIR"
}

# agy's footer shows "esc to cancel" for any in-flight turn (tool or text)
# and "? for shortcuts" once fully idle — never both, so one check suffices.
core_pane_is_busy() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION" 2>/dev/null)" || return 0
  printf '%s\n' "$pane" | tail -6 | grep -Fq 'esc to cancel'
}

core_pane_is_idle_ready() {
  local pane
  pane="$(tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION" 2>/dev/null)" || return 1
  printf '%s\n' "$pane" | tail -6 | grep -Fq '? for shortcuts' || return 1
  ! printf '%s\n' "$pane" | tail -6 | grep -Fq 'esc to cancel'
}

wait_for_core_idle() {
  local started; started="$(date +%s)"
  while ! core_pane_is_idle_ready; do
    tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || return 1
    if [ $(( $(date +%s) - started )) -ge "$CORE_READY_TIMEOUT" ]; then
      log_notifier "core did not become idle within ${CORE_READY_TIMEOUT}s"
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

pane_line_count() {
  tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION" 2>/dev/null | wc -l | tr -d ' '
}

# A wide tail: at the default 80-col pane width our newline-free prompt wraps
# across several rows, so a narrow tail can miss the marker (verified live).
#
# `baseline` (the pane's line count taken right before THIS attempt typed)
# restricts the match to lines the pane gained since then. The marker text is
# byte-identical across retries of the same task, so without this a marker
# left over from an EARLIER successful dispatch of the same filename — still
# sitting in the last 20 rows as sent history — reads as "staged" even when
# the current paste was swallowed and nothing new actually landed.
prompt_is_staged() {
  local filename="$1" baseline="${2:-0}"
  tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION" 2>/dev/null \
    | tail -n "+$((baseline + 1))" | tail -20 | grep -Fq "Sutando task ready: $filename"
}

# Type + poll for staged (a one-shot check retyped a still-landing paste
# into a duplicate, verified live), then Enter + verify submitted.
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 staged=0 waited=0 baseline
  wait_for_core_idle || true
  while :; do
    baseline="$(pane_line_count)"
    tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION" -l -- "$prompt"
    waited=0
    while [ "$waited" -lt "$TYPE_CONFIRM_TIMEOUT_TICKS" ]; do
      if prompt_is_staged "$filename" "$baseline"; then staged=1; break 2; fi
      sleep "$POLL_INTERVAL"
      waited=$((waited + 1))
    done
    type_tries=$((type_tries + 1))
    [ "$type_tries" -ge 2 ] && break
    log_notifier "typed prompt for $filename did not stage; retyping"
  done
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION" Enter
  [ "$staged" = 1 ] || { log_notifier "prompt for $filename sent unverified (never observed staged)"; return 0; }
  # Confirmed by the pane going BUSY, not by the marker vanishing — it stays
  # visible as sent history, so that check always read "still staged."
  waited=0
  while [ "$waited" -lt "$SUBMIT_CONFIRM_TIMEOUT" ]; do
    if has_result "$filename" || core_pane_is_busy; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log_notifier "prompt not yet submitted after Enter for $filename; re-pressing once"
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION" Enter
}

submit_task() {
  local filename="$1" prompt started
  case "$filename" in
    ""|*/*|*..*) return 0 ;;
  esac
  has_result "$filename" && return 0
  prompt="Sutando task ready: $filename. Read $TASKS_DIR/$filename, follow AGENTS.md, complete the task, and write the result to $RESULTS_DIR/$filename."
  if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
    log_notifier "no session $SESSION — dropping $filename"
    return 0
  fi
  deliver_prompt "$filename" "$prompt"
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
  [ -n "${2:-}" ] || { echo "agy-task-notifier: --event requires a filename" >&2; exit 2; }
  submit_task "$2"
  exit 0
fi

event_dir="$(mktemp -d "${TMPDIR:-/tmp}/sutando-agy-task-notifier.XXXXXX")"
mkfifo "$event_dir/events"
"$NOTIFIER_PY" -c \
  'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1], sys.argv[2]])' \
  "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" > "$event_dir/events" &
watcher_pid=$!

while IFS= read -r event; do
  case "$event" in
    "TASK_FILE: "*)
      next_pending_task >/dev/null || continue
      wait_for_core_idle || exit 1
      filename="$(next_pending_task)" || continue
      submit_task "$filename"
      ;;
  esac
done < "$event_dir/events"
