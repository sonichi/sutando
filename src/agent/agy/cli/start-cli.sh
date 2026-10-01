#!/bin/bash
# Standalone persistent tmux launcher for `agy` (Google's Antigravity CLI);
# not wired into core selection — see src/agent/agy/README.md for scope.
set -euo pipefail

# This script lives at src/agent/agy/cli/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

# Own socket/session names so a manual run of this scaffold can never collide
# with the real sutando-core session (claude or codex) on the same host.
TMUX_SOCKET="${SUTANDO_AGY_TMUX_SOCKET:-${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}}"
SESSION="${SUTANDO_AGY_TMUX_SESSION:-sutando-agy}"
WATCHER_SESSION="${SESSION}-watcher"
ONBOARDING_PATH="${SUTANDO_AGY_ONBOARDING_PATH:-$HOME/.gemini/antigravity-cli/cache/onboarding.json}"
NOTIFIER="${SUTANDO_AGY_NOTIFIER_SCRIPT:-$REPO/src/agent/agy/cli/task-notifier.sh}"

PY=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  # shellcheck source=scripts/python-binary.sh
  . "$REPO/scripts/python-binary.sh"
  PY="$(resolve_python "$REPO")"
fi

usage() {
  cat <<'EOF'
Usage: start-cli.sh [--check]

  (no args)  Start (or attach to) the persistent sutando-agy tmux session
             running `agy --dangerously-skip-permissions`.
  --check    Verify the agy CLI is on PATH and report auth status. Makes no
             launch and no filesystem changes.
EOF
}

tmux_available() { command -v tmux >/dev/null 2>&1; }
session_exists() { tmux_available && tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; }
watcher_session_exists() { tmux_available && tmux -S "$TMUX_SOCKET" has-session -t "=$WATCHER_SESSION" 2>/dev/null; }

# Starts task-notifier.sh in its own tmux session, once per core session.
# fresh_core=1 recycles a surviving-but-stale watcher; its initial sweep re-arms durable pending work.
ensure_task_notifier() {
  local fresh_core="${1:-0}"
  if watcher_session_exists; then
    [ "$fresh_core" = 1 ] || return 0
    tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
    for _ in $(seq 1 25); do
      watcher_session_exists || break
      sleep 0.2
    done
    if watcher_session_exists; then
      echo "  ⚠ stale agy task notifier would not terminate — leaving it running, tasks may lag" >&2
      return 0
    fi
  fi
  [ -x "$NOTIFIER" ] || { echo "  ⚠ agy task notifier not found/executable: $NOTIFIER — tasks will not reach this session" >&2; return 0; }
  # task-notifier.sh hard-requires fswatch; without it the pane process dies
  # within ~1s, so a tmux new-session that "succeeds" leaves nothing alive.
  if ! command -v fswatch >/dev/null 2>&1; then
    echo "  ⚠ fswatch not found — required by the agy task notifier (brew install fswatch); tasks will not reach this session" >&2
    return 0
  fi
  # Bind every queue-related var explicitly, never omit -e: the tmux server's
  # global env can carry a foreign value that only an explicit -e overrides.
  NOTIFIER_ENV_ARGS=(-e "SUTANDO_AGY_TMUX_SOCKET=$TMUX_SOCKET" -e "SUTANDO_AGY_TMUX_SESSION=$SESSION" -e "SUTANDO_INSTANCE_ID=agy-task-notifier")
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TASKS_DIR=${SUTANDO_TASKS_DIR:-}")
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_RESULTS_DIR=${SUTANDO_RESULTS_DIR:-}")
  if ! tmux -S "$TMUX_SOCKET" new-session -d -s "$WATCHER_SESSION" \
      "${NOTIFIER_ENV_ARGS[@]}" bash "$NOTIFIER"; then
    echo "  ⚠ could not start the agy task notifier — tasks will not reach this session" >&2
    return 0
  fi
  # new-session rc=0 only means tmux accepted it; poll rather than trust a
  # session that can still die on its first tick (e.g. a notifier crash).
  for _ in $(seq 1 10); do
    watcher_session_exists || break
    sleep 0.2
  done
  watcher_session_exists \
    || echo "  ⚠ agy task notifier exited immediately after starting — tasks will not reach this session" >&2
}

attach_or_report_existing() {
  if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
    echo "$SESSION already running — attaching (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "$SESSION already running."
  exit 0
}

# `agy` exposes no dedicated auth-status subcommand; `agy models` makes one
# authenticated round trip and doubles as the lightest available probe.
check_mode() {
  if ! command -v agy >/dev/null 2>&1; then
    echo "agy: not found on PATH"
    return 127
  fi
  echo "agy: $(command -v agy)"
  echo "agy version: $(agy --version 2>/dev/null || echo unknown)"
  local _agy_rc=0
  agy models >/dev/null 2>&1 || _agy_rc=$?
  if [ "$_agy_rc" -eq 0 ]; then
    echo "auth: OK (agy models round-trip succeeded)"
    return 0
  fi
  if [ "$_agy_rc" -eq 126 ] || [ "$_agy_rc" -eq 127 ]; then
    echo "auth: UNKNOWN (agy models could not execute, exit $_agy_rc)"
    return "$_agy_rc"
  fi
  # No documented auth-specific exit code exists for agy models, so a
  # generic nonzero here is not proof of an unauthenticated session.
  echo "auth: UNKNOWN — authenticated round trip: FAILED (agy models exited $_agy_rc)"
  return "$_agy_rc"
}

case "${1:-}" in
  --check)
    check_mode
    exit $?
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "start-cli.sh: unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac

if ! command -v agy >/dev/null 2>&1; then
  echo "agy CLI not found on PATH. Install it and retry." >&2
  exit 127
fi

if ! tmux_available; then
  echo "tmux not found — required for the persistent agy session." >&2
  exit 127
fi

# Idempotency guard: a second invocation attaches (or reports) instead of
# starting a duplicate session.
if session_exists; then
  ensure_task_notifier
  attach_or_report_existing
fi

# Onboarding-skip pre-seed (see onboarding_seed.py); non-fatal — a skipped
# seed means the wizard may show once, not that the session can't start.
if [ -n "$PY" ]; then
  "$PY" "$REPO/src/agent/agy/onboarding_seed.py" "$ONBOARDING_PATH" \
    || echo "  ⚠ onboarding-seed failed (non-fatal): $ONBOARDING_PATH" >&2
else
  echo "  ⚠ no runnable python3 — onboarding-seed NOT applied; first launch may hit the onboarding wizard" >&2
fi

# tmux serializes session creation; a nonzero rc here can be a real failure
# or a peer that won the race above — recheck before treating it as ours.
if ! tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" agy --dangerously-skip-permissions; then
  if session_exists; then
    ensure_task_notifier
    attach_or_report_existing
  fi
  echo "  ⚠ failed to start $SESSION." >&2
  exit 1
fi

# new-session rc=0 only means tmux accepted it; poll rather than assume a
# session whose command exited immediately is actually up.
for _ in $(seq 1 25); do
  session_exists && break
  sleep 0.2
done

if ! session_exists; then
  echo "  ⚠ $SESSION did not come up within ~5s." >&2
  exit 1
fi

ensure_task_notifier 1

if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
  exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
fi
echo "Started $SESSION detached. Attach via:"
echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
