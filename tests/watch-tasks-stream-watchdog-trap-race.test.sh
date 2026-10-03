#!/usr/bin/env bash
# wait_bounded()'s watchdog subshell (src/bounded-wait.sh, the bound under
# run_handler_now and resolve_inbox_entry) registers a TERM trap referencing
# `$_s` before any `_s=$!` is assigned. Under `set -u` (which the watcher runs
# with), a TERM landing in that window used to make the trap's own `$_s`
# expansion an unbound-variable error, killing the watchdog subshell uncleanly
# instead of cancelling its sleep.
#
# The window is reached deterministically here: a zero-tick bound takes the
# watchdog straight past its loop into the foreground `sleep 1` before KILL,
# with `_s` never assigned. The harness waits for the timeout flag (the
# watchdog is in that sleep) and only then sends TERM, which bash defers to
# the end of the sleep and then runs the trap -- the shipped code never TERMs
# a watchdog that may not have run yet (#5081).
#
# This test extracts the REAL watchdog subshell verbatim from the shipped
# file (never hand-copied, so it can't drift from what actually ships).
#
# Run: bash tests/watch-tasks-stream-watchdog-trap-race.test.sh
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
N="${WATCHDOG_TRAP_RACE_ITERATIONS:-3}"
fails=0
errs=0
runfails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s -- %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

SNIPPET="$(sed -n '/^  ( trap .* TERM$/,/^  watchdog_pid=\$!$/p' "$REPO/src/bounded-wait.sh")"
if [ -z "$SNIPPET" ]; then
  bad "extracted the watchdog subshell from src/bounded-wait.sh" "not found -- has it moved or been renamed?"
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

for _ in $(seq 1 "$N"); do
  out="$(
    bash -c '
      set -u
      sleep 30 &
      pid=$!
      # wait_bounded sets these before the extracted range starts; the harness
      # sets them the same way, out of range. ticks=0: no loop, no `_s=$!`.
      ticks=0
      timeout_flag="$(mktemp -u "${TMPDIR:-/tmp}/wdrace-timeout.XXXXXX")"
      done_flag="$(mktemp -u "${TMPDIR:-/tmp}/wdrace-done.XXXXXX")"
      '"$SNIPPET"'
      for _ in $(seq 1 100); do [ -e "$timeout_flag" ] && break; sleep 0.05; done
      [ -e "$timeout_flag" ] || { echo "watchdog never flagged the bound" >&2; exit 99; }
      kill -TERM "$watchdog_pid" 2>/dev/null
      wait "$watchdog_pid" 2>/dev/null
      rc=$?
      wait "$pid" 2>/dev/null
      rm -f "$timeout_flag"
      exit $rc
    ' 2>&1
  )"
  rc=$?
  if [ -n "$out" ]; then
    errs=$((errs + 1))
    printf '%s\n' "$out" >> "$STDERR_LOG"
  fi
  # 0 = the trap ran and exited cleanly. Anything else is the unbound-variable
  # crash (rc 1) or a signal-death shape the trap should have prevented.
  case "$rc" in
    0) ;;
    *) runfails=$((runfails + 1)) ;;
  esac
done

if [ "$errs" -eq 0 ]; then
  ok "$N/$N runs produced no stderr (no unbound-variable crash)"
else
  bad "$N/$N runs produced no stderr" \
    "$errs run(s) wrote to stderr -- first: $(head -1 "$STDERR_LOG")"
fi

if [ "$runfails" -eq 0 ]; then
  ok "$N/$N runs exited cleanly (0) with _s unset at trap time"
else
  bad "$N/$N runs exited cleanly (0) with _s unset at trap time" "$runfails run(s) did not"
fi

echo "watchdog trap race ($N iterations):"
if [ "$fails" -eq 0 ] && [ "$errs" -eq 0 ]; then
  echo "  ALL PASS"
  exit 0
else
  echo "  $((fails + errs)) FAILURE(S)"
  exit 1
fi
