#!/usr/bin/env bash
# The bound under run_handler_now and resolve_inbox_entry (src/bounded-wait.sh)
# used to fork a watchdog subshell and signal it. A TERM that reached that
# subshell before bash had reset its inherited traps was dropped (the watcher
# sat out the whole bound) or ran the PARENT's trap in the child (the watcher's
# own cleanup killed the watcher mid-handler, #5081); a trap referencing an
# unassigned variable under `set -u` crashed it outright.
#
# run_bounded forks no watchdog and sets no trap: the only background job is
# the command itself, and the only signals go to it, by jobspec. This pins
# that shape, then runs the primitive the way the watcher does -- `set -u`,
# a TERM trap armed on the parent -- and shows the parent's trap never runs.
#
# Run: bash tests/watch-tasks-stream-watchdog-trap-race.test.sh
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
N="${WATCHDOG_TRAP_RACE_ITERATIONS:-3}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s -- %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

SRC="$REPO/src/bounded-wait.sh"
traps="$(grep -c '^[^#]*\btrap\b' "$SRC")"
if [ "$traps" = "0" ]; then ok "the shipped primitive sets no trap (nothing for a signal to race)"
else bad "the shipped primitive sets no trap" "$traps trap line(s)"; fi

bg="$(grep -c ') &$' "$SRC")"
if [ "$bg" = "1" ]; then ok "one background job in the shipped primitive: the command itself"
else bad "one background job in the shipped primitive" "$bg lines end a background job"; fi

pidkills="$(grep -c '^[^#]*kill -[A-Z]*[A-Z] "\$' "$SRC")"
if [ "$pidkills" = "0" ]; then ok "no signal in the shipped primitive names a pid (all by jobspec)"
else bad "no signal in the shipped primitive names a pid" "$pidkills pid kill site(s)"; fi

# The watcher's shape: set -u, TERM trapped on the parent with a cleanup that
# would be fatal if it ran in a forked copy. Each run bounds a short child,
# then a child that must be TERMed; the trap text must never execute.
runfails=0; errs=0; first_err=""
for _ in $(seq 1 "$N"); do
  out="$(
    bash -c '
      set -u
      . "$1"
      trap "echo PARENT-TRAP-RAN >&2; exit 90" TERM
      run_bounded 5 -- sh -c "exit 3"; a=$?
      run_bounded 1 -- sleep 30; b=$?
      [ "$a" = 3 ] && [ "$b" = 143 ] && exit 0
      echo "statuses a=$a b=$b" >&2; exit 1
    ' _ "$SRC" 2>&1
  )"
  rc=$?
  if [ -n "$out" ]; then errs=$((errs + 1)); [ -n "$first_err" ] || first_err="$out"; fi
  [ "$rc" = "0" ] || runfails=$((runfails + 1))
done

if [ "$errs" -eq 0 ]; then ok "$N/$N runs wrote nothing to stderr (no trap ran, no crash)"
else bad "$N/$N runs wrote nothing to stderr" "$errs run(s) did -- first: $first_err"; fi

if [ "$runfails" -eq 0 ]; then ok "$N/$N runs returned each child's own status under set -u with TERM trapped"
else bad "$N/$N runs returned each child's own status" "$runfails run(s) did not"; fi

echo "watchdog trap race ($N iterations):"
if [ "$fails" -eq 0 ]; then
  echo "  ALL PASS"
  exit 0
else
  echo "  $fails FAILURE(S)"
  exit 1
fi
