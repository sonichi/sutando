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
  # Module-aware: the contract candidate can be a bundled runtime that simply
  # lacks this channel's dep, while a later candidate in the same order has it.
  PYTHON="$(resolve_python_for_module "$REPO" "$MODULE")"
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
  echo "[$CHANNEL-bridge-wrapper] no python3 can 'import $MODULE' (tried \$SUTANDO_PY, the bundled runtime, then PATH) — install it for one of those, or set SUTANDO_CHANNEL_BRIDGE_PYTHON" >&2
  exit 1
fi

WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
STATE_DIR="$WORKSPACE/state/channel-bridge-supervisor"
mkdir -p "$STATE_DIR" "$WORKSPACE/results"
MARKER="$STATE_DIR/$CHANNEL.started"
# A deliberate restart (src/restart.sh) stamps this; an exit inside the window is
# not a crash and gets no owner alert. Same channel alerted within the cooldown: one line, not a stream.
DELIBERATE="$STATE_DIR/deliberate-restart"
DELIBERATE_WINDOW_S="${SUTANDO_BRIDGE_DELIBERATE_WINDOW_S:-180}"
ALERT_STAMP="$STATE_DIR/$CHANNEL.last-alert"
ALERT_COOLDOWN_S="${SUTANDO_BRIDGE_ALERT_COOLDOWN_S:-600}"
# Burst limit: the 4th notice inside the window says "repeatedly" and opens a long quiet period.
ALERT_TIMES="$STATE_DIR/$CHANNEL.alert-times"
ALERT_QUIET="$STATE_DIR/$CHANNEL.alert-quiet-until"
ALERT_BURST_MAX=3
ALERT_BURST_WINDOW_S="${SUTANDO_BRIDGE_ALERT_BURST_WINDOW_S:-3600}"
ALERT_QUIET_S="${SUTANDO_BRIDGE_ALERT_QUIET_S:-21600}"
# 78 == channel_auth_exit.EXIT_AUTH_REJECTED (src/channel_auth_exit.py): the provider rejected the token.
EXIT_AUTH_REJECTED=78
AUTH_HELD="$STATE_DIR/$CHANNEL.auth-held"
AUTH_HOLD_POLL_S="${SUTANDO_BRIDGE_AUTH_HOLD_POLL_S:-30}"
case "$CHANNEL" in
  slack) AUTH_KEYS="SLACK_BOT_TOKEN or SLACK_APP_TOKEN" ;;
  *) AUTH_KEYS="$TOKEN_VAR" ;;
esac
_age() { local t; t="$(head -n 1 "$1" 2>/dev/null || echo 0)"; echo $(( $(date +%s) - ${t:-0} )); }
_stamp() { local t; t="$(head -n 1 "$1" 2>/dev/null || echo 0)"; echo "${t:-0}"; }
publish_notice() {  # $1=file tag, $2=text
  printf '%s\n' "$2" \
    | "$PYTHON" "$REPO/src/result_publish.py" "$WORKSPACE/results/proactive-$CHANNEL-bridge-$1-$NOW.txt" \
    || echo "[$CHANNEL-bridge-wrapper] could not publish the $1 notice" >&2
  osascript -e "display notification \"$2\" with title \"Sutando\"" >/dev/null 2>&1 || true
}
emit_restart_alert() {
  NOW="$(date +%s)"
  echo "[$CHANNEL-bridge-wrapper] previous process exited; automatically restarting" >&2
  if [ -f "$DELIBERATE" ] && [ "$(_age "$DELIBERATE")" -lt "$DELIBERATE_WINDOW_S" ]; then
    echo "[$CHANNEL-bridge-wrapper] exit within a deliberate restart window; no alert" >&2
    return 0
  fi
  if [ "$(_stamp "$ALERT_QUIET")" -gt "$NOW" ]; then
    echo "[$CHANNEL-bridge-wrapper] restarting repeatedly; notices quiet until $(_stamp "$ALERT_QUIET")" >&2
    return 0
  fi
  if [ -f "$ALERT_STAMP" ] && [ "$(_age "$ALERT_STAMP")" -lt "$ALERT_COOLDOWN_S" ]; then
    echo "[$CHANNEL-bridge-wrapper] alert cooldown active; not repeating" >&2
    return 0
  fi
  echo "$NOW" > "$ALERT_STAMP"
  local recent
  recent="$(awk -v c=$(( NOW - ALERT_BURST_WINDOW_S )) '$1 > c' "$ALERT_TIMES" 2>/dev/null || true)"
  if [ -n "$recent" ] && [ "$(printf '%s\n' "$recent" | wc -l)" -ge "$ALERT_BURST_MAX" ]; then
    echo $(( NOW + ALERT_QUIET_S )) > "$ALERT_QUIET"
    rm -f "$ALERT_TIMES"
    publish_notice restarting-repeatedly \
      "⚠️ The $CHANNEL bridge is restarting repeatedly; see logs/$CHANNEL-bridge.log. No further restart notices for $(( ALERT_QUIET_S / 3600 )) hours."
    return 0
  fi
  { [ -z "$recent" ] || printf '%s\n' "$recent"; echo "$NOW"; } > "$ALERT_TIMES.tmp" && mv "$ALERT_TIMES.tmp" "$ALERT_TIMES"
  publish_notice restarted "⚠️ The $CHANNEL bridge exited and was automatically restarted."
}
# Exit 0 is no way out: KeepAlive is unconditional, so launchd would relaunch at once.
# Hold, with no child, until a deliberate-restart stamp newer than the one this child launched under.
hold_for_credentials() {
  NOW="$(date +%s)"
  echo "[$CHANNEL-bridge-wrapper] held: credential rejected ($AUTH_KEYS); vault set <KEY> <value>, then bash src/restart.sh" >&2
  # The held file records the stamp each rejection ran under: one notice per owner restart, across relaunches.
  if [ ! -f "$AUTH_HELD" ] || [ "$LAUNCH_STAMP" -gt "$(_stamp "$AUTH_HELD")" ]; then
    # The launch stamp in the name keeps a retry's notice from overwriting one sent the same second.
    publish_notice "auth-failed-r$LAUNCH_STAMP" \
      "⚠️ The $CHANNEL bridge is stopped: the provider rejected its token ($AUTH_KEYS). Store a valid one with \`vault set <KEY> <value>\`, then restart Sutando (bash src/restart.sh); a stale token in that channel's .env wins over the vault, so clear it there too. It will not retry until then."
  fi
  printf '%s\n%s\n' "$LAUNCH_STAMP" "$$" > "$AUTH_HELD"
  while [ "$STOPPING" = 0 ] && [ "$(_stamp "$DELIBERATE")" -le "$LAUNCH_STAMP" ]; do
    sleep "$AUTH_HOLD_POLL_S" &
    CHILD_PID=$!
    set +e
    wait "$CHILD_PID"
    set -e
    CHILD_PID=''
  done
  [ "$STOPPING" = 0 ] && echo "[$CHANNEL-bridge-wrapper] deliberate restart seen; retrying once" >&2
  return 0
}
# A relaunch out of an auth hold is not a crash; the held notice already went out.
if [ -f "$MARKER" ] && [ ! -f "$AUTH_HELD" ]; then emit_restart_alert; fi
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
  # Best-effort HERE by design: a discovery failure (nonzero return) must not
  # set -e-abort the wrapper and park the bridge; the singleton lock still holds.
  evict_own_bridge "$CHANNEL" "$REPO" || echo "[$CHANNEL-bridge-wrapper] eviction skipped (rc=$?)" >&2
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
  LAUNCH_STAMP="$(_stamp "$DELIBERATE")"
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
  [ "$CHILD_RC" -eq 75 ] && { rm -f "$MARKER" "$AUTH_HELD"; exit 0; }
  if [ "$CHILD_RC" -eq "$EXIT_AUTH_REJECTED" ]; then
    hold_for_credentials
    continue
  fi
  rm -f "$AUTH_HELD"
  emit_restart_alert
  sleep "$RESTART_DELAY" &
  CHILD_PID=$!
  set +e
  wait "$CHILD_PID"
  set -e
  CHILD_PID=''
done
