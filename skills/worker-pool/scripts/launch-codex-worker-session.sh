#!/usr/bin/env bash
set -euo pipefail

case "$0" in
  */*) self_dir="${0%/*}" ;;
  *) self_dir="." ;;
esac
REPO="$(cd "$self_dir/../../.." && pwd)"
cd "$REPO"

for required in SUTANDO_INSTANCE_ID SUTANDO_TMUX_SESSION SUTANDO_TASKS_DIR \
                SUTANDO_WORKSPACE_DIR SUTANDO_RESULTS_DIR SUTANDO_INBOX_RESOLVER \
                SUTANDO_POOL_DELIVERY_SCRIPT; do
  if [ -z "${!required:-}" ]; then
    echo "launch-codex-worker-session: $required is required" >&2
    exit 2
  fi
done
SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="$SUTANDO_TMUX_SESSION"

# shellcheck source=../../../src/agent/launch-path.sh
. "$REPO/src/agent/launch-path.sh"
sutando_append_launch_path "$REPO" "$REPO/../bin" "$(sutando_bundled_node_dir "$REPO")" "$HOME/.npm-global/bin"

config_env="$(bash "$REPO/scripts/sutando-config.sh" core-config-dir-env-name codex)"
config_value="$(bash "$REPO/scripts/sutando-config.sh" core-config-dir-value codex)"
if [ -n "$config_env" ] && [ -n "$config_value" ]; then
  mkdir -p "$config_value"
  export "$config_env=$config_value"
fi
# shellcheck source=../../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
WORKER_PY="$(require_python "$REPO" "launch Codex worker")" || exit 1
export SUTANDO_PY="$WORKER_PY"

WORKING_DIR="${SUTANDO_CODEX_WORKING_DIR:-$REPO}"
WORKING_DIR="${WORKING_DIR/#\~/$HOME}"
mkdir -p "$WORKING_DIR"
WORKING_DIR="$(cd "$WORKING_DIR" && pwd -P)"

# Explicit empties override stale variables held by the tmux server.
ENV_ARGS=(-e SUTANDO_CORE_SESSION= -e SUTANDO_CORE_RUNTIME=codex
          -e SUTANDO_TASK_EVENT_HANDLER= -e SUTANDO_WORKER_RUNTIME=codex
          -e "SUTANDO_TMUX_SOCKET=$SOCKET" -e "SUTANDO_TMUX_SESSION=$SESSION"
          -e "PATH=$PATH" -e "SUTANDO_PY=$WORKER_PY"
          -e SUTANDO_CORE_ID= -e SUTANDO_CORE_POOL_SIZE= -e SUTANDO_WORKER_SEAT=)
for forwarded in SUTANDO_INSTANCE_ID SUTANDO_WORKER_ID SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR \
                 SUTANDO_RESULTS_DIR SUTANDO_INBOX_KIND SUTANDO_INBOX_RESOLVER \
                 SUTANDO_INBOX_RESOLVER_TIMEOUT SUTANDO_POOL_DELIVERY_SCRIPT \
                 SUTANDO_DEFAULT_WORKSPACE; do
  [ -n "${!forwarded:-}" ] && ENV_ARGS+=(-e "$forwarded=${!forwarded}")
done
[ -n "${CODEX_HOME:-}" ] && ENV_ARGS+=(-e "CODEX_HOME=$CODEX_HOME")
if [ -n "$config_env" ] && [ -n "$config_value" ]; then
  ENV_ARGS+=(-e "$config_env=$config_value")
fi
if [ "${SUTANDO_SELF_DEVELOPMENT_ENABLED+x}" = x ]; then
  ENV_ARGS+=(-e "SUTANDO_SELF_DEVELOPMENT_ENABLED=$SUTANDO_SELF_DEVELOPMENT_ENABLED")
fi
if [ "${SUTANDO_CODEX_AUTO_RESET_ENABLED+x}" = x ]; then
  ENV_ARGS+=(-e "SUTANDO_CODEX_AUTO_RESET_ENABLED=$SUTANDO_CODEX_AUTO_RESET_ENABLED")
fi

CHANNEL_ENV_NAMES="$("$WORKER_PY" "$REPO/src/channel_env_resolve.py" --launch-env-names)"
while IFS= read -r forwarded; do
  [ -n "$forwarded" ] || continue
  if [ "${1:-}" = "--print-env" ]; then
    ENV_ARGS+=(-e "$forwarded=<redacted>")
  else
    ENV_ARGS+=(-e "$forwarded=${!forwarded}")
  fi
done <<< "$CHANNEL_ENV_NAMES"

if [ "${1:-}" = "--print-env" ]; then
  printf '%s\n' "${ENV_ARGS[@]}"
  exit 0
fi
if [ "${1:-}" = "--restart" ] || [ "${1:-}" = "--force-restart" ]; then
  echo "launch-codex-worker-session: the pool owns worker recovery" >&2
  exit 2
fi
for dependency in codex tmux fswatch; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "launch-codex-worker-session: $dependency is not installed" >&2
    exit 127
  fi
done
if ! codex login status >/dev/null 2>&1; then
  echo "launch-codex-worker-session: Codex is not authenticated" >&2
  exit 1
fi
if tmux -S "$SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
  echo "launch-codex-worker-session: $SESSION already exists" >&2
  exit 2
fi

BOOT_PROMPT="You are Sutando worker $SUTANDO_INSTANCE_ID. The pool delivers tasks to your inbox. Do not run core startup, register schedules, start a watcher, or write core status. Handle only a delivered task prompt, read its named payload, and write the answer to its named result file. Reply Worker ready, then wait."
CODEX_ARGS=(-C "$WORKING_DIR" --add-dir "$HOME" --sandbox danger-full-access
            --ask-for-approval never --search --no-alt-screen
            # Headless like the core: an interactive startup update menu would block it.
            -c check_for_update_on_startup=false)
tmux -S "$SOCKET" new-session -d -s "$SESSION" "${ENV_ARGS[@]}" \
  codex "${CODEX_ARGS[@]}" "$BOOT_PROMPT"
for i in 1 2 3 4 5 6 7 8 9 10; do
  tmux -S "$SOCKET" has-session -t "=$SESSION" 2>/dev/null && break
  sleep 0.5
done
if ! tmux -S "$SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
  echo "launch-codex-worker-session: $SESSION exited during startup" >&2
  exit 1
fi

# The worker's observer, as the core's (start-cli.sh ensure_codex_observer): its tool calls become
# phase records for the task it is on, and its runtime record is written for this worker's seat.
# A fresh worker session always gets a fresh observer; it exits by itself once the session is gone.
# Fail-open, every branch: the worker is up before this runs and nothing here can exit the launcher.
if [ "${#SUTANDO_INSTANCE_ID}" != 32 ] || [ -n "${SUTANDO_INSTANCE_ID//[0-9a-f]/}" ]; then
  echo "launch-codex-worker-session: SUTANDO_INSTANCE_ID is not a 32-hex seat; no observer for $SESSION, phases stay lifecycle-only" >&2
elif ! node_bin="$(command -v node 2>/dev/null)"; then
  echo "launch-codex-worker-session: no node; no observer for $SESSION, phases stay lifecycle-only" >&2
else
  tmux -S "$SOCKET" kill-session -t "=${SESSION}-observer" 2>/dev/null || true
  tmux -S "$SOCKET" new-session -d -s "${SESSION}-observer" \
    "$node_bin" "$REPO/src/agent/codex/cli/codex-observer.mjs" --engine "$REPO" --tmux-socket "$SOCKET" \
    --session "$SESSION" --workspace "$SUTANDO_WORKSPACE_DIR" --seat "$SUTANDO_INSTANCE_ID" 2>/dev/null \
    || echo "launch-codex-worker-session: observer for $SESSION did not start; phases stay lifecycle-only" >&2
fi

if [ "$(uname -s)" = "Darwin" ]; then
  reset_timer="$REPO/skills/proactive-loop/scripts/codex-auto-reset-timer.py"
  if [ -f "$reset_timer" ] && ! "$WORKER_PY" "$reset_timer" ensure \
      --workspace "$SUTANDO_WORKSPACE_DIR" --codex-home "${CODEX_HOME:-$HOME/.codex}" >/dev/null; then
    echo "launch-codex-worker-session: could not reconcile the Codex earned-reset timer" >&2
  fi
fi

# The supervisor owns this worker's standby watcher and task notifier.
unset SUTANDO_TASK_EVENT_HANDLER SUTANDO_CORE_SESSION
export SUTANDO_WORKER_RUNTIME=codex SUTANDO_NOTIFIER_GRACE_PERIOD=0
bash "$REPO/skills/worker-pool/scripts/worker-watcher-supervisor.sh"
