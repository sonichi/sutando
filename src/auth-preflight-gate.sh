#!/bin/bash
# auth-preflight-gate.sh — boot gate for the logged-out-CLI class (#2396).
#
# Runs the auth_preflight static probe (src/auth_preflight.py, #2405) against
# the given CLAUDE_CONFIG_DIR and, on a login-required verdict, fails LOUD on
# three channels before any service starts:
#   1. stderr — the exact remedy (visible in tmux/console/startup log)
#   2. macOS notification (works even when every bridge is down)
#   3. per-host pending-questions.md + a results/proactive-*.txt file so the
#      first bridge that comes up DMs the remedy to the owner
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
  _ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$_ws/hosts/$_host" "$_ws/results"
  # Insert at the TOP of the active region, never `>>` at EOF.
  #
  # `check-pending-questions.py` (and morning-briefing, agent-api,
  # friction-detector, dashboard) count only the text ABOVE the file's
  # top-level `# Resolved` divider — everything after it is the audit trail.
  # An EOF append therefore lands BELOW the divider and is permanently
  # uncounted. Measured on this host's real file (2099 lines, divider at 1652):
  #
  #     baseline                            21 waiting
  #     after this block appended with `>>`  21   <- INVISIBLE
  #     same text placed above the divider   22   <- counted
  #
  # It reports success in every cheap way: bytes land, the path is right,
  # nothing errors, `wc -c` grows. Only calling the reader shows the zero.
  # And this is the worst case to lose: the gate writes precisely when a boot
  # was ABORTED, so the record of why is dropped at the moment it matters.
  #
  # Top-of-file rather than "just above the divider" deliberately: it needs no
  # divider regex at all, so it cannot be defeated by the divider-detection
  # edge cases #2419 catalogues (a quoted `# Resolved` in a comment, a fenced
  # block, an inline code span). A boot-abort question also belongs first.
  _pq="$_ws/hosts/$_host/pending-questions.md"
  {
    echo "## [$_ts] BOOT ABORTED — CLI login required ($_host)"
    echo "auth-preflight-gate stopped startup before services launched."
    echo "Remedy: $_remedy"
    echo ""
  } > "$_pq.new"
  if [ -f "$_pq" ]; then
    # Keep a leading `# ` H1 as the first line if the file has one, so the
    # insert goes into the active region rather than above the title.
    if head -1 "$_pq" | grep -qE '^# [^ ]'; then
      { head -1 "$_pq"; echo ""; cat "$_pq.new"; tail -n +2 "$_pq"; } > "$_pq.tmp"
    else
      { cat "$_pq.new"; cat "$_pq"; } > "$_pq.tmp"
    fi
    mv "$_pq.tmp" "$_pq"
  else
    mv "$_pq.new" "$_pq"
  fi
  rm -f "$_pq.new"
  printf '[dm-only]\nSutando boot on %s ABORTED: CLI login required.\n%s\n' \
    "$_host" "$_remedy" > "$_ws/results/proactive-$(date +%s).txt"
fi

exit 2
