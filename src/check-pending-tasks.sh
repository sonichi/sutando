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

# Worker-pool awareness (sonichi/sutando#4281, #4338). The router
# (skills/worker-pool/scripts/pool_route_handler.py) delegates a task by
# writing a SENTINEL into deliveries/<recipient>/<task-id>{.txt,.accepted,
# .claimed} — the payload itself never leaves tasks/, by design (the
# recipient reads it from there via the inbox resolver). Two different
# sessions read this state, and each asks a different question:
#   - the core asks "is this still mine to report?" — no, once ANY worker
#     holds a sentinel for it, even unaccepted, the router already made it
#     that worker's, not a core orphan.
#   - a worker (SUTANDO_INSTANCE_ID set) asks "do I still owe a reply?" —
#     answered from its OWN deliveries folder only, never the core's tasks/
#     queue, which is not this session's to report on.
sentinel_task_id() {
  case "$1" in
    *.accepted) printf '%s' "${1%.accepted}" ;;
    *.claimed)  printf '%s' "${1%.claimed}" ;;
    *.txt)      printf '%s' "${1%.txt}" ;;
    *)          return 1 ;;
  esac
}

claimed_by_a_worker() {
  # True if some worker's own deliveries/ folder holds a sentinel for this
  # task id — the router already routed it away from the core.
  local task_id="$1" d
  for d in "$DELIVERIES_DIR"/*/; do
    [ -d "$d" ] || continue
    if [ -e "${d}${task_id}.txt" ] || [ -e "${d}${task_id}.accepted" ] || [ -e "${d}${task_id}.claimed" ]; then
      return 0
    fi
  done
  return 1
}

# Readiness is owned by src/delivery/readiness.py, the same policy every
# delivery consumer uses; a local re-implementation drifts from what will
# actually be sent.
is_ready_result() {
  [ -f "$1" ] || return 1
  SUTANDO_SRC="$REPO_DIR/src" SUTANDO_RESULT="$1" "$PYBIN" -c 'import os,sys; sys.path.insert(0, os.environ["SUTANDO_SRC"]); from delivery.readiness import read_ready_result; sys.exit(0 if read_ready_result(os.environ["SUTANDO_RESULT"]) is not None else 1)'
}

UNPROCESSED=""
shopt -s nullglob 2>/dev/null

if [ -n "${SUTANDO_INSTANCE_ID:-}" ]; then
  # Worker mode: judge only this instance's own folder, never the core's queue.
  for f in "$DELIVERIES_DIR/$SUTANDO_INSTANCE_ID"/*.txt "$DELIVERIES_DIR/$SUTANDO_INSTANCE_ID"/*.accepted "$DELIVERIES_DIR/$SUTANDO_INSTANCE_ID"/*.claimed; do
    SNAME=$(basename "$f")
    TASK_ID="$(sentinel_task_id "$SNAME")" || continue
    RESULT_FILE="$RESULTS_DIR/$TASK_ID.txt"
    if [ -f "$RESULT_FILE" ]; then
      if is_ready_result "$RESULT_FILE"; then continue; fi
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
    claimed_by_a_worker "${BASENAME%.txt}" && continue
    if [ -f "$RESULTS_DIR/$BASENAME" ]; then
      if is_ready_result "$RESULTS_DIR/$BASENAME"; then continue; fi
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
STOP_REASON="$("$PYBIN" "$REPO_DIR/src/turn_ledger.py" --workspace "$WORKSPACE" stop-gate 2>/dev/null)"
STOP_RC=$?

# Fail OPEN on anything but an explicit refusal (rc 1 AND a reason): a gate that
# cannot run must never wedge the agent into a turn it has no way to end.
if [ "$STOP_RC" -eq 1 ] && [ -n "$STOP_REASON" ]; then
  SUTANDO_HOOK_REASON="$STOP_REASON" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":"Turn is ending without a message or an explicit no-send","additionalContext":os.environ.get("SUTANDO_HOOK_REASON","")}, separators=(",",":"), ensure_ascii=False))'
else
  echo '{}'
fi
