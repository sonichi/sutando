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

# The bridge resolves env -> .env -> vault, so an .env-only gate here is NARROWER
# than the thing it gates and parks a bridge whose token is merely in the vault.
# Honor the SAME interpreter contract the bridge launch uses below, or a host
# relying on the explicit override gets a runnable bridge and an unrunnable gate.
_GATE_PY="${SUTANDO_CHANNEL_BRIDGE_PYTHON:-}"
if [ -z "$_GATE_PY" ] && command -v python3 >/dev/null 2>&1; then _GATE_PY=python3; fi
if [ -z "$TOKEN" ] && [ -n "$_GATE_PY" ]; then
  _tok_rc=0
  "$_GATE_PY" "$REPO/src/channel_token.py" --has "$TOKEN_VAR" --env-file "$ENV_FILE" 2>/dev/null || _tok_rc=$?
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

# Interpreter: honor an explicit override, else the PATH-resolved python3. The
# launchd plist sets PATH to "__BREW_BIN__:/usr/bin:...", where __BREW_BIN__ is
# the dir the installer resolved via its own `command -v python3` — so a bare
# `python3` here is the interpreter the installer validated, with no clone-,
# arch-, or user-specific candidate list baked into this committed file.
PYTHON="${SUTANDO_CHANNEL_BRIDGE_PYTHON:-}"
if [ -z "$PYTHON" ] && command -v python3 >/dev/null 2>&1; then
  python3 -c "import $MODULE" >/dev/null 2>&1 && PYTHON=python3
fi
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
  set -e
  CHILD_PID=''
  [ "$STOPPING" = 0 ] || break
  emit_restart_alert
  sleep "$RESTART_DELAY" &
  CHILD_PID=$!
  set +e
  wait "$CHILD_PID"
  set -e
  CHILD_PID=''
done
