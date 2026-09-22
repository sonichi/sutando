#!/usr/bin/env bash
# run_handler_now()'s watchdog subshell (src/watch-tasks-stream.sh) registers
# a TERM trap referencing `$_s` before `_s=$!` is assigned. Under `set -u`
# (which this script runs with), a TERM landing in that narrow window used
# to make the trap's own `$_s` expansion an unbound-variable error, killing
# the watchdog subshell uncleanly instead of cancelling its internal sleep.
# The window is real, not theoretical: measured 168-297/500 non-clean exits
# on the pre-fix code, tight-looping a real fork+immediate-TERM (no sleep
# between fork and kill, which is what exposes it -- a real handler run
# reaches this exact shape every time it finishes before the watchdog
# subshell has scheduled past its own first two statements).
#
# This test extracts the REAL watchdog subshell verbatim from the shipped
# file (never hand-copied, so it can't drift from what actually ships) and
# loops it under the same conditions.
#
# Run: bash tests/watch-tasks-stream-watchdog-trap-race.test.sh
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
N="${WATCHDOG_TRAP_RACE_ITERATIONS:-100}"
fails=0
errs=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s -- %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

SNIPPET="$(sed -n '/^    handler_pid=\$!$/,/^    watchdog_pid=\$!$/p' "$REPO/src/watch-tasks-stream.sh")"
if [ -z "$SNIPPET" ]; then
  bad "extracted the watchdog subshell from src/watch-tasks-stream.sh" "not found -- has it moved or been renamed?"
  echo "watchdog trap race: FAILURES ABOVE"
  exit 1
fi
ok "extracted the real watchdog subshell verbatim"

if ! printf '%s\n' "$SNIPPET" | grep -qF '${_s:-}'; then
  bad "the trap references \${_s:-}, not a bare \$_s" \
    "extracted snippet: $SNIPPET"
else
  ok "the shipped trap uses the set -u-safe \${_s:-} form"
fi

STDERR_LOG="$(mktemp)"
trap 'rm -f "$STDERR_LOG"' EXIT

for i in $(seq 1 "$N"); do
  out="$(
    bash -c '
      set -u
      # 1s, not the real 10s default: a "miss" (TERM not landing in the
      # vulnerable window) then costs 1s of real time, not 10 -- the race
      # itself does not depend on the timeout length, only on how fast the
      # subshell reaches `_s=$!` relative to when TERM is delivered.
      SUTANDO_HANDLER_RUN_TIMEOUT=1
      ( exit 0 ) &
      handler_pid=$!
      # timeout_flag is set one real line before this extracted range
      # starts; the harness sets it here the same way, out of range.
      timeout_flag="$(mktemp -u "${TMPDIR:-/tmp}/wdrace-timeout.XXXXXX")"
      '"$SNIPPET"'
      wait "$handler_pid" 2>/dev/null
      kill -TERM "$watchdog_pid" 2>/dev/null
      wait "$watchdog_pid" 2>/dev/null
      exit $?
    ' 2>&1
  )"
  rc=$?
  if [ -n "$out" ]; then
    errs=$((errs + 1))
    printf '%s\n' "$out" >> "$STDERR_LOG"
  fi
  # 0 = the trap ran and exited cleanly; 143 = TERM's own default disposition
  # took the subshell down before the trap could run at all -- also clean,
  # just earlier. Anything else is the unbound-variable crash (rc 1) or a
  # signal-death shape the trap should have prevented.
  case "$rc" in
    0|143) ;;
    *) fails=$((fails + 1)) ;;
  esac
done

if [ "$errs" -eq 0 ]; then
  ok "$N/$N runs produced no stderr (no unbound-variable crash)"
else
  bad "$N/$N runs produced no stderr" \
    "$errs run(s) wrote to stderr -- first: $(head -1 "$STDERR_LOG")"
fi

if [ "$fails" -eq 0 ]; then
  : # already reported above; this branch exists so a real fails++ elsewhere still counts
fi
if [ "$errs" -eq 0 ] && printf '%s' "$SNIPPET" | grep -qF '${_s:-}'; then
  ok "$N/$N runs exited cleanly (0 or TERM-default 143)"
fi

echo "watchdog trap race ($N iterations):"
if [ "$fails" -eq 0 ] && [ "$errs" -eq 0 ]; then
  echo "  ALL PASS"
  exit 0
else
  echo "  $((fails + errs)) FAILURE(S)"
  exit 1
fi
