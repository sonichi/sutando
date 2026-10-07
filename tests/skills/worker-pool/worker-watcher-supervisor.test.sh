#!/usr/bin/env bash
# The pool gives each worker inbox its own hosting-mode supervisor, through
# skills/worker-pool/scripts/worker-watcher-supervisor.sh:
#   (a) --print-command names the core's supervisor script, the claude notifier,
#       the worker's inbox, session, identity and the pool beat, all as -e env;
#   (b) no worker session -> exit 3 and no tmux new-session;
#   (c) worker session up, supervisor absent -> exactly one new-session, named
#       <worker session>-watcher, with the env from (a);
#   (d) supervisor already up -> nothing started (idempotent);
#   (e) a missing required variable refuses before touching tmux;
#   (f) an inbox already held by a standby-kind watcher no supervisor started -> exit 4, nothing
#       started, and the same ensure starts one once that watcher is gone.
# tmux is a recording stub: has-session answers from a list of "live" names,
# new-session appends its argv to a log.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENSURE="$REPO/skills/worker-pool/scripts/worker-watcher-supervisor.sh"
fail=0
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-wsup.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/ws/deliveries/w-abc123"
cat > "$WORK/bin/tmux" <<EOF
#!/bin/sh
# has-session: alive iff the name (after '=') is listed in \$LIVE; new-session: record argv.
LIVE_FILE="$WORK/live"
case "\$3" in
  has-session) name="\$5"; name="\${name#=}"; grep -qx "\$name" "\$LIVE_FILE" 2>/dev/null; exit \$? ;;
  new-session) printf '%s\n' "\$@" >> "$WORK/new-session.log"; exit 0 ;;
esac
exit 0
EOF
chmod +x "$WORK/bin/tmux"
export PATH="$WORK/bin:$PATH"
export SUTANDO_INSTANCE_ID="w-abc123" SUTANDO_TASKS_DIR="$WORK/ws/deliveries/w-abc123" \
       SUTANDO_TMUX_SESSION="sutando-worker-w-abc123" SUTANDO_TMUX_SOCKET="$WORK/sock" \
       SUTANDO_WORKSPACE_DIR="$WORK/ws" SUTANDO_INBOX_KIND="deliveries" SUTANDO_PY="/usr/bin/python3" \
       SUTANDO_INBOX_RESOLVER="$REPO/skills/worker-pool/scripts/resolve-inbox-entry" SUTANDO_INBOX_RESOLVER_TIMEOUT="5"

echo "worker-watcher-supervisor:"
# (a) the command names every piece and runs nothing.
cmd="$(bash "$ENSURE" --print-command)"; rc=$?
check "(a) --print-command exits 0" "$rc"
for want in "new-session" "-s" "sutando-worker-w-abc123-watcher" \
            "SUTANDO_NOTIFIER_SCRIPT=$REPO/src/agent/claude/cli/task-notifier.sh" \
            "SUTANDO_TASKS_DIR=$WORK/ws/deliveries/w-abc123" "SUTANDO_TMUX_SESSION=sutando-worker-w-abc123" \
            "SUTANDO_INSTANCE_ID=w-abc123" "SUTANDO_WATCHER_BEAT=$REPO/skills/worker-pool/scripts/pool_beat.py" \
            "SUTANDO_INBOX_KIND=deliveries" "SUTANDO_NOTIFIER_PY=/usr/bin/python3" \
            "SUTANDO_INBOX_RESOLVER=$REPO/skills/worker-pool/scripts/resolve-inbox-entry" "SUTANDO_INBOX_RESOLVER_TIMEOUT=5" \
            "$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"; do
  printf '%s\n' "$cmd" | grep -qxF -- "$want"; check "(a) command carries $want" $?
done
[ ! -e "$WORK/new-session.log" ] && r=0 || r=1; check "(a) ...and started nothing" "$r"

# (b) no worker session: refuse with 3, start nothing.
: > "$WORK/live"
bash "$ENSURE" >"$WORK/b.out" 2>"$WORK/b.err"; rc=$?
check "(b) no worker session -> exit 3" "$([ "$rc" = 3 ] && echo 0 || echo 1)" "rc=$rc"
grep -q "not running" "$WORK/b.err" && r=0 || r=1; check "(b) ...saying so" "$r"
[ ! -e "$WORK/new-session.log" ] && r=0 || r=1; check "(b) ...and started nothing" "$r"

# (c) worker session up, supervisor absent: one start with the right env.
echo "sutando-worker-w-abc123" > "$WORK/live"
bash "$ENSURE" >"$WORK/c.out" 2>"$WORK/c.err"; rc=$?
check "(c) worker up, supervisor absent -> exit 0" "$rc" "$(cat "$WORK/c.err")"
[ "$(grep -c '^new-session$' "$WORK/new-session.log" 2>/dev/null)" = 1 ] && r=0 || r=1; check "(c) ...exactly one new-session" "$r"
grep -qx "sutando-worker-w-abc123-watcher" "$WORK/new-session.log" && r=0 || r=1; check "(c) ...named <worker session>-watcher" "$r"
grep -qx "SUTANDO_TASKS_DIR=$WORK/ws/deliveries/w-abc123" "$WORK/new-session.log" && r=0 || r=1; check "(c) ...on the worker's inbox" "$r"
grep -q "started sutando-worker-w-abc123-watcher" "$WORK/c.out" && r=0 || r=1; check "(c) ...and reports the start" "$r"

# (d) supervisor already up: nothing more is started.
printf '%s\n' "sutando-worker-w-abc123" "sutando-worker-w-abc123-watcher" > "$WORK/live"
bash "$ENSURE" >"$WORK/d.out" 2>"$WORK/d.err"; rc=$?
check "(d) supervisor already up -> exit 0" "$rc"
grep -q "already running" "$WORK/d.out" && r=0 || r=1; check "(d) ...saying so" "$r"
[ "$(grep -c '^new-session$' "$WORK/new-session.log")" = 1 ] && r=0 || r=1; check "(d) ...and no second new-session" "$r"

# (e) a missing required variable refuses before tmux is consulted.
rm -f "$WORK/new-session.log"
env -u SUTANDO_TASKS_DIR bash "$ENSURE" >/dev/null 2>"$WORK/e.err"; rc=$?
check "(e) no SUTANDO_TASKS_DIR -> refused" "$([ "$rc" != 0 ] && echo 0 || echo 1)" "rc=$rc"
grep -q "SUTANDO_TASKS_DIR is required" "$WORK/e.err" && r=0 || r=1; check "(e) ...naming the variable" "$r"
[ ! -e "$WORK/new-session.log" ] && r=0 || r=1; check "(e) ...and started nothing" "$r"

# (f) a standby-kind holder nobody supervises (a legacy untagged watcher has this
# shape): a real process named watch-tasks-stream.sh with the inbox as its tag,
# classified by the real watcher_identity.py against the real process table.
mkdir -p "$WORK/fake"; printf '#!/bin/bash\nsleep 60\n' > "$WORK/fake/watch-tasks-stream.sh"; chmod +x "$WORK/fake/watch-tasks-stream.sh"
echo "sutando-worker-w-abc123" > "$WORK/live"; rm -f "$WORK/new-session.log"
bash "$WORK/fake/watch-tasks-stream.sh" --role standby --inbox "$SUTANDO_TASKS_DIR" & HOLDER=$!
sleep 1
bash "$ENSURE" >"$WORK/f.out" 2>"$WORK/f.err"; rc=$?
check "(f) a standby-kind holder on the inbox -> exit 4" "$([ "$rc" = 4 ] && echo 0 || echo 1)" "rc=$rc — $(tail -1 "$WORK/f.err")"
grep -q "already served by a standby-kind watcher" "$WORK/f.err" && r=0 || r=1; check "(f) ...saying so, and how to replace it" "$r"
[ ! -e "$WORK/new-session.log" ] && r=0 || r=1; check "(f) ...and started nothing" "$r"
kill "$HOLDER" 2>/dev/null; wait "$HOLDER" 2>/dev/null; sleep 0.5
bash "$ENSURE" >"$WORK/f2.out" 2>"$WORK/f2.err"; rc=$?
check "(f) ...and once the holder is gone the same ensure starts one" "$rc" "$(cat "$WORK/f2.err")"
[ "$(grep -c '^new-session$' "$WORK/new-session.log" 2>/dev/null)" = 1 ] && r=0 || r=1; check "(f) ...exactly one new-session" "$r"

if [ "$fail" = 0 ]; then echo "  ok  one supervisor per worker inbox, idempotent"; else echo "  FAILED"; fi
exit "$fail"
