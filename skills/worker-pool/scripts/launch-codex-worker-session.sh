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

for fallback in "$REPO/../bin" "$REPO/../runtime/node/bin" \
                "$HOME/.npm-global/bin" "$HOME/.local/bin"; do
  [ -d "$fallback" ] || continue
  case ":$PATH:" in
    *":$fallback:"*) ;;
    *) PATH="$PATH:$fallback" ;;
  esac
done
export PATH

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
          -e "PATH=$PATH" -e "SUTANDO_PY=$WORKER_PY")
for forwarded in SUTANDO_INSTANCE_ID SUTANDO_TASKS_DIR SUTANDO_WORKSPACE_DIR \
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
            --ask-for-approval never --search --no-alt-screen)
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

# The supervisor owns this worker's standby watcher and task notifier.
unset SUTANDO_TASK_EVENT_HANDLER SUTANDO_CORE_SESSION
export SUTANDO_WORKER_RUNTIME=codex SUTANDO_NOTIFIER_GRACE_PERIOD=0
bash "$REPO/skills/worker-pool/scripts/worker-watcher-supervisor.sh"
