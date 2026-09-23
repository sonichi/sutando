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
#   TASK_FILE: <name>   a basename, or an ABSOLUTE path when a resolver is set.
#   INITIAL_SCAN block at startup for pre-existing files, same shape.
#
# The agent reads the named files via the Read tool when notifications
# arrive — no need to inline file contents in stdout (Monitor's 200ms
# batching window would group multi-line content awkwardly).

# fd 9 is a stable dup of the real stdout, taken before anything can rebind fd 1.
# A shutdown emit invoked one $( ) deep writes to the capture pipe, not to stdout.
exec 9>&1

set -u

# Defined above the runner because the runner is where a worker can put its name
# down BEFORE the handler publishes the result that record attributes.
record_worker_done() {
  local task_id="${1%.txt}" stage="$2" ws="$3"
  # Only a pool recipient has a claim to record, and the core cannot locate the
  # writer: its spawner injects one, so unset means "not a worker", not an error.
  [ -n "${SUTANDO_INSTANCE_ID:-}" ] || return 0
  [ -n "${SUTANDO_POOL_DELIVERY_SCRIPT:-}" ] || return 0
  # `-f` not `-x`: we hand it to the interpreter below, so the execute bit is
  # the wrong property to require of a script the pool may ship non-executable.
  [ -f "${SUTANDO_POOL_DELIVERY_SCRIPT}" ] || return 1
  # The RESOLVED interpreter, never the shebang: a worker's PATH python3 may be
  # the macOS CLT stub, which is why the launcher forwards one at all.
  [ -n "${SUTANDO_PY_BIN:-}" ] || return 1
  "$SUTANDO_PY_BIN" "$SUTANDO_POOL_DELIVERY_SCRIPT" \
    --workspace "$ws" --recipient "$SUTANDO_INSTANCE_ID" \
    mark-done --task-id "$task_id" --stage "$stage" >/dev/null || return 1
}

__SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=watcher_sentinel.sh
source "$__SCRIPT_DIR/watcher_sentinel.sh"
# shellcheck source=task-emit.sh
source "$__SCRIPT_DIR/task-emit.sh"
# shellcheck source=inbox-resolve.sh
source "$__SCRIPT_DIR/inbox-resolve.sh"
# shellcheck source=agent/task-event-handler-lookup.sh
source "$__SCRIPT_DIR/agent/task-event-handler-lookup.sh"
# shellcheck source=tasks-dir-resolve.sh
source "$__SCRIPT_DIR/tasks-dir-resolve.sh"
__REPO_ROOT="$(cd "$__SCRIPT_DIR/.." && pwd)"

# --role/--inbox are stripped before the positional TASKS_DIR check below so a
# tagged invocation (`watch-tasks-stream.sh /path --role session --inbox /path`)
# still resolves its tasks dir the same as an untagged one. Logged to stderr
# only, never into the sentinel: three readers int() that file as a bare pid
# (watcher_sentinel.sh).
WATCHER_ROLE=""
WATCHER_INBOX_TAG=""
FORCE_RESTART=""
__args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --role) WATCHER_ROLE="${2:-}"; shift 2 ;;
    --role=*) WATCHER_ROLE="${1#--role=}"; shift ;;
    --inbox) WATCHER_INBOX_TAG="${2:-}"; shift 2 ;;
    --inbox=*) WATCHER_INBOX_TAG="${1#--inbox=}"; shift ;;
    --force-restart) FORCE_RESTART=1; shift ;;
    *) __args+=("$1"); shift ;;
  esac
done
set -- "${__args[@]+"${__args[@]}"}"
# A watcher is started by its monitor and says so: the session Monitor command
# or a notifier's standby. No tag, no start, and no sentinel is touched.
case "$WATCHER_ROLE" in
  session|standby) ;;
  "") echo "watch-tasks-stream: refusing to start: no --role. A watcher is started by its monitor; pass --role session|standby --inbox <dir>." >&2; exit 64 ;;
  *) echo "watch-tasks-stream: refusing to start: --role must be session or standby, got '$WATCHER_ROLE'." >&2; exit 64 ;;
esac
if [ -z "$WATCHER_INBOX_TAG" ]; then
  echo "watch-tasks-stream: refusing to start: no --inbox. Pass --role $WATCHER_ROLE --inbox <dir>; the tag is what the supervisor and the self-check read." >&2
  exit 64
fi
# The tag names the inbox when no positional does, so the two can never differ.
[ $# -gt 0 ] || set -- "$WATCHER_INBOX_TAG"
echo "watch-tasks-stream: role=$WATCHER_ROLE inbox=$WATCHER_INBOX_TAG pid=$$" >&2

# One resolver (tasks-dir-resolve.sh) for this watcher and the supervisor, so the
# two can never name different inboxes: explicit arg -> SUTANDO_TASKS_DIR -> M0 loader.
TASKS_DIR="$(resolve_tasks_dir "${1:-}" "$__REPO_ROOT")" || {
  echo "watch-tasks-stream: cannot resolve workspace — scripts/sutando-config.sh not found at \$__REPO_ROOT. Verify the sutando checkout is intact." >&2
  exit 1
}
mkdir -p "$TASKS_DIR"
# Canonicalize watched dir for the parent-dir filter below. fswatch always
# emits PHYSICAL paths (e.g. /private/tmp/... not /tmp/...), so we resolve
# symlinks with `pwd -P` to match. Without -P, on macOS the comparison
# `dirname "$path"` == `$TASKS_DIR_ABS` fails when /tmp is symlinked to
# /private/tmp — which is the default.
TASKS_DIR_ABS="$(canonical_tasks_dir "$TASKS_DIR")"
if [ "$(canonical_tasks_dir "$WATCHER_INBOX_TAG")" != "$TASKS_DIR_ABS" ]; then
  echo "watch-tasks-stream: refusing to start: --inbox $WATCHER_INBOX_TAG names a different directory than the inbox operand $TASKS_DIR_ABS." >&2
  exit 64
fi
# A watcher on <ws>/deliveries/<id> must not infer the workspace from its
# inbox; whoever named that inbox names the workspace too (tasks-dir-resolve.sh).
WORKSPACE_DIR="$(workspace_dir_for_inbox "$TASKS_DIR")"
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$WORKSPACE_DIR/results}"

# shellcheck source=../scripts/python-binary.sh
. "$__REPO_ROOT/scripts/python-binary.sh"
SUTANDO_PY_BIN="$(require_python "$__REPO_ROOT" "watch tasks")" || exit 1

# One announcer per inbox, enforced here rather than by every launcher: a start
# over a holder exits 0 as covered and says so on stdout, and only
# --force-restart replaces the holder, whatever its kind. The one exception is a
# session watcher over a supervisor's standby, which proceeds: the supervisor
# stands its standby down once this one proves ready. An untagged holder is
# nobody's standby, so it counts as covered.
# A holder must be PROVEN: an unreadable process table, or a line that cannot be
# decided, starts the watcher anyway. Refusing would leave the inbox with no
# announcer at all, which is worse than the duplicate this check prevents.
__my_kind="$WATCHER_ROLE"
__holders="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/watcher_identity.py" inbox-holders --inbox "$TASKS_DIR_ABS" --exclude "$$" 2>/dev/null)" || __holders="unobserved"
case "$__holders" in
  none) ;;
  unobserved|"")
    echo "watch-tasks-stream: could not read the process table; starting without the duplicate check on $TASKS_DIR_ABS" >&2 ;;
  *)
    while IFS=' ' read -r __hpid __hrole; do
      [ -n "$__hpid" ] || continue
      if [ -z "$FORCE_RESTART" ]; then
        if [ "$__my_kind" = "session" ] && [ "$__hrole" = "standby" ]; then
          continue   # the designed handoff: the standby leaves once this watcher is ready
        fi
        # stdout, in the TASK_FILE shape: a Monitor-hosted caller sees stdout as
        # its event stream and would never read the stderr line.
        __hread="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/watcher_identity.py" output-sink "$__hpid" 2>/dev/null | sed -n 's/^read=//p')"
        __hsince="$(ps -o lstart= -p "$__hpid" 2>/dev/null | sed 's/^ *//; s/ *$//')"
        echo "WATCHER_HELD: inbox=$TASKS_DIR_ABS pid=$__hpid role=$__hrole since=\"${__hsince:-unknown}\" read=${__hread:-unknown} replace=\"watch-tasks-stream.sh --force-restart --role ${WATCHER_ROLE} --inbox $TASKS_DIR_ABS\""
        echo "watch-tasks-stream: $TASKS_DIR_ABS is already watched by pid $__hpid ($__hrole); exiting 0. Use --force-restart to replace it." >&2
        exit 0
      fi
      echo "watch-tasks-stream: --force-restart: stopping watcher pid $__hpid ($__hrole) on $TASKS_DIR_ABS" >&2
      # A parent that has not reaped the holder leaves a zombie that kill -0
      # still sees; its fswatch child is collected first so it cannot linger.
      # Each child is captured with its start time: a recycled pid has another.
      __hkids=""
      for __k in $(pgrep -P "$__hpid" 2>/dev/null || true); do
        __kstart="$(ps -o lstart= -p "$__k" 2>/dev/null | sed 's/^ *//; s/ *$//')"
        # An empty start time could never be re-proven later; say so instead of
        # carrying a child that would be silently left running.
        if [ -z "$__kstart" ]; then
          echo "watch-tasks-stream: --force-restart: child pid $__k of $__hpid has no readable start time; not signaled" >&2
          continue
        fi
        __hkids="$__hkids$__k|$__kstart
"
      done
      # Live means "not proven gone": kill -0 also answers for a zombie, so ps stat
      # settles that, and a ps that cannot answer leaves the pid live.
      __holder_live() {
        local s; kill -0 "$1" 2>/dev/null || return 1
        s="$(ps -o stat= -p "$1" 2>/dev/null)" || return 0
        [ -n "$s" ] || return 1; [ "${s#Z}" = "$s" ]
      }
      # 0: the pid still holds this inbox; 1: observed, and it does not; 2: could
      # not observe (ps unreadable, or an undecidable line that could be it).
      __still_holder() {
        local out
        out="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/watcher_identity.py" inbox-holders \
          --inbox "$TASKS_DIR_ABS" --exclude "$$" 2>&1)" || return 2
        printf '%s\n' "$out" | grep -q "^$1 " && return 0
        printf '%s\n' "$out" | grep -q '^undecided=' && return 2
        return 1
      }
      __abort_blind() {
        echo "watch-tasks-stream: pid $1 is still live but cannot be re-proven as the holder of $TASKS_DIR_ABS; not starting and not signaling it" >&2
        exit 3
      }
      # A signal goes only to a pid re-proven as the holder right before it: a dead
      # holder's pid can be recycled onto an unrelated process.
      __signaled=0
      __still_holder "$__hpid"; __sh=$?
      case "$__sh" in
        0) kill -TERM "$__hpid" 2>/dev/null || true; __signaled=1 ;;
        2) __holder_live "$__hpid" && __abort_blind "$__hpid" ;;
      esac
      for _ in $(seq 1 100); do __holder_live "$__hpid" || break; sleep 0.1; done
      if __holder_live "$__hpid"; then
        __still_holder "$__hpid"; __sh=$?
        case "$__sh" in
          0) kill -KILL "$__hpid" 2>/dev/null || true; __signaled=1
             for _ in $(seq 1 30); do __holder_live "$__hpid" || break; sleep 0.1; done ;;
          2) __abort_blind "$__hpid" ;;
        esac
      fi
      if __holder_live "$__hpid"; then
        __still_holder "$__hpid"; __sh=$?
        case "$__sh" in
          0) echo "watch-tasks-stream: pid $__hpid did not exit; not starting" >&2; exit 3 ;;
          2) __abort_blind "$__hpid" ;;
        esac
      fi
      # The children were captured from the holder; only a holder this watcher
      # signaled can have left them behind, and only a pid whose start time still
      # matches its capture is that child rather than a process reusing its pid.
      if [ "$__signaled" = 1 ]; then
        while IFS='|' read -r __k __kstart; do
          [ -n "$__k" ] || continue
          [ "$(ps -o lstart= -p "$__k" 2>/dev/null | sed 's/^ *//; s/ *$//')" = "$__kstart" ] || continue
          kill -TERM "$__k" 2>/dev/null || true
        done <<< "$__hkids"
      fi
    done <<< "$__holders"
    ;;
esac
unset __my_kind __holders __hpid __hrole __hkids __k __kstart __hread __hsince __sh __signaled
unset -f __holder_live __still_holder __abort_blind 2>/dev/null || true
# Optional task handlers run synchronously, inline -- see run_handler_now().
HANDLER_STATE_READY=""
WATCH_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sutando-task-watch.XXXXXX")"
mkfifo "$WATCH_RUNTIME_DIR/events"
FSWATCH_PID=""
CLEANING_UP=0
CLAIMS_DIR="$WORKSPACE_DIR/state/task-event-handler-claims"
# Per instance: this receipt says "MY optional handler declined this task", and
# a shared one makes another instance bypass its own handler. Owner: util_paths.
FALLBACKS_DIR="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/util_paths.py" handler-fallbacks-dir "$WORKSPACE_DIR/state")" || {
  echo "watch-tasks-stream: could not resolve the fallback receipt dir" >&2
  exit 1
}
WATCHER_ID="$$-${RANDOM:-0}"

# Core only: a worker's own inbox is already the routing decision (#4502), so
# it never reads or watches this file. A skill declares the handler by writing
# it (e.g. worker-pool's register_worker()); this process fswatches it below
# and reloads CURRENT_HANDLER the moment it changes -- no restart needed.
HANDLER_CONFIG_PATH=""
HANDLER_CONFIG_DIR=""
CURRENT_HANDLER=""
if [ -z "${SUTANDO_INSTANCE_ID:-}" ]; then
  HANDLER_CONFIG_PATH="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/util_paths.py" task-event-handler-config-path "$WORKSPACE_DIR/state")" || {
    echo "watch-tasks-stream: could not resolve the task-event-handler config path" >&2
    exit 1
  }
  HANDLER_CONFIG_DIR="$(dirname "$HANDLER_CONFIG_PATH")"
  # fswatch names this directory by its physical path; handle_event compares to it.
  if [ -n "$HANDLER_CONFIG_PATH" ]; then
    mkdir -p "$HANDLER_CONFIG_DIR"
    HANDLER_CONFIG_DIR="$(canonical_tasks_dir "$HANDLER_CONFIG_DIR")" || {
      echo "watch-tasks-stream: could not canonicalize the task-event-handler config dir" >&2
      exit 1
    }
    HANDLER_CONFIG_PATH="$HANDLER_CONFIG_DIR/${HANDLER_CONFIG_PATH##*/}"
  fi
fi

# absent: no config on disk (the core takes every task). ready: parsed. broken: a
# config exists but could not be copied or parsed; nothing routes on it.
HANDLER_STATE="absent"
HELD_NAMES=""
HELD_RETRY_AT=0
DISPATCHED_IDS=""
DECISION_IDENTITY=""
# Called only where a task is actually admitted: announced to the core, or
# handed to the handler with the claim held. A decision that admits nothing
# records nothing.
record_admission() {
  [ -z "$DECISION_IDENTITY" ] || DISPATCHED_IDS="$DISPATCHED_IDS$DECISION_IDENTITY
"
}
# `ls -di` is one line on every POSIX ls; GNU `stat -f` prints a filesystem dump.
task_file_identity() {
  local inode sum
  inode="$(ls -di -- "$1" 2>/dev/null | awk 'NR==1 {print $1}')"
  sum="$(cksum < "$1" 2>/dev/null | awk 'NR==1 {print $1 "-" $2}')"
  printf '%s' "${inode}:${sum}"
}
# A held set replays at once on a task or config event; on any other watched
# event it replays at most this often. No timer exists.
HELD_RETRY_INTERVAL="${SUTANDO_HELD_RETRY_INTERVAL:-30}"
# One read per routing decision: the bytes are copied once into a private
# snapshot and parsed from there; no cache, no compare, nothing to go stale.
read_handler_config_now() {
  local snap="$WATCH_RUNTIME_DIR/handler-config.snap" state="absent" handler=""
  if [ -n "$HANDLER_CONFIG_PATH" ]; then
    # A dangling or cyclic symlink is a config that exists and cannot be read.
    if [ ! -e "$HANDLER_CONFIG_PATH" ] && [ ! -L "$HANDLER_CONFIG_PATH" ]; then
      handler="$(task_event_handler "$snap.none")" || handler=""
    elif cat -- "$HANDLER_CONFIG_PATH" > "$snap" 2>/dev/null && handler="$(task_event_handler "$snap")"; then
      state="ready"
    else
      state="broken"
      handler=""
    fi
    rm -f "$snap"
  fi
  if [ "$state" = "broken" ] && [ "$HANDLER_STATE" != "broken" ]; then
    echo "watch-tasks-stream: task-event-handler config exists but cannot be read; holding every task until it can" >&2
  fi
  HANDLER_STATE="$state"
  CURRENT_HANDLER="$handler"
}
reload_current_handler() { read_handler_config_now; }
# Tasks decided while the config was broken; replayed at most once per event.
redispatch_held_tasks() {
  local held="$HELD_NAMES" fn
  [ -n "$held" ] || return 0
  # Mid-shutdown a held task stays held: the next lifetime's sweep takes it.
  [ -f "$STATE_DIR/shutdown.sentinel" ] && return 0
  HELD_NAMES=""
  HELD_RETRY_AT=$(( $(date +%s) + HELD_RETRY_INTERVAL ))
  while IFS= read -r fn; do
    [ -n "$fn" ] && [ -f "$TASKS_DIR/$fn" ] && dispatch_task "$TASKS_DIR/$fn"
  done <<< "$held"
}
# An elapsed deadline, checked after every event; a busy stream never resets it.
retry_held_tasks_if_due() {
  [ -n "$HELD_NAMES" ] || return 0
  [ "$(date +%s)" -ge "$HELD_RETRY_AT" ] || return 0
  redispatch_held_tasks
}
[ -n "$HANDLER_CONFIG_PATH" ] && reload_current_handler

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
  # Same atomic move-away-then-delete shape as retire_stale_claim: no separate
  # scratch dir needed now that there's no async queue to house one in.
  retired="$CLAIMS_DIR/.settled-$WATCHER_ID-$filename"
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
# One place for "this delivery is over", so every terminal publication settles the
# record rather than each call site remembering to.
settle_worker_record() {
  record_worker_done "$1" done "$WORKSPACE_DIR" || true
}

publish_terminal_failure() {
  # $3 is the resolved payload: a sentinel entry's FAILED row must key on the
  # body the resolver named, never the sentinel a basename alone would resolve.
  local filename="$1" reason="$2" payload="${3:-$TASKS_DIR/$1}" result temporary rc
  result="$RESULTS_DIR/$filename"
  # The shared readiness contract, not -f/-s: an empty OR whitespace-only body
  # is the undeliverable placeholder state and must not suppress this failure.
  if handler_result_exists "$filename"; then
    settle_worker_record "$filename"
    return 0
  fi
  mkdir -p "$RESULTS_DIR"
  temporary="$(mktemp "$RESULTS_DIR/.$filename.XXXXXX.tmp")" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  printf '%s\n' "I $TERMINAL_REFUSAL_MARK this Team-tier task because the restricted runtime $reason. No unrestricted fallback was used." > "$temporary"
  # `ln` is the only write to the destination: it establishes ownership or fails.
  # Reading then mutating a path a provider can still claim has no safe ordering.
  if ln "$temporary" "$result" 2>/dev/null; then
    rc=0
    # The scheduler's FAILED: the task ends here, whatever a provider observed.
    ( "${SUTANDO_PY_BIN:-python3}" "$__SCRIPT_DIR/activity_bus.py" transition FAILED --task-file "$payload" --reason "$reason" >/dev/null 2>&1 & ) 2>/dev/null || true
  elif handler_result_exists "$filename"; then
    rc=0
  else
    echo "watch-tasks-stream: $filename holds an unready result this watcher does not own; leaving it and the claim unsettled rather than publishing a failure over a provider that may still be writing" >&2
    rc=1
  fi
  rm -f "$temporary"
  # A terminal publication settles the delivery, so the record must reach its
  # published stage or `residue` reads `completed` forever and nothing retires it.
  [ "$rc" -eq 0 ] && settle_worker_record "$filename"
  return "$rc"
}

# Only core makes a routing decision (should this task go to a bound worker);
# a worker's own inbox already IS that decision, made by whoever delivered the
# sentinel there (#4502). So a worker never probes a handler, regardless of
# CURRENT_HANDLER, and only core's branch ever calls this.
#
# Idempotent, so a handler declared later still works with no restart; only
# the claim bookkeeping remains -- handler runs are synchronous, no queue.
prepare_handler_state() {
  [ -z "$HANDLER_STATE_READY" ] || return 0
  mkdir -p "$CLAIMS_DIR" "$FALLBACKS_DIR"
  shopt -s nullglob
  for claim in "$CLAIMS_DIR"/task-*.txt; do
    # Overlapping watchers preserve a live owner's claim. A dead owner's
    # record is atomically quarantined before the new sweep retries it.
    claim_is_live "$claim" || retire_stale_claim "$claim" || true
  done
  shopt -u nullglob
  HANDLER_STATE_READY=1
}

if [ -n "$CURRENT_HANDLER" ] && [ -x "$CURRENT_HANDLER" ]; then
  prepare_handler_state
fi

# Our own terminal-refusal wording, shared by the writer and the reader below so
# a reworded refusal cannot silently stop counting as one.
TERMINAL_REFUSAL_MARK="could not safely process"

handler_result_is_answer() {
  # An archive-only result (no live file) still belongs to the reap path, not
  # to this guard; unchanged from before this function's fix.
  local filename="$1" live="$RESULTS_DIR/$1" ready first
  [ -f "$live" ] || return 1
  [ -n "$SUTANDO_PY_BIN" ] || return 1
  # Once a live file exists, even a placeholder, the refusal-or-answer line
  # must come from find-ready's own READY path, never a hardcoded $live.
  ready="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/delivery/task_dispatch.py" find-ready "$RESULTS_DIR" "$filename" 2>/dev/null)" || return 1
  # The FIRST line, anchored: an answer that merely mentions the phrase is an
  # answer, and mistaking it for a refusal re-runs work that already completed.
  IFS= read -r first < "$ready" || first=""
  case "$first" in "I $TERMINAL_REFUSAL_MARK"*) return 1 ;; esac
  return 0
}

handler_result_exists() {
  # Completion is delivery/task_dispatch's contract (every archive layout, walked past
  # empty placeholders to a READY body); a first-hit lookup here would re-decide it.
  local filename="$1"
  [ -n "$SUTANDO_PY_BIN" ] || return 1
  "$SUTANDO_PY_BIN" "$__REPO_ROOT/src/delivery/task_dispatch.py" has-result "$RESULTS_DIR" "$filename" 2>/dev/null
}

# Runs the real (non-probe) handler synchronously, inline: the rc is known
# the instant this returns, so no reap-loop can misjudge crashed-vs-finished.
#
# MUST_HANDLE's failure never falls back to the live core (rc 4 always
# outranks the stored disposition); ACCEPT's failure safely may.
SUTANDO_HANDLER_RUN_TIMEOUT="${SUTANDO_HANDLER_RUN_TIMEOUT:-10}"

run_handler_now() {
  local task_path="$1" disposition="${2:-fallback}" filename announce handler_rc verdict claim_settled
  local handler_pid watchdog_pid timeout_flag timed_out
  filename="$(basename "$task_path")"
  announce="$(task_announce "$task_path")"
  prepare_handler_state
  if ! acquire_task_claim "$filename" "$task_path" "$disposition"; then
    return 0
  fi
  record_admission
  activity_transition RUNNING "$task_path"
  timed_out=0
  # `pending` before the run so a result the live core sees always has
  # attribution beside it; an injected-but-broken writer fails the task
  # instead of racing the handler.
  if ! record_worker_done "$filename" pending "$WORKSPACE_DIR"; then
    echo "watch-tasks-stream: could not record ownership of $filename for ${SUTANDO_INSTANCE_ID:-}; not running its handler" >&2
    handler_rc=1
  else
    # Bounded the same way resolve_inbox_entry already bounds a resolver in
    # this same single-threaded dispatch loop: never plain `timeout`, which
    # isn't reliably present (this host has neither `timeout` nor
    # `gtimeout`), and a bare `timeout` with no kill-after leaves a
    # TERM-resistant handler unbounded anyway. A genuinely hung handler was
    # never observed, but is no longer isolated in its own process either
    # now that this call is inline -- SUTANDO_HANDLER_RUN_TIMEOUT (10s,
    # ~250x the measured normal ~35-40ms cost) bounds it regardless.
    timeout_flag="$(mktemp -u "${TMPDIR:-/tmp}/sutando-handler-timeout.XXXXXX")"
    "$CURRENT_HANDLER" \
      --runtime "${SUTANDO_CORE_RUNTIME:-}" \
      --workspace "$WORKSPACE_DIR" \
      --task-file "$task_path" \
      --results-dir "$RESULTS_DIR" \
      --repo "$__REPO_ROOT" >/dev/null &
    handler_pid=$!
    ( trap 'kill "${_s:-}" 2>/dev/null; exit 0' TERM
      sleep "$SUTANDO_HANDLER_RUN_TIMEOUT" & _s=$!; wait "$_s"
      # Reaching here (not cancelled by the handler finishing first) means
      # the timeout genuinely elapsed -- flag it BEFORE killing, so the
      # caller can tell "we gave up waiting" apart from a real exit/signal.
      : > "$timeout_flag"
      kill -TERM "$handler_pid" 2>/dev/null; sleep 1
      kill -KILL "$handler_pid" 2>/dev/null ) &
    watchdog_pid=$!
    wait "$handler_pid" 2>/dev/null
    handler_rc=$?
    kill -TERM "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    if [ -f "$timeout_flag" ]; then
      timed_out=1
      rm -f "$timeout_flag"
    fi
    if [ "$handler_rc" -eq 0 ]; then
      record_worker_done "$filename" done "$WORKSPACE_DIR" || handler_rc=1
    fi
  fi

  if [ "$handler_rc" -ne 0 ] && claim_is_ours "$filename"; then
    claim_settled=1
    # A watchdog timeout means WE gave up waiting -- the handler may still
    # be doing legitimate work (e.g. blocked on a real delivery lock), not
    # declining. Never safe to read that as "optional decline, hand to
    # core" regardless of the stored disposition: treat it the same
    # fail-closed way MUST_HANDLE's own rc 4 verdict is treated. Distinct
    # from the handler's own exit code, which this does not override.
    if [ "$timed_out" -eq 1 ] || [ "$handler_rc" -eq 4 ]; then
      verdict=0
    else
      claim_disposition "$filename"
      verdict=$?
    fi
    case $verdict in
      0)
        if [ "$timed_out" -eq 1 ]; then
          echo "watch-tasks-stream: handler timed out for $filename after ${SUTANDO_HANDLER_RUN_TIMEOUT}s (still running, not a decline); publishing safe terminal failure rather than assuming core may inherit it" >&2
          publish_terminal_failure "$filename" "timed out" "$task_path" || claim_settled=0
        else
          echo "watch-tasks-stream: required Team handler failed for $filename (exit $handler_rc); publishing safe terminal failure" >&2
          # An unsettled publish leaves the claim held rather than clobbering a
          # destination this watcher does not own; cross-restart retry is separate.
          publish_terminal_failure "$filename" "failed" "$task_path" || claim_settled=0
        fi
        ;;
      1)
        printf '%s\n' "$task_path" > "$FALLBACKS_DIR/$filename"
        echo "watch-tasks-stream: optional task handler failed for $filename (exit $handler_rc); falling back to live core (possible at-least-once retry)" >&2
        record_worker_done "$filename" abandon "$WORKSPACE_DIR" || true  # the live core owns it now
        emit_fallback_task_file "$announce"
        ;;
      *)
        echo "watch-tasks-stream: claim for $filename has no recognised disposition; not publishing it to the live core" >&2
        ;;
    esac
    [ "$claim_settled" -eq 1 ] && { release_task_claim "$filename" || true; }
  elif [ "$handler_rc" -eq 0 ]; then
    release_task_claim "$filename" || true
  fi
}

# A name is read relative to the reader's own inbox: a body that resolution
# moved OUT of that inbox must be announced by its full path, not a bare name.

# Shared by dispatch_task's own emit and the queued-handler completion and
# shutdown-recovery paths, which only have the resolved path to work from.
task_announce() {
  local path="$1" dir
  dir="$(cd "$(dirname "$path")" 2>/dev/null && pwd -P)"
  if [ "$dir" = "$TASKS_DIR_ABS" ]; then
    basename "$path"
  else
    printf '%s\n' "$path"
  fi
}

# Order only (urgent > normal > low, mtime FIFO within a tier) -- every
# *.txt; dispatch_task's own checks still decide eligibility, unchanged.
# Empty output with files present is ambiguous (a real empty dir, or the
# helper failing silently) -- fall back to mtime-glob order rather than ever
# silently dropping the backlog; ordering degrades, dispatch never does.
priority_sorted_tasks() {
  local out rc=0 had_files=0 f
  out="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/delivery/task_dispatch.py" sort-by-priority "$TASKS_DIR" 2>/dev/null)" || rc=$?
  if [ -n "$out" ]; then
    printf '%s\n' "$out"
    return 0
  fi
  shopt -s nullglob
  for f in "$TASKS_DIR"/*.txt; do
    had_files=1
    break
  done
  shopt -u nullglob
  [ "$had_files" -eq 1 ] || return 1
  echo "watch-tasks-stream: priority sort broken (rc=$rc); dispatching in mtime order" >&2
  shopt -s nullglob
  for f in "$TASKS_DIR"/*.txt; do
    basename "$f"
  done
  shopt -u nullglob
}

dispatch_task() {
  local task_path="$1" rc filename announce resolved attempt identity
  # Graceful-shutdown gate: every path in (sweep, replay, event, held retry)
  # holds new tasks while the sentinel is present; emitting one would orphan it.
  [ -f "$STATE_DIR/shutdown.sentinel" ] && return 0
  # Resolve before anything observes it: claim, handler and emit must all name
  # the body, never the sentinel that merely pointed at it.
  #
  # A sentinel can be visible to fswatch before its payload's own write is —
  # two separate files, no ordering guarantee between them — so one failed
  # resolve is retried briefly rather than treated as permanent. Same bounded
  # shape as acquire_task_claim's lock race, applied to filesystem visibility
  # instead of lock contention.
  attempt=0
  until resolved="$(resolve_inbox_entry "$task_path")"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 3 ]; then
      echo "watch-tasks-stream: resolve_inbox_entry did not resolve $task_path after $attempt attempts; not dispatching" >&2
      return 0
    fi
    sleep 0.2
  done
  announce="$(task_announce "$resolved")"
  task_path="$resolved"
  filename="$(basename "$task_path")"
  # A sentinel nothing retires is re-swept after every restart, and resolution
  # turns that from re-reading an empty file into RE-RUNNING the real task.
  if handler_result_is_answer "$filename"; then
    printf 'already answered, not dispatching again: %s\n' "$announce" >&2
    return 0
  fi
  # By announce, not filename: a resolved entry's activity row must key on
  # the real payload, never the sentinel that basename alone would resolve.
  queued_activity_row "$announce"
  # Only core makes a routing decision; a worker's own inbox already IS that
  # decision (#4502). CURRENT_HANDLER is never populated for a worker (see the
  # SUTANDO_INSTANCE_ID gate at its assignment above), so this is enforced
  # structurally too, not just by this early return.
  # One admission per file identity per watcher lifetime: a later event for the
  # same bytes in the same inode is not a new task; a replaced file is.
  identity="$filename|$(task_file_identity "$task_path")"
  # A malformed identity never dedupes: a duplicate is recoverable, a silently
  # dropped task is not.
  case "$identity" in
    *"|"[0-9]*:[0-9]*-[0-9]*) ;;
    *) echo "watch-tasks-stream: no usable file identity for $filename; dispatching without dedupe" >&2; identity="" ;;
  esac
  if [ -n "$identity" ] && [ -n "$DISPATCHED_IDS" ] && printf '%s' "$DISPATCHED_IDS" | grep -qxF -- "$identity"; then
    return 0
  fi
  # This decision is its own read: a fresh snapshot, parsed here, used here.
  [ -z "$HELD_NAMES" ] || HELD_NAMES="$(printf '%s' "$HELD_NAMES" | grep -vxF -- "$filename")
"
  read_handler_config_now
  if [ -z "${SUTANDO_INSTANCE_ID:-}" ] && [ "$HANDLER_STATE" = "broken" ]; then
    [ -n "$HELD_NAMES" ] || HELD_RETRY_AT=$(( $(date +%s) + HELD_RETRY_INTERVAL ))
    HELD_NAMES="$HELD_NAMES$filename
"
    echo "watch-tasks-stream: holding $filename: the task-event-handler config exists but cannot be read" >&2
    return 0
  fi
  DECISION_IDENTITY="$identity"
  if [ -n "${SUTANDO_INSTANCE_ID:-}" ] || [ -z "$CURRENT_HANDLER" ] || [ ! -x "$CURRENT_HANDLER" ]; then
    emit_dispatch_task_file "$announce" && record_admission
    return
  fi
  prepare_handler_state
  "$CURRENT_HANDLER" \
    --runtime "${SUTANDO_CORE_RUNTIME:-}" \
    --workspace "$WORKSPACE_DIR" \
    --task-file "$task_path" \
    --results-dir "$RESULTS_DIR" \
    --repo "$__REPO_ROOT" \
    --probe >/dev/null
  rc=$?
  if [ "$rc" -eq 0 ]; then
    if [ -f "$FALLBACKS_DIR/$filename" ]; then
      emit_dispatch_task_file "$announce" && record_admission
      return
    fi
    run_handler_now "$task_path" "fallback"
  elif [ "$rc" -eq 4 ]; then
    # A required handler is a security boundary. Remove any legacy fallback
    # receipt and never make this task visible to the unrestricted live core.
    rm -f "$FALLBACKS_DIR/$filename"
    run_handler_now "$task_path" "must-handle"
  elif [ "$rc" -eq 3 ]; then
    emit_dispatch_task_file "$announce" && record_admission
  else
    echo "watch-tasks-stream: optional task handler probe failed for $filename (exit $rc); falling back to live core" >&2
    emit_dispatch_task_file "$announce" && record_admission
  fi
}

# PID file for the Stop-hook cleanup path (see .claude/settings.json Stop
# hook). When a Claude Code session ends, the Stop hook reads this file and
# kills the watcher PID it points at, so the fswatch process doesn't outlive
# the session and turn into an orphan. The trap below removes the file on a
# clean exit; the Stop hook removes it after the kill on dirty exits.
#
# Derived from WORKSPACE_DIR like CLAIMS_DIR and FALLBACKS_DIR, never re-resolved:
# an argv tasks dir moves every other state path but left this one on the checkout.
STATE_DIR="$WORKSPACE_DIR/state"
mkdir -p "$STATE_DIR"
# Per instance: N watchers on one host each stamped the same file, so the
# readers tracked only the newest. Unset $SUTANDO_INSTANCE keeps the old name.
PID_FILE="$(sentinel_path_for "$STATE_DIR")"
# Stamped only once fswatch is confirmed up (below the launch): until then the
# file may still name a live standby that this watcher must not displace.
WATCHER_BEAT_PID=""

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
#
# SIGTERM interrupts `wait` immediately, so run_handler_now() never resumes
# to settle its own claim -- this settles directly from CLAIMS_DIR instead.
settle_own_claims_on_shutdown() {
  local claim filename task_path announce claim_settled verdict
  [ -n "${CLAIMS_DIR:-}" ] && [ -d "$CLAIMS_DIR" ] || return
  shopt -s nullglob
  for claim in "$CLAIMS_DIR"/task-*.txt; do
    filename="$(basename "$claim")"
    [ "$(sed -n '2p' "$claim" 2>/dev/null)" = "$WATCHER_ID" ] || continue
    task_path="$(sed -n '3p' "$claim" 2>/dev/null)"
    [ -n "$task_path" ] || continue
    announce="$(task_announce "$task_path")"
    claim_settled=1
    claim_disposition "$filename"
    verdict=$?
    case $verdict in
      0)
        echo "watch-tasks-stream: required Team handler interrupted for $filename; publishing safe terminal failure" >&2
        publish_terminal_failure "$filename" "was interrupted" "$task_path" || claim_settled=0
        ;;
      1)
        printf '%s\n' "$task_path" > "$FALLBACKS_DIR/$filename"
        echo "watch-tasks-stream: optional task handler interrupted for $filename; falling back to live core (possible at-least-once retry)" >&2
        record_worker_done "$filename" abandon "$WORKSPACE_DIR" || true  # the live core owns it now
        emit_task_file "$announce"
        ;;
      *)
        echo "watch-tasks-stream: claim for $filename has no recognised disposition; not publishing it to the live core" >&2
        ;;
    esac
    [ "$claim_settled" -eq 1 ] && { release_task_claim "$filename" || true; }
  done
  shopt -u nullglob
}

cleanup() {
  [ "${CLEANING_UP:-0}" -eq 0 ] || return
  CLEANING_UP=1
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
  if [ -n "${WATCHER_BEAT_PID:-}" ]; then
    kill -TERM "$WATCHER_BEAT_PID" 2>/dev/null || true
  fi
  settle_own_claims_on_shutdown
  if [ -n "${WATCH_RUNTIME_DIR:-}" ]; then
    rm -f "$WATCH_RUNTIME_DIR/events"
    rmdir "$WATCH_RUNTIME_DIR" 2>/dev/null || true
  fi
  kill -TERM 0 2>/dev/null || true
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
# restart gap, in priority order. Install cleanup first so an immediately
# exiting fswatch cannot kill a just-started provider before its durable
# fallback receipt is emitted.
startup_sweep() {
  local fn
  while IFS= read -r fn; do
    dispatch_task "$TASKS_DIR/$fn"
  done < <(priority_sorted_tasks)
}
# A session watcher sweeps only once the standby has stopped (below); any other
# role has no peer on its inbox and sweeps before it subscribes, as always.
if [ "$WATCHER_ROLE" != "session" ]; then
  startup_sweep
fi

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
#
# HANDLER_CONFIG_DIR (core only) rides the SAME fswatch process as a second
# path -- not the file itself, which inotify/poll_monitor can't watch reliably before it exists.
fswatch_paths=("$TASKS_DIR")
if [ -n "$HANDLER_CONFIG_DIR" ]; then
  mkdir -p "$HANDLER_CONFIG_DIR"
  fswatch_paths+=("$HANDLER_CONFIG_DIR")
fi
fswatch \
  -l 0.5 \
  --event Created \
  --event Renamed \
  --event Updated \
  "${fswatch_paths[@]}" > "$WATCH_RUNTIME_DIR/events" 2>/dev/null &
FSWATCH_PID=$!
# The writer end opens only once a reader exists, so fswatch execs here, not at
# the launch above; fd 3 stays open so the loop's own open can never be the last reader.
exec 3< "$WATCH_RUNTIME_DIR/events"
fswatch_alive=1
for _ in 1 2 3 4 5; do
  kill -0 "$FSWATCH_PID" 2>/dev/null || { fswatch_alive=0; break; }
  sleep 0.1
done
# Exit before the sentinel stamp: a watcher that cannot serve the inbox leaves
# whatever is serving it untouched.
if [ "$fswatch_alive" -eq 0 ]; then
  echo "watch-tasks-stream: fswatch failed to start; no sentinel written and no standby touched" >&2
  exit 1
fi

# One fswatch line. Shared by the readiness replay and the main loop so a line
# read early is handled exactly as a line read late.
handle_event() {
  local path="$1" parent
  case "$path" in
    "$HANDLER_CONFIG_PATH"|"$HANDLER_CONFIG_DIR")
      # Matches the file OR its bare dir -- poll_monitor reports the watched
      # DIRECTORY, not the file, on a rename-into-place (measured locally).
      # Reload only: the next task (already fswatched separately, on tasks/)
      # sees the new CURRENT_HANDLER in dispatch_task() and run_handler_now()
      # calls the real handler right there -- nothing queued, nothing to
      # drain on a bare config change. No more HANDLER_DONE case here either:
      # that signaled a background --handler-runner subprocess's completion,
      # which no longer exists now that the handler runs synchronously.
      reload_current_handler
      [ -n "$CURRENT_HANDLER" ] && [ -x "$CURRENT_HANDLER" ] && prepare_handler_state
      redispatch_held_tasks
      ;;
    *.txt)
      parent="$(dirname "$path")"
      if [ "$parent" = "$TASKS_DIR_ABS" ] && [ -f "$path" ]; then
        dispatch_task "$path"
        redispatch_held_tasks
      fi
      ;;
  esac
}

# Readiness is a real round-trip: a probe this watcher writes into its own inbox
# must come back through fswatch. Its name matches no admission pattern.
PRE_READY_EVENTS=""
if [ "$WATCHER_ROLE" = "session" ]; then
  READY_DEADLINE=$(( $(date +%s) + ${SUTANDO_WATCHER_READY_TIMEOUT:-10} ))
  probe_n=0
  : > "$TASKS_DIR/.ready-$$-$probe_n"
  ready=0
  while [ "$(date +%s)" -lt "$READY_DEADLINE" ]; do
    if ! IFS= read -r -t 1 path <&3; then
      kill -0 "$FSWATCH_PID" 2>/dev/null || break
      # A probe written before fswatch subscribed raises nothing: write a fresh one.
      probe_n=$((probe_n + 1))
      : > "$TASKS_DIR/.ready-$$-$probe_n"
      continue
    fi
    case "$path" in
      */.ready-$$-*|.ready-$$-*) ready=1; break ;;
      *) PRE_READY_EVENTS="$PRE_READY_EVENTS$path
" ;;
    esac
  done
  rm -f "$TASKS_DIR"/.ready-$$-*
  if [ "$ready" -ne 1 ]; then
    echo "watch-tasks-stream: no event came back from the inbox within ${SUTANDO_WATCHER_READY_TIMEOUT:-10}s; no sentinel written and no standby touched" >&2
    exit 1
  fi
fi
# In place, never write-elsewhere-then-mv: mv preserves mtime, and
# sentinel_pid_wrote_file reads mtime as "when this watcher stamped".
echo "$$" > "$PID_FILE"
# The watcher beat, `state/watchers/<id>.alive` (docs/worker-pool-design.md). It is
# handed this pid and exits when it dies: SIGKILL and a crash run no cleanup trap.
# INJECTED, never located: a core helper may run a path it is handed but must not
# find an optional skill itself (docs/architecture-boundaries.md). Unset = no beat.
if [ -n "${SUTANDO_WATCHER_BEAT:-}" ] && [ -f "${SUTANDO_WATCHER_BEAT}" ]; then
  "$SUTANDO_PY_BIN" "$SUTANDO_WATCHER_BEAT" --workspace "$WORKSPACE_DIR" \
      --kind watcher --id "${SUTANDO_INSTANCE_ID:-core}" --parent-pid "$$" >/dev/null 2>&1 &
  WATCHER_BEAT_PID=$!
fi
# The standby is the supervisor's to stop (its session also hosts the supervisor,
# the only re-arm); sweep once it is gone, but never wait on it forever.
if [ "$WATCHER_ROLE" = "session" ]; then
  STANDBY_DEADLINE=$(( $(date +%s) + ${SUTANDO_STANDBY_STOP_TIMEOUT:-15} ))
  standby="unknown"
  while [ "$(date +%s)" -lt "$STANDBY_DEADLINE" ]; do
    standby="$("$SUTANDO_PY_BIN" "$__REPO_ROOT/src/watcher_identity.py" standby-present --inbox "$TASKS_DIR_ABS" 2>/dev/null)" || standby="unknown"
    [ "$standby" = "no" ] && break
    sleep 0.5
  done
  if [ "$standby" != "no" ]; then
    echo "watch-tasks-stream: standby watcher still present after ${SUTANDO_STANDBY_STOP_TIMEOUT:-15}s; sweeping anyway" >&2
  fi
  # Buffered events first, in fswatch's order, so a task sees the handler config
  # delivered before it; the sweep then covers only what no event announced.
  while IFS= read -r path; do
    [ -n "$path" ] && handle_event "$path"
  done <<< "$PRE_READY_EVENTS"
  startup_sweep
fi
# EOF (fswatch died) ends the loop on the normal exit path; fd 3 is shared with the
# readiness replay above, since a second open of the FIFO would race it for bytes.
while IFS= read -r path <&3; do
  handle_event "$path"
  retry_held_tasks_if_due
done
