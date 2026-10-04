#!/bin/bash
# auth-preflight-gate.sh — boot gate for the logged-out-CLI class (#2396).
#
# Runs the auth_preflight static probe (src/auth_preflight.py, #2405) against
# the given CLAUDE_CONFIG_DIR and, on a login-required verdict, fails LOUD on
# three channels before any service starts:
#   1. stderr — the exact remedy (visible in tmux/console/startup log)
#   2. macOS notification (works even when every bridge is down)
#   3. a pending question for the owner (scripts/ask-owner.py: the room database,
#      else the workspace outbox) whose queued DM the first bridge up delivers
# then exits 2 so the caller (startup.sh) aborts BEFORE launching services —
# a half-up core (tmux + bridges alive, CLI parked at /login, processing
# nothing) is strictly worse than a clean loud abort (2026-07-30 outage).
#
# Exit codes: 0 = authenticated / gate skipped, 2 = login required (abort).
# Skips fail-open when the probe module is absent (pre-#2405 install) or
# SUTANDO_SKIP_AUTH_PREFLIGHT=1 (operator escape hatch).
#
# Usage: bash src/auth-preflight-gate.sh "$CLAUDE_CONFIG_DIR"

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${1:?usage: auth-preflight-gate.sh <claude-config-dir>}"

if [ "${SUTANDO_SKIP_AUTH_PREFLIGHT:-0}" = "1" ]; then
  echo "auth-preflight-gate: skipped (SUTANDO_SKIP_AUTH_PREFLIGHT=1)"
  exit 0
fi

PROBE="$REPO/src/auth_preflight.py"
if [ ! -f "$PROBE" ]; then
  echo "auth-preflight-gate: probe module missing ($PROBE) — skipping (pre-#2405 install)" >&2
  exit 0
fi

if [ -n "${SSH_CONNECTION:-}" ]; then
  echo "auth-preflight-gate: SSH session detected — if login is needed, the" >&2
  echo "  keychain is likely locked and /login WILL stall here. Prefer a GUI Terminal." >&2
fi

_out="$(python3 "$PROBE" --config-dir "$CONFIG_DIR" --json 2>&1)"
_rc=$?
if [ "$_rc" -eq 0 ]; then
  echo "auth-preflight-gate: OK — $CONFIG_DIR can boot authenticated"
  exit 0
fi

# login_required (or probe error): extract the remedy; fall back to raw output.
_remedy="$(printf '%s' "$_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("remedy") or "")
except Exception:
    pass' 2>/dev/null)"
[ -n "$_remedy" ] || _remedy="$_out"

echo "" >&2
echo "✗ auth-preflight-gate: CLI login required for $CONFIG_DIR — ABORTING startup" >&2
echo "  before services launch (half-up core is worse than a loud stop; #2396)." >&2
echo "  Remedy: $_remedy" >&2
echo "" >&2

osascript -e "display notification \"CLI login required — startup aborted. $( printf '%s' "$_remedy" | head -c 120 )\" with title \"Sutando\"" 2>/dev/null

_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
_host="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null)"
if [ -n "$_ws" ] && [ -n "$_host" ]; then
  # ask-owner records the question (room database, else the workspace outbox) and
  # queues the owner's DM itself; it exits 0 after any failure and prints why.
  python3 "$REPO/scripts/ask-owner.py" "BOOT ABORTED — CLI login required ($_host)" \
    --context "auth-preflight-gate stopped startup before services launched. Remedy: $_remedy" \
    --urgency durable --workspace "$_ws" \
    || echo "  auth-preflight-gate: the pending question could not be recorded (reason above)." >&2
fi

exit 2
