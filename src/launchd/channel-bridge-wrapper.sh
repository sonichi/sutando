#!/bin/bash
# launchd entry point shared by Slack, Discord, and Telegram bridges.

set -euo pipefail

CHANNEL="${1:-}"
case "$CHANNEL" in slack|discord|telegram) ;; *) exit 2 ;; esac

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

ENV_FILE="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path "channels/$CHANNEL/.env" 2>/dev/null || true)"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

case "$CHANNEL" in
  slack) TOKEN_VAR=SLACK_BOT_TOKEN; TOKEN="${SLACK_BOT_TOKEN:-}"; MODULE=slack_bolt ;;
  discord) TOKEN_VAR=DISCORD_BOT_TOKEN; TOKEN="${DISCORD_BOT_TOKEN:-}"; MODULE=discord ;;
  # telegram has no third-party dep; urllib.request is stdlib, so this stays an
  # interpreter probe rather than a network one.
  telegram) TOKEN_VAR=TELEGRAM_BOT_TOKEN; TOKEN="${TELEGRAM_BOT_TOKEN:-}"; MODULE=urllib.request ;;
esac

# Interpreter: resolved ONCE, before anything invokes it. Honor an explicit
# override, else the safe-interpreter-contract candidate -- but only after the
# module probe accepts it. A bare `command -v python3` can be the Xcode Command
# Line Tools stub, which prompts an install dialog when executed, so the token
# gate below must never be the thing that discovers that. scripts/python-binary.sh
# (already used by src/startup.sh and start-cli.sh) resolves the same way this
# wrapper needs to: it rejects the /usr/bin stub via `xcode-select -p` unless the
# developer tools are actually installed, so its candidate is safe to run here.
PYTHON="${SUTANDO_CHANNEL_BRIDGE_PYTHON:-}"
if [ -z "$PYTHON" ] && [ -r "$REPO/scripts/python-binary.sh" ]; then
  # shellcheck source=../../scripts/python-binary.sh
  . "$REPO/scripts/python-binary.sh"
  _candidate="$(resolve_python "$REPO")"
  [ -n "$_candidate" ] && "$_candidate" -c "import $MODULE" >/dev/null 2>&1 && PYTHON="$_candidate"
fi

# The bridge resolves env -> .env -> vault, so an .env-only gate here is NARROWER
# than the thing it gates and parks a bridge whose token is merely in the vault.
# Uses the SAME validated $PYTHON the child gets: a host relying on the explicit
# override otherwise gets a runnable bridge and an unrunnable gate, and a host
# whose PATH python3 is a stub must not have it invoked here at all.
if [ -z "$TOKEN" ] && [ -n "$PYTHON" ]; then
  _tok_rc=0
  "$PYTHON" "$REPO/src/channel_token.py" --has "$TOKEN_VAR" --env-file "$ENV_FILE" 2>/dev/null || _tok_rc=$?
  # 0 = usable, 3 = definitively absent, anything else = resolver unrunnable, so
  # fall through to the .env answer rather than taking the bridge down on a bug.
  [ "$_tok_rc" -eq 0 ] && TOKEN="vault"
fi
if [ -z "$TOKEN" ]; then
  # KeepAlive=true is intentionally unconditional: the conditional
  # Crashed/SuccessfulExit dictionary can remain pended instead of respawning
  # after SIGKILL on current macOS. Stay resident without a child when the
  # channel is deconfigured, avoiding both a 10s crash loop and false bridge
  # activity. startup.sh sees no bridge PID and kickstarts this wrapper after
  # credentials return.
  echo "[$CHANNEL-bridge-wrapper] token removed; waiting idle" >&2
  while :; do sleep 300; done
fi

# $PYTHON was resolved above, before the token gate could invoke anything. The
# fatal check stays HERE, after the idle branch: a deconfigured channel with no
# usable interpreter must still park quietly rather than exit 1 into a respawn.
if [ -z "$PYTHON" ]; then
  echo "[$CHANNEL-bridge-wrapper] no usable Python interpreter" >&2
  exit 1
fi

WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
STATE_DIR="$WORKSPACE/state/channel-bridge-supervisor"
mkdir -p "$STATE_DIR" "$WORKSPACE/results"
MARKER="$STATE_DIR/$CHANNEL.started"
emit_restart_alert() {
  NOW="$(date +%s)"
  RESULT="$WORKSPACE/results/proactive-$CHANNEL-bridge-restarted-$NOW.txt"
  echo "[$CHANNEL-bridge-wrapper] previous process exited; automatically restarting" >&2
  printf '%s\n' "⚠️ The $CHANNEL bridge exited and was automatically restarted." > "$RESULT"
  osascript -e "display notification \"The $CHANNEL bridge exited and was automatically restarted.\" with title \"Sutando\"" >/dev/null 2>&1 || true
}
if [ -f "$MARKER" ]; then emit_restart_alert; fi
date +%s > "$MARKER"

# Remove a pre-existing bare process for this channel before exec — but ONLY one
# belonging to THIS checkout. A plain `pkill -f src/<channel>-bridge.py$` matches
# the same bridge launched from ANY checkout on the host, so starting/upgrading
# one install could kill another install's live bridge (CR #2068). evict_own_bridge
# validates each candidate's identity (command path under $REPO, or cwd == $REPO).
# All three bridges also have single-instance protection, but eviction makes the
# launchd ownership transition immediate and deterministic. The helper is sourced
# only if present, so a partial deploy (or a test fixture that copies just this
# wrapper) degrades to no-eviction instead of `set -e`-aborting before the child
# is launched (CR #2068 round 2, qingyun-wu).
_EVICT_HELPER="$REPO/src/launchd/evict-own-bridge.sh"
if [ -f "$_EVICT_HELPER" ]; then
  # shellcheck source=evict-own-bridge.sh
  . "$_EVICT_HELPER"
  evict_own_bridge "$CHANNEL" "$REPO"
fi
sleep 0.3

# Keep this wrapper resident and supervise the bridge as its child. launchd's
# KeepAlive can deliberately defer a repeatedly-killed job as "inefficient";
# owning the bridge child here makes recovery deterministic and also lets us
# alert immediately after the actual channel process exits.
CHILD_PID=''
STOPPING=0
stop_wrapper() {
  STOPPING=1
  [ -z "$CHILD_PID" ] || kill "$CHILD_PID" 2>/dev/null || true
}
trap stop_wrapper TERM INT HUP
RESTART_DELAY="${SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY:-10}"
while [ "$STOPPING" = 0 ]; do
  "$PYTHON" "$REPO/src/$CHANNEL-bridge.py" &
  CHILD_PID=$!
  set +e
  wait "$CHILD_PID"
  CHILD_RC=$?
  set -e
  CHILD_PID=''
  [ "$STOPPING" = 0 ] || break
  # 75 == single_instance.EXIT_STANDDOWN: a peer holds the lock. Gate on THAT,
  # never on 0 -- a bridge whose main loop returns also exits 0, and treating
  # that as deliberate would leave it down silently, with the alert suppressed.
  # Clear the marker too: launchd KeepAlive is unconditional, so exiting hands
  # the respawn back to launchd, which re-enters above and alerts off a marker
  # this run left behind.
  [ "$CHILD_RC" -eq 75 ] && { rm -f "$MARKER"; exit 0; }
  emit_restart_alert
  sleep "$RESTART_DELAY" &
  CHILD_PID=$!
  set +e
  wait "$CHILD_PID"
  set -e
  CHILD_PID=''
done
