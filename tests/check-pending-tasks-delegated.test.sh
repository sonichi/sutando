#!/bin/bash
# The Stop hook (src/check-pending-tasks.sh) does not report a task the core
# handed to a background agent while its marker (src/task_delegation.py) is
# live; an expired, malformed or absent marker still blocks (#5918).
# Isolation follows check-pending-tasks-in-progress-and-repeat.test.sh.
# Run: bash tests/check-pending-tasks-delegated.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-dg.XXXXXX")"
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
export SUTANDO_CORE_SESSION=1
export SUTANDO_STOP_HOOK_WATCHER_GATE=0

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
trap 'rm -rf "$TMPWS"' EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }
record_delivery() { "$PYBIN" "$REPO/src/turn_ledger.py" --workspace "$TMPWS" no-send "hook unit test" >/dev/null 2>&1 || true; }
run_hook() { printf '' | bash "$HOOK" 2>"$TMPWS/err"; }
mark() { "$PYBIN" "$REPO/src/task_delegation.py" mark "$@" --workspace "$WS" >/dev/null; }
marker_for() {  # $1 task id, $2 delegated_at offset (s), $3 ttl: a marker written by the production writer
  SUTANDO_SRC="$REPO/src" "$PYBIN" -c 'import os,sys,time; sys.path.insert(0, os.environ["SUTANDO_SRC"]); import task_delegation as m; m.mark(sys.argv[1], sys.argv[2], float(sys.argv[4]), now=time.time() + float(sys.argv[3]))' "$WS" "$1" "$2" "$3"
}

mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries" "$WS/state"
TASK="task-cron-deal-sweep-hooktest-$$"
printf 'id: %s\ntask: sweep deals\n' "$TASK" > "$WS/tasks/$TASK.txt"

record_delivery
OUT="$(run_hook)"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*"$TASK.txt"*) ok "an undelegated task blocks (the control)" ;; *) bad "an undelegated task blocks" "got: ${OUT:0:160}" ;; esac

mark "$WS/tasks/$TASK.txt" --ttl 1800
record_delivery
OUT="$(run_hook)"
[ "$OUT" = "{}" ] && ok "a task delegated with a live marker does not block" || bad "a delegated task does not block" "got: ${OUT:0:160}"

marker_for "$TASK" -1900 1800
OUT="$(run_hook)"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*"$TASK.txt"*) ok "an expired marker blocks again (a dead agent is caught)" ;; *) bad "an expired marker blocks" "got: ${OUT:0:160}" ;; esac

printf '{"task_id":"%s","delegated_at":' "$TASK" > "$WS/state/task-delegations/$TASK.json"
OUT="$(run_hook)"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*"$TASK.txt"*) ok "a malformed marker blocks" ;; *) bad "a malformed marker blocks" "got: ${OUT:0:160}" ;; esac

OTHER="task-other-hooktest-$$"
printf 'id: %s\ntask: other\n' "$OTHER" > "$WS/tasks/$OTHER.txt"
mark "$TASK" --ttl 1800
OUT="$(run_hook)"
case "$OUT" in *"$OTHER.txt"*) ;; *) OUT="" ;; esac
case "$OUT" in ""|*"$TASK.txt"*) bad "a marker skips only its own task" "got: ${OUT:0:200}" ;; *) ok "a marker skips only its own task; an undelegated neighbour still blocks" ;; esac

rm -f "$WS/tasks/$OTHER.txt"
touch -t 202001010000 "$WS/tasks/$TASK.txt"
marker_for "$TASK" -300 1800
record_delivery
OUT="$(run_hook)"
[ "$OUT" = "{}" ] || bad "wake-turn setup: the marker is live" "got: ${OUT:0:160}"
touch "$WS/tasks/$TASK.txt"
OUT="$(run_hook)"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*"$TASK.txt"*) ok "a wake turn rewritten after the marker blocks (the marker covers one turn)" ;; *) bad "a wake turn rewritten after the marker blocks" "got: ${OUT:0:160}" ;; esac

# A real worker sentinel holds the task through the same predicate; an unreadable store holds too.
rm -f "$WS/state/task-delegations/$TASK.json"
mkdir -p "$WS/deliveries/w1" && : > "$WS/deliveries/w1/$TASK.accepted"
OUT="$(run_hook)"
[ "$OUT" = "{}" ] && ok "a worker sentinel holds the task (holder's injected lookup)" || bad "a worker sentinel holds the task" "got: ${OUT:0:160}"
rm -rf "$WS/deliveries" && printf 'not a dir' > "$WS/deliveries"
OUT="$(run_hook)"
[ "$OUT" = "{}" ] && ok "an unreadable worker store reads as held (undecidable, not absent)" || bad "an unreadable worker store reads as held" "got: ${OUT:0:160}"
rm -f "$WS/deliveries" && mkdir -p "$WS/deliveries"

# Stub repo: the hook plus a config stub, so python or the marker script can be absent.
STUB="$TMPWS/stub-repo"
mkdir -p "$STUB/src" "$STUB/scripts"
cp "$HOOK" "$STUB/src/"
stub_config() {  # $1: what python-bin prints ("" = no usable interpreter)
  printf '#!/bin/bash\ncase "$1" in workspace) echo %q ;; python-bin) [ -n %q ] && echo %q || exit 1 ;; *) exit 1 ;; esac\n' \
    "$WS" "$1" "$1" > "$STUB/scripts/sutando-config.sh"
}
mark "$TASK" --ttl 1800
record_delivery
OUT="$(run_hook)"
[ "$OUT" = "{}" ] || bad "stub control: the real hook skips the freshly marked task" "got: ${OUT:0:160}"
stub_config ""
OUT="$(printf '' | bash "$STUB/src/check-pending-tasks.sh" 2>"$TMPWS/err")"
if [ "$OUT" = "{}" ] && grep -q "no usable interpreter; queue not reported" "$TMPWS/err"; then
  ok "no python: a delegated task is still counted unprocessed (not skipped by the marker)"
else
  bad "no python: a delegated task is still counted unprocessed" "out: ${OUT:0:120} err: $(head -c 160 "$TMPWS/err")"
fi
stub_config "$PYBIN"
OUT="$(printf '' | bash "$STUB/src/check-pending-tasks.sh" 2>/dev/null)"
case "$OUT" in *'"reason":"Unprocessed tasks in tasks/'*"$TASK.txt"*) ok "no task_delegation.py: a marked task still blocks (fails closed)" ;; *) bad "no task_delegation.py: a marked task still blocks" "got: ${OUT:0:160}" ;; esac

# The hook's only hold check is `task_delegation.py holder`: its rc decides, with this argv.
cat > "$STUB/src/task_delegation.py" <<'PY'
import os, sys
open(os.environ["STUB_ARGV"], "w").write("\n".join(sys.argv[1:]))
sys.exit(int(os.environ["STUB_RC"]))
PY
for rc in 0 1 2 3; do
  OUT="$(printf '' | STUB_RC=$rc STUB_ARGV="$TMPWS/argv" bash "$STUB/src/check-pending-tasks.sh" 2>/dev/null)"
  case "$rc:$OUT" in
    0:"{}"|3:"{}") ok "holder rc $rc: the task is held" ;;
    1:*"$TASK.txt"*|2:*"$TASK.txt"*) ok "holder rc $rc: the task blocks" ;;
    *) bad "holder rc $rc decides the hold" "got: ${OUT:0:160}" ;;
  esac
done
WANT="$(printf 'holder\n%s\n--task-file\n%s\n--deliveries\n%s\n--workspace\n%s' "$TASK" "$WS/tasks/$TASK.txt" "$WS/deliveries" "$WS")"
[ "$(cat "$TMPWS/argv")" = "$WANT" ] && ok "the hook asks holder with the task file, deliveries root and workspace" \
  || bad "the hook's holder argv" "got: $(tr '\n' ' ' < "$TMPWS/argv")"
if grep -Eq 'worker-holds|task_delegation\.py" check' "$HOOK"; then
  bad "no second hold predicate in the hook" "worker-holds or 'check' still called"
else
  ok "no second hold predicate in the hook"
fi

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
