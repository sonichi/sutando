#!/bin/bash
# SUTANDO_ANNOUNCE_GENERATION=1: every TASK_FILE line the watcher prints is preceded by the
# generation of the file it read BEFORE routing the task, on every emit path; unset, no line is added.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${SUTANDO_PY_BIN:-python3}"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
gen() { "$PY" -c 'import os, sys
s = os.stat(sys.argv[1])
print(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)' "$1"; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# One watcher lifetime over a core inbox holding one task; prints what it wrote to stdout.
# $1 = arm name, $2 = SUTANDO_ANNOUNCE_GENERATION value ('' = unset), $3 = handler path ('' = none).
run_watcher() {
  local arm="$1" flag="$2" handler="$3" ws="$TMP/$1" pid i
  mkdir -p "$ws/tasks" "$ws/results" "$ws/state"
  printf 'id: task-%s\naccess_tier: owner\ntask: the first file\n' "$arm" > "$ws/tasks/task-$arm.txt"
  gen "$ws/tasks/task-$arm.txt" > "$ws/read-first"
  : > "$ws/out"
  set -m
  env ${flag:+SUTANDO_ANNOUNCE_GENERATION="$flag"} ${handler:+SUTANDO_TASK_EVENT_HANDLER="$handler"} \
    SUTANDO_WORKSPACE_DIR="$ws" SUTANDO_RESULTS_DIR="$ws/results" \
    bash "$REPO/src/watch-tasks-stream.sh" "$ws/tasks" --role standby --inbox "$ws/tasks" > "$ws/out" 2> "$ws/err" &
  pid=$!
  set +m
  for i in $(seq 1 80); do grep -q 'TASK_FILE:' "$ws/out" 2>/dev/null && break; sleep 0.25; done
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  cat "$ws/out"
}
# The line right before the TASK_FILE line.
before_task_file() { awk '/^TASK_FILE: /{print prev; exit} {prev=$0}'; }

# --- no handler: the plain dispatch ------------------------------------------------------------
out="$(run_watcher plain 1 "")"
[ "$(printf '%s\n' "$out" | before_task_file)" = "TASK_GENERATION: task-plain.txt $(cat "$TMP/plain/read-first")" ]
check $? "plain dispatch: the generation the watcher read comes right before TASK_FILE"
[ "$(printf '%s\n' "$out" | grep -c '^TASK_GENERATION: ')" = "1" ]
check $? "plain dispatch: exactly one generation line"

# --- unset: nothing is added ---------------------------------------------------------------------
out="$(run_watcher unset "" "")"
printf '%s\n' "$out" | grep -q '^TASK_FILE: task-unset.txt$'
check $? "switch unset: the task is announced"
! printf '%s\n' "$out" | grep -q 'TASK_GENERATION'
check $? "switch unset: no generation line"

# --- a handler that declines (probe rc 3) ----------------------------------------------------
DECLINE="$TMP/decline.sh"; printf '#!/bin/sh\nexit 3\n' > "$DECLINE"; chmod +x "$DECLINE"
out="$(run_watcher declined 1 "$DECLINE")"
[ "$(printf '%s\n' "$out" | before_task_file)" = "TASK_GENERATION: task-declined.txt $(cat "$TMP/declined/read-first")" ]
check $? "handler declined: the generation comes with the announcement"

# --- the handoff: a handler takes the task, puts ANOTHER file under its name, then fails -----------
# The fallback emit runs after the handler, outside the probe. What it announces must be the file the
# watcher read when it made the decision, never the one that is there now.
SWAP="$TMP/swap.sh"
cat > "$SWAP" << 'EOF'
#!/bin/sh
f=""
while [ $# -gt 0 ]; do
  case "$1" in --probe) exit 0 ;; --task-file) f="$2"; shift ;; esac
  shift
done
printf 'id: someone-else\naccess_tier: team\ntask: a different file, longer than the first one was\n' > "$f.new"
mv -f "$f.new" "$f"
exit 1
EOF
chmod +x "$SWAP"
out="$(run_watcher swapped 1 "$SWAP")"
line="$(printf '%s\n' "$out" | before_task_file)"
[ "$line" = "TASK_GENERATION: task-swapped.txt $(cat "$TMP/swapped/read-first")" ]
check $? "handler fallback: the announcement carries the generation read before the handler ran"
[ "$line" != "TASK_GENERATION: task-swapped.txt $(gen "$TMP/swapped/tasks/task-swapped.txt")" ]
check $? "handler fallback: and that is not the file there now (the reader will hold it)"
if [ "$fail" != "0" ]; then echo "  stdout:"; sed 's/^/    /' "$TMP/swapped/out"; echo "  stderr:"; tail -5 "$TMP/swapped/err" | sed 's/^/    /'; fi

# --- the emitters alone: a decision about another name, or none, is never borrowed ---------------
emit() { SUTANDO_ANNOUNCE_GENERATION=1 DECISION_GENERATION_NAME="$1" DECISION_GENERATION="$2" \
  bash -c 'exec 9>&1; source "$1"; "$2" task-x.txt' _ "$REPO/src/task-emit.sh" "$3" 2>/dev/null | head -1; }
[ "$(emit task-other.txt "1 2 3 4" emit_dispatch_task_file)" = "TASK_GENERATION: task-x.txt -" ]
check $? "a generation read for another task is not announced for this one"
[ "$(emit task-x.txt "" emit_dispatch_task_file)" = "TASK_GENERATION: task-x.txt -" ]
check $? "a failed metadata read is announced as unknown, not left out"
for fn in emit_dispatch_task_file emit_fallback_task_file emit_task_file; do
  [ "$(emit task-x.txt "1 2 3 4" "$fn")" = "TASK_GENERATION: task-x.txt 1 2 3 4" ]
  check $? "$fn announces the decision's generation"
done
[ -z "$(SUTANDO_ANNOUNCE_GENERATION=1 bash -c 'source "$1"; task_file_generation "$2"' _ "$REPO/src/task-emit.sh" "$TMP/no-such-file")" ]
check $? "a file that cannot be read has no generation"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
