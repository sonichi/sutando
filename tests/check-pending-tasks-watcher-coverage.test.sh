#!/bin/bash
# The Stop hook's watcher-coverage gate, run from a bundle whose watcher_identity.py
# is a stub answering $STUB_VERDICT; the counter is the production module.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }

BUNDLE="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hook-coverage.XXXXXX")"
trap 'rm -rf "$BUNDLE"' EXIT
mkdir -p "$BUNDLE/src/delivery" "$BUNDLE/src/runtime-api" "$BUNDLE/scripts" "$BUNDLE/workspace/tasks" "$BUNDLE/workspace/results" "$BUNDLE/workspace/state" "$BUNDLE/workspace/deliveries/w1" "$BUNDLE/stubbin"
cp "$REPO/src/check-pending-tasks.sh" "$REPO/src/watch-tasks-stream.sh" "$BUNDLE/src/"
cp "$REPO/scripts/git-binary.sh" "$BUNDLE/scripts/"
# The queue gates run before the coverage gate and need the dispatch modules
# (owned ids, readiness), so the bundle carries the real src/ python whole.
cp "$REPO"/src/*.py "$BUNDLE/src/"
cp "$REPO"/src/delivery/*.py "$BUNDLE/src/delivery/"
cp "$REPO"/src/runtime-api/*.py "$BUNDLE/src/runtime-api/"
# Hermetic on any host: the polling stand-in for fswatch, first on PATH.
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$BUNDLE/stubbin/fswatch"; chmod +x "$BUNDLE/stubbin/fswatch"
PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
if [ -z "$PY" ]; then echo "FAIL — scripts/sutando-config.sh python-bin resolved nothing; this suite does not guess an interpreter"; exit 1; fi
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s/workspace"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' "$BUNDLE" "$PY" > "$BUNDLE/scripts/sutando-config.sh"
chmod +x "$BUNDLE/scripts/sutando-config.sh"
ARGV_LOG="$BUNDLE/identity.argv"
cat > "$BUNDLE/src/watcher_identity.py" <<EOF
import os, sys
open("$ARGV_LOG", "a").write(" ".join(sys.argv[1:]) + "\n")
v = os.environ.get("STUB_VERDICT", "unknown")
print(v)
sys.exit(2 if v == "unknown" else 0)
EOF
WS="$BUNDLE/workspace"
HOOK="$BUNDLE/src/check-pending-tasks.sh"
export SUTANDO_TEST_MODE=1
# The ledger gate must pass so only the coverage gate decides these cases.
ledger_ok() { "$PY" "$REPO/src/turn_ledger.py" --workspace "$WS" no-send "coverage suite" >/dev/null 2>&1 || true; }
run_hook() {  # $1 verdict, rest: extra env assignments
  local v="$1"; shift
  ledger_ok
  (cd "$BUNDLE" && env -u CLAUDE_CODE_SESSION_ID -u SUTANDO_INSTANCE_ID -u SUTANDO_CORE_SESSION -u SUTANDO_AGENT_ID -u AGENT_MXID -u AGENT_ID STUB_VERDICT="$v" "$@" bash "$HOOK" 2>"$BUNDLE/err")
}
counter_path() {  # the production module names the file, under the same env the hook saw
  (cd "$BUNDLE" && env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_MXID -u AGENT_ID "$@" "$PY" src/stop_hook_unwatched.py path --state "$WS/state")
}
CORE_COUNT="$(counter_path)"

echo "the verdict reaches the gate through watcher_identity's role-present:"
: > "$ARGV_LOG"
OUT="$(run_hook yes)"
[ "$OUT" = "{}" ] && ok "a ready session-role watcher lets the turn end ({})" || bad "a ready session-role watcher lets the turn end ({})" "got: $OUT"
grep -q "role-present session --inbox $WS/tasks --ready $WS/state" "$ARGV_LOG" && ok "the hook asked for a READY session-role holder of its own inbox" || bad "the hook asked for a READY session-role holder of its own inbox" "argv: $(cat "$ARGV_LOG")"
[ ! -e "$CORE_COUNT" ] && ok "no counter is written while the inbox is watched" || bad "no counter is written while the inbox is watched" "counter present"

echo "an unwatched inbox blocks the turn end, up to the cap, then fails open:"
for n in 1 2 3; do
  OUT="$(run_hook no)"
  case "$OUT" in
    *'"decision":"block"'*) ok "unwatched turn end $n of 3 blocks" ;;
    *) bad "unwatched turn end $n of 3 blocks" "got: ${OUT:0:160}" ;;
  esac
done
case "$OUT" in
  *"No ready session-role watcher holds $WS/tasks"*) ok "the reason names the inbox" ;;
  *) bad "the reason names the inbox" "got: ${OUT:0:200}" ;;
esac
CORE_REARM="$(printf '%s' "$OUT" | "$PY" -c 'import json,re,sys; r=json.load(sys.stdin)["reason"]; m=re.search(r"bash \"(/[^\"]*)/src/watch-tasks-stream\.sh\" --role session --inbox", r); print(m.group(1) if m else "")' 2>/dev/null)"
[ -n "$CORE_REARM" ] && [ -f "$CORE_REARM/src/watch-tasks-stream.sh" ] && ok "the core's re-arm names the watcher by an absolute path that exists (a foreign cwd still finds it)" || bad "the core's re-arm names the watcher by an absolute path that exists" "reason tail: ${OUT: -260}"
case "$OUT" in
  *"3 of 3"*) ok "the reason counts the attempt against the cap" ;;
  *) bad "the reason counts the attempt against the cap" "got: ${OUT:0:200}" ;;
esac
[ "$(cat "$CORE_COUNT" 2>/dev/null)" = "3" ] && ok "the counter file holds 3 (at $(basename "$CORE_COUNT"))" || bad "the counter file holds 3" "got: $(cat "$CORE_COUNT" 2>/dev/null) at $CORE_COUNT"
OUT="$(run_hook no)"
[ "$OUT" = "{}" ] && ok "the fourth unwatched turn end fails open ({})" || bad "the fourth unwatched turn end fails open ({})" "got: ${OUT:0:160}"
grep -q "unwatched at 4 consecutive turn ends; failing open" "$BUNDLE/err" && ok "...and says so on stderr" || bad "...and says so on stderr" "stderr: $(cat "$BUNDLE/err")"
OUT="$(run_hook yes)"
[ "$OUT" = "{}" ] && [ ! -e "$CORE_COUNT" ] && ok "a watched inbox clears the counter, so the next loss blocks again" || bad "a watched inbox clears the counter, so the next loss blocks again" "out=${OUT:0:80} counter=$(cat "$CORE_COUNT" 2>/dev/null)"
OUT="$(run_hook no)"
case "$OUT" in *'"decision":"block"'*) ok "...it does" ;; *) bad "...it does" "got: ${OUT:0:120}" ;; esac
rm -f "$CORE_COUNT"

echo "a worker judges its own deliveries folder, with its own counter and re-arm:"
: > "$ARGV_LOG"
OUT="$(run_hook no SUTANDO_INSTANCE_ID=w1)"
case "$OUT" in
  *"holds $WS/deliveries/w1"*) ok "the worker's reason names deliveries/w1" ;;
  *) bad "the worker's reason names deliveries/w1" "got: ${OUT:0:200}" ;;
esac
grep -q -- "--inbox $WS/deliveries/w1 " "$ARGV_LOG" && ok "the worker asked about its own inbox" || bad "the worker asked about its own inbox" "argv: $(cat "$ARGV_LOG")"
W1_COUNT="$(counter_path SUTANDO_INSTANCE_ID=w1)"
[ "$W1_COUNT" != "$CORE_COUNT" ] && [ "$(cat "$W1_COUNT" 2>/dev/null)" = "1" ] && ok "the worker's counter is its own file, named by instance_scope_key ($(basename "$W1_COUNT"))" || bad "the worker's counter is its own file, named by instance_scope_key" "path=$W1_COUNT value=$(cat "$W1_COUNT" 2>/dev/null)"
[ ! -e "$CORE_COUNT" ] && ok "...and the core's counter is untouched" || bad "...and the core's counter is untouched" "core counter present"
# Two actors on one instance id are two runtime identities; one exhausting the
# cap must not fail the other open on its first unwatched turn end.
OUT="$(run_hook no SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-a)"; OUT="$(run_hook no SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-a)"; OUT="$(run_hook no SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-a)"
OUT="$(run_hook no SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-b)"
A_COUNT="$(counter_path SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-a)"; B_COUNT="$(counter_path SUTANDO_INSTANCE_ID=w1 SUTANDO_AGENT_ID=actor-b)"
case "$OUT" in
  *'"decision":"block"'*) [ "$A_COUNT" != "$B_COUNT" ] && [ "$(cat "$A_COUNT" 2>/dev/null)" = "3" ] && [ "$(cat "$B_COUNT" 2>/dev/null)" = "1" ] && ok "actor-b on the same instance id blocks on ITS first unwatched turn end (actor-a's 3 are actor-a's)" || bad "actor-b on the same instance id blocks on ITS first unwatched turn end" "a=$(cat "$A_COUNT" 2>/dev/null) b=$(cat "$B_COUNT" 2>/dev/null) same_path=$([ "$A_COUNT" = "$B_COUNT" ] && echo yes || echo no)" ;;
  *) bad "actor-b on the same instance id blocks on ITS first unwatched turn end" "got: ${OUT:0:160}" ;;
esac

echo "the worker's re-arm line is one the real watcher accepts:"
REARM_LINE="$(printf '%s' "$(run_hook no SUTANDO_INSTANCE_ID=w2)" | "$PY" -c 'import json,re,sys; r=json.load(sys.stdin)["reason"]; print(re.search(r"bash \"\$SUTANDO_WATCHER_CMD\"[^\n]*", r).group(0))' 2>/dev/null)"
case "$REARM_LINE" in
  *'--role session --inbox "$SUTANDO_TASKS_DIR"'*) ok "the worker's re-arm carries --role session --inbox" ;;
  *) bad "the worker's re-arm carries --role session --inbox" "got: $REARM_LINE" ;;
esac
# Control first: the bare launcher line is what the watcher refuses (rc 64), so
# a pass below means the flags were read. A start is proven by the production
# readiness sentinel, which the session role stamps only after the inbox answered.
run_rearm() {  # $1 command line; prints "rc|stderr head", killing a watcher that did start
  local err="$BUNDLE/rearm.err" rc i
  : > "$err"; rm -f "$WS"/state/*.pid; mkdir -p "$WS/deliveries/w2"
  set -m
  (cd "$BUNDLE" && env PATH="$BUNDLE/stubbin:$PATH" SUTANDO_WATCHER_CMD="$REPO/src/watch-tasks-stream.sh" SUTANDO_TASKS_DIR="$WS/deliveries/w2" \
    SUTANDO_WORKSPACE_DIR="$WS" SUTANDO_INSTANCE_ID=w2 bash -c "$1" </dev/null >/dev/null 2>"$err") &
  local p=$!
  set +m
  for i in $(seq 1 100); do
    ls "$WS"/state/*.pid >/dev/null 2>&1 && break
    kill -0 "$p" 2>/dev/null || break
    sleep 0.1
  done
  if ls "$WS"/state/*.pid >/dev/null 2>&1; then rc=ready; else wait "$p" 2>/dev/null; rc=$?; fi
  kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; wait "$p" 2>/dev/null
  printf '%s|%s' "$rc" "$(head -c 120 "$err" | tr '\n' ' ')"
}
CTRL="$(run_rearm 'bash "$SUTANDO_WATCHER_CMD" "$SUTANDO_TASKS_DIR"')"
case "$CTRL" in
  64\|*'refusing to start'*) ok "control: the bare launcher line is refused (rc 64)" ;;
  *) bad "control: the bare launcher line is refused (rc 64)" "got: $CTRL" ;;
esac
GOT="$(run_rearm "$REARM_LINE")"
case "$GOT" in
  ready\|*) ok "the real watcher starts on the hook's re-arm line and stamps its readiness sentinel" ;;
  *) bad "the real watcher starts on the hook's re-arm line and stamps its readiness sentinel" "got: $GOT" ;;
esac
rm -f "$WS"/state/*.pid "$WS"/state/stop-hook-unwatched*

echo "what is not evidence, and what cannot wedge:"
OUT="$(run_hook unknown)"
[ "$OUT" = "{}" ] && [ ! -e "$CORE_COUNT" ] && ok "an unobservable process table (rc 2) is not an unwatched inbox" || bad "an unobservable process table (rc 2) is not an unwatched inbox" "out=${OUT:0:80}"
OUT="$(run_hook no SUTANDO_STOP_HOOK_WATCHER_GATE=0)"
[ "$OUT" = "{}" ] && ok "the gate can be switched off explicitly" || bad "the gate can be switched off explicitly" "got: ${OUT:0:80}"
OUT="$(run_hook no SUTANDO_STOP_HOOK_UNWATCHED_FAIL_OPEN_AFTER=1)"
case "$OUT" in *'"decision":"block"'*) ok "the cap is a knob: 1 blocks once" ;; *) bad "the cap is a knob: 1 blocks once" "got: ${OUT:0:80}" ;; esac
OUT="$(run_hook no SUTANDO_STOP_HOOK_UNWATCHED_FAIL_OPEN_AFTER=1)"
[ "$OUT" = "{}" ] && ok "...then fails open" || bad "...then fails open" "got: ${OUT:0:80}"
rm -f "$WS"/state/stop-hook-unwatched*
# A counter that cannot be persisted can never reach the cap, so the gate must
# fail open at once rather than block on every turn end forever.
if [ "$(id -u)" = "0" ]; then
  echo "  skip a read-only state dir cannot be arranged as root"
else
  chmod a-w "$WS/state"
  OUT="$(run_hook no)"
  chmod u+w "$WS/state"
  [ "$OUT" = "{}" ] && ok "an unwritable state dir fails open at once ({})" || bad "an unwritable state dir fails open at once ({})" "got: ${OUT:0:120}"
  grep -q "the counter could not be written" "$BUNDLE/err" && ok "...and stderr names the persistence failure" || bad "...and stderr names the persistence failure" "stderr: $(cat "$BUNDLE/err")"
fi

echo
if [ "$FAILED" = "0" ]; then echo "PASS — check-pending-tasks watcher-coverage gate"; else echo "FAIL — check-pending-tasks watcher-coverage gate"; exit 1; fi
