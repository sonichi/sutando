#!/bin/bash
# Stop hook: blocks Claude from finishing when unprocessed tasks exist.
# Skips tasks that already have a corresponding result file.
#
# The queue lives under the WORKSPACE, not the repo (CLAUDE.md "Workspace
# contract"). This hook used to resolve `$(dirname "$0")/..` — the repo root —
# so it watched <repo>/tasks/ while every producer and consumer used
# <workspace>/tasks/. That directory is empty on a normal install, so the hook
# emitted `{}` on every Stop and never blocked on anything: a guard that cannot
# fire is a guard that is switched off.
#
# Resolve through the same helper every other service uses, so a configured
# workspace (sutando.config.local.json) is honored rather than assumed.

# WHO THIS SESSION IS decides what it owes. The core is the session the launcher
# marked (src/agent/claude/cli/start-cli.sh and the Codex launcher export
# SUTANDO_CORE_SESSION=1, since 2026-07); a pool worker is enrolled with
# SUTANDO_INSTANCE_ID. Every other Claude Code session in this checkout -- an
# ad-hoc `claude` for a PR review, a `claude -p <skill>` one-shot -- is a GUEST:
# the same rule schedule-crons-session-hint.sh and personal-claude-compact-hint.sh
# already apply. A guest owes nothing here: user report 2026-09-24, a one-shot
# skill session was blocked at every Stop on the live core's own queue (Slack
# DMs the real core was draining at that moment) and, unable to read the task,
# looped forever. The earlier rule read identity from the cwd's git repo instead,
# which cannot tell a guest in the checkout from the core in the checkout.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# An explicit non-core session owns no inbox unless enrolled as a pool worker.
if [ "${SUTANDO_CORE_SESSION:-}" = "0" ] && [ -z "${SUTANDO_INSTANCE_ID:-}" ]; then
  echo '{}'
  exit 0
fi
UNIDENTIFIED=""
if [ -z "${SUTANDO_INSTANCE_ID:-}" ] && [ "${SUTANDO_CORE_SESSION:-}" != "1" ]; then
  UNIDENTIFIED=1
fi
WORKSPACE="$(bash "$REPO_DIR/scripts/sutando-config.sh" workspace 2>/dev/null)"
# Fall back to the documented default, never to the repo root: a resolver
# failure must still leave this pointed at a real queue rather than silently
# re-disabling the hook the way the old path did.
[ -n "$WORKSPACE" ] || WORKSPACE="$REPO_DIR/workspace"

# The resolver owns which interpreter is usable; a bare `python3` is wrong on a
# configured install and re-enters the CLT stub it refused, so a refusal stands.
if ! PYBIN="$(bash "$REPO_DIR/scripts/sutando-config.sh" python-bin 2>/dev/null)" \
   || [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  PYBIN=""
fi

TASKS_DIR="$WORKSPACE/tasks"
RESULTS_DIR="$WORKSPACE/results"
DELIVERIES_DIR="$WORKSPACE/deliveries"

# An unmarked session is a guest only while a marked core is alive ON THIS HOST
# to own the queue (a fresh state/cores/<host-label>.alive; the workspace syncs
# other hosts' heartbeats too, and a peer's core cannot own this host's session).
# With none, it may BE the core, launched by hand, so it is gated: fail closed.
if [ -n "$UNIDENTIFIED" ]; then
  CORE_ALIVE=""
  if [ -n "$PYBIN" ]; then
    CORE_ALIVE="$(SUTANDO_SRC="$REPO_DIR/src" SUTANDO_CORES_DIR="$WORKSPACE/state/cores" SUTANDO_ALIVE_MAX_AGE="${SUTANDO_STOP_HOOK_CORE_ALIVE_MAX_AGE:-90}" "$PYBIN" -c 'import os, socket, sys, time
sys.path.insert(0, os.environ["SUTANDO_SRC"])
labels = {socket.gethostname().split(".")[0]}
try:
    from util_paths import _host_label
    labels.add(_host_label())
except Exception:
    pass
d = os.environ["SUTANDO_CORES_DIR"]; cap = float(os.environ["SUTANDO_ALIVE_MAX_AGE"]); now = time.time()
fresh = []
for label in labels:
    p = os.path.join(d, f"{label}.alive")
    try:
        if -5 <= now - os.stat(p).st_mtime < cap:
            fresh.append(p)
    except OSError:
        pass
print("1" if fresh else "")' 2>/dev/null)" || CORE_ALIVE=""
  fi
  if [ -n "$CORE_ALIVE" ]; then
    echo "check-pending-tasks: guest session (no SUTANDO_CORE_SESSION=1 or SUTANDO_INSTANCE_ID) beside a live marked core; nothing to gate" >&2
    echo '{}'
    exit 0
  fi
  echo "check-pending-tasks: unmarked session with no live marked core; gating it as the core (fail closed)" >&2
fi

# Claude Code's Stop input arrives as JSON on stdin; stop_hook_active is true when
# this Stop already follows a block in this turn. Read ONCE, bounded to a second:
# a hook must never sit on a tty or an open pipe. Anything unreadable reads as 0.
STOP_HOOK_ACTIVE_READ=""
read_stop_hook_active() {
  if [ -z "$STOP_HOOK_ACTIVE_READ" ]; then
    STOP_HOOK_ACTIVE_READ="$("$PYBIN" -c 'import json, select, sys
try:
    ready, _, _ = select.select([sys.stdin], [], [], 1.0)
    d = json.loads(sys.stdin.read() or "{}") if ready else {}
except Exception:
    d = {}
print("1" if isinstance(d, dict) and d.get("stop_hook_active") is True else "0")' 2>/dev/null)" || STOP_HOOK_ACTIVE_READ=0
  fi
  printf '%s' "${STOP_HOOK_ACTIVE_READ:-0}"
}

# Worker-pool awareness (sonichi/sutando#4281, #4338). An optional router,
# injected at the adapter edge (never named here — see
# docs/architecture-boundaries.md "Optional adapter capabilities"), delegates
# a task by writing a SENTINEL into deliveries/<recipient>/<task-id><stage>, whose
# stage suffixes are task_dispatch.py's contract and are not repeated in this
# file — the payload itself never leaves tasks/, by design (the
# recipient reads it from there via the inbox resolver). Two different
# sessions read this state, and each asks a different question:
#   - the core asks "is this still mine to report?" — no, once ANY worker
#     holds a sentinel for it, even unaccepted, the router already made it
#     that worker's, not a core orphan.
#   - a worker (SUTANDO_INSTANCE_ID set) asks "do I still owe a reply?" —
#     answered from its OWN deliveries folder only, never the core's tasks/
#     queue, which is not this session's to report on.
owned_task_ids() {
  # What THIS worker was handed. Same owner as claimed_by_a_worker's contract
  # (src/delivery/task_dispatch.py), so the sentinel suffixes are spelled there
  # and never here. rc 2 = cannot decide: reported, never silently skipped.
  "$PYBIN" "$REPO_DIR/src/delivery/task_dispatch.py" owned-by "$DELIVERIES_DIR" "$1"
}

claimed_by_a_worker() {
  # The sentinel layout is src/delivery/task_dispatch.py:worker_holds's contract,
  # shared with the task notifiers; no python reads as "not held" (reported, like already_delivered).
  local task_id="$1" rc
  [ -n "$PYBIN" ] || return 1
  # A missing script (minimal bundle) can hold nothing -- its rc 2 must not
  # collapse into the same code path as "deliveries root unreadable" below.
  [ -f "$REPO_DIR/src/delivery/task_dispatch.py" ] || return 1
  "$PYBIN" "$REPO_DIR/src/delivery/task_dispatch.py" worker-holds "$DELIVERIES_DIR" "$task_id.txt" >/dev/null 2>&1; rc=$?
  # 2 = cannot decide (root unreadable): hold, never report it to the core.
  [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]
}

# A delivered result is claimed out of results/ within about a second
# (proactive-loop's own documented poller latency) — by the time this hook
# next runs, `results/<id>.txt` is routinely already gone even though the
# reply went out. Checking only the live path reads a correctly finished task
# as still open forever. There is no cheaper true-done signal:
# `state/workers/<id>/done/<task-id>.flag` (pool_delivery.mark_done) exists as a
# concept but nothing in the current production path calls the writer, so gating
# on it would report every task as unfinished instead.
#
# "Ready result, live or in any archive layout" is owned by
# src/delivery/task_dispatch.py:has_ready_result (sonichi/sutando#4317) — the
# same policy every task-notifier now shares. A local re-implementation here
# drifted twice already (missed the month-bucket layout, then the
# archive-YYYY-MM-DD layout and the prefix-collision case the shared module's
# own test suite pins), which is exactly the duplicated-policy failure mode
# the shared owner exists to close off.
already_delivered() {
  local task_id="$1"
  [ -n "$PYBIN" ] || return 1
  "$PYBIN" "$REPO_DIR/src/delivery/task_dispatch.py" has-result "$RESULTS_DIR" "$task_id.txt" >/dev/null 2>&1
}

# A task the core is on RIGHT NOW is not a stop-time orphan (user report
# 2026-09-24: the core was forced into a fresh turn at every Stop for the whole
# life of a task it was working, ~6/hour). The scheduler's own activity snapshot
# (src/activity_bus.py, state/activity/<task-id>.json) says RUNNING with fresh
# activity; queued, waiting on a person, stale, unreadable or absent still blocks.
task_in_progress() {
  [ -n "$PYBIN" ] || return 1
  [ -f "$REPO_DIR/src/activity_bus.py" ] || return 1
  "$PYBIN" "$REPO_DIR/src/activity_bus.py" in-progress "$1" --workspace "$WORKSPACE" \
    --max-age "${SUTANDO_STOP_HOOK_IN_PROGRESS_MAX_AGE:-1800}" >/dev/null 2>&1
}

UNPROCESSED=""
UNPROCESSED_NAMES=""
shopt -s nullglob 2>/dev/null

if [ -n "${SUTANDO_INSTANCE_ID:-}" ]; then
  # Worker mode: judge only this instance's own folder, never the core's queue.
  # An unreadable folder (rc 2) must not read as "nothing owed", so the ids are
  # captured first and a non-zero status reports rather than skips.
  OWNED="$(owned_task_ids "$SUTANDO_INSTANCE_ID" 2>/dev/null)"; OWNED_RC=$?
  if [ "$OWNED_RC" -ne 0 ]; then
    UNPROCESSED+="--- deliveries/$SUTANDO_INSTANCE_ID/ could not be read (task_dispatch rc $OWNED_RC) — cannot tell what this worker owes ---

"
  fi
  for TASK_ID in $OWNED; do
    already_delivered "$TASK_ID" && continue
    UNPROCESSED_NAMES="$UNPROCESSED_NAMES $TASK_ID.txt"
    if [ -f "$RESULTS_DIR/$TASK_ID.txt" ]; then
      # Readiness is owned by src/delivery/readiness.py; existence is not
      # readiness, so with no interpreter to ask, this stays UNPROCESSED.
      if [ -z "$PYBIN" ]; then
        UNPROCESSED+="--- $TASK_ID.txt (readiness unknown — no interpreter to check) ---

"
        continue
      fi
      if SUTANDO_SRC="$REPO_DIR/src" SUTANDO_RESULT="$RESULTS_DIR/$TASK_ID.txt" "$PYBIN" -c 'import os,sys; sys.path.insert(0, os.environ["SUTANDO_SRC"]); from delivery.readiness import read_ready_result; sys.exit(0 if read_ready_result(os.environ["SUTANDO_RESULT"]) is not None else 1)'; then
        continue
      fi
      UNPROCESSED+="--- $TASK_ID.txt (result file is EMPTY — it delivers nothing; write a real reply) ---

"
      continue
    fi
    PAYLOAD="$TASKS_DIR/$TASK_ID.txt"
    UNPROCESSED+="--- $TASK_ID.txt ---
$( [ -f "$PAYLOAD" ] && cat "$PAYLOAD" || echo "(payload not found at $PAYLOAD)" )

"
  done
else
  # Core mode: the queue is tasks/, minus anything the router already handed
  # to a worker.
  for f in "$TASKS_DIR"/*.txt; do
    BASENAME=$(basename "$f")
    TASK_ID="${BASENAME%.txt}"
    claimed_by_a_worker "$TASK_ID" && continue
    already_delivered "$TASK_ID" && continue
    task_in_progress "$TASK_ID" && continue
    UNPROCESSED_NAMES="$UNPROCESSED_NAMES $BASENAME"
    if [ -f "$RESULTS_DIR/$BASENAME" ]; then
      # Readiness is owned by src/delivery/readiness.py; existence is not
      # readiness, so with no interpreter to ask, this stays UNPROCESSED.
      if [ -z "$PYBIN" ]; then
        UNPROCESSED+="--- $BASENAME (readiness unknown — no interpreter to check) ---

"
        continue
      fi
      if SUTANDO_SRC="$REPO_DIR/src" SUTANDO_RESULT="$RESULTS_DIR/$BASENAME" "$PYBIN" -c 'import os,sys; sys.path.insert(0, os.environ["SUTANDO_SRC"]); from delivery.readiness import read_ready_result; sys.exit(0 if read_ready_result(os.environ["SUTANDO_RESULT"]) is not None else 1)'; then
        continue
      fi
      UNPROCESSED+="--- $BASENAME (result file is EMPTY — it delivers nothing; write a real reply) ---

"
      continue
    fi
    UNPROCESSED+="--- $BASENAME ---
$(cat "$f")

"
  done
fi

if [ -n "${SUTANDO_INSTANCE_ID:-}" ]; then
  BLOCK_REASON="Unprocessed deliveries in deliveries/$SUTANDO_INSTANCE_ID/"
else
  BLOCK_REASON="Unprocessed tasks in tasks/"
fi

if [ -n "$UNPROCESSED" ] && [ -z "$PYBIN" ]; then
  # Encoding needs an interpreter the resolver would not supply. Say so on stderr
  # and allow the stop: a hand-rolled JSON block is what made this guard unparseable.
  echo "check-pending-tasks: no usable interpreter; queue not reported" >&2
  echo '{}'
  exit 0
elif [ -n "$UNPROCESSED" ]; then
  # Bounded: Claude Code says on stdin (stop_hook_active) when this Stop already
  # follows a block in this turn. The same queue blocking the same turn end more
  # than the cap means the session is not going to answer it (it cannot read the
  # task, or it is not the right session after all); past the cap the gate fails
  # open, logged, and the watcher/notifier path still re-delivers the task.
  REPEAT_CAP="${SUTANDO_STOP_HOOK_REPEAT_CAP:-3}"
  ACTIVE="$(read_stop_hook_active)"
  [ "$ACTIVE" = 1 ] || "$PYBIN" "$REPO_DIR/src/stop_hook_repeat.py" clear --state "$WORKSPACE/state" 2>/dev/null || true
  REPEAT_N="$("$PYBIN" "$REPO_DIR/src/stop_hook_repeat.py" bump --state "$WORKSPACE/state" --sig "$UNPROCESSED_NAMES" 2>/dev/null)" || REPEAT_N=""
  case "$REPEAT_N" in ''|*[!0-9]*) REPEAT_N="" ;; esac
  if [ -n "$REPEAT_N" ] && [ "$REPEAT_N" -gt "$REPEAT_CAP" ]; then
    echo "check-pending-tasks: the same queue ($UNPROCESSED_NAMES ) blocked this turn end $REPEAT_N times (cap $REPEAT_CAP); failing open" >&2
    "$PYBIN" "$REPO_DIR/src/stop_hook_repeat.py" clear --state "$WORKSPACE/state" 2>/dev/null || true
    echo '{}'
    exit 0
  fi
  # `reason` is what a blocking Stop delivers to the model, so the task names and
  # bodies ride it (a top-level additionalContext is not read on this event; it is
  # kept for the wire-shape pin). A real JSON encoder: hand-rolled escaping put a raw
  # newline inside a string value, so every block decision was unparseable.
  SUTANDO_BLOCK_REASON="$BLOCK_REASON:$UNPROCESSED_NAMES (block ${REPEAT_N:-?} of $REPEAT_CAP this turn end). Read and process them NOW:" \
    SUTANDO_HOOK_BODY="$UNPROCESSED" "$PYBIN" -c 'import json,os,sys
body = os.environ.get("SUTANDO_HOOK_BODY", "")
cap = 8000
shown = body if len(body) <= cap else body[:cap] + "\n… (" + str(len(body) - cap) + " more characters; read the task files themselves)"
sys.stdout.write(json.dumps({"decision":"block","reason":os.environ.get("SUTANDO_BLOCK_REASON") + "\n" + shown,"additionalContext":"UNPROCESSED TASKS — process these NOW:\n" + body}, separators=(",",":"), ensure_ascii=False))'
  exit 0
fi

# The loop above only sees a turn that ANSWERS A QUEUED TASK. This gate covers the
# rest: a turn must end in a message or a recorded no-send (src/turn_ledger.py).
# No --session here: turn_ledger.py reads $CLAUDE_CODE_SESSION_ID itself (same
# established idiom as scripts/skill-read-receipt.py's _session_id()), which
# Claude Code sets on every subprocess it spawns, hooks included — see
# turn_ledger.py's SESSION SCOPING note. Absent that env var (a non-Claude-Code
# context), behavior is exactly the original shared-file default.

# An empty PYBIN must never reach "$PYBIN" as a command -- fail open explicitly
# rather than lean on an empty command's exit code happening not to equal 1.
if [ -z "$PYBIN" ]; then
  echo '{}'
  exit 0
fi
"$PYBIN" "$REPO_DIR/src/stop_hook_repeat.py" clear --state "$WORKSPACE/state" 2>/dev/null || true

# A turn must not end with this session's own inbox unwatched: nothing announces
# a delivery then, and a turn end is the one moment the session can re-arm.
if [ "${SUTANDO_STOP_HOOK_WATCHER_GATE:-1}" != "0" ]; then
  if [ -n "${SUTANDO_INSTANCE_ID:-}" ]; then
    COVERAGE_INBOX="$WORKSPACE/deliveries/$SUTANDO_INSTANCE_ID"
    COVERAGE_REARM='bash "$SUTANDO_WATCHER_CMD" "$SUTANDO_TASKS_DIR" --role session --inbox "$SUTANDO_TASKS_DIR"'
  else
    COVERAGE_INBOX="$TASKS_DIR"
    # Absolute: the core may run from a foreign cwd (SUTANDO_CLAUDE_WORKING_DIR).
    COVERAGE_REARM="bash \"$REPO_DIR/src/watch-tasks-stream.sh\" --role session --inbox \"$TASKS_DIR\""
  fi
  # Consecutive unwatched turn ends, per runtime instance: a watcher that cannot
  # start must not wedge the session, so the gate fails open past the cap, logged.
  COVERAGE_FAIL_OPEN_AFTER="${SUTANDO_STOP_HOOK_UNWATCHED_FAIL_OPEN_AFTER:-3}"
  COVERAGE_VERDICT="$("$PYBIN" "$REPO_DIR/src/watcher_identity.py" role-present session --inbox "$COVERAGE_INBOX" --ready "$WORKSPACE/state" 2>/dev/null)" || COVERAGE_VERDICT=""
  case "$COVERAGE_VERDICT" in
    yes) "$PYBIN" "$REPO_DIR/src/stop_hook_unwatched.py" clear --state "$WORKSPACE/state" 2>/dev/null || true ;;
    no)
      # A counter that cannot be persisted can never reach the cap: fail open now.
      COVERAGE_ERR="$(mktemp "${TMPDIR:-/tmp}/stop-hook-unwatched.XXXXXX" 2>/dev/null || echo /dev/null)"
      if ! COVERAGE_N="$("$PYBIN" "$REPO_DIR/src/stop_hook_unwatched.py" bump --state "$WORKSPACE/state" 2>"$COVERAGE_ERR")"; then
        echo "check-pending-tasks: $COVERAGE_INBOX is unwatched but the counter could not be written ($(tail -1 "$COVERAGE_ERR" 2>/dev/null)); failing open" >&2
        COVERAGE_N=""
      fi
      [ "$COVERAGE_ERR" != /dev/null ] && rm -f "$COVERAGE_ERR"
      case "$COVERAGE_N" in ''|*[!0-9]*) COVERAGE_N="" ;; esac
      if [ -n "$COVERAGE_N" ] && [ "$COVERAGE_N" -le "$COVERAGE_FAIL_OPEN_AFTER" ]; then
        SUTANDO_HOOK_REASON="No ready session-role watcher holds $COVERAGE_INBOX: nothing announces deliveries while it is missing. Re-arm it before ending the turn (unwatched turn end $COVERAGE_N of $COVERAGE_FAIL_OPEN_AFTER, then this gate fails open): via the Monitor tool, $COVERAGE_REARM" \
          "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":os.environ["SUTANDO_HOOK_REASON"]}, separators=(",", ":")))'
        exit 0
      fi
      [ -n "$COVERAGE_N" ] && echo "check-pending-tasks: $COVERAGE_INBOX unwatched at $COVERAGE_N consecutive turn ends; failing open" >&2
      ;;
    *) : ;;  # unknown or unobservable is not evidence of an unwatched inbox
  esac
fi

# Only a real Stop event may move the turn boundary; a hand run (no Stop payload,
# stdin at EOF) reports the same decision and records nothing.
COMMIT=()
# No EOF within 2s is a slow real writer, so that records the stop as before.
if [ ! -t 0 ] && "$PYBIN" -c 'import json,os,select,sys,time
buf, end = b"", time.monotonic() + 2
while True:
    left = end - time.monotonic()
    if left <= 0 or not select.select([0], [], [], left)[0]:
        sys.exit(0)
    chunk = os.read(0, 65536)
    if not chunk:
        break
    buf += chunk
try: d = json.loads(buf or b"{}")
except ValueError: d = {}
sys.exit(0 if isinstance(d, dict) and d.get("hook_event_name") == "Stop" else 1)' 2>/dev/null; then
  COMMIT=(--commit)
fi
STOP_REASON="$("$PYBIN" "$REPO_DIR/src/turn_ledger.py" --workspace "$WORKSPACE" stop-gate "${COMMIT[@]}" 2>/dev/null)"
STOP_RC=$?

# Fail OPEN on anything but a committed refusal: an uncommitted one never spends
# its reminder, so blocking on it would refuse every Stop. A hand run reports on stderr.
if [ "$STOP_RC" -eq 1 ] && [ -n "$STOP_REASON" ] && [ "${#COMMIT[@]}" -eq 0 ]; then
  echo "check-pending-tasks (not a Stop event, nothing recorded): $STOP_REASON" >&2
  echo '{}'
elif [ "$STOP_RC" -eq 1 ] && [ -n "$STOP_REASON" ]; then
  SUTANDO_HOOK_REASON="$STOP_REASON" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":os.environ.get("SUTANDO_HOOK_REASON") or "Turn is ending without a message or an explicit no-send"}, separators=(",",":"), ensure_ascii=False))'
else
  echo '{}'
fi
