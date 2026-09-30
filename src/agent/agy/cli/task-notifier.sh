#!/bin/bash
# External task-file-injection notifier for the agy (Antigravity CLI) core.
# agy notifies on subprocess completion only, not per line, so it cannot self-arm
# a never-exiting watcher; this injects each task into the pane from outside.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_AGY_TMUX_SOCKET:-${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}}"
SESSION="${SUTANDO_AGY_TMUX_SESSION:-sutando-agy}"
# agy's watcher runs ALONGSIDE the canonical one, not instead of it — sharing
# workspace/tasks+results would let two watchers double-process every task.
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
# Scrollback depth for staging checks -- a wrapped marker can scroll above a
# narrow pane's viewport, so retyping needs deeper history to see it.
PANE_HISTORY_LINES="${SUTANDO_AGY_NOTIFIER_PANE_HISTORY_LINES:-500}"

# shellcheck source=../../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
NOTIFIER_PY="$(resolve_python "$REPO")"
if [ -z "$NOTIFIER_PY" ]; then
  echo "agy-task-notifier: no runnable python3 — cannot resolve task priority" >&2
  exit 1
fi

sentinel_path() {
  # The sentinel this notifier's own watcher stamps once ready; the launcher
  # polls it, so the two must derive one path from one inbox and one identity.
  . "$REPO/src/tasks-dir-resolve.sh"
  SUTANDO_INSTANCE_ID="agy-task-notifier" "$NOTIFIER_PY" "$REPO/src/util_paths.py" \
    watcher-sentinel "$(workspace_dir_for_inbox "$TASKS_DIR")/state"
}
# One receipt file per launch, named by its nonce: no generation can read or remove another's.
receipt_path() { printf '%s.launch.%s\n' "$(sentinel_path)" "$1"; }
case "${1:-}" in
  --sentinel-path) sentinel_path; exit $? ;;
  --launch-ready)
    # Ready only for THIS launch: the receipt this notifier publishes once its own watcher
    # child owns the ready sentinel must carry the launcher's nonce and this exact inbox.
    [ -n "${2:-}" ] || { echo "agy-task-notifier: --launch-ready needs the launch nonce" >&2; exit 2; }
    R="$(receipt_path "$2")" || exit 1
    NONCE="$2" RECEIPT="$R" INBOX="$TASKS_DIR" "$NOTIFIER_PY" - <<'PY'
import os, sys
want = {"nonce": os.environ["NONCE"], "inbox": os.environ["INBOX"]}
try:
    got = dict(l.split("=", 1) for l in open(os.environ["RECEIPT"]).read().splitlines() if "=" in l)
except OSError:
    sys.exit(1)
if any(got.get(k) != v for k, v in want.items()):
    sys.exit(1)
for k in ("watcher", "notifier"):
    try:
        os.kill(int(got[k]), 0)
    except (KeyError, ValueError, ProcessLookupError):
        sys.exit(1)
    except PermissionError:
        pass
sys.exit(0)
PY
    exit $? ;;
esac

# agy publishes its result HERE (not the watcher, which only owns TASKS_DIR),
# so an absent results dir must fail at startup, not deep inside a dispatch.
mkdir -p "$RESULTS_DIR" || { echo "agy-task-notifier: cannot create results dir $RESULTS_DIR" >&2; exit 1; }

watcher_pid=""
event_dir=""

stop_watcher() {
  [ -n "$watcher_pid" ] || return 0
  kill -TERM "-$watcher_pid" 2>/dev/null || kill -TERM "$watcher_pid" 2>/dev/null || true
  wait "$watcher_pid" 2>/dev/null || true
  watcher_pid=""
}

cleanup_notifier() {
  [ -z "${RECEIPT_FILE:-}" ] || rm -f "$RECEIPT_FILE"
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
# src/delivery/task_dispatch.py, not duplicated here.
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

pane_capture() {
  tmux -S "$TMUX_SOCKET" capture-pane -p -t "$SESSION" -S "-$PANE_HISTORY_LINES" 2>/dev/null
}

# `capture-pane -p` returns the pane's fixed row count regardless of typed
# content, so a line-count offset can't isolate new lines — diff the TEXT.
prompt_is_staged() {
  local filename="$1" baseline="$2" current added joined
  current="$(pane_capture)"
  [ "$current" = "$baseline" ] && return 1
  added="$(diff <(printf '%s' "$baseline") <(printf '%s' "$current") 2>/dev/null || true)"
  # A long marker can hard-wrap across several `>` lines in a narrow pane;
  # join them before matching, since any single line can miss a split marker.
  joined="$(printf '%s\n' "$added" | sed -n 's/^> //p' | tr -d '\n')"
  case "$joined" in *"Sutando task ready: $filename"*) return 0 ;; esac
  return 1
}

# Type + poll for staged (a one-shot check retyped a still-landing paste
# into a duplicate, verified live), then Enter + verify submitted.
deliver_prompt() {
  local filename="$1" prompt="$2" type_tries=0 staged=0 waited=0 baseline
  # A failed idle wait must never fall through to send-keys: that's the
  # fail-open bug -- a busy pane still gets typed into and Entered.
  wait_for_core_idle || { log_notifier "core not idle -- refusing to send for $filename"; return 1; }
  while :; do
    baseline="$(pane_capture)"
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
  # A file gone since its event (cancelled, archived) has nothing to dispatch.
  [ -f "$TASKS_DIR/$filename" ] || { log_notifier "task file gone before dispatch: $filename"; return 0; }
  prompt="Sutando task ready: $filename. Read $TASKS_DIR/$filename, follow AGENTS.md, complete the task, and write the result to $RESULTS_DIR/$filename."
  if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
    log_notifier "no session $SESSION — dropping $filename"
    return 0
  fi
  # Nothing was sent if delivery refused -- the task stays pending in
  # TASKS_DIR and next_pending_task picks it up again on a later event.
  deliver_prompt "$filename" "$prompt" || return 1
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
  # A deferred delivery (core busy) is not a script failure under set -e.
  submit_task "$2" || true
  exit 0
fi

# Bind RESULTS_DIR (never an env var itself) and FORCE the instance id —
# never fall back on it, or an inherited tmux-global value collides here.
export SUTANDO_RESULTS_DIR="$RESULTS_DIR"
export SUTANDO_INSTANCE_ID="agy-task-notifier"
event_dir="$(mktemp -d "${TMPDIR:-/tmp}/sutando-agy-task-notifier.XXXXXX")"
mkfifo "$event_dir/events"
# The launch nonce rides the watcher's own ready event (<sentinel>.token), so readiness can
# never be inferred from a pid or a timestamp that an earlier generation could also produce.
LAUNCH_NONCE="${SUTANDO_AGY_LAUNCH_NONCE:-}"
export SUTANDO_WATCHER_READY_TOKEN="$LAUNCH_NONCE"
"$NOTIFIER_PY" -c \
  'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", *sys.argv[1:]])' \
  "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" --role session --inbox "$TASKS_DIR" > "$event_dir/events" &
watcher_pid=$!
# The watcher's open-for-write on the FIFO blocks until a reader exists: open it now, not at
# the read loop, or the receipt wait below would hold the watcher before its first line.
exec 3< "$event_dir/events"

# The receipt is the launcher's readiness signal: published only once the ready sentinel
# names this notifier's own child, never inferred from a pid, an mtime or an argv shape.
RECEIPT_FILE=""
# The child owns the sentinel only when it names the child AND the watcher's ready event wrote
# this launch's nonce beside it; a crash-left or future-dated file has neither to offer.
child_owns_sentinel() {
  [ "$(cat "$1" 2>/dev/null)" = "$2" ] && [ "$(cat "$1.token" 2>/dev/null)" = "$3" ]
}
if [ -n "$LAUNCH_NONCE" ]; then
  SENTINEL_FILE="$(sentinel_path)" && RECEIPT_FILE="$(receipt_path "$LAUNCH_NONCE")"
  # Receipts of generations whose notifier is gone are dead files; a live pid keeps its file.
  # Only complete names (32-hex nonce) are candidates: a temp file mid-publish is not.
  for r in "$SENTINEL_FILE".launch.*; do
    [ -e "$r" ] && [[ "$r" =~ \.launch\.[0-9a-f]{32}$ ]] || continue
    n="$(sed -n 's/^notifier=//p' "$r" 2>/dev/null)"
    [ -n "$n" ] && kill -0 "$n" 2>/dev/null || rm -f "$r"
  done
  deadline=$(( $(date +%s) + ${SUTANDO_WATCHER_READY_TIMEOUT:-10} + ${SUTANDO_STANDBY_STOP_TIMEOUT:-15} + 5 ))
  while [ "$(date +%s)" -lt "$deadline" ] && kill -0 "$watcher_pid" 2>/dev/null; do
    if child_owns_sentinel "$SENTINEL_FILE" "$watcher_pid" "$LAUNCH_NONCE"; then
      tmp="$(mktemp "$RECEIPT_FILE.XXXXXX")" \
        && printf 'nonce=%s\ninbox=%s\nwatcher=%s\nnotifier=%s\n' "$LAUNCH_NONCE" "$TASKS_DIR" "$watcher_pid" "$$" > "$tmp" \
        && mv -f "$tmp" "$RECEIPT_FILE" \
        || log_notifier "could not publish the launch receipt $RECEIPT_FILE"
      break
    fi
    sleep 0.2
  done
fi

while IFS= read -r event <&3; do
  case "$event" in
    "TASK_FILE: "*)
      next_pending_task >/dev/null || continue
      # A busy pane must not kill the persistent watcher: keep re-waiting
      # (no second task-file event needed) until idle or the session is gone.
      while ! wait_for_core_idle; do
        tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || continue 2
      done
      filename="$(next_pending_task)" || continue
      # deliver_prompt runs its OWN idle check; a busy gap here must retry
      # in place too, like the outer gate above, not drop silently.
      while ! submit_task "$filename"; do
        tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || continue 2
      done
      ;;
  esac
done
