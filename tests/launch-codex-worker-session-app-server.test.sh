#!/bin/bash
# The Codex worker launcher's SUTANDO_CODEX_APP_SERVER switch. Off (0, or unset with the core's
# choice unreadable or off), the launcher issues the tmux calls and creates the sessions of the
# launcher without the switch (its marked blocks removed). On (1, or unset with the choice on, the
# default), the worker's session holds the app-server seat: no observer session, no notifier supervisor.
# The choice comes from the stub `sutando-config.sh codex-app-server`, which prints $STUB_CHOICE.
# tmux, codex, node and the helper scripts are stubs, as in launch-codex-worker-session-failopen.test.sh.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
TMP="$(cd "$(mktemp -d)" && pwd -P)"; trap 'rm -rf "$TMP"' EXIT
REAL_PY="$(command -v python3)"

# --- a fake repo: the real launchers, stub helpers ----------------------------
FAKE="$TMP/repo"
POOL="$FAKE/skills/worker-pool/scripts"
SEAT="$FAKE/src/agent/codex/app-server"
mkdir -p "$POOL" "$FAKE/scripts" "$FAKE/src/agent/codex/cli" "$FAKE/skills/proactive-loop/scripts" "$SEAT"
for f in launch-codex-worker-session.sh worker-mcp-credential.sh worker-watcher-supervisor.sh; do
  cp "$REPO/skills/worker-pool/scripts/$f" "$POOL/"
done
cp "$REPO/src/agent/launch-path.sh" "$FAKE/src/agent/"
cp "$REPO/src/agent/codex/codex-home.sh" "$FAKE/src/agent/codex/"
cp "$REPO/src/codex_home.py" "$REPO/src/codex_host_instructions.py" "$REPO/src/sutando_config.py" "$FAKE/src/"
cp "$REPO/src/agent/codex/app-server/codex-seat" "$REPO/src/agent/codex/app-server/seat-launch.sh" "$SEAT/"
# The seat itself is the real one: through the link it finds the real checkout's modules.
ln -s "$REPO/src/agent/codex/app-server/seat.py" "$SEAT/seat.py"
touch "$FAKE/src/__init__.py"
printf '{"workspace":{"path":"%s/ws"},"core_config_dirs":[{"id":"x","type":"codex","env_name":"CODEX_HOME","synced":false,"value":"%s/codex"}]}\n' "$TMP" "$TMP" > "$FAKE/sutando.config.json"
printf '#!/bin/bash\n[ "$1" = codex-app-server ] && printf %%s "${STUB_CHOICE:-}"\nexit 0\n' > "$FAKE/scripts/sutando-config.sh"
printf 'require_python() { printf "%%s\\n" "$SUTANDO_PY"; }\nresolve_python() { printf "%%s\\n" "$SUTANDO_PY"; }\n' > "$FAKE/scripts/python-binary.sh"
printf '// stub observer\n' > "$FAKE/src/agent/codex/cli/codex-observer.mjs"
# The per-seat MCP composer: two overrides, one per line pair, as the real one prints them.
printf 'print("-c")\nprint("mcp_servers.s-space.enabled=true")\n' > "$POOL/codex_worker_mcp.py"
# The launcher without the switch, beside the real one so both resolve the same repo.
strip_seat() { awk '/# >>> codex-app-server-seat/{skip=1} !skip{print} /# <<< codex-app-server-seat/{skip=0}' "$1"; }
strip_seat "$POOL/launch-codex-worker-session.sh" > "$POOL/launch-before.sh"
cp "$POOL/worker-watcher-supervisor.sh" "$POOL/supervisor-real.sh"
strip_seat "$POOL/worker-watcher-supervisor.sh" > "$POOL/supervisor-before.sh"
printf '#!/bin/bash\ntouch "$TEST_TMP/supervisor-ran"\n' > "$POOL/worker-watcher-supervisor.sh"
chmod +x "$FAKE"/scripts/*.sh "$POOL"/*.sh

# --- stubs on PATH ------------------------------------------------------------
mkdir -p "$TMP/bin"
# Python: the seat and the MCP composer run for real, every other helper is a no-op as in the failopen test.
cat > "$TMP/bin/python3" <<PY
#!/bin/sh
case "\$1" in */seat.py|*/codex_worker_mcp.py) exec "$REAL_PY" "\$@" ;; esac
exit 0
PY
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/codex"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/fswatch"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/node"
cat > "$TMP/bin/tmux" <<'TMUX'
#!/bin/bash
printf '%s\n' "$*" >> "$TEST_TMP/tmux.log"
shift 2
cmd="$1"; shift
case "$cmd" in
  new-session)
    name=""; while [ $# -gt 0 ]; do
      case "$1" in -d) shift ;; -s) name="$2"; shift 2 ;; -e) shift 2 ;; *) break ;; esac
    done
    [ -f "$TEST_TMP/sessions/$name" ] && { echo "duplicate session: $name" >&2; exit 1; }
    touch "$TEST_TMP/sessions/$name"; exit 0 ;;
  has-session) n="${2#=}"
    # race-once: the launcher's pre-check misses a session a competing launch creates before new-session.
    [ -f "$TEST_TMP/race-once" ] && { rm -f "$TEST_TMP/race-once"; exit 1; }
    [ -f "$TEST_TMP/sessions/$n" ] ;;
  kill-session) n="${2#=}"; rm -f "$TEST_TMP/sessions/$n"; exit 0 ;;
  show-environment) [ -n "${TMUX_STUB_SEAT_ENV:-}" ] && echo "${TMUX_STUB_SEAT_ENV_LINE:-SUTANDO_CODEX_SEAT=1}"; exit 0 ;;
  *) exit 0 ;;
esac
TMUX
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ID=0123456789abcdef0123456789abcdef
SESSION="sutando-worker-$ID"
DECL="$TMP/ws/state/codex-seat/$SESSION/host.json"

launch() {  # launch <script> [extra env...] -> exit code; stderr in $TMP/err, tmux calls in $TMP/tmux.log
  local script="$1"; shift
  rm -rf "$TMP/sessions" "$TMP/supervisor-ran" "$TMP/tmux.log" "$TMP/ws"; mkdir -p "$TMP/sessions" "$TMP/ws/deliveries/$ID" "$TMP/ws/tasks"
  env -i HOME="$TMP" PATH="$STUB_PATH" TEST_TMP="$TMP" SUTANDO_PY="$TMP/bin/python3" \
      SUTANDO_INSTANCE_ID="$ID" SUTANDO_TMUX_SESSION="$SESSION" SUTANDO_TMUX_SOCKET="$TMP/sock" \
      SUTANDO_TASKS_DIR="$TMP/ws/deliveries/$ID" SUTANDO_WORKSPACE_DIR="$TMP/ws" SUTANDO_RESULTS_DIR="$TMP/ws/results" \
      SUTANDO_INBOX_RESOLVER=/bin/true SUTANDO_POOL_DELIVERY_SCRIPT=/bin/true SUTANDO_CODEX_WORKING_DIR="$TMP/wd" "$@" \
      bash "$POOL/$script" >/dev/null 2>"$TMP/err"
}
sessions() { ls "$TMP/sessions" | tr '\n' ' '; }

# --- 1. flag unset, empty or 0: the calls and sessions of the launcher without the switch ---
launch launch-before.sh; rc_before=$?
calls_before="$(cat "$TMP/tmux.log")"; sessions_before="$(sessions)"
[ -f "$TMP/supervisor-ran" ]; sup_before=$?
check "$rc_before" "without the switch: the launcher exits 0"
grep -q "new-session -d -s $SESSION .* codex -C .* --search .* -c mcp_servers.s-space.enabled=true You are Sutando worker" <<< "$calls_before"
check $? "without the switch: the worker session is the Codex TUI"
# The last: a worker spawned by a running seat inherits its marker and resolved mode, never an override.
for setting in "" "SUTANDO_CODEX_APP_SERVER=" "SUTANDO_CODEX_APP_SERVER=0" "STUB_CHOICE=0" \
               "SUTANDO_CODEX_APP_SERVER=0 STUB_CHOICE=1" "SUTANDO_CODEX_SEAT=1 CODEX_SEAT_ON=1 STUB_CHOICE=0"; do
  # shellcheck disable=SC2086 # one or two NAME=value words
  launch launch-codex-worker-session.sh $setting; rc=$?
  label="${setting:-unset}"
  [ "$rc" = "$rc_before" ]; check $? "$label: the same exit code"
  [ "$(cat "$TMP/tmux.log")" = "$calls_before" ]; check $? "$label: the same tmux calls, in the same order"
  [ "$(sessions)" = "$sessions_before" ] && [ "$sessions_before" = "$SESSION $SESSION-observer " ]
  check $? "$label: the same sessions (worker and observer)"
  [ -f "$TMP/supervisor-ran" ]; [ "$?" = "$sup_before" ] && [ "$sup_before" = 0 ]
  check $? "$label: the notifier supervisor runs"
  [ ! -e "$DECL" ]; check $? "$label: no seat declaration is written"
done

# --- 2. flag on: the seat in the worker's session, no observer, no notifier supervisor ---
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_STOP_INTERRUPT=1; rc=$?
check "$rc" "flag on: the launcher exits 0"
[ "$(sessions)" = "$SESSION " ]; check $? "flag on: the only session is the worker's own name"
[ ! -f "$TMP/supervisor-ran" ]; check $? "flag on: the notifier supervisor is not started"
line="$(grep "new-session -d -s $SESSION " "$TMP/tmux.log")"
grep -q " $SEAT/codex-seat --host $DECL --workspace $TMP/ws --seat $SESSION --worker $ID --codex $TMP/bin/codex --instructions You are Sutando worker $ID\. " <<< "$line"
check $? "flag on: the session runs the seat for this worker, with the boot prompt as its standing instructions"
grep -q -- '-c web_search="live" -c check_for_update_on_startup=false -c mcp_servers.s-space.enabled=true --watcher$' <<< "$line"
check $? "flag on: --search and every -c override of the TUI launch reach the app-server"
grep -q -- "--sandbox danger-full-access" <<< "$line"; check $? "flag on: the sandbox is the TUI launch's"
grep -q -- "-e SUTANDO_CODEX_SEAT=1 -e SUTANDO_STANDBY_MODE=type -e SUTANDO_STOP_INTERRUPT=1 -e SUTANDO_WATCHER_BEAT=$POOL/pool_beat.py" <<< "$line"
check $? "flag on: the seat's watcher gets the notifier session's settings"
grep -q -- "-e SUTANDO_INSTANCE_ID=$ID" <<< "$line" && grep -q -- "-e AG2_MCP_WORKER_CREDENTIAL=" <<< "$line"
check $? "flag on: the worker's own environment is forwarded as before"
! grep -q -- "--ask-for-approval\|--no-alt-screen\| codex -C " <<< "$line"; check $? "flag on: no TUI is started"
"$REAL_PY" - "$DECL" "$TMP" "$ID" <<'PY'
import json, sys
decl, tmp, wid = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
assert decl["schema"] == "ag2-sparrow.supervised-host/1", decl
assert decl["inbox"] == f"{tmp}/ws/deliveries/{wid}" and decl["payloads"] == f"{tmp}/ws/tasks", decl
assert decl["results"] == f"{tmp}/ws/results" and decl["cwd"] == f"{tmp}/wd", decl
assert decl["stage_command"] == [f"{tmp}/bin/python3", "/bin/true", "--workspace", f"{tmp}/ws",
                                 "--recipient", wid, "mark-done"], decl
PY
check $? "flag on: the declaration names the delivery inbox, the task store and the pool's stage hook"
grep -q "is a Codex app-server seat; no observer or notifier session was started" "$TMP/err"
check $? "flag on: the launcher says what it started"
[ ! -e "$(dirname "$DECL")/incarnation" ]; check $? "flag on, no run id: no run stamp is written"
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_WORKER_INCARNATION_ID=i-this-run
[ "$(cat "$(dirname "$DECL")/incarnation" 2>/dev/null)" = "i-this-run" ]
check $? "flag on: the declaration is stamped with this run's incarnation id"
# An unstamped seat from an older launcher, then a TUI launch under the same session name.
launch_keep() {  # as launch, but over the workspace the previous launch left
  local script="$1"; shift
  rm -f "$TMP/tmux.log"; mkdir -p "$TMP/sessions"
  env -i HOME="$TMP" PATH="$STUB_PATH" TEST_TMP="$TMP" SUTANDO_PY="$TMP/bin/python3" \
      SUTANDO_INSTANCE_ID="$ID" SUTANDO_TMUX_SESSION="$SESSION" SUTANDO_TMUX_SOCKET="$TMP/sock" \
      SUTANDO_TASKS_DIR="$TMP/ws/deliveries/$ID" SUTANDO_WORKSPACE_DIR="$TMP/ws" SUTANDO_RESULTS_DIR="$TMP/ws/results" \
      SUTANDO_INBOX_RESOLVER=/bin/true SUTANDO_POOL_DELIVERY_SCRIPT=/bin/true SUTANDO_CODEX_WORKING_DIR="$TMP/wd" "$@" \
      bash "$POOL/$script" >/dev/null 2>"$TMP/err"
}
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1
rm -f "$(dirname "$DECL")/incarnation"; rm -rf "$TMP/sessions"
launch_keep launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=0 SUTANDO_WORKER_INCARNATION_ID=i-tui-run
rc=$?
[ "$rc" = 0 ] && [ ! -e "$DECL" ] && [ ! -e "$(dirname "$DECL")/incarnation" ]
check $? "an unstamped earlier seat, then a TUI launch: the seat's declaration is removed"
[ "$(sessions)" = "$SESSION $SESSION-observer " ]
check $? "an unstamped earlier seat, then a TUI launch: the session is the TUI with its observer"
# A live seat wins the session name between a competing TUI launch's check and its new-session.
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_WORKER_INCARNATION_ID=i-live-seat
decl_before="$(cat "$DECL")"; touch "$TMP/race-once"
launch_keep launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=0 SUTANDO_WORKER_INCARNATION_ID=i-losing-tui
[ $? = 75 ]; check $? "a TUI launch that loses to a live seat: exit 75, the launch-lost code"
! grep -q -- "-observer" "$TMP/tmux.log"; check $? "a TUI launch that loses to a live seat: no observer is started for the winner's session"
grep -q "new-session -d -s $SESSION .* codex -C " "$TMP/tmux.log" && [ "$(cat "$DECL" 2>/dev/null)" = "$decl_before" ] \
  && [ "$(cat "$(dirname "$DECL")/incarnation" 2>/dev/null)" = "i-live-seat" ]
check $? "a TUI launch that loses to a live seat: the seat's declaration and run stamp are untouched"
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_WORKER_INCARNATION_ID=i-live-seat
touch "$TMP/race-once"
launch_keep launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_WORKER_INCARNATION_ID=i-losing-seat
[ $? = 75 ] && [ "$(cat "$(dirname "$DECL")/incarnation" 2>/dev/null)" = "i-live-seat" ]
check $? "a seat launch that loses to a live seat: exit 75 and the winner's run stamp is kept"

# --- 3. flag on, a worker on its own folders: a plain inbox, no payload store ---
# Only a launcher that accepts a worker without a resolver has this case.
if grep -q 'RESOLVER_REQUIRED=$' "$POOL/launch-codex-worker-session.sh"; then
  launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_INBOX_KIND= SUTANDO_INBOX_RESOLVER=; rc=$?
  check "$rc" "own folders: the launcher exits 0"
  [ -f "$DECL" ] && ! grep -q '"payloads"' "$DECL"; check $? "own folders: the declaration names no payload store"
fi

# --- 4. flag on, the declaration cannot be written: the worker is not started ---
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1 SUTANDO_RESULTS_DIR="$TMP/ws/deliveries/$ID"; rc=$?
[ "$rc" = 1 ] && [ -z "$(sessions)" ]; check $? "bad declaration: exit 1 and no session"
grep -q "could not write the seat's host declaration" "$TMP/err"; check $? "bad declaration: the launcher says so"

# --- 5. the watcher supervisor leaves a seat alone, and only a seat ---
supervise() {  # supervise <script> [extra env...] -> exit code; stdout in $TMP/out
  local script="$1"; shift
  rm -f "$TMP/tmux.log"
  env -i HOME="$TMP" PATH="$STUB_PATH" TEST_TMP="$TMP" SUTANDO_PY="$TMP/bin/python3" SUTANDO_WORKER_RUNTIME=codex \
      SUTANDO_INSTANCE_ID="$ID" SUTANDO_TMUX_SESSION="$SESSION" SUTANDO_TMUX_SOCKET="$TMP/sock" \
      SUTANDO_TASKS_DIR="$TMP/ws/deliveries/$ID" SUTANDO_WORKSPACE_DIR="$TMP/ws" "$@" \
      bash "$POOL/$script" >"$TMP/out" 2>&1
}
launch launch-codex-worker-session.sh SUTANDO_CODEX_APP_SERVER=1
supervise supervisor-real.sh TMUX_STUB_SEAT_ENV=1; rc=$?
check "$rc" "supervisor, a seat's session: exit 0"
grep -q "is a Codex app-server seat; it needs no notifier" "$TMP/out" && ! grep -q "new-session" "$TMP/tmux.log"
check $? "supervisor, a seat's session: no notifier session is started"
supervise supervisor-real.sh TMUX_STUB_SEAT_ENV=1 TMUX_STUB_SEAT_ENV_LINE=SUTANDO_CODEX_APP_SERVER=1; rc=$?
check "$rc" "supervisor, a seat started before the marker was renamed: exit 0"
grep -q "is a Codex app-server seat; it needs no notifier" "$TMP/out" && ! grep -q "new-session" "$TMP/tmux.log"
check $? "supervisor, a seat started before the marker was renamed: no notifier session is started"
supervise supervisor-real.sh; rc=$?
grep -q "new-session -d -s $SESSION-watcher " "$TMP/tmux.log"
check $? "supervisor, a declaration left behind but a TUI session: the notifier session is started"
rm -f "$TMP/sessions/$SESSION-watcher" "$DECL"
supervise supervisor-real.sh TMUX_STUB_SEAT_ENV=1; real_calls="$(cat "$TMP/tmux.log")"
rm -f "$TMP/sessions/$SESSION-watcher"
supervise supervisor-before.sh TMUX_STUB_SEAT_ENV=1
[ "$real_calls" = "$(cat "$TMP/tmux.log")" ] && grep -q "new-session -d -s $SESSION-watcher " <<< "$real_calls"
check $? "supervisor, no declaration: the same tmux calls as the supervisor without the switch"

# --- the core's persisted choice, on (the default), with no variable: the worker is a seat too ---
for setting in "STUB_CHOICE=1" "SUTANDO_CODEX_APP_SERVER= STUB_CHOICE=1"; do
  # shellcheck disable=SC2086
  launch launch-codex-worker-session.sh $setting; rc=$?
  check "$rc" "$setting: the launcher exits 0"
  [ "$(sessions)" = "$SESSION " ] && [ ! -f "$TMP/supervisor-ran" ] && [ -e "$DECL" ]
  check $? "$setting: the worker is a seat (declaration written, no observer, no notifier)"
  grep -q -- "new-session -d -s $SESSION .*-e SUTANDO_CODEX_SEAT=1 .*$SEAT/codex-seat --host $DECL " "$TMP/tmux.log"
  check $? "$setting: its session runs the seat"
done

echo "---"
if [ "$fail" -gt 0 ]; then echo "FAILED — $fail of $((pass+fail)) checks"; exit 1; fi
echo "PASS — $pass checks green"
