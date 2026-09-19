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

# A session whose cwd is an unrelated repo's worktree that merely inherited
# this CLAUDE.md is a guest, not the core -- its Stop must not gate on our queue.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# A bare `git` can be the macOS CLT stub (REVIEW.md lesson 7) — resolve
# through the same rules src/git_binary.py uses, not PATH directly.
. "$REPO_DIR/scripts/git-binary.sh"
GIT_BIN="$(resolve_git)"
if [ -n "$GIT_BIN" ]; then
  # A caller-inherited ceiling, repository override, or locale can each turn
  # a real, still-ours directory into a false "different repo" or "absent".
  GIT_PROBE_ENV="env -u GIT_CEILING_DIRECTORIES -u GIT_DIR -u GIT_COMMON_DIR LC_ALL=C LANGUAGE=C"
  # --path-format=absolute (git >= 2.31): a plain rev-parse, run from a
  # different cwd, can print a path relative to <dir> instead of to the caller.
  CWD_COMMON_DIR="$($GIT_PROBE_ENV "$GIT_BIN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
  # Capture git's own stderr instead of discarding it: only git explicitly
  # saying "not a git repository" may ever license the guest exit below.
  REPO_COMMON_DIR="$($GIT_PROBE_ENV "$GIT_BIN" -C "$REPO_DIR" rev-parse --path-format=absolute --git-common-dir 2>&1)"
  REPO_PROBE_RC=$?
  REPO_PROBE_ERR=""
  [ "$REPO_PROBE_RC" -ne 0 ] && REPO_PROBE_ERR="$REPO_COMMON_DIR" && REPO_COMMON_DIR=""
  # A `-C DIR` probe and an actually-`cd`'d one can disagree in ways neither
  # side's stdout reveals; retry via `cd` before trusting an empty result.
  if [ -z "$REPO_COMMON_DIR" ]; then
    FALLBACK_OUT="$(cd "$REPO_DIR" 2>/dev/null && $GIT_PROBE_ENV "$GIT_BIN" rev-parse --path-format=absolute --git-common-dir 2>&1)"
    if [ $? -eq 0 ]; then
      REPO_COMMON_DIR="$FALLBACK_OUT"
      REPO_PROBE_ERR=""
    else
      [ -n "$FALLBACK_OUT" ] && REPO_PROBE_ERR="$FALLBACK_OUT"
    fi
  fi
  # Kept PRE-canonicalization -- a path that then fails to `cd` must not
  # read the same as no identity ever being found (see the elif below).
  REPO_COMMON_DIR_RAW="$REPO_COMMON_DIR"
  # Canonicalize past any symlink in the path itself (e.g. macOS /tmp -> /private/tmp) —
  # --path-format=absolute fixes relative-vs-cwd, not a same-directory answer spelled two ways.
  [ -n "$CWD_COMMON_DIR" ] && CWD_COMMON_DIR="$(cd "$CWD_COMMON_DIR" 2>/dev/null && pwd -P)"
  [ -n "$REPO_COMMON_DIR" ] && REPO_COMMON_DIR="$(cd "$REPO_COMMON_DIR" 2>/dev/null && pwd -P)"
  # A known identity wins regardless of the marker; marker absence (incl. `-L`,
  # so a dangling symlink still counts as present) only breaks the empty-probe tie.
  case "$REPO_PROBE_ERR" in
    *"not a git repository"*) REPO_CONFIRMED_ABSENT=1 ;;
    *) REPO_CONFIRMED_ABSENT="" ;;
  esac
  if [ -n "$REPO_COMMON_DIR" ]; then
    if [ -n "$CWD_COMMON_DIR" ] && [ "$CWD_COMMON_DIR" != "$REPO_COMMON_DIR" ]; then
      echo '{}'
      exit 0
    fi
  elif [ -n "$REPO_COMMON_DIR_RAW" ]; then
    : # a real answer that then failed to canonicalize -- ambiguous, fall through to gate
  elif [ -e "$REPO_DIR/.git" ] || [ -L "$REPO_DIR/.git" ]; then
    : # marker present, probe still failed -- ambiguous, fall through to gate
  elif [ -n "$CWD_COMMON_DIR" ] && [ -n "$REPO_CONFIRMED_ABSENT" ]; then
    # Both probes failed AND git itself confirmed no repo -- not just an
    # unresolved probe on a markerless child that IS still ours.
    echo '{}'
    exit 0
  fi
fi
# No runnable git -- cannot prove a DIFFERENT repo, so proceed as core rather
# than raising a CLT dialog or silently skipping every guest.
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
  # A minimal bundle (no src/delivery/ at all) has no worker-pool capability,
  # so nothing can be "held" -- python's rc 2 for a missing script must not
  # collapse into the SAME code path as "deliveries root unreadable" below.
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
      # Readiness is owned by src/delivery/readiness.py, the same policy every delivery
      # consumer uses; a local re-implementation drifts from what will actually be sent.
      # No interpreter to ask readiness.py -- existence is not readiness (its own
      # contract), so this stays UNPROCESSED rather than silently read as done.
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
    if [ -f "$RESULTS_DIR/$BASENAME" ]; then
      # Readiness is owned by src/delivery/readiness.py, the same policy every delivery
      # consumer uses; a local re-implementation drifts from what will actually be sent.
      # No interpreter to ask readiness.py -- existence is not readiness (its own
      # contract), so this stays UNPROCESSED rather than silently read as done.
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

# An empty PYBIN must never reach "$PYBIN" as a command -- fail open explicitly
# rather than lean on an empty command's exit code happening not to equal 1.
if [ -z "$PYBIN" ]; then
  echo '{}'
  exit 0
fi
STOP_REASON="$("$PYBIN" "$REPO_DIR/src/turn_ledger.py" --workspace "$WORKSPACE" stop-gate 2>/dev/null)"
STOP_RC=$?

# Fail OPEN on anything but an explicit refusal (rc 1 AND a reason): a gate that
# cannot run must never wedge the agent into a turn it has no way to end.
if [ "$STOP_RC" -eq 1 ] && [ -n "$STOP_REASON" ]; then
  # `reason` is what a blocking Stop delivers to the model; the guidance used to
  # ride a top-level additionalContext, which this event does not read.
  SUTANDO_HOOK_REASON="$STOP_REASON" "$PYBIN" -c 'import json,os,sys; sys.stdout.write(json.dumps({"decision":"block","reason":os.environ.get("SUTANDO_HOOK_REASON") or "Turn is ending without a message or an explicit no-send"}, separators=(",",":"), ensure_ascii=False))'
else
  echo '{}'
fi
