#!/bin/bash
# Streaming task watcher — the canonical task-detection path.
#
# Runs fswatch indefinitely and emits ONE line per new task file appearance.
# Designed to be invoked via Claude Code's `Monitor` tool, which streams
# stdout lines as per-event notifications without process-restart cycles.
#
# Replaces the one-shot `watch-tasks.sh` (retired 2026-05-14) — that one
# exited on first event so the caller had to restart it; this one stays
# alive for the lifetime of the CLI session.
#
# Output format per event:
#   TASK_FILE: <basename>
# Plus an INITIAL_SCAN block at startup for any pre-existing files:
#   TASK_FILE: <basename>  (one per line)
#
# The agent reads the named files via the Read tool when notifications
# arrive — no need to inline file contents in stdout (Monitor's 200ms
# batching window would group multi-line content awkwardly).

# fd 9 is a stable dup of the real stdout, taken before anything can rebind fd 1.
# A shutdown emit invoked one $( ) deep writes to the capture pipe, not to stdout.
exec 9>&1

set -u

if [ "${1:-}" = "--handler-runner" ]; then
  handler="$2"
  runtime="$3"
  workspace="$4"
  task_path="$5"
  results="$6"
  repo="$7"
  events_fifo="$8"
  filename="$9"
  if "$handler" \
      --runtime "$runtime" \
      --workspace "$workspace" \
      --task-file "$task_path" \
      --results-dir "$results" \
      --repo "$repo" >/dev/null; then
    handler_rc=0
  else
    handler_rc=$?
  fi
  printf 'HANDLER_DONE: %s %s\n' "$handler_rc" "$filename" > "$events_fifo"
  exit 0
fi

__SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=watcher_sentinel.sh
source "$__SCRIPT_DIR/watcher_sentinel.sh"
# shellcheck source=task-emit.sh
source "$__SCRIPT_DIR/task-emit.sh"
__REPO_ROOT="$(cd "$__SCRIPT_DIR/.." && pwd)"

# Resolve TASKS_DIR. Priority: explicit positional arg → canonical M0 loader.
# Post-v0.8 (#1440 + Mini opinion-requested 2026-06-06) the legacy env-var
# fallback and hardcoded pre-v0.8 default fallback are gone: the bridges
# (discord-bridge.py, telegram-bridge.py, dm-result.py — see PRs
# #708/#720/#722/#723) write to the resolved workspace, and if this watcher
# diverged from that resolution owner DMs would land silently. Diagnosed
# 2026-05-15 (~3 dropped DMs over 17 min) and again 2026-05-16 (~45 min
# silent gap when the Monitor was started without the env var exported
# into its env). Single resolution path = no divergence.
if [ -n "${1:-}" ]; then
  TASKS_DIR="$1"
elif [ -f "$__REPO_ROOT/scripts/sutando-config.sh" ]; then
  __WS="$(bash "$__REPO_ROOT/scripts/sutando-config.sh" workspace)"
  TASKS_DIR="$__WS/tasks"
else
  echo "watch-tasks-stream: cannot resolve workspace — scripts/sutando-config.sh not found at \$__REPO_ROOT. Verify the sutando checkout is intact." >&2
  exit 1
fi
mkdir -p "$TASKS_DIR"
# Canonicalize watched dir for the parent-dir filter below. fswatch always
# emits PHYSICAL paths (e.g. /private/tmp/... not /tmp/...), so we resolve
# symlinks with `pwd -P` to match. Without -P, on macOS the comparison
# `dirname "$path"` == `$TASKS_DIR_ABS` fails when /tmp is symlinked to
# /private/tmp — which is the default.
TASKS_DIR_ABS="$(cd "$TASKS_DIR" && pwd -P)"
WORKSPACE_DIR="$(dirname "$TASKS_DIR_ABS")"
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$WORKSPACE_DIR/results}"

# Optional task handlers are injected by runtime adapters. Two provider workers
# may run at once; further eligible tasks stay as tiny on-disk receipts instead
# of spawning an unbounded process fanout. Unhandled work still emits its
# TASK_FILE event immediately, even while the provider queue is full.
TASK_HANDLER_WORKERS=2
SUTANDO_PY_BIN="$(bash "$__REPO_ROOT/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
DISPATCH_DIR=""
WATCH_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sutando-task-watch.XXXXXX")"
mkfifo "$WATCH_RUNTIME_DIR/events"
FSWATCH_PID=""
CLEANING_UP=0
GROUP_TERM_SENT=0
CLAIMS_DIR="$WORKSPACE_DIR/state/task-event-handler-claims"
FALLBACKS_DIR="$WORKSPACE_DIR/state/task-event-handler-fallbacks"
WATCHER_ID="$$-${RANDOM:-0}"

claim_is_live() {
  local claim="$1" owner_pid
  [ -f "$claim" ] || return 1
  owner_pid="$(sed -n '1p' "$claim" 2>/dev/null)"
  case "$owner_pid" in
    ""|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$owner_pid" 2>/dev/null
}

remove_claim() {
  local claim="$1"
  rm -f "$claim"
}

retire_stale_claim() {
  local claim="$1" retired
  claim_is_live "$claim" && return 1
  retired="$CLAIMS_DIR/.stale-$WATCHER_ID-$(basename "$claim")"
  if mv "$claim" "$retired" 2>/dev/null; then
    remove_claim "$retired"
    return 0
  fi
  return 1
}

acquire_task_claim() {
  local filename="$1" task_path="$2" disposition="${3:-fallback}" claim temporary attempts=0
  claim="$CLAIMS_DIR/$filename"
  temporary="$CLAIMS_DIR/.claim-$WATCHER_ID-$filename"
  printf '%s\n%s\n%s\n%s\n' "$$" "$WATCHER_ID" "$task_path" "$disposition" > "$temporary"
  while [ "$attempts" -lt 3 ]; do
    # A hard link publishes the fully written claim atomically and fails if
    # another watcher already owns the destination; it never clobbers.
    if ln "$temporary" "$claim" 2>/dev/null; then
      rm -f "$temporary"
      return 0
    fi
    if claim_is_live "$claim"; then
      rm -f "$temporary"
      return 1
    fi
    retire_stale_claim "$claim" || true
    attempts=$((attempts + 1))
  done
  rm -f "$temporary"
  return 1
}

release_task_claim() {
  local filename="$1" claim retired owner_id
  claim="$CLAIMS_DIR/$filename"
  owner_id="$(sed -n '2p' "$claim" 2>/dev/null)"
  [ "$owner_id" = "$WATCHER_ID" ] || return 1
  retired="$DISPATCH_DIR/settled/claim-$filename"
  if mv "$claim" "$retired" 2>/dev/null; then
    remove_claim "$retired"
    return 0
  fi
  return 1
}

claim_is_ours() {
  local filename="$1" owner_id
  owner_id="$(sed -n '2p' "$CLAIMS_DIR/$filename" 2>/dev/null)"
  [ "$owner_id" = "$WATCHER_ID" ]
}

# 0 = must-handle, 1 = fallback, 2 = unknown.
# Only must-handle/fallback may reach the live-core branches.
claim_disposition() {
  local filename="$1"
  case "$(sed -n '4p' "$CLAIMS_DIR/$filename" 2>/dev/null)" in
    must-handle) return 0 ;;
    fallback) return 1 ;;
    *) return 2 ;;
  esac
}

# 0 = the task is settled (failure published, or a real answer already exists).
# 1 = NOT settled: another writer may own the destination, so nothing was touched.
publish_terminal_failure() {
  local filename="$1" reason="$2" result temporary rc
  result="$RESULTS_DIR/$filename"
  # The shared readiness contract, not -f/-s: an empty OR whitespace-only body
  # is the undeliverable placeholder state and must not suppress this failure.
  handler_result_exists "$filename" && return 0
  mkdir -p "$RESULTS_DIR"
  temporary="$(mktemp "$RESULTS_DIR/.$filename.XXXXXX.tmp")" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  printf '%s\n' "I could not safely process this Team-tier task because the restricted runtime $reason. No unrestricted fallback was used." > "$temporary"
  # `ln` is the only write to the destination: it establishes ownership or fails.
  # Reading then mutating a path a provider can still claim has no safe ordering.
  if ln "$temporary" "$result" 2>/dev/null; then
    rc=0
    # The scheduler's FAILED: the task ends here, whatever a provider observed.
    ( "${SUTANDO_PY_BIN:-python3}" "$__SCRIPT_DIR/activity_bus.py" transition FAILED --task-file "$TASKS_DIR/$filename" --reason "$reason" >/dev/null 2>&1 & ) 2>/dev/null || true
  elif handler_result_exists "$filename"; then
    rc=0
  else
    echo "watch-tasks-stream: $filename holds an unready result this watcher does not own; leaving it and the claim unsettled rather than publishing a failure over a provider that may still be writing" >&2
    rc=1
  fi
  rm -f "$temporary"
  return "$rc"
}

if [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ] && [ -x "$SUTANDO_TASK_EVENT_HANDLER" ]; then
  DISPATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sutando-task-dispatch.XXXXXX")"
  mkdir "$DISPATCH_DIR/pending" "$DISPATCH_DIR/running" "$DISPATCH_DIR/settled" \
    "$DISPATCH_DIR/workers"
  mkdir -p "$CLAIMS_DIR" "$FALLBACKS_DIR"
  shopt -s nullglob
  for claim in "$CLAIMS_DIR"/task-*.txt; do
    # Overlapping watchers preserve a live owner's claim. A dead owner's
    # record is atomically quarantined before the new sweep retries it.
    claim_is_live "$claim" || retire_stale_claim "$claim" || true
  done
  shopt -u nullglob
fi

acquire_dispatch_lock() {
  [ -n "$DISPATCH_DIR" ] || return 1
  while ! mkdir "$DISPATCH_DIR/lock" 2>/dev/null; do
    [ -d "$DISPATCH_DIR" ] || return 1
    sleep 0.01
  done
}

release_dispatch_lock() {
  rmdir "$DISPATCH_DIR/lock" 2>/dev/null || true
}

finish_handler_task() {
  local marker="$1" task_path="$2" rc="$3" filename settled worker_receipt claim_settled
  filename="$(basename "$task_path")"
  worker_receipt="$DISPATCH_DIR/workers/$filename"
  settled="$DISPATCH_DIR/settled/$filename.worker"
  # Cleanup and the completion path race by atomically moving the same receipt.
  # On failure, keep the durable at-least-once order: fallback receipt, event,
  # then claim release. A signal between event and release may duplicate the
  # event during cleanup, but it cannot strand the task without either path.
  if mv "$marker" "$settled" 2>/dev/null; then
    if [ "$rc" -ne 0 ] && claim_is_ours "$filename"; then
      claim_settled=1
      claim_disposition "$filename"
      case $? in
        0)
          echo "watch-tasks-stream: required Team handler failed for $filename (exit $rc); publishing safe terminal failure" >&2
          # An unsettled publish leaves the claim held rather than clobbering a
          # destination this watcher does not own; cross-restart retry is separate.
          publish_terminal_failure "$filename" "failed" || claim_settled=0
          ;;
        1)
          printf '%s\n' "$task_path" > "$FALLBACKS_DIR/$filename"
          echo "watch-tasks-stream: optional task handler failed for $filename (exit $rc); falling back to live core (possible at-least-once retry)" >&2
          emit_fallback_task_file "$filename"
          ;;
        *)
          echo "watch-tasks-stream: claim for $filename has no recognised disposition; not publishing it to the live core" >&2
          ;;
      esac
      [ "$claim_settled" -eq 1 ] && { release_task_claim "$filename" || true; }
    elif [ "$rc" -eq 0 ]; then
      release_task_claim "$filename" || true
    fi
    rm -f "$settled"
  fi
  rm -f "$worker_receipt"
  drain_dispatch_queue
}

handler_result_exists() {
  # Readiness is delivery/readiness's contract (rejects whitespace-only too) and the
  # live-then-archive lookup is local_task_protocol's; this must not re-decide either.
  local filename="$1" task_id="${filename%.txt}"
  [ -n "$SUTANDO_PY_BIN" ] || return 1
  "$SUTANDO_PY_BIN" - "$__REPO_ROOT" "$RESULTS_DIR" "$task_id" <<'PYEOF' 2>/dev/null
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "src"))
from local_task_protocol import find_result
from delivery.readiness import read_ready_result
found = find_result(pathlib.Path(sys.argv[2]), sys.argv[3])
raise SystemExit(0 if found is not None and read_ready_result(found) is not None else 1)
PYEOF
}

drain_dispatch_queue() {
  local marker candidate task_path running_marker worker_receipt running_count=0
  local filename worker_pid
  # finish_handler_task ends by calling this function, and the dispatch lock is
  # a mkdir spinlock with no timeout — a nested call would deadlock on it.
  [ -n "${DRAIN_ACTIVE:-}" ] && return
  [ -n "$DISPATCH_DIR" ] && [ ! -e "$DISPATCH_DIR/shutting-down" ] || return
  acquire_dispatch_lock || return
  if [ -e "$DISPATCH_DIR/shutting-down" ]; then
    release_dispatch_lock
    return
  fi
  DRAIN_ACTIVE=1
  shopt -s nullglob
  # Count LIVE workers, not marker files: a worker that died before emitting
  # HANDLER_DONE leaves its marker and would retire the slot permanently.
  for marker in "$DISPATCH_DIR/running/"*; do
    filename="$(basename "$marker")"
    worker_pid="$(cat "$DISPATCH_DIR/workers/$filename" 2>/dev/null)"
    if [ -n "$worker_pid" ] && kill -0 "$worker_pid" 2>/dev/null; then
      running_count=$((running_count + 1))
      continue
    fi
    # rc decides whether the sender gets a terminal-failure reply — only when
    # the worker died before producing a deliverable result.
    if handler_result_exists "$filename"; then
      finish_handler_task "$marker" "$(cat "$marker" 2>/dev/null)" 0
    else
      finish_handler_task "$marker" "$(cat "$marker" 2>/dev/null)" 1
    fi
  done
  while [ "$running_count" -lt "$TASK_HANDLER_WORKERS" ]; do
    marker=""
    for candidate in "$DISPATCH_DIR/pending/"*; do
      marker="$candidate"
      break
    done
    [ -n "$marker" ] || break
    running_marker="$DISPATCH_DIR/running/$(basename "$marker")"
    # Cleanup may concurrently settle a pending receipt. Moving it is the
    # ownership boundary: never read or spawn unless this dispatcher won.
    if ! mv "$marker" "$running_marker" 2>/dev/null; then
      continue
    fi
    task_path="$(cat "$running_marker")"
    worker_receipt="$DISPATCH_DIR/workers/$(basename "$marker")"
    : > "$worker_receipt"
    /bin/bash "$0" --handler-runner \
      "$SUTANDO_TASK_EVENT_HANDLER" \
      "${SUTANDO_CORE_RUNTIME:-}" \
      "$WORKSPACE_DIR" \
      "$task_path" \
      "$RESULTS_DIR" \
      "$__REPO_ROOT" \
      "$WATCH_RUNTIME_DIR/events" \
      "$(basename "$marker")" &
    printf '%s\n' "$!" > "$worker_receipt"
    running_count=$((running_count + 1))
  done
  shopt -u nullglob
  release_dispatch_lock
  DRAIN_ACTIVE=""
}

queue_handler_task() {
  local task_path="$1" disposition="${2:-fallback}" filename marker
  filename="$(basename "$task_path")"
  acquire_dispatch_lock || return 1
  if [ -e "$DISPATCH_DIR/shutting-down" ]; then
    release_dispatch_lock
    return 1
  fi
  marker="$DISPATCH_DIR/pending/$filename"
  if [ ! -e "$marker" ] && [ ! -e "$DISPATCH_DIR/running/$filename" ]; then
    if ! acquire_task_claim "$filename" "$task_path" "$disposition"; then
      release_dispatch_lock
      return 0
    fi
    printf '%s\n' "$task_path" > "$marker"
  fi
  release_dispatch_lock
  drain_dispatch_queue
}

dispatch_task() {
  local task_path="$1" rc filename
  filename="$(basename "$task_path")"
  queued_activity_row "$filename"
  if [ -z "$DISPATCH_DIR" ]; then
    emit_dispatch_task_file "$filename"
    return
  fi
  "$SUTANDO_TASK_EVENT_HANDLER" \
    --runtime "${SUTANDO_CORE_RUNTIME:-}" \
    --workspace "$WORKSPACE_DIR" \
    --task-file "$task_path" \
    --results-dir "$RESULTS_DIR" \
    --repo "$__REPO_ROOT" \
    --probe >/dev/null
  rc=$?
  if [ "$rc" -eq 0 ]; then
    if [ -f "$FALLBACKS_DIR/$filename" ]; then
      emit_dispatch_task_file "$filename"
      return
    fi
    queue_handler_task "$task_path" "fallback" || emit_dispatch_task_file "$filename"
  elif [ "$rc" -eq 4 ]; then
    # A required handler is a security boundary. Remove any legacy fallback
    # receipt and never make this task visible to the unrestricted live core.
    rm -f "$FALLBACKS_DIR/$filename"
    if ! queue_handler_task "$task_path" "must-handle"; then
      publish_terminal_failure "$filename" "could not be queued" || true
    fi
  elif [ "$rc" -eq 3 ]; then
    emit_dispatch_task_file "$filename"
  else
    echo "watch-tasks-stream: optional task handler probe failed for $filename (exit $rc); falling back to live core" >&2
    emit_dispatch_task_file "$filename"
  fi
}

# PID file for the Stop-hook cleanup path (see .claude/settings.json Stop
# hook). When a Claude Code session ends, the Stop hook reads this file and
# kills the watcher PID it points at, so the fswatch process doesn't outlive
# the session and turn into an orphan. The trap below removes the file on a
# clean exit; the Stop hook removes it after the kill on dirty exits.
#
# Same workspace resolution as TASKS_DIR (above): M0 cutover routes through
# the canonical loader. Living under state/ matches the workspace contract
# in CLAUDE.md (loose status/state files belong there). Post-v0.8 the legacy
# env-var + hardcoded fallbacks are gone — fail-loud if helper missing.
STATE_DIR="$(bash "$__REPO_ROOT/scripts/sutando-config.sh" workspace)/state"
mkdir -p "$STATE_DIR"
PID_FILE="$STATE_DIR/watch-tasks-stream.pid"
# In place, never write-elsewhere-then-mv: mv preserves mtime, and
# sentinel_pid_wrote_file reads mtime as "when this watcher stamped".
echo "$$" > "$PID_FILE"
# PID-file cleanup is folded into the unified `cleanup` function below so a
# single trap covers both responsibilities (rm + kill children). An earlier
# version set `trap 'rm -f "$PID_FILE"' EXIT` here AND `trap cleanup EXIT...`
# later — the second trap shadowed the first, so the PID file was never
# removed on clean exit. Stale PID files don't break the `kill -0` gate (it
# correctly identifies dead PIDs), but they accumulated forever, and the
# Stop-hook path that relies on this file being current got confused by
# leftover entries from prior sessions. Dirty exits (SIGKILL, panic) still
# skip the trap — the Stop hook + startup reaper cover those.

# tmux socket for the wakeup signal. Sutando.app creates the CLI session via
# this socket. If the socket doesn't exist (different setup), wakeup is a
# silent no-op thanks to 2>/dev/null || true.
# Honors SUTANDO_TMUX_SOCKET (the name start-cli.sh + the desktop private-socket
# runtime use) so the wakeup ping targets the SAME tmux server as the core when
# a caller overrides the default socket; the legacy SUTANDO_TMUX_SOCK is kept as
# a one-release fallback so any straggler setter still works.
TMUX_SOCK="${SUTANDO_TMUX_SOCKET:-${SUTANDO_TMUX_SOCK:-/tmp/sutando-tmux.sock}}"
TMUX_SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"

# Wake helper, kept but NOT called on the task paths below. Under the only
# launch path that exists — Claude Code's `Monitor` tool (CLAUDE.md, the
# schedule-crons / proactive-loop / startup skills, and the menu-app restart) —
# Monitor re-invokes the session on each stdout line, which wakes an IDLE
# session on its own (controlled test 2026-06-13: synthetic task processed in
# ~30s with no poke — see reference_monitor_notification_wakes_idle_session).
# So calling this per task only duplicated the wake and spammed the CLI input
# line on a restart sweep (Chi saw 7-in-a-row, 2026-06-13). The calls were
# removed in #1679. The helper stays for a future setup that runs this watcher
# WITHOUT a Monitor consuming stdout (a bare background process in a tmux
# session) — wire it back into the loops below if you build that path.
# shellcheck disable=SC2317  # defined-but-unreferenced is intentional
_tmux_wake() {
  # Poke the idle CLI session so it processes the new task without waiting
  # for the next 5-min proactive-loop cron tick (sutando-skills#27 / #1289).
  tmux -S "$TMUX_SOCK" send-keys -t "$TMUX_SESSION" '[watcher-ping]' Enter 2>/dev/null || true
}

# Clean up on exit:
# - rm PID file (so the next session's PID-gate check sees "absent" rather
#   than a stale entry that needs `kill -0` to disqualify).
# - kill 0 → kill all processes in this process group, including the
#   fswatch subprocess (Mode B fix — #1088). Without this, when the parent
#   shell exits the watcher reparents to launchd (PPID=1) and runs
#   indefinitely with no consumer, silently dropping every event.
# - `trap '' TERM HUP INT` right before kill 0: this process IS a member of
#   its own process group, so `kill 0` re-delivers TERM/HUP/INT to itself —
#   while already inside a trap handler for one of those same signals. On
#   some bash/kernel combinations that self-delivery re-enters the trap
#   before `exit 0` runs, so the process never actually terminates on a
#   plain signal (only `kill -9` stops it). Ignoring the signals we're about
#   to re-send to ourselves closes that window; the process is exiting
#   either way so nothing downstream needs to observe them again.
fallback_outstanding_handlers() {
  local marker task_path filename settled made_progress found claim owner_id cleanup_ready claim_settled
  local worker_receipt worker_pid job_pid
  [ -n "$DISPATCH_DIR" ] && [ -d "$DISPATCH_DIR" ] || return
  : > "$DISPATCH_DIR/shutting-down"
  shopt -s nullglob
  while true; do
    # A drain that acquired its lock just before shutdown may move a pending
    # receipt after this glob. Rescan until both namespaces are empty so that
    # ownership transfer cannot make cleanup miss the running receipt.
    found=0
    made_progress=0
    for marker in "$DISPATCH_DIR/pending/"* "$DISPATCH_DIR/running/"*; do
      found=1
      settled="$DISPATCH_DIR/settled/$(basename "$marker").cleanup"
      mv "$marker" "$settled" 2>/dev/null || continue
      task_path="$(cat "$settled")"
      filename="$(basename "$task_path")"
      if claim_is_ours "$filename"; then
        claim_settled=1
        claim_disposition "$filename"
        case $? in
          0)
            echo "watch-tasks-stream: required Team handler interrupted for $filename; publishing safe terminal failure" >&2
            # As above: hold the claim rather than publish over a destination this
            # watcher does not own.
            publish_terminal_failure "$filename" "was interrupted" || claim_settled=0
            ;;
          1)
            printf '%s\n' "$task_path" > "$FALLBACKS_DIR/$filename"
            echo "watch-tasks-stream: optional task handler interrupted for $filename; falling back to live core (possible at-least-once retry)" >&2
            emit_task_file "$filename"
            ;;
          *)
            echo "watch-tasks-stream: claim for $filename has no recognised disposition; not publishing it to the live core" >&2
            ;;
        esac
        [ "$claim_settled" -eq 1 ] && { release_task_claim "$filename" || true; }
      fi
      rm -f "$settled"
      made_progress=1
    done
    [ "$found" -eq 1 ] || break
    [ "$made_progress" -eq 1 ] || sleep 0.01
  done
  # A signal can land after atomic claim publication but before the pending
  # receipt write, or after a worker moved its receipt but before completion
  # was consumed. Persist and emit before releasing: duplicate delivery is
  # acceptable here, while release-before-emit could permanently strand work.
  for claim in "$CLAIMS_DIR"/task-*.txt; do
    owner_id="$(sed -n '2p' "$claim" 2>/dev/null)"
    [ "$owner_id" = "$WATCHER_ID" ] || continue
    task_path="$(sed -n '3p' "$claim" 2>/dev/null)"
    [ -n "$task_path" ] || continue
    filename="$(basename "$task_path")"
    claim_settled=1
    claim_disposition "$filename"
    case $? in
      0)
        echo "watch-tasks-stream: required Team handler interrupted for $filename; publishing safe terminal failure" >&2
        # Hold the claim rather than release: a task that is neither delivered nor
        # failed must keep its last record. Cross-restart retry is separate work.
        publish_terminal_failure "$filename" "was interrupted" || claim_settled=0
        ;;
      1)
        printf '%s\n' "$task_path" > "$FALLBACKS_DIR/$filename"
        echo "watch-tasks-stream: optional task handler interrupted for $filename; falling back to live core (possible at-least-once retry)" >&2
        emit_task_file "$filename"
        ;;
      *)
        echo "watch-tasks-stream: claim for $filename has no recognised disposition; not publishing it to the live core" >&2
        ;;
    esac
    [ "$claim_settled" -eq 1 ] && { release_task_claim "$filename" || true; }
  done

  # Claims and fallback events are durable now. TERM the whole process group,
  # then explicitly KILL and reap direct jobs/worker runners: an asynchronous
  # bash can otherwise survive long enough to retain stdout/stderr pipe FDs.
  kill -TERM 0 2>/dev/null || true
  GROUP_TERM_SENT=1
  for worker_receipt in "$DISPATCH_DIR/workers/"*; do
    worker_pid="$(cat "$worker_receipt" 2>/dev/null)"
    case "$worker_pid" in
      ""|*[!0-9]*) ;;
      *)
        kill -KILL "$worker_pid" 2>/dev/null || true
        wait "$worker_pid" 2>/dev/null || true
        ;;
    esac
    rm -f "$worker_receipt"
  done
  while IFS= read -r job_pid; do
    case "$job_pid" in
      ""|*[!0-9]*) continue ;;
    esac
    kill -KILL "$job_pid" 2>/dev/null || true
    wait "$job_pid" 2>/dev/null || true
  done < <(jobs -pr 2>/dev/null)

  # A killed completion path can leave its atomically-owned settled receipt or
  # mkdir lock behind. Claims were reconciled above, and shutting-down still
  # prevents new dispatch, so these local artifacts are now safe to sweep.
  for settled in "$DISPATCH_DIR/settled/"*.worker \
      "$DISPATCH_DIR/settled/"*.cleanup \
      "$DISPATCH_DIR/settled/claim-"*; do
    rm -f "$settled"
  done
  rmdir "$DISPATCH_DIR/lock" 2>/dev/null || true
  shopt -u nullglob
  cleanup_ready=1
  rmdir "$DISPATCH_DIR/lock" 2>/dev/null || [ ! -d "$DISPATCH_DIR/lock" ] || cleanup_ready=0
  rmdir "$DISPATCH_DIR/pending" 2>/dev/null || cleanup_ready=0
  rmdir "$DISPATCH_DIR/running" 2>/dev/null || cleanup_ready=0
  rmdir "$DISPATCH_DIR/settled" 2>/dev/null || cleanup_ready=0
  rmdir "$DISPATCH_DIR/workers" 2>/dev/null || cleanup_ready=0
  if [ "$cleanup_ready" -eq 1 ]; then
    rm -f "$DISPATCH_DIR/shutting-down"
    rmdir "$DISPATCH_DIR" 2>/dev/null || true
  fi
  [ -d "$DISPATCH_DIR" ] || DISPATCH_DIR=""
}

cleanup() {
  [ "${CLEANING_UP:-0}" -eq 0 ] || return
  CLEANING_UP=1
  # FIRST, before any release/kill/sweep work: the drain and queue guards read
  # this file, so anything they do before it exists can still promote a worker.
  if [ -n "${DISPATCH_DIR:-}" ] && [ -d "$DISPATCH_DIR" ]; then
    : > "$DISPATCH_DIR/shutting-down"
  fi
  # EXIT and signal traps share this function. Disarm EXIT before spawning
  # cleanup helpers so a subshell cannot recursively re-enter the trap.
  trap - EXIT
  trap '' TERM HUP INT
  # A duplicate watcher can overwrite the sentinel before the stale watcher
  # exits. Only the watcher named by the file may remove it; otherwise the live
  # watcher would look orphaned and recovery would spawn another duplicate.
  sentinel_release_if_owner "$PID_FILE" "$$"
  if [ -n "${FSWATCH_PID:-}" ]; then
    kill -TERM "$FSWATCH_PID" 2>/dev/null || true
  fi
  if declare -F fallback_outstanding_handlers >/dev/null; then
    fallback_outstanding_handlers
  fi
  if [ -n "${WATCH_RUNTIME_DIR:-}" ]; then
    rm -f "$WATCH_RUNTIME_DIR/events"
    rmdir "$WATCH_RUNTIME_DIR" 2>/dev/null || true
  fi
  if [ "${GROUP_TERM_SENT:-0}" -eq 0 ]; then
    kill -TERM 0 2>/dev/null || true
  fi
}
trap cleanup EXIT
# HUP/INT/TERM must explicitly exit after cleanup — a trap only overrides the
# signal's default disposition, it doesn't terminate the process on its own.
# Without the explicit `exit`, `kill <pid>` (plain SIGTERM) ran cleanup() and
# then let the fswatch read-loop resume, so the process never actually died
# (confirmed 2026-07-01: had to `kill -9` to stop stragglers that `kill`
# alone left running). `exit 0` here also re-fires the EXIT trap above, but
# cleanup() is idempotent (rm -f on an already-removed file, kill 0 on an
# already-terminating group are both safe no-ops).
trap 'cleanup; exit 0' HUP INT TERM

# Initial sweep — surface any pre-existing tasks that arrived during a
# restart gap. Install cleanup first so an immediately exiting fswatch cannot
# kill a just-started provider before its durable fallback receipt is emitted.
shopt -s nullglob
for f in "$TASKS_DIR"/*.txt; do
  dispatch_task "$f"
done
shopt -u nullglob

# Stream subsequent events. -l 0.5 = 500ms latency batch (fswatch coalesces
# burst events). --event Created --event Renamed catches new file
# appearance whether it lands as a fresh write or a rename-into-place.
#
# TWO filters before emit:
#
# 1. Parent-dir match: the macOS FSEvents monitor (fswatch's default) is
#    recursive even without `-r`, so a rename from `tasks/X.txt` to
#    `tasks/archive/.../X.txt` fires events for BOTH the source AND the
#    destination — and the destination path is in a subdir we don't care
#    about. We only want events for files that landed AS A DIRECT CHILD
#    of $TASKS_DIR. `dirname "$path"` against the absolute watched dir
#    catches this. Caught 2026-05-03 #2: archives in tasks/archive/2026-05/
#    were re-firing TASK_FILE: <name> with a different path but the same
#    basename, making the agent re-process every just-archived task.
#
# 2. Existence check: fswatch fires Renamed events on BOTH ends of a
#    rename — including the source path AFTER the file has moved out.
#    `[ -f "$path" ]` filters those rename-OUT-of-watched-dir events.
#    Caught 2026-05-03 #1 (PR #572).
#
# Mode A fix (#1088): `|| exit 0` on printf — if the consumer pipe is
# dead, the first failed write exits immediately instead of silently
# buffering ~100 events into the kernel pipe buffer.
fswatch \
  -l 0.5 \
  --event Created \
  --event Renamed \
  "$TASKS_DIR" > "$WATCH_RUNTIME_DIR/events" 2>/dev/null &
FSWATCH_PID=$!
while IFS= read -r path; do
  case "$path" in
    "HANDLER_DONE: "*)
      completion="${path#HANDLER_DONE: }"
      handler_rc="${completion%% *}"
      filename="${completion#* }"
      case "$handler_rc" in
        ""|*[!0-9]*) handler_rc=1 ;;
      esac
      running_marker="$DISPATCH_DIR/running/$filename"
      if [ -f "$running_marker" ]; then
        task_path="$(cat "$running_marker")"
        finish_handler_task "$running_marker" "$task_path" "$handler_rc"
      fi
      ;;
    *.txt)
      parent="$(dirname "$path")"
      if [ "$parent" = "$TASKS_DIR_ABS" ] && [ -f "$path" ]; then
        # Graceful-shutdown gate (#2165): hold new tasks while the sentinel is present;
        # emitting one mid-shutdown would orphan it.
        if [ -f "$STATE_DIR/shutdown.sentinel" ]; then
          continue
        fi
        dispatch_task "$path"
      fi
      ;;
  esac
done < "$WATCH_RUNTIME_DIR/events"
