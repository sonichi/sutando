#!/bin/bash
# Sutando restart — stops the services in the declared SCOPE, then restarts via startup.sh.
# Does NOT touch the Claude Code CLI (core agent) — that's managed separately.
# Usage: bash src/restart.sh
#   --stop-only    Stop without restarting
#   --rebuild-app  Rebuild the menu-bar app (scripts/install-menu-bar-app.sh) before relaunching it
#   --scope core          (default) this instance's own session and the components it owns
#   --scope worker <id>   that worker's watcher and tmux session, nothing else
#   --scope all           adds the host-wide components every instance shares
#
# Scope exists because a host runs more than one Sutando: a core plus pool
# workers, or two cores. Anything outside the declared scope belongs to another
# instance's lifecycle, and stopping it is the outage this file must not cause.

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Every process this script touches goes through pops_* — one seam a test can
# replace wholesale, instead of hoping a PATH stub shadows the right binary.
# shellcheck source=process-ops.sh
. "$REPO/src/process-ops.sh" || { echo "restart.sh: src/process-ops.sh unreadable — refusing to touch any process" >&2; exit 1; }

# Distinct rc: "I could not prove that watcher is mine" is not "stopped".
RC_WATCHER_UNCONFIRMED=3

SCOPE="core"
WORKER_ID=""
WORKER_SESSION="${SUTANDO_WORKER_TMUX_SESSION:-}"
WORKER_SOCKET="${SUTANDO_WORKER_TMUX_SOCKET:-${SUTANDO_TMUX_SOCKET:-}}"
REBUILD_APP=0
STOP_ONLY=0
ARGS_GIVEN="$*"
while [ $# -gt 0 ]; do
    case "$1" in
        --rebuild-app)     REBUILD_APP=1 ;;
        --stop-only)       STOP_ONLY=1 ;;
        --scope)           SCOPE="${2:-}"; shift ;;
        --scope=*)         SCOPE="${1#--scope=}" ;;
        --worker-session)  WORKER_SESSION="${2:-}"; shift ;;
        --worker-socket)   WORKER_SOCKET="${2:-}"; shift ;;
        *)
            # `--scope worker <id>` is the one two-word value; an id may not
            # follow anything else, or a typo would silently become a scope.
            if [ "$SCOPE" = "worker" ] && [ -z "$WORKER_ID" ]; then
                WORKER_ID="$1"
            else
                echo "restart.sh: unknown argument: $1" >&2; exit 2
            fi ;;
    esac
    shift
done
case "$SCOPE" in
    core|all) ;;
    worker) [ -n "$WORKER_ID" ] || { echo "restart.sh: --scope worker needs an instance id: --scope worker <id>" >&2; exit 2; } ;;
    *) echo "restart.sh: unknown scope: $SCOPE (core|worker <id>|all)" >&2; exit 2 ;;
esac

# The sentinel IS the clean-exit signal, so a failed write must be visible: a
# stub interpreter here would let a stop look successful while nothing changed.
PY_BIN=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  PY_BIN="$(resolve_python "$REPO")"
fi
_shutdown_state() {
  if [ -z "$PY_BIN" ]; then
    echo "restart.sh: no runnable python3 — shutdown sentinel NOT $1" >&2
    return 1
  fi
  "$PY_BIN" "$REPO/src/shutdown.py" "$@" >/dev/null || {
    echo "restart.sh: shutdown.py $1 failed — sentinel state is NOT $1" >&2
    return 1
  }
}

# `kill -0` (pops_alive) proves a pid EXISTS and that we may signal it. It never
# proves the pid is OURS — a dead watcher's number is reissued, and the next
# holder answers identically. Ownership is the sentinel's identity record
# agreeing with the live process on every field it claims; a check that cannot
# be answered is a refusal, never a pass.
# Sets WATCHER_OWNER_PID on success, WATCHER_OWNER_REASON on refusal. NOT via
# stdout: `$( )` is a subshell and the reason would die with it.
_confirm_watcher_owner() {
    local sentinel="$1" want_instance="${2:-}" want_workspace="${3:-}"
    local pid argv recorded code_path inc inc_file inc_live
    WATCHER_OWNER_REASON=""; WATCHER_OWNER_PID=""
    pid="$(head -n1 "$sentinel" 2>/dev/null | tr -d '[:space:]')"
    case "$pid" in ''|*[!0-9]*)
        WATCHER_OWNER_REASON="line 1 of $sentinel is not a pid (read \"$pid\")"; return 1 ;;
    esac
    if ! sentinel_has_record "$sentinel"; then
        WATCHER_OWNER_REASON="$sentinel records a pid only — no instance, incarnation or code_path to check pid $pid against"
        return 1
    fi
    # Unconditional: the default instance's key IS the empty string, so a
    # "compare only when non-empty" gate never checks the core's own sentinel.
    recorded="$(sentinel_record_field "$sentinel" instance)"
    if [ "$recorded" != "$want_instance" ]; then
        WATCHER_OWNER_REASON="instance: $sentinel says \"$recorded\", this scope resolves \"$want_instance\""
        return 1
    fi
    recorded="$(sentinel_record_field "$sentinel" workspace)"
    if [ -n "$recorded" ] && [ -n "$want_workspace" ] && [ "$recorded" != "$want_workspace" ]; then
        WATCHER_OWNER_REASON="workspace: $sentinel says \"$recorded\", this install is \"$want_workspace\""
        return 1
    fi
    code_path="$(sentinel_record_field "$sentinel" code_path)"
    if [ -z "$code_path" ]; then
        WATCHER_OWNER_REASON="code_path: $sentinel records none, so pid $pid's argv cannot be matched to our checkout"
        return 1
    fi
    if ! pops_alive "$pid"; then
        WATCHER_OWNER_REASON="pid $pid is not alive"
        return 1
    fi
    argv="$(pops_argv "$pid")"
    case "$argv" in *"$WATCHER_SENTINEL_STEM"*) ;; *)
        WATCHER_OWNER_REASON="argv: pid $pid is not a live $WATCHER_SENTINEL_STEM"; return 1 ;;
    esac
    case "$argv" in *"$code_path"*) ;; *)
        WATCHER_OWNER_REASON="code_path: pid $pid does not run $code_path"; return 1 ;;
    esac
    inc="$(sentinel_record_field "$sentinel" incarnation)"
    if [ -n "$inc" ]; then
        inc_file="$(sentinel_incarnation_path "$sentinel")"
        if [ ! -f "$inc_file" ]; then
            WATCHER_OWNER_REASON="incarnation: $sentinel claims \"$inc\" but the live process exposes no marker at $inc_file"
            return 1
        fi
        inc_live="$(head -n1 "$inc_file" 2>/dev/null | tr -d '[:space:]')"
        if [ "$inc_live" != "$inc" ]; then
            WATCHER_OWNER_REASON="incarnation: $sentinel claims \"$inc\", the live marker says \"$inc_live\""
            return 1
        fi
    fi
    WATCHER_OWNER_PID="$pid"
}

# Stop the ONE watcher a sentinel names, and only once it is proven ours.
# A pattern kill is never the fallback here: `pkill -f watch-tasks` matched every
# watcher on the host, which is the outage this whole path exists to prevent.
_stop_watcher_at() {            # <sentinel> [expected-instance] [expected-workspace]
    local sentinel="$1" pid
    if [ ! -f "$sentinel" ]; then
        echo "  watcher stop: no sentinel at $sentinel — nothing of ours to stop"
        return 0
    fi
    if ! _confirm_watcher_owner "$sentinel" "${2:-}" "${3:-}"; then
        echo "  watcher stop: OWNERSHIP NOT CONFIRMED — $WATCHER_OWNER_REASON"
        echo "  watcher stop: nothing signalled, $sentinel left in place"
        return "$RC_WATCHER_UNCONFIRMED"
    fi
    pid="$WATCHER_OWNER_PID"
    echo "  watcher stop: signalling this core's watcher (pid $pid)"
    pops_signal "$pid" TERM
    sentinel_release_if_owner "$sentinel" "$pid"
    return 0
}

# Resolve the sentinel for one instance and stop the watcher it names.
# $1 = state dir, $2 = instance id ('' = this process's own identity).
_stop_own_task_watcher() {
    local state_dir="$1" instance="${2:-}" sentinel expect_instance
    if [ -z "$state_dir" ]; then
        echo "  watcher stop: no workspace resolved — cannot name this core's watcher; every watcher left alone"
        return "$RC_WATCHER_UNCONFIRMED"
    fi
    # shellcheck source=watcher_sentinel.sh
    if ! . "$REPO/src/watcher_sentinel.sh" 2>/dev/null; then
        echo "  watcher stop: src/watcher_sentinel.sh unreadable — every watcher left alone"
        return "$RC_WATCHER_UNCONFIRMED"
    fi
    if ! sentinel="$(sentinel_path_for "$state_dir" "$instance")" || [ -z "$sentinel" ]; then
        echo "  watcher stop: could not resolve the sentinel for instance \"${instance:-<this process>}\" — every watcher left alone"
        return "$RC_WATCHER_UNCONFIRMED"
    fi
    # Compare the record against the key ENCODED IN THE PATH we resolved, never
    # the raw id: util_paths derives one from the other, and the suffix is the
    # canonical form both this scope and the watcher's own writer agree on.
    expect_instance="$(basename "$sentinel")"
    expect_instance="${expect_instance#"$WATCHER_SENTINEL_STEM"}"
    expect_instance="${expect_instance%.pid}"
    expect_instance="${expect_instance#-}"
    _stop_watcher_at "$sentinel" "$expect_instance" "${state_dir%/state}"
}

WATCHER_STOP_RC=0

_resolve_workspace() {
    _WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
}

# ---------------------------------------------------------------- worker scope
if [ "$SCOPE" = "worker" ]; then
    echo "Stopping Sutando worker $WORKER_ID..."
    _resolve_workspace
    _stop_own_task_watcher "${_WS:+$_WS/state}" "$WORKER_ID" || WATCHER_STOP_RC=$?
    if [ -n "$WORKER_SESSION" ] && [ -n "$WORKER_SOCKET" ]; then
        # Exact-match selector: a similarly-prefixed session is another instance.
        echo "  worker $WORKER_ID: killing tmux session $WORKER_SESSION on $WORKER_SOCKET"
        pops_tmux -S "$WORKER_SOCKET" kill-session -t "=$WORKER_SESSION" 2>/dev/null || true
    else
        echo "  worker $WORKER_ID: no tmux session named (--worker-session/--worker-socket, or SUTANDO_WORKER_TMUX_SESSION/_SOCKET) — no session touched"
    fi
    echo "  worker $WORKER_ID stopped (nothing outside this worker was touched)"
    [ "$WATCHER_STOP_RC" -ne 0 ] && echo "restart.sh: watcher stop rc=$WATCHER_STOP_RC — ownership unconfirmed, see above"
    exit "$WATCHER_STOP_RC"
fi

echo "Stopping Sutando services (scope: $SCOPE)..."
# Marked before killing so the intake gate holds new tasks while services stop.
# --stop-only leaves it set: that IS the core's clean-exit signal.
_shutdown_state mark "restart.sh${ARGS_GIVEN:+ $ARGS_GIVEN}" || true
# Voice-agent stop goes through the GUARDED lock takeover, never a broad
# `pkill -f voice-agent` (voice-reliability plan amendment U2): the old blind
# pkill could kill an unvalidated process and leave a live lock behind (or
# race a concurrent guarded acquisition). The whole validate → TERM → wait →
# KILL → revalidate → unlink transaction runs inside one voice-lock.py
# invocation under the fcntl guard; identity mismatch → takeover-blocked and
# nothing is signaled. Interpreter unavailable ⇒ fail closed (skip, warn) —
# never signal without validation.
if _VOICE_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"; then
    _VOICE_WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
    _VOICE_PIDFILE="$(bash "$REPO/scripts/sutando-config.sh" voice-pidfile "$_VOICE_WS" 2>/dev/null || true)"
    if [ -n "$_VOICE_WS" ] && [ -n "$_VOICE_PIDFILE" ] && [ -f "$_VOICE_PIDFILE" ]; then
        "$_VOICE_PY" "$REPO/scripts/voice-lock.py" takeover \
            --pidfile "$_VOICE_PIDFILE" \
            --guard "$_VOICE_WS/.voice-agent.lock.guard" \
            --workspace "$_VOICE_WS" \
            --mode adopted --port 9900 \
            --entry "$REPO/src/voice-agent.ts" \
            --entry "$REPO/dist/voice-agent.js" \
            || echo "  WARN voice-agent takeover blocked/failed — not killing blindly (lock left untouched)"
    fi
else
    echo "  WARN no usable python3 for the guarded voice lock helper — skipping voice-agent stop (fail closed)"
fi
# Deliberate restart: the launchd bridge wrappers treat an exit inside this
# window as ours, not a crash, so the owner is not alerted for every restart.
_resolve_workspace
if [ -n "$_WS" ]; then mkdir -p "$_WS/state/channel-bridge-supervisor"; date +%s > "$_WS/state/channel-bridge-supervisor/deliberate-restart"; fi
# The heartbeat sidecar outlives the core on purpose, so a restart must hand it over explicitly:
# startup.sh only starts one when none is running, and an old writer keeps its old schema.
# No interpreter → no handoff, said aloud; an argv sweep is never the fallback.
if [ -n "${PY_BIN:-}" ]; then
    "$PY_BIN" "$REPO/src/core_heartbeat.py" --stop 2>/dev/null || echo "  WARN heartbeat handoff (--stop) failed — the old writer may still be running"
else
    echo "  WARN no runnable python3 for the heartbeat handoff — old writer left running (startup will not replace it)"
fi
pops_pattern_kill "dashboard.py"
pops_pattern_kill "agent-api.py"
pops_pattern_kill "screen-capture-server"
pops_pattern_kill "telegram-bridge"
pops_pattern_kill "discord-bridge"
pops_pattern_kill "slack-bridge"
pops_pattern_kill "remote-gateway-bridge"
# The deprecated `remote-relay-bridge.py` stub runpy-execs the gateway bridge
# IN-PROCESS, so its argv keeps the OLD filename while it runs the NEW code.
# `pkill -f remote-gateway-bridge` therefore cannot see it: measured on a peer
# host 2026-08-03, a stub-launched instance had been up 39 DAYS, survived every
# restart, and kept stamping tasks from 39-day-old code. Kill both names.
pops_pattern_kill "remote-relay-bridge"
pops_pattern_kill "observability/boot"
_stop_own_task_watcher "${_WS:+$_WS/state}" || WATCHER_STOP_RC=$?
# Every other stopped service is relaunched below or by startup.sh. This one
# cannot be: the watcher is armed by the AGENT via the Monitor tool, so a
# shell cannot restore it and the caller is the only thing that can.
if [ "$WATCHER_STOP_RC" -eq 0 ]; then
    echo "  ⚠ task watcher STOPPED — nothing here re-arms it; the agent must:"
    echo "      Monitor  bash src/watch-tasks-stream.sh  (persistent)"
    echo "      until then tasks/ is not drained."
else
    echo "  ⚠ task watcher NOT stopped — ownership unconfirmed (rc $WATCHER_STOP_RC); the old"
    echo "      watcher may still be draining tasks/. Nothing here re-arms one either:"
    echo "      Monitor  bash src/watch-tasks-stream.sh  (persistent)"
fi
pops_pattern_kill "conversation-server"
pops_pattern_kill "ngrok"

# --- app-wide, every instance on this host shares these ----------------------
# web-client (one listener per host), the launchd credential proxy every session
# authenticates through, and the desktop app. A core restart must leave all of
# them running or it takes them away from the other instances.
if [ "$SCOPE" = "all" ]; then
    pops_pattern_kill "web-client.ts"
    # Credential proxy: handle the launchd-supervised job explicitly. pkill alone
    # only bounces the worker — launchd's KeepAlive respawns it on its own throttle,
    # so restart.sh wouldn't actually control the cycle. For a restart, kickstart -k;
    # for --stop-only, bootout so KeepAlive doesn't resurrect it (startup.sh
    # re-bootstraps it next start). Legacy bare-& launch (no job) falls back to pkill.
    _PROXY_LABEL="com.sutando.credential-proxy"
    _PROXY_SERVICE="gui/$(id -u)/$_PROXY_LABEL"
    if pops_launchctl print "$_PROXY_SERVICE" >/dev/null 2>&1; then
        if [ "$STOP_ONLY" -eq 1 ]; then
            echo "  Stopping launchd-supervised credential proxy..."
            pops_launchctl bootout "$_PROXY_SERVICE" 2>/dev/null || true
        else
            echo "  Restarting launchd-supervised credential proxy..."
            pops_launchctl kickstart -k "$_PROXY_SERVICE" 2>/dev/null
            # Wait for an actual LISTENer, not just any socket on 7846 — a bare
            # `lsof -i :7846` also matches transient client connections and would
            # break out before the proxy has rebound.
            for _ in $(seq 1 20); do pops_port_listening 7846 && break; sleep 0.25; done
        fi
    else
        pops_pattern_kill "credential-proxy"
    fi
    pops_pattern_kill "src/Sutando/Sutando"
else
    echo "  ⊘ host-wide components (web-client, credential proxy, Sutando.app) left running — scope is $SCOPE"
fi
echo "  All services stopped"
[ "$WATCHER_STOP_RC" -ne 0 ] && echo "restart.sh: watcher stop rc=$WATCHER_STOP_RC — ownership unconfirmed, see above"

if [ "$STOP_ONLY" -eq 1 ]; then
    echo "Done. Run 'bash src/startup.sh' to start again."
    exit "$WATCHER_STOP_RC"
fi

# Wait for shutdown to drain before exec-ing startup.sh. Fixed `sleep 1`
# raced the pkill'd processes: if Sutando.app (or any SIGTERM-respecting
# service) took >1s to exit cleanly, startup.sh's `if ! pgrep ...` guard
# skipped the relaunch and the user saw "restart did nothing."
# See feedback_pkill_then_open_race.md and PR #499 for the same class on
# startup.sh's recompile-replace path.
STOP_PATTERNS=(
    "voice-agent" "dashboard.py" "agent-api.py"
    "screen-capture-server" "telegram-bridge" "discord-bridge" "slack-bridge"
    "remote-gateway-bridge" "remote-relay-bridge" "observability/boot"
    "conversation-server" "ngrok" "$REPO/src/core_heartbeat.py"
)
# Waited on only under --scope all: a peer instance's web-client or desktop app
# is not ours to outlast, and draining on it would block on a live service.
APP_STOP_PATTERNS=( "web-client.ts" "src/Sutando/Sutando" )
[ "$SCOPE" = "all" ] && STOP_PATTERNS+=( "${APP_STOP_PATTERNS[@]}" )
for _ in $(seq 1 30); do
    still=0
    for pat in "${STOP_PATTERNS[@]}"; do
        if pops_pattern_running "$pat"; then still=1; break; fi
    done
    [ $still -eq 0 ] && break
    sleep 0.1
done

# Restart, not a stop: the core is NOT in STOP_PATTERNS and survives this, so a
# A restart is not a shutdown: a sentinel left set would make the surviving
# core read it as one. --stop-only exits above and deliberately keeps it.
_shutdown_state clear || true

# The app is already stopped (pkill above, drained by the wait loop), so the
# build replaces a binary nothing is running. A failed build keeps the old one.
if [ "$REBUILD_APP" -eq 1 ]; then
    echo "Rebuilding the menu-bar app..."
    if bash "$REPO/scripts/install-menu-bar-app.sh" > /tmp/sutando-app-build.log 2>&1; then
        echo "  ✓ menu-bar app rebuilt"
    else
        echo "  ✗ menu-bar app rebuild failed — see /tmp/sutando-app-build.log; relaunching the existing binary"
    fi
fi

# Relaunch what the app-wide stop killed. This belongs here, not in startup.sh:
# that file is guarded headless (tests/startup-headless.test.sh) and owns no
# desktop UI. Scope-gated with the kill: relaunching an app this scope never
# stopped would adopt another instance's component.
APP_BIN="$REPO/src/Sutando/Sutando"
if [ "$SCOPE" != "all" ]; then
    echo "  ⊘ Sutando.app not relaunched — scope is $SCOPE and it was never stopped"
elif pops_name_running Sutando; then
    echo "  ✓ Sutando.app (already running)"
elif [ -x "$APP_BIN" ]; then
    # The app is the OUT-of-session restart path: a core-session marker inherited
    # here makes restart-guard refuse every restart the app later requests.
    env -u SUTANDO_CORE_SESSION nohup "$APP_BIN" > /tmp/sutando-app.log 2>&1 &
    sleep 1
    # `pgrep -x`, never `-f`: -f matches this script's own argv and would report
    # a launch that did not happen. The ✓ stays inside the verified branch.
    if pops_name_running Sutando; then
        echo "  ✓ Sutando.app relaunched"
    else
        echo "  ✗ Sutando.app — launched but not running; see /tmp/sutando-app.log"
    fi
else
    echo "  ⊘ Sutando.app skipped — no binary at $APP_BIN"
fi

echo "Starting..."
exec bash "$REPO/src/startup.sh"
