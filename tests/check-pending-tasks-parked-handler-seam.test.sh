#!/bin/bash
# The Stop hook asks a NAMED handler whether a task is parked — never a path.
#
# WHY. The route handler now ships in the optional worker-pool skill, and a core
# install may not have it at all. So the hook asks whatever
# $SUTANDO_TASK_EVENT_HANDLER names — the same seam the watcher and the spawner
# already use — and a hook that spelled `skills/worker-pool/...` would break the
# moment that skill moved or was absent.
#
# WHAT SEPARATES A FIX FROM THE BUG. Four answers, four dispositions, in one
# workspace: unnamed handler -> reported (nothing could have parked it); exit 1
# -> reported; exit 0 -> exempt; exit 2 -> exempt AND said on stderr. A hook that
# exempted everything, or nothing, fails at least two of them. The absence of a
# skill path in the shipped hook is asserted directly, not inferred.
#
# ISOLATION: SUTANDO_TEST_MODE=1 + SUTANDO_WORKSPACE with the resolved path
# ASSERTED before anything is written, as in check-pending-tasks-worker-held.
#
# Run: bash tests/check-pending-tasks-parked-handler-seam.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-parkedseam.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"

LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE \
             bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ] \
   || { [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; }; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."
  rm -rf "$TMPWS"; exit 1
fi
trap 'rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PROBE="task-zz-parked-$$"
mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries"
printf 'id: %s\ntask: probe\n' "$PROBE" > "$WS/tasks/$PROBE.txt"

# One stub per answer; it records the argv the hook passed so the call shape is
# pinned too, not just the exit code.
stub() {   # $1 = exit code
  printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexit %s\n' "$WS/argv" "$1" \
    > "$WS/handler.sh"
  chmod +x "$WS/handler.sh"
}

verdict() {   # $@ = env assignments for the hook run
  case "$(env "$@" bash "$HOOK" 2>/dev/null)" in
    '{}') echo allow ;;
    *'"decision":"block"'*) echo block ;;
    *) echo other ;;
  esac
}

# 1. No handler named — a core install with the skill absent. Nothing could have
#    parked the task, so it is reported; a blind exemption would silence the hook.
GOT="$(verdict -u SUTANDO_TASK_EVENT_HANDLER)"
[ "$GOT" = block ] && ok "an unnamed handler does not exempt the task" \
                   || bad "an unnamed handler does not exempt the task" "hook said $GOT"

# 2. The handler says "not parked".
stub 1
GOT="$(verdict SUTANDO_TASK_EVENT_HANDLER="$WS/handler.sh")"
[ "$GOT" = block ] && ok "\"not parked\" (exit 1) is reported" \
                   || bad "\"not parked\" (exit 1) is reported" "hook said $GOT"
case "$(cat "$WS/argv" 2>/dev/null)" in
  *"--parked $PROBE"*) ok "the hook asks --parked with the task id" ;;
  *) bad "the hook asks --parked with the task id" "argv was: $(cat "$WS/argv" 2>/dev/null)" ;;
esac

# 3. THE CASE. The handler says "parked" — the task is a worker's, not the core's.
stub 0
GOT="$(verdict SUTANDO_TASK_EVENT_HANDLER="$WS/handler.sh")"
[ "$GOT" = allow ] && ok "a parked task (exit 0) is not reported to the core" \
                   || bad "a parked task (exit 0) is not reported to the core" "hook said $GOT"

# 4. The handler cannot say. Unknown is not a negative, and silence would hide it.
stub 2
GOT="$(verdict SUTANDO_TASK_EVENT_HANDLER="$WS/handler.sh")"
ERR="$(SUTANDO_TASK_EVENT_HANDLER="$WS/handler.sh" bash "$HOOK" 2>&1 >/dev/null)"
[ "$GOT" = allow ] && ok "an undeterminable park (exit 2) is not reported" \
                   || bad "an undeterminable park (exit 2) is not reported" "hook said $GOT"
case "$ERR" in
  *"$PROBE"*could*not*say*parked*) ok "the unknown is named on stderr" ;;
  *) bad "the unknown is named on stderr" "stderr was: $ERR" ;;
esac

# 5. The shipped hook holds no path into the skill that owns the answer.
if grep -q 'pool_route_handler\|skills/worker-pool' "$HOOK"; then
  bad "the hook names no handler file" "$(grep -n 'pool_route_handler\|skills/worker-pool' "$HOOK")"
else
  ok "the hook names no handler file"
fi

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
