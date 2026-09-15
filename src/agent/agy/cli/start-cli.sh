#!/bin/bash
# src/agent/agy/cli/start-cli.sh — Slice 1 (sonichi#4272) scaffold: a
# standalone persistent tmux launcher for `agy` (Google's Antigravity CLI,
# Gemini-backed), a CANDIDATE third Sutando core runtime.
#
# NOT wired into core selection: src/agent/start-cli.sh (the dispatcher every
# other launch path goes through) does not dispatch to this runtime, and
# nothing reads tasks/*.txt into the session this starts. This script is only
# invokable directly. See src/agent/agy/README.md for exact scope.
#
# Usage:
#   bash src/agent/agy/cli/start-cli.sh           # start (or attach if running)
#   bash src/agent/agy/cli/start-cli.sh --check   # verify agy + auth, no launch
set -euo pipefail

# This script lives at src/agent/agy/cli/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

# Own socket/session names so a manual run of this scaffold can never collide
# with the real sutando-core session (claude or codex) on the same host.
TMUX_SOCKET="${SUTANDO_AGY_TMUX_SOCKET:-${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}}"
SESSION="${SUTANDO_AGY_TMUX_SESSION:-sutando-agy}"
ONBOARDING_PATH="${SUTANDO_AGY_ONBOARDING_PATH:-$HOME/.gemini/antigravity-cli/cache/onboarding.json}"

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

# `agy` exposes no dedicated auth-status subcommand (checked `agy --help` —
# see sutando#4272); `agy models` makes one real authenticated round trip and
# exits immediately, so it doubles as the lightest available auth probe.
check_mode() {
  if ! command -v agy >/dev/null 2>&1; then
    echo "agy: not found on PATH"
    return 127
  fi
  echo "agy: $(command -v agy)"
  echo "agy version: $(agy --version 2>/dev/null || echo unknown)"
  if agy models >/dev/null 2>&1; then
    echo "auth: OK (agy models round-trip succeeded)"
    return 0
  fi
  echo "auth: NOT authenticated (agy models failed) — run agy interactively once to log in"
  return 1
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
  if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
    echo "$SESSION already running — attaching (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "$SESSION already running."
  exit 0
fi

# Onboarding-skip pre-seed — see src/agent/agy/onboarding_seed.py for why.
# Non-fatal: a skipped seed means the first launch may hit the wizard once,
# not that the session can't start.
if [ -n "$PY" ]; then
  "$PY" "$REPO/src/agent/agy/onboarding_seed.py" "$ONBOARDING_PATH" \
    || echo "  ⚠ onboarding-seed failed (non-fatal): $ONBOARDING_PATH" >&2
else
  echo "  ⚠ no runnable python3 — onboarding-seed NOT applied; first launch may hit the onboarding wizard" >&2
fi

tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" agy --dangerously-skip-permissions

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

if [ -t 1 ] && [ -z "${TMUX:-}" ]; then
  exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
fi
echo "Started $SESSION detached. Attach via:"
echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
