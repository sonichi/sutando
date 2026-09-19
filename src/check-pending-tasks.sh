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

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
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

UNPROCESSED=""
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
    if [ -f "$RESULTS_DIR/$TASK_ID.txt" ]; then
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
    if [ -f "$RESULTS_DIR/$BASENAME" ]; then
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
  # A real JSON encoder: hand-rolled escaping put a raw newline inside a string
  # value, so every block decision was unparseable and the guard never fired.
  SUTANDO_BLOCK_REASON="$BLOCK_REASON" SUTANDO_HOOK_BODY="$UNPROCESSED" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":os.environ.get("SUTANDO_BLOCK_REASON"),"additionalContext":"UNPROCESSED TASKS — process these NOW:\n"+os.environ.get("SUTANDO_HOOK_BODY","")}, separators=(",",":"), ensure_ascii=False))'
  exit 0
fi

# The loop above only sees a turn that ANSWERS A QUEUED TASK. This gate covers the
# rest: a turn must end in a message or a recorded no-send (src/turn_ledger.py).
# No --session here: turn_ledger.py reads $CLAUDE_CODE_SESSION_ID itself (same
# established idiom as scripts/skill-read-receipt.py's _session_id()), which
# Claude Code sets on every subprocess it spawns, hooks included — see
# turn_ledger.py's SESSION SCOPING note. Absent that env var (a non-Claude-Code
# context), behavior is exactly the original shared-file default.
# Only a real Stop event may move the turn boundary; a hand run of this script
# (no Stop payload on stdin) reports the same decision and records nothing.
HOOK_PAYLOAD=""
[ -t 0 ] || IFS= read -r -d '' -t 2 HOOK_PAYLOAD || true
COMMIT=()
if SUTANDO_HOOK_PAYLOAD="$HOOK_PAYLOAD" "$PYBIN" -c 'import json,os,sys
try: d = json.loads(os.environ.get("SUTANDO_HOOK_PAYLOAD") or "{}")
except ValueError: d = {}
sys.exit(0 if isinstance(d, dict) and d.get("hook_event_name") == "Stop" else 1)' 2>/dev/null; then
  COMMIT=(--commit)
fi
STOP_REASON="$("$PYBIN" "$REPO_DIR/src/turn_ledger.py" --workspace "$WORKSPACE" stop-gate "${COMMIT[@]}" 2>/dev/null)"
STOP_RC=$?

# Fail OPEN on anything but an explicit refusal (rc 1 AND a reason): a gate that
# cannot run must never wedge the agent into a turn it has no way to end.
if [ "$STOP_RC" -eq 1 ] && [ -n "$STOP_REASON" ]; then
  SUTANDO_HOOK_REASON="$STOP_REASON" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":"Turn is ending without a message or an explicit no-send","additionalContext":os.environ.get("SUTANDO_HOOK_REASON","")}, separators=(",",":"), ensure_ascii=False))'
else
  echo '{}'
fi
