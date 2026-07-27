#!/bin/bash
# Persistent Codex CLI implementation of the Sutando core.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
WATCHER_SESSION="${SESSION}-watcher"
export SUTANDO_CORE_SESSION=1
export SUTANDO_CORE_RUNTIME=codex

tmux_available() { command -v tmux >/dev/null 2>&1; }
session_exists() { tmux_available && tmux -S "$TMUX_SOCKET" has-session -t "=$1" 2>/dev/null; }

session_runtime() {
  tmux -S "$TMUX_SOCKET" show-environment -t "=$SESSION" SUTANDO_CORE_RUNTIME 2>/dev/null \
    | sed -n 's/^SUTANDO_CORE_RUNTIME=//p' || true
}

if ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI is not installed. Install it, run 'codex login', then retry." >&2
  exit 127
fi

# Use the configured CODEX_HOME (or another env name chosen by the operator).
# The default points at ~/.codex to reuse the existing authenticated install.
config_env="$(bash "$REPO/scripts/sutando-config.sh" core-config-dir-env-name codex)"
config_value="$(bash "$REPO/scripts/sutando-config.sh" core-config-dir-value codex)"
if [ -n "$config_env" ] && [ -n "$config_value" ]; then
  mkdir -p "$config_value"
  export "$config_env=$config_value"
  echo "  ✓ $config_env=$config_value"
fi

if ! codex login status >/dev/null 2>&1; then
  echo "Codex CLI is not authenticated for ${CODEX_HOME:-~/.codex}. Run 'codex login' and retry." >&2
  exit 1
fi

# The Codex adapter depends on tmux for a persistent pane and fswatch for
# file-bridge wakeups. Match startup.sh's first-run behavior when this launcher
# is invoked directly.
for dependency in tmux fswatch; do
  if ! command -v "$dependency" >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    echo "$dependency not found — installing via Homebrew..."
    brew install "$dependency" 2>&1 | tail -3
  fi
done

WORKING_DIR="${SUTANDO_CODEX_WORKING_DIR:-${SUTANDO_CORE_WORKING_DIR:-$REPO}}"
WORKING_DIR="${WORKING_DIR/#\~/$HOME}"
mkdir -p "$WORKING_DIR"
WORKING_DIR="$(cd "$WORKING_DIR" && pwd -P)"

CORE_ENV_ARGS=(-e SUTANDO_CORE_SESSION=1 -e SUTANDO_CORE_RUNTIME=codex)
[ -n "${SUTANDO_DEFAULT_WORKSPACE:-}" ] && CORE_ENV_ARGS+=(-e "SUTANDO_DEFAULT_WORKSPACE=$SUTANDO_DEFAULT_WORKSPACE")
[ -n "${CODEX_HOME:-}" ] && CORE_ENV_ARGS+=(-e "CODEX_HOME=$CODEX_HOME")

CODEX_ARGS=(
  -C "$WORKING_DIR"
  --add-dir "$HOME"
  --sandbox danger-full-access
  --ask-for-approval never
  --search
  --no-alt-screen
)
if [ -n "${SUTANDO_CORE_MODEL:-}" ]; then
  CODEX_ARGS+=(-m "$SUTANDO_CORE_MODEL")
fi

apply_tmux_defaults() {
  tmux_available || return 0
  tmux -S "$TMUX_SOCKET" start-server 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" set-option -g mouse on 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" bind -n WheelUpPane if-shell -F -t = '#{mouse_any_flag}' 'send-keys -M' 'copy-mode -e; send-keys -M' 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" bind -n WheelDownPane send-keys -M 2>/dev/null || true
}

ensure_task_notifier() {
  session_exists "$WATCHER_SESSION" && return 0
  NOTIFIER_ENV_ARGS=(-e "SUTANDO_TMUX_SOCKET=$TMUX_SOCKET" -e "SUTANDO_TMUX_SESSION=$SESSION")
  [ -n "${SUTANDO_TASKS_DIR:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TASKS_DIR=$SUTANDO_TASKS_DIR")
  [ -n "${SUTANDO_RESULTS_DIR:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_RESULTS_DIR=$SUTANDO_RESULTS_DIR")
  tmux -S "$TMUX_SOCKET" new-session -d -s "$WATCHER_SESSION" \
    "${NOTIFIER_ENV_ARGS[@]}" bash "$REPO/src/agent/codex/cli/task-notifier.sh"
}

# Keep the same core-supervisor signal available for both runtimes. The
# monitor's liveness derivation is tmux/session based; its prompt classifier is
# best-effort and safely falls back to the generic running/idle/hung states for
# Codex panes it does not recognize.
ensure_core_monitor() {
  local ws mon_out
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mon_out="$ws/state/core-supervisor.json"
  if pgrep -f "core-input-watch\.py .*--socket ${TMUX_SOCKET} .*--out ${mon_out}" >/dev/null 2>&1; then
    return 0
  fi
  python3 "$REPO/src/core-input-watch.py" \
    --socket "$TMUX_SOCKET" --session "$SESSION" --out "$mon_out" \
    >/tmp/core-input-watch.log 2>&1 &
}

ensure_codex_scheduler() {
  local ws host scheduler
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  host="${SUTANDO_HOST_LABEL:-}"
  if [ -z "$host" ]; then
    host="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null)" || return 0
  fi
  scheduler="${SUTANDO_CODEX_SCHEDULER_SCRIPT:-$REPO/skills/schedule-crons/scripts/codex-scheduler.py}"
  if ! python3 "$scheduler" install --workspace "$ws" --host-label "$host" >/dev/null; then
    echo "  ⚠ Could not reconcile the durable Codex scheduler; run: python3 $scheduler install" >&2
  fi
}

# Codex has no session CronCreate surface. Reconcile the OS-backed scheduler
# on every launcher invocation so the canonical five-minute main loop is owned
# before the core starts (or while attaching to an existing core).
ensure_codex_scheduler

if [ "${1:-}" = "--restart" ]; then
  tmux_available && tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
  tmux_available && tmux -S "$TMUX_SOCKET" kill-session -t "=$SESSION" 2>/dev/null || true
elif session_exists "$SESSION" && [ "$(session_runtime)" != "codex" ]; then
  # Sessions created before runtime markers existed are Claude sessions. Never
  # attach a selected Codex launcher to an unknown/foreign canonical session.
  echo "Replacing unmarked or non-Codex $SESSION session."
  tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" kill-session -t "=$SESSION" 2>/dev/null || true
fi

if session_exists "$SESSION"; then
  apply_tmux_defaults
  ensure_task_notifier
  ensure_core_monitor
  if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
    echo "Attaching to existing $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "$SESSION already running (codex)."
  exit 0
fi

if ! tmux_available; then
  echo "  ⚠ tmux not found — Codex will run, but file-bridge task wakeups are unavailable" >&2
  exec codex "${CODEX_ARGS[@]}"
fi

if ! command -v fswatch >/dev/null 2>&1; then
  echo "fswatch not found — install it to enable Sutando task wakeups (brew install fswatch)." >&2
  exit 127
fi

apply_tmux_defaults
if ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" && [ -n "$ws" ]; then
  mkdir -p "$ws/state"
  printf '{"runtime":"codex","session":"%s","started_at":%s}\n' "$SESSION" "$(date +%s)" > "$ws/state/core-runtime.json"
  printf '{"host":"%s","session_started_at":%s,"iso":"%s","source":"start-cli","runtime":"codex"}\n' \
    "$(hostname | sed 's/\..*//')" "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >> "$ws/state/session-starts.log"
fi

if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" "${CORE_ENV_ARGS[@]}" codex "${CODEX_ARGS[@]}"
  ensure_task_notifier
  ensure_core_monitor
  exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
else
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" "${CORE_ENV_ARGS[@]}" codex "${CODEX_ARGS[@]}"
  ensure_task_notifier
  ensure_core_monitor
  echo "Started $SESSION detached with Codex. Attach via:"
  echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
fi
