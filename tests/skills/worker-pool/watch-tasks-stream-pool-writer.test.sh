#!/usr/bin/env bash
# The pool done-flag writer, reached from the core's handler runner.
#
# Three things the runner must get right, each with its control:
#   1. Without a pool (no SUTANDO_INSTANCE_ID / SUTANDO_POOL_DELIVERY_SCRIPT) the
#      hook is a no-op and the handler runs exactly as before -- the no-pool host
#      must be byte-for-byte untouched.
#   2. With a pool, the writer runs through the repo's RESOLVED interpreter, not
#      the script's shebang: a `python3` first on PATH is whatever this host put
#      there, and the worker inherits that PATH.
#   3. Every terminal disposition settles the record: success and a published
#      terminal failure promote to `.flag`; a fallback to the live core withdraws
#      the `.pending`; nothing else is left for the sweep to misread.
#
# Runs the REAL runner (`--handler-runner`) and the REAL writer, and checks the
# parent-side call sites structurally, since those only run inside a full watcher.
# `bash tests/watch-tasks-stream-pool-writer.test.sh`

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
WRITER="$REPO/skills/worker-pool/scripts/pool_delivery.py"
PY="$(command -v python3)"

fail=0
check() {  # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: expected '$2', got '$3'"; fail=1; fi
}

# ── structural: the parent-side call sites exist and use the resolved interpreter ──
hook="$(awk '/^record_worker_done\(\) \{/,/^\}/' "$WATCHER")"
check "the hook runs the writer through SUTANDO_PY_BIN, not the shebang" \
      "1" "$(printf '%s\n' "$hook" | grep -c '"\$SUTANDO_PY_BIN" "\$SUTANDO_POOL_DELIVERY_SCRIPT"')"
check "the hook refuses to run without a resolved interpreter" \
      "1" "$(printf '%s\n' "$hook" | grep -c 'SUTANDO_PY_BIN:-}" \] || return 1')"
# "the runner is handed the resolved interpreter" (a re-exec'd `bash "$0"
# --handler-runner` subprocess) is retired: run_handler_now() calls the
# handler inline, in this same process, so $SUTANDO_PY_BIN is already in
# scope for record_worker_done -- there is no separate runner to hand it to.
# The hook's own two checks above already cover the real property (resolved
# interpreter, not the shebang).
settle="$(awk '/^settle_worker_record\(\) \{/,/^\}/' "$WATCHER")"
check "settling a record means promoting it to the published stage" \
      "1" "$(printf '%s\n' "$settle" | grep -c 'record_worker_done "\$1" done "\$WORKSPACE_DIR"')"
ptf="$(awk '/^publish_terminal_failure\(\) \{/,/^\}/' "$WATCHER")"
check "both terminal paths (failure published, answer already there) settle the record" \
      "2" "$(printf '%s\n' "$ptf" | grep -c 'settle_worker_record "\$filename"')"
# 3 -> 2: drain_dispatch_queue's own site collapsed into run_handler_now's
# single failure switch, and fallback_outstanding_handlers' DISPATCH_DIR-queue
# loop has no equivalent now that there is no queue -- only its CLAIMS_DIR
# loop (settle_own_claims_on_shutdown's one site) still applies.
check "every fallback-to-live-core site withdraws the pending hold first" \
      "2" "$(grep -c 'record_worker_done "\$filename" abandon "\$WORKSPACE_DIR"' "$WATCHER")"

# ── functional: the real runner, the real writer ──
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
ws="$tmp/ws"; mkdir -p "$ws/tasks" "$ws/results" "$ws/state"
printf 'id: task-x\ntask: hi\n' > "$ws/tasks/task-x.txt"

# A handler that publishes a result and succeeds; a second that fails.
ok_handler="$tmp/handler-ok.sh"; printf '#!/bin/bash\nprintf "done\\n" > "%s/results/task-x.txt"\nexit 0\n' "$ws" > "$ok_handler"; chmod +x "$ok_handler"
bad_handler="$tmp/handler-bad.sh"; printf '#!/bin/bash\nexit 7\n' > "$bad_handler"; chmod +x "$bad_handler"

# Poisoned PATH: a python3 that must never run. If the shebang were used, the
# writer would fail and the record would be missing.
poison="$tmp/poison"; mkdir -p "$poison"; printf '#!/bin/sh\necho POISONED-PYTHON-RAN >&2\nexit 97\n' > "$poison/python3"; chmod +x "$poison/python3"

# --handler-runner (a re-exec'd subprocess mode) is retired along with the
# rest of the async runner -- record_worker_done() itself is unchanged, so
# extract and eval just that one function and reproduce the exact pending/
# run/done call sequence run_handler_now() makes, in-process, no re-exec.
eval "$(awk '/^record_worker_done\(\) \{/,/^\}/' "$WATCHER")"
run_runner() {  # run_runner <handler> ; env comes from the caller
    local filename=task-x.txt rc
    record_worker_done "$filename" pending "$ws" || { printf 'HANDLER_DONE: 1 %s\n' "$filename"; return; }
    "$1" --runtime "" --workspace "$ws" --task-file "$ws/tasks/task-x.txt" \
      --results-dir "$ws/results" --repo "$REPO" >/dev/null 2>"$tmp/stderr"
    rc=$?
    [ "$rc" -eq 0 ] && record_worker_done "$filename" done "$ws"
    printf 'HANDLER_DONE: %s %s\n' "$rc" "$filename"
}
flags() { ls "$ws/state/workers/worker-3/done" 2>/dev/null | tr '\n' ' ' | sed 's/ $//'; }

# 1. no pool: no record, handler runs as before
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$( (unset SUTANDO_INSTANCE_ID SUTANDO_POOL_DELIVERY_SCRIPT; SUTANDO_PY_BIN="$PY" run_runner "$ok_handler") )"
check "no pool: handler ran and reported success" "HANDLER_DONE: 0 task-x.txt" "$out"
check "no pool: no record was written" "" "$(ls "$ws/state/workers" 2>/dev/null)"
check "no pool: the result was published" "done" "$(cat "$ws/results/task-x.txt")"

# 2. a pool, poisoned PATH, resolved interpreter: the record lands and the stub never runs
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$(PATH="$poison:$PATH" SUTANDO_INSTANCE_ID=worker-3 SUTANDO_POOL_DELIVERY_SCRIPT="$WRITER" SUTANDO_PY_BIN="$PY" run_runner "$ok_handler")"
check "pool: handler success promotes to .flag" "task-x.flag" "$(flags)"
check "pool: reported success" "HANDLER_DONE: 0 task-x.txt" "$out"
check "pool: the poisoned python3 never ran" "0" "$(grep -c POISONED-PYTHON-RAN "$tmp/stderr")"

# 2b. the same with the interpreter missing: refused, handler not run (fail closed)
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$( (unset SUTANDO_PY_BIN; SUTANDO_INSTANCE_ID=worker-3 SUTANDO_POOL_DELIVERY_SCRIPT="$WRITER" run_runner "$ok_handler") )"
check "pool without a resolved interpreter: the handler is not run" "HANDLER_DONE: 1 task-x.txt" "$out"
check "pool without a resolved interpreter: no result was published" "" "$(cat "$ws/results/task-x.txt" 2>/dev/null)"

# 3. a pool, handler fails: the pending hold stays for the parent to settle (promote or abandon)
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$(SUTANDO_INSTANCE_ID=worker-3 SUTANDO_POOL_DELIVERY_SCRIPT="$WRITER" SUTANDO_PY_BIN="$PY" run_runner "$bad_handler")"
check "pool: a failed handler reports its exit code" "HANDLER_DONE: 7 task-x.txt" "$out"
check "pool: a failed handler leaves only the pending stage" "task-x.pending" "$(flags)"
# ... which the parent's terminal-failure path promotes, and the fallback path withdraws:
"$PY" "$WRITER" --workspace "$ws" --recipient worker-3 mark-done --task-id task-x --stage done >/dev/null
check "promote after a terminal failure: the flag replaces the pending stage" "task-x.flag" "$(flags)"
rm -rf "$ws/state/workers"; "$PY" "$WRITER" --workspace "$ws" --recipient worker-3 mark-done --task-id task-x --stage pending >/dev/null
"$PY" "$WRITER" --workspace "$ws" --recipient worker-3 mark-done --task-id task-x --stage abandon >/dev/null
check "abandon on fallback: the pending stage is withdrawn and nothing is promoted" "" "$(flags)"

[ "$fail" -eq 0 ] && echo "PASS — watch-tasks-stream pool writer" || { echo "FAIL — watch-tasks-stream pool writer"; exit 1; }
