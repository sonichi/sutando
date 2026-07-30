#!/bin/bash
# Canonical persistent-core launcher. Runtime-specific behavior lives under
# src/agent/<runtime>/cli/; every caller uses this dispatcher.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"

# Direct restarts (menu bar, health-check recovery, and manual --restart) do
# not pass through startup.sh. Load the same repo configuration here so policy
# switches such as SUTANDO_SELF_DEVELOPMENT_ENABLED survive every launch path.
# Preserve an explicit ambient override (including an empty/invalid value): the
# skill-config contract puts process env above .env, and invalid values must
# reach the policy helper so it can fail closed instead of using the enabled
# manifest default.
if [ -f "$REPO/.env" ]; then
  _self_dev_was_set=0
  if [ "${SUTANDO_SELF_DEVELOPMENT_ENABLED+x}" = x ]; then
    _self_dev_was_set=1
    _self_dev_ambient="$SUTANDO_SELF_DEVELOPMENT_ENABLED"
  fi
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
  if [ "$_self_dev_was_set" = 1 ]; then
    export SUTANDO_SELF_DEVELOPMENT_ENABLED="$_self_dev_ambient"
  fi
  unset _self_dev_was_set _self_dev_ambient
fi

runtime="$(bash "$REPO/scripts/sutando-config.sh" core-runtime)" || {
  echo "start-cli: failed to resolve core runtime" >&2
  exit 1
}

case "$runtime" in
  claude|codex)
    launcher="$REPO/src/agent/$runtime/cli/start-cli.sh"
    ;;
  *)
    echo "start-cli: unsupported core runtime: $runtime" >&2
    exit 2
    ;;
esac

if [ ! -x "$launcher" ]; then
  echo "start-cli: $runtime launcher is missing or not executable: $launcher" >&2
  exit 1
fi

export SUTANDO_CORE_RUNTIME="$runtime"

# A config switch may find the other runtime still occupying the canonical
# tmux session. Detect it before delegation and turn a normal start into a
# restart; otherwise the selected launcher could attach to/reuse the wrong CLI.
tmux_socket="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
session="${SUTANDO_TMUX_SESSION:-sutando-core}"
active_runtime=""
if command -v tmux >/dev/null 2>&1 && tmux -S "$tmux_socket" has-session -t "=$session" 2>/dev/null; then
  active_runtime="$(tmux -S "$tmux_socket" show-environment -t "=$session" SUTANDO_CORE_RUNTIME 2>/dev/null | sed 's/^SUTANDO_CORE_RUNTIME=//' || true)"
  if [ -z "$active_runtime" ]; then
    pane_command="$(tmux -S "$tmux_socket" display-message -p -t "=$session" '#{pane_current_command}' 2>/dev/null || true)"
    case "$pane_command" in claude|codex) active_runtime="$pane_command" ;; esac
  fi
fi
if [ -n "$active_runtime" ] && [ "$active_runtime" != "$runtime" ] \
   && [ "${1:-}" != "--restart" ] && [ "${1:-}" != "--force-restart" ]; then
  echo "Core runtime changed ($active_runtime → $runtime); restarting canonical session."
  set -- --restart "$@"
fi

# The Codex notifier is runtime-specific. Always reap it before launching a
# non-Codex core, including upgrades from sessions that predate runtime markers.
if [ "$runtime" != "codex" ] && command -v tmux >/dev/null 2>&1; then
  tmux -S "$tmux_socket" kill-session -t "=${session}-watcher" 2>/dev/null || true
fi

exec bash "$launcher" "$@"
