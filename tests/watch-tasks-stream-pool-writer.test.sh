#!/usr/bin/env bash
# The pool done-flag writer, reached from the core's handler runner.
#
# Three things the runner must get right, each with its control:
#   1. Without a pool (no SUTANDO_INSTANCE_ID) the
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
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
NOTIFIER="$REPO/src/agent/codex/cli/task-notifier.sh"
STAGE_WRITER="$REPO/src/delivery/worker-stage.sh"
WRITER="$REPO/skills/worker-pool/scripts/pool_delivery.py"
PY="$(command -v python3)"

fail=0
check() {  # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: expected '$2', got '$3'"; fail=1; fi
}

# ── structural: both runtimes use one writer with their resolved interpreters ──
hook="$(awk '/^record_worker_done\(\) \{/,/^\}/' "$WATCHER")"
check "the watcher sources the shared stage writer" \
      "1" "$(grep -c 'source "\$__SCRIPT_DIR/delivery/worker-stage.sh"' "$WATCHER")"
check "the Codex notifier sources the shared stage writer" \
      "1" "$(grep -c 'source "\$REPO/src/delivery/worker-stage.sh"' "$NOTIFIER")"
check "the watcher passes its resolved interpreter" \
      "1" "$(printf '%s\n' "$hook" | grep -c 'write_worker_stage "\$1" "\$2" "\$3" "\${SUTANDO_PY_BIN:-}"')"
check "the writer runs through the supplied interpreter, not the shebang" \
      "1" "$(grep -c '"\$py" "\$writer"' "$STAGE_WRITER")"
# "the runner is handed the resolved interpreter" (a re-exec'd `bash "$0"
# --handler-runner` subprocess) is retired: run_handler_now() calls the
# handler inline, in this same process, so $SUTANDO_PY_BIN is already in
# scope for record_worker_done -- there is no separate runner to hand it to.
# The shared writer's interpreter check above covers the same property.
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
# rest of the async runner -- extract the wrapper and source the shared writer,
# then reproduce the exact pending/
# run/done call sequence run_handler_now() makes, in-process, no re-exec.
source "$STAGE_WRITER"
eval "$(awk '/^record_worker_done\(\) \{/,/^\}/' "$WATCHER")"
run_runner() {  # run_runner <handler> ; env comes from the caller
    local filename=task-x.txt rc
    record_worker_done "$filename" pending "$ws" || { printf 'HANDLER_DONE: 1 %s\n' "$filename"; return; }
    "$1" --runtime "" --workspace "$ws" --task-file "$ws/tasks/task-x.txt" \
      --results-dir "$ws/results" --repo "$REPO" >/dev/null 2>"$tmp/stderr"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        record_worker_done "$filename" done "$ws" || rc=1
    fi
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

# A broken delivery script cannot authorize a handler: no result is published.
bad_writer="$tmp/writer-bad.py"
printf 'import sys\nsys.exit(9)\n' > "$bad_writer"
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$(SUTANDO_INSTANCE_ID=worker-3 SUTANDO_POOL_DELIVERY_SCRIPT="$bad_writer" SUTANDO_PY_BIN="$PY" run_runner "$ok_handler" 2>"$tmp/writer-bad.err")"
check "nonzero pending writer: handler is not run" "HANDLER_DONE: 1 task-x.txt" "$out"
check "nonzero pending writer: no result was published" "" "$(cat "$ws/results/task-x.txt" 2>/dev/null)"
check "nonzero pending writer: failure is logged" "1" "$(grep -c 'could not record pending for task-x' "$tmp/writer-bad.err")"

# A writer that accepts pending but fails done must not turn an already
# published result into handler failure or fallback work.
done_bad_writer="$tmp/writer-done-bad.py"
cat > "$done_bad_writer" <<'PY'
import os
import subprocess
import sys

if sys.argv[-1] == "done":
    sys.exit(9)
sys.exit(subprocess.call([sys.executable, os.environ["REAL_WRITER"], *sys.argv[1:]]))
PY
rm -rf "$ws/state/workers"; rm -f "$ws/results/task-x.txt"
out="$(REAL_WRITER="$WRITER" SUTANDO_INSTANCE_ID=worker-3 SUTANDO_POOL_DELIVERY_SCRIPT="$done_bad_writer" SUTANDO_PY_BIN="$PY" run_runner "$ok_handler" 2>"$tmp/done-bad.err")"
check "nonzero done writer: handler still reports success" "HANDLER_DONE: 0 task-x.txt" "$out"
check "nonzero done writer: result remains published" "done" "$(cat "$ws/results/task-x.txt")"
check "nonzero done writer: pending record remains for recovery" "task-x.pending" "$(flags)"
check "nonzero done writer: failure is logged" "1" "$(grep -c 'could not record done for task-x' "$tmp/done-bad.err")"

# Reproduce the two callers under a REAL `set -e`, with their production
# wrappers extracted above. A command substitution or an outer `||` here would
# disable errexit inside the function and make this test a false positive.
# Pending failure is the negative control: a bare call must abort before the
# marker. Done failure must log but reach the marker, including through the
# watcher's `|| handler_rc=1` branch without changing handler_rc.
notifier_hook="$(awk '/^mark_worker_stage\(\) \{/,/^\}/' "$NOTIFIER")"
submit_hook="$(awk '/^submit_task\(\) \{/,/^\}/' "$NOTIFIER")"
cat > "$tmp/stage-errexit.sh" <<'BASH'
#!/bin/bash
set -euo pipefail
source "$STAGE_WRITER"
case "$1" in
  codex)
    eval "$NOTIFIER_HOOK"
    mark_worker_stage task-x.txt "$2"
    ;;
  managed_submit)
    eval "$NOTIFIER_HOOK"
    eval "$SUBMIT_HOOK"
    RESULTS_DIR="$WORKSPACE_DIR/results"
    TMUX_SOCKET=/tmp/test-socket SESSION=test-worker
    COMPLETION_TIMEOUT=2 POLL_INTERVAL=0.01
    workstream_context_file=""
    task_payload() { printf '%s/tasks/%s' "$WORKSPACE_DIR" "$1"; }
    has_result() { [ -s "$RESULTS_DIR/$1" ]; }
    tmux() { return 0; }
    clear_workstream_context() { :; }
    deliver_prompt() { printf 'done\n' > "$RESULTS_DIR/$1"; }
    log_notifier() { :; }
    # This is a BARE managed-loop call, not inside `if`, `||` or `$()`.
    submit_task task-x.txt 1
    ;;
  watcher_bare)
    eval "$WATCHER_HOOK"
    record_worker_done task-x.txt "$2" "$WORKSPACE_DIR"
    ;;
  watcher_guarded)
    eval "$WATCHER_HOOK"
    handler_rc=0
    record_worker_done task-x.txt "$2" "$WORKSPACE_DIR" || handler_rc=1
    ;;
esac
printf 'AFTER_STAGE:%s:%s\n' "${handler_rc:-0}" "$WORKER_STAGE_WRITE_SUCCEEDED"
BASH

run_stage_errexit() {  # run_stage_errexit <caller> <stage> <writer> <output-stem> <expected-status>
    STAGE_WRITER="$STAGE_WRITER" NOTIFIER_HOOK="$notifier_hook" SUBMIT_HOOK="$submit_hook" WATCHER_HOOK="$hook" \
    WORKSPACE_DIR="$ws" NOTIFIER_PY="$PY" SUTANDO_PY_BIN="$PY" \
    WORKER_INSTANCE=worker-3 SUTANDO_INSTANCE_ID=worker-3 \
    SUTANDO_POOL_DELIVERY_SCRIPT="$3" REAL_WRITER="$WRITER" \
    bash "$tmp/stage-errexit.sh" "$1" "$2" > "$tmp/$4.out" 2> "$tmp/$4.err"
    check "$4 exit status" "$5" "$?"
}

run_stage_errexit codex done "$done_bad_writer" codex-done 0
check "Codex managed bare done call survives set -e" "AFTER_STAGE:0:0" "$(cat "$tmp/codex-done.out")"
check "Codex managed done failure is logged" "1" "$(grep -c 'could not record done for task-x' "$tmp/codex-done.err")"
run_stage_errexit codex pending "$bad_writer" codex-pending 1
check "negative control: bare Codex pending failure aborts under set -e" "" "$(cat "$tmp/codex-pending.out")"

rm -f "$ws/results/task-x.txt"
run_stage_errexit managed_submit done "$done_bad_writer" managed-submit-done 0
check "managed submit_task survives a failed done writer under set -e" "AFTER_STAGE:0:0" "$(cat "$tmp/managed-submit-done.out")"
check "managed submit_task published the result before done failed" "done" "$(cat "$ws/results/task-x.txt")"
check "managed submit_task logged the done failure" "1" "$(grep -c 'could not record done for task-x' "$tmp/managed-submit-done.err")"

run_stage_errexit watcher_bare done "$done_bad_writer" watcher-bare-done 0
check "watcher bare done call survives set -e" "AFTER_STAGE:0:0" "$(cat "$tmp/watcher-bare-done.out")"
run_stage_errexit watcher_guarded done "$done_bad_writer" watcher-guarded-done 0
check "watcher done failure does not mark a successful handler failed" "AFTER_STAGE:0:0" "$(cat "$tmp/watcher-guarded-done.out")"
run_stage_errexit watcher_guarded pending "$bad_writer" watcher-guarded-pending 0
check "negative control: watcher guarded pending failure sets handler_rc" "AFTER_STAGE:1:0" "$(cat "$tmp/watcher-guarded-pending.out")"

# Mutation control for the reviewer's exact regression: if done returned false,
# the managed bare call would exit early and the watcher would mark a successful
# handler failed. This copy is temporary; the production writer is untouched.
fatal_done_writer="$tmp/worker-stage-done-fatal.sh"
sed 's/^\([[:space:]]*\)\[ "\$stage" = done \]$/\1false/' "$STAGE_WRITER" > "$fatal_done_writer"
check "mutation control changed exactly the done fallback" "1" "$(grep -c '^  false$' "$fatal_done_writer")"
rm -f "$ws/results/task-x.txt"
STAGE_WRITER="$fatal_done_writer" run_stage_errexit managed_submit done "$done_bad_writer" mutated-managed-done 1
check "mutation control: managed submit_task exits before the marker" "" "$(cat "$tmp/mutated-managed-done.out")"
STAGE_WRITER="$fatal_done_writer" run_stage_errexit watcher_guarded done "$done_bad_writer" mutated-watcher-done 0
check "mutation control: watcher marks a failed done write as handler failure" "AFTER_STAGE:1:0" "$(cat "$tmp/mutated-watcher-done.out")"

[ "$fail" -eq 0 ] && echo "PASS — watch-tasks-stream pool writer" || { echo "FAIL — watch-tasks-stream pool writer"; exit 1; }
