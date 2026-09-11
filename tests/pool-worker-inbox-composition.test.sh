#!/usr/bin/env bash
# A launched worker must receive the TASK, not the sentinel that names it
# (@keweichen's REQUEST_CHANGES blocker 2 on #4110 — the cumulative stack).
#
# THE DEFECT. spawn_worker points the worker's watcher at deliveries/<worker>;
# the router writes a ZERO-BYTE sentinel there; the watcher emitted that
# basename and handed that path to the handler. pool_delivery.read_payload could
# resolve the body, but no launcher or watcher path called it, so the probe read
# payload_bytes=61 / watched_file_bytes=0. The Codex side lost the workspace
# too: results resolved to deliveries/results, which no bridge drains.
#
# SHAPE. The router is the production function; dispatch_task and both results
# resolutions are lifted VERBATIM out of the production scripts, so this pins the
# shipped text rather than a restatement. Nothing is launched: no tmux, no CLI,
# no watcher process, no fswatch.
#
# WHAT SEPARATES A FIX FROM THE BUG. The sentinel's own size is asserted to be 0
# in the same run, so "the handler got 61 bytes" cannot be true by accident; and
# an ordinary core inbox is asserted UNCHANGED, so the fix cannot be "always
# rewrite the path".
#
# Run: bash tests/pool-worker-inbox-composition.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

PYBIN="$(command -v python3 || true)"
[ -n "$PYBIN" ] || { echo "  skip: no python3"; echo PASS; exit 0; }

WS="$(mktemp -d "${TMPDIR:-/tmp}/pool-inbox.XXXXXX")"
trap 'rm -rf "$WS"' EXIT
# Physical + normalised: TMPDIR ends in a slash on macOS, and python would
# compare a path this shell spelled with a doubled one.
WS="$(cd "$WS" && pwd -P)"
W="$(printf 'a%.0s' $(seq 1 32))"
mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$WS/deliveries"
printf '{"version":1,"workers":{"%s":{"state":"live"}},"bindings":{"!room:x":"%s"}}\n' \
  "$W" "$W" > "$WS/state/roster.json"
printf 'id: task-body\nchannel_id: !room:x\ntask: the body a worker must read\n' > "$WS/tasks/task-body.txt"
PAYLOAD_BYTES="$(wc -c < "$WS/tasks/task-body.txt" | tr -d ' ')"

# 1. Route it with the production router: the delivery is a sentinel, not a copy.
ROUTED="$("$PYBIN" - "$REPO/src" "$WS" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import pool_roster as pr, pool_router as rt
print(",".join(rt.route(sys.argv[2], {"id": "task-body", "channel_id": "!room:x"},
                        pr.load_roster(sys.argv[2]))["delivered"]))
PY
)"
[ "$ROUTED" = "$W" ] && ok "the production router delivered to the bound worker" \
                     || bad "the production router delivered to the bound worker" "delivered=$ROUTED"
INBOX="$WS/deliveries/$W"
SENT_BYTES="$(wc -c < "$INBOX/task-body.txt" | tr -d ' ')"
[ "$SENT_BYTES" = 0 ] && ok "the delivery is a 0-byte sentinel (the payload is never copied)" \
                      || bad "the delivery is a 0-byte sentinel" "it is $SENT_BYTES bytes"

# 2. THE CASE. dispatch_task, lifted verbatim, run over the worker's inbox with
#    a handler that records the file it was actually given.
# The stub handler: it records the --task-file it was actually given.
cat > "$WS/record-handler.sh" <<'H'
#!/bin/bash
prev=""
for a in "$@"; do
  case "$prev" in --task-file) printf '%s\n' "$a" > "$RECORD" ;; esac
  prev="$a"
done
exit 0
H
chmod +x "$WS/record-handler.sh"

run_dispatch() {   # $1 = inbox, $2 = inbox kind, $3 = the event path
  BODY="$(awk '/^dispatch_task\(\) \{/,/^\}/' "$REPO/src/watch-tasks-stream.sh")"
  RECORD="$WS/record" SUTANDO_TASK_EVENT_HANDLER="$WS/record-handler.sh" \
  SUTANDO_PY_BIN="$PYBIN" WORKSPACE_DIR="$WS" RESULTS_DIR="$WS/results" \
  TASKS_DIR="$1" INBOX_KIND="$2" DISPATCH_DIR="" FALLBACKS_DIR="$WS/fallbacks" \
  __REPO_ROOT="$REPO" BODY="$BODY" bash -c '
    set -u
    queued_activity_row()     { :; }
    emit_dispatch_task_file() { printf "CORE_EVENT %s\n" "$1"; }
    eval "$BODY"
    dispatch_task "$1"
  ' _ "$3" 2>/dev/null
}

# DISPATCH_DIR is empty here, so this is the emit path; the handler call below is
# the same task_path, asserted directly.
OUT="$(run_dispatch "$INBOX" deliveries "$INBOX/task-body.txt")"
case "$OUT" in
  *"CORE_EVENT task-body.txt"*) ok "the event still names the task by its id" ;;
  *) bad "the event still names the task by its id" "dispatch printed: $OUT" ;;
esac

# The handler probe path: DISPATCH_DIR set, so the production probe runs and the
# stub records the --task-file it received.
run_probe() {   # $1 = inbox, $2 = kind, $3 = event path
  rm -f "$WS/record"
  mkdir -p "$WS/dispatch/pending" "$WS/dispatch/running" "$WS/dispatch/settled" "$WS/dispatch/workers"
  BODY="$(awk '/^dispatch_task\(\) \{/,/^\}/' "$REPO/src/watch-tasks-stream.sh")"
  RECORD="$WS/record" SUTANDO_TASK_EVENT_HANDLER="$WS/record-handler.sh" \
  SUTANDO_PY_BIN="$PYBIN" WORKSPACE_DIR="$WS" RESULTS_DIR="$WS/results" \
  TASKS_DIR="$1" INBOX_KIND="$2" DISPATCH_DIR="$WS/dispatch" FALLBACKS_DIR="$WS/fallbacks" \
  __REPO_ROOT="$REPO" BODY="$BODY" bash -c '
    set -u
    queued_activity_row()     { :; }
    emit_dispatch_task_file() { :; }
    queue_handler_task()      { :; }
    eval "$BODY"
    dispatch_task "$1"
  ' _ "$3" 2>/dev/null
  cat "$WS/record" 2>/dev/null
}

GOT="$(run_probe "$INBOX" deliveries "$INBOX/task-body.txt")"
GOT_BYTES="$(wc -c < "$GOT" 2>/dev/null | tr -d ' ')"
[ "$GOT" = "$WS/tasks/task-body.txt" ] \
  && ok "the handler is given the canonical payload, not the sentinel" \
  || bad "the handler is given the canonical payload" "it was given: $GOT"
[ "${GOT_BYTES:-0}" = "$PAYLOAD_BYTES" ] \
  && ok "the bytes the handler receives are the payload's ($PAYLOAD_BYTES, sentinel is $SENT_BYTES)" \
  || bad "the bytes the handler receives are the payload's" "got ${GOT_BYTES:-0}, payload is $PAYLOAD_BYTES"

# 3. CONTROL — a core inbox is untouched: same lift, no kind, the event path IS
#    the payload. A fix that always rewrote the path would fail here.
GOT="$(run_probe "$WS/tasks" tasks "$WS/tasks/task-body.txt")"
[ "$GOT" = "$WS/tasks/task-body.txt" ] \
  && ok "an ordinary inbox resolves to the file the event named" \
  || bad "an ordinary inbox resolves to the file the event named" "it was given: $GOT"

# 4. CONTROL — a sentinel whose payload is gone is dispatched to nobody.
: > "$INBOX/task-stale.txt"
GOT="$(run_probe "$INBOX" deliveries "$INBOX/task-stale.txt")"
[ -z "$GOT" ] && ok "a sentinel with no payload dispatches nothing" \
              || bad "a sentinel with no payload dispatches nothing" "handler got: $GOT"

# 5. BOTH RUNTIMES resolve results to <workspace>/results, under exactly the env
#    spawn_worker hands the launcher. Both resolutions are lifted from production.
"$PYBIN" - "$REPO" "$WS" "$W" > "$WS/plan-env" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/src")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime="claude",
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
WATCHER_LINES="$(grep -E '^(WORKSPACE_DIR|RESULTS_DIR)=' "$REPO/src/watch-tasks-stream.sh")"
NOTIFIER_LINES="$(grep -E '^(WORKSPACE_DIR|RESULTS_DIR)=' "$REPO/src/agent/codex/cli/task-notifier.sh")"
# shellcheck disable=SC2046
WATCHER_RESULTS="$(env -i HOME="$HOME" PATH=/usr/bin:/bin \
  $(grep -E '^SUTANDO_(TASKS|WORKSPACE|RESULTS)_DIR=' "$WS/plan-env") \
  TASKS_DIR_ABS="$INBOX" LINES="$WATCHER_LINES" \
  bash -c 'eval "$LINES"; printf "%s\n" "$RESULTS_DIR"')"
# shellcheck disable=SC2046
NOTIFIER_RESULTS="$(env -i HOME="$HOME" PATH=/usr/bin:/bin \
  $(grep -E '^SUTANDO_(TASKS|WORKSPACE|RESULTS)_DIR=' "$WS/plan-env") \
  TASKS_DIR="$INBOX" LINES="$NOTIFIER_LINES" \
  bash -c 'eval "$LINES"; printf "%s\n" "$RESULTS_DIR"')"
[ "$WATCHER_RESULTS" = "$WS/results" ] \
  && ok "the Claude-side watcher writes results to <workspace>/results" \
  || bad "the Claude-side watcher writes results to <workspace>/results" "got $WATCHER_RESULTS"
[ "$NOTIFIER_RESULTS" = "$WS/results" ] \
  && ok "the Codex notifier writes results to <workspace>/results" \
  || bad "the Codex notifier writes results to <workspace>/results" "got $NOTIFIER_RESULTS"
[ "$WATCHER_RESULTS" = "$NOTIFIER_RESULTS" ] \
  && ok "the two runtimes agree on the results path (matches=True)" \
  || bad "the two runtimes agree on the results path" "$WATCHER_RESULTS vs $NOTIFIER_RESULTS"

# 6. The Codex prompt names the payload, not the sentinel: payload_file lifted
#    from the production notifier and run over the worker's inbox.
NOTIFIER_PAYLOAD="$(NOTIFIER_PY="$PYBIN" REPO="$REPO" WORKSPACE_DIR="$WS" \
  TASKS_DIR="$INBOX" INBOX_KIND=deliveries \
  BODY="$(awk '/^payload_file\(\) \{/,/^\}/' "$REPO/src/agent/codex/cli/task-notifier.sh")" \
  bash -c 'set -u; eval "$BODY"; payload_file task-body.txt' 2>/dev/null)"
[ "$NOTIFIER_PAYLOAD" = "$WS/tasks/task-body.txt" ] \
  && ok "the Codex prompt points the worker at the payload" \
  || bad "the Codex prompt points the worker at the payload" "it points at: $NOTIFIER_PAYLOAD"

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
