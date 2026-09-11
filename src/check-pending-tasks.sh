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

UNPROCESSED=""
shopt -s nullglob 2>/dev/null
for f in "$TASKS_DIR"/*.txt; do
  BASENAME=$(basename "$f")
  # A pool worker owns any task with a sentinel in its delivery folder. Which
  # names count is pool_delivery's to say; a second spelling here would drift.
  TASK_ID="${BASENAME%.txt}"
  # 0 held, 1 not held, anything else "could not say". No interpreter is the
  # separate refusal below, so it keeps reading as 1 rather than as a crash.
  HELD_RC=1
  if [ -n "$PYBIN" ]; then
    "$PYBIN" "$REPO_DIR/src/pool_delivery.py" \
      --workspace "$WORKSPACE" --held "$TASK_ID" >/dev/null 2>&1
    HELD_RC=$?
  fi
  if [ "$HELD_RC" -gt 1 ]; then
    # Unknown is not a negative: reporting it would tell the core to answer work
    # a worker may already hold, and dropping it silently would hide the fault.
    echo "check-pending-tasks: $TASK_ID — pool_delivery could not say whether a worker holds it (exit $HELD_RC); not reported" >&2
    continue
  fi
  if [ "$HELD_RC" = 0 ]; then continue; fi
  # A task parked for a retry pass is a worker's too. The handler the pool's
  # install named answers; core holds no path to it, so a skill can move.
  PARKED_RC=1
  if [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ] && [ -x "${SUTANDO_TASK_EVENT_HANDLER}" ]; then
    "$SUTANDO_TASK_EVENT_HANDLER" \
      --workspace "$WORKSPACE" --parked "$TASK_ID" >/dev/null 2>&1
    PARKED_RC=$?
  fi
  if [ "$PARKED_RC" -gt 1 ]; then
    # Same rule as the hold above: an unanswerable question is not a negative.
    echo "check-pending-tasks: $TASK_ID — the task-event handler could not say whether it is parked for retry (exit $PARKED_RC); not reported" >&2
    continue
  fi
  if [ "$PARKED_RC" = 0 ]; then continue; fi
  # Readiness is owned by src/delivery/readiness.py, the same policy every delivery
  # consumer uses; a local re-implementation drifts from what will actually be sent.
  if [ -f "$RESULTS_DIR/$BASENAME" ]; then
    if SUTANDO_SRC="$REPO_DIR/src" SUTANDO_RESULT="$RESULTS_DIR/$BASENAME" "$PYBIN" -c 'import os,sys; sys.path.insert(0, os.environ["SUTANDO_SRC"]); from delivery.readiness import read_ready_result; sys.exit(0 if read_ready_result(os.environ["SUTANDO_RESULT"]) is not None else 1)'; then continue; fi
    UNPROCESSED+="--- $BASENAME (result file is EMPTY — it delivers nothing; write a real reply) ---

"
    continue
  fi
  UNPROCESSED+="--- $BASENAME ---
$(cat "$f")

"
done

if [ -n "$UNPROCESSED" ] && [ -z "$PYBIN" ]; then
  # Encoding needs an interpreter the resolver would not supply. Say so on stderr
  # and allow the stop: a hand-rolled JSON block is what made this guard unparseable.
  echo "check-pending-tasks: no usable interpreter; queue not reported" >&2
  echo '{}'
elif [ -n "$UNPROCESSED" ]; then
  # A real JSON encoder: hand-rolled escaping put a raw newline inside a string
  # value, so every block decision was unparseable and the guard never fired.
  SUTANDO_HOOK_BODY="$UNPROCESSED" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":"Unprocessed tasks in tasks/","additionalContext":"UNPROCESSED TASKS — process these NOW:\n"+os.environ.get("SUTANDO_HOOK_BODY","")}, separators=(",",":"), ensure_ascii=False))'
else
  echo '{}'
fi
