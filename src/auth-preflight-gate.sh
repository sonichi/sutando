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
  # Serialise the read-modify-write, and use PER-INVOCATION scratch names.
  #
  # The first cut of this used fixed `$_pq.new` / `$_pq.tmp` siblings. Two gates
  # tripping for the same host at once clobber each other's scratch file: one
  # boot-abort record is lost and one writer can fail while the other succeeds
  # (reproduced in review at f4e17019 — rcs [1,0], reader saw 2 of 3). That
  # regresses the durability of the `>>` path it replaces, in exactly the record
  # that is meant to explain why startup aborted. Visibility is not worth losing
  # an entry for.
  #
  # Serialise the read-modify-write with a `mkdir` lock, and FAIL CLOSED if we
  # cannot get it. `mkdir` is the portable atomic test-and-set — `flock(1)` is
  # not on stock macOS.
  #
  # The first cut proceeded WITHOUT the lock after a bounded wait, and cleaned up
  # by unconditionally removing it. Review reproduced the result: 10 writers
  # against a pre-existing lock, every one returning 0, 2 of 10 records surviving,
  # and the foreign lock deleted. That is the same silent record-loss this whole
  # change exists to close, re-entered through the escape hatch — availability
  # bought with the durability that is the entire point. So:
  #   * only the acquirer ever touches the file, and
  #   * only the acquirer ever removes the lock.
  # On timeout we leave the file and the foreign lock untouched and say so on
  # stderr. Losing one duplicate record while a peer gate writes an equivalent
  # one beats corrupting the file for everybody.
  #
  # There is deliberately NO stale-lock reclamation, and that is the second half
  # of the same rule. This loop used to stat the lock and `rmdir` it once its
  # mtime passed 30s, to stop a killed gate wedging later boots. `rmdir` names a
  # PATH, not the directory you observed: if the owner releases and a new gate
  # acquires between the `stat` and the `rmdir`, the reclaimer deletes the
  # REPLACEMENT's lock and two writers enter the read-modify-write together —
  # the classic ABA, and it loses exactly the boot-abort record this block
  # exists to preserve. There is no identity to check either, since `mkdir`
  # gives the acquirer no token the observer can compare against. A wedged lock
  # costs one skipped record per boot and says so on stderr; the race costs a
  # silently truncated file. Prefer the loud, bounded failure.
  _lock="$_pq.lock"
  _have_lock=0
  _waited=0
  while [ "$_waited" -lt 100 ]; do
    if mkdir "$_lock" 2>/dev/null; then _have_lock=1; break; fi
    _waited=$((_waited + 1))
    sleep 0.1
  done

  if [ "$_have_lock" != "1" ]; then
    echo "  auth-preflight-gate: could not acquire $_lock after ~10s; leaving pending-questions.md untouched (a concurrent gate holds it and is recording an equivalent abort). If no gate is running, this lock is stale — remove it by hand: rmdir '$_lock'" >&2
  else
    _new="$_pq.new.$$"
    _tmp="$_pq.tmp.$$"
    {
      echo "## [$_ts] BOOT ABORTED — CLI login required ($_host)"
      echo "auth-preflight-gate stopped startup before services launched."
      echo "Remedy: $_remedy"
      echo ""
    } > "$_new"
    if [ -f "$_pq" ]; then
      if head -1 "$_pq" | grep -qE '^# [^ ]'; then
        { head -1 "$_pq"; echo ""; cat "$_new"; tail -n +2 "$_pq"; } > "$_tmp"
      else
        { cat "$_new"; cat "$_pq"; } > "$_tmp"
      fi
      mv -f "$_tmp" "$_pq"
    else
      mv -f "$_new" "$_pq"
    fi
    rm -f "$_new" "$_tmp"
    rmdir "$_lock" 2>/dev/null || true
  fi
  # --- end pending-question write (unique sentinel; tests extract to here) ---
  printf '[dm-only]\nSutando boot on %s ABORTED: CLI login required.\n%s\n' \
    "$_host" "$_remedy" > "$_ws/results/proactive-$(date +%s).txt"
fi

exit 2
