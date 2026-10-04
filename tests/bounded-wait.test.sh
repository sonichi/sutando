#!/usr/bin/env bash
# run_bounded (src/bounded-wait.sh): the one bound under the watcher's inline
# resolver and handler runs. The parent polls bash's own job table and signals by
# jobspec; it never reads a pid, never holds a pipe, never touches an fd. Cases
# 5-6 are the pid-reuse shape (a KILL chosen while the child lived, executed after
# its reap), cases 8-11 the completion-proxy shapes: fast exits read as timeouts,
# a target that closes an inherited fd, the caller's fd 9 rebound, `08` rejected.
set -uo pipefail
REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=../src/bounded-wait.sh
. "$REPO/src/bounded-wait.sh"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
# A private TMPDIR: the leftover scan below must not see a sibling suite's files.
TMPDIR="$(mktemp -d)"; export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT
FLAG="$TMPDIR/bounded-wait-flag"
# Fast-exit repetitions per status; 2000 reproduces the shipped-3.2 misclassification rate.
N="${BOUNDED_WAIT_FAST_EXITS:-300}"

# 1. A child that finishes inside the bound: its own status, no flag.
run_bounded 5 "$FLAG" -- sh -c 'exit 7'; rc=$?
[ "$rc" = "7" ] && [ ! -e "$FLAG" ]
check $? "a child inside the bound returns its own status (rc=$rc) and raises no flag"

# 2. A child past the bound is TERMed, and the flag says the bound fired.
start=$(date +%s); run_bounded 1 "$FLAG" -- sleep 30; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "143" ] && [ -e "$FLAG" ] && [ "$elapsed" -lt 10 ]
check $? "a child past the bound is TERMed (rc=$rc, ${elapsed}s) and the flag is raised"
rm -f "$FLAG"

# 3. A TERM-resistant child still dies: KILL follows TERM.
start=$(date +%s); run_bounded 1 "$FLAG" -- bash -c 'trap "" TERM; exec sleep 30'; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "137" ] && [ -e "$FLAG" ] && [ "$elapsed" -lt 10 ]
check $? "a TERM-resistant child is KILLed (rc=$rc, ${elapsed}s)"
rm -f "$FLAG"

# 4. A finished child returns at once against a long bound, and without a
#    flag path nothing is written for it.
start=$(date +%s); run_bounded 3 -- sh -c 'exit 0'; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "0" ] && [ ! -e "$FLAG" ] && [ "$elapsed" -lt 3 ]
check $? "a finished child returns within a tick of a 3s bound (rc=$rc, ${elapsed}s)"

# 4b. A child that exits 0 but leaves a grandchild running: the JOB has ended,
#     so its own status, no flag, back at once. Case 2 is the control.
GCPID="$TMPDIR/grandchild.pid"
start=$(date +%s); run_bounded 2 "$FLAG" -- sh -c "sleep 30 & echo \$! > '$GCPID'; exit 0"; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "0" ] && [ ! -e "$FLAG" ] && [ "$elapsed" -le 1 ]
check $? "a child that exited 0 behind a live grandchild returns its own status at once, no flag (rc=$rc, ${elapsed}s, flag=$([ -e "$FLAG" ] && echo present || echo absent))"
/bin/kill -KILL "$(cat "$GCPID" 2>/dev/null)" 2>/dev/null; rm -f "$FLAG" "$GCPID"

# 5. The reuse race, deterministically: every signal the primitive sends must
#    go to a target that is still THIS shell's own job, by jobspec. The shim
#    shadows the kill builtin inside run_bounded, delays the KILL after the
#    primitive has chosen it, and records what the kill was aimed at while a
#    TERM-resistant child exits naturally in that delay. With a pid target the
#    KILL would land after the reap, on a reusable pid.
SIGLOG="$TMPDIR/signals"
kill() {
  local sig="$1" target="$2" state=ended
  case "$sig" in -KILL) sleep 3 ;; esac
  # $pid is run_bounded's local, visible here by dynamic scope.
  # shellcheck disable=SC2154
  builtin kill -0 "$pid" 2>/dev/null && state=live
  printf '%s %s %s\n' "$sig" "$target" "$state" >> "$SIGLOG"
  builtin kill "$@"
}
run_bounded 1 "$FLAG" -- bash -c 'trap "" TERM; sleep 4.5; exit 42'; rc=$?
unset -f kill
[ "$rc" = "42" ] && [ -e "$FLAG" ]
check $? "a TERM-resistant child that exits on its own during the KILL delay keeps its own status (rc=$rc), flag raised"
targets="$(awk '{ print $2 }' "$SIGLOG" | sort -u | tr '\n' ' ')"
case "$targets" in
  "%?sutando_run_bounded ") check 0 "every signal names the job, never a pid ($targets)" ;;
  *) check 1 "every signal names the job, never a pid (got: ${targets:-none})" ;;
esac
awk '$1 == "-TERM" && $3 == "live" { t = 1 } $1 == "-KILL" && $3 == "ended" { k = 1 } END { exit (t && k) ? 0 : 1 }' "$SIGLOG"
check $? "TERM went to the live child and the KILL was issued after it had ended -- the window was reached ($(tr '\n' ';' < "$SIGLOG"))"
rm -f "$FLAG" "$SIGLOG"

# 6. The jobspec path cannot touch a stranger: with the job already ended and
#    an unrelated background job of the caller alive, a jobspec KILL is a no-op.
sleep 30 & bystander=$!
run_bounded 5 -- sh -c 'exit 0' >/dev/null
builtin kill -KILL '%?sutando_run_bounded' 2>/dev/null; krc=$?
sleep 0.2
builtin kill -0 "$bystander" 2>/dev/null; alive=$?
builtin kill -KILL "$bystander" 2>/dev/null; wait "$bystander" 2>/dev/null
[ "$krc" != "0" ] && [ "$alive" = "0" ]
check $? "a KILL by jobspec after the job ended hits nothing (kill rc=$krc, bystander alive=$alive)"

# 7. The primitive forks no watchdog: no background job but the command's own.
bg="$(grep -c ') &$' "$REPO/src/bounded-wait.sh")"
[ "$bg" = "1" ]
check $? "exactly one background job in the shipped source, the command itself ($bg)"

# 8. Repeated fast exits, zero and non-zero: never a flag, every status kept.
#    A completion proxy read before bash marked the job dead called some of
#    these timeouts; rc 3 plus a flag makes the watcher publish a terminal failure.
flags=0; badrc=0; start=$(date +%s)
for _ in $(seq 1 "$N"); do
  run_bounded 5 "$FLAG" -- sh -c 'exit 0'; rc=$?
  [ "$rc" = "0" ] || badrc=$((badrc+1))
  [ ! -e "$FLAG" ] || { flags=$((flags+1)); rm -f "$FLAG"; }
done
for _ in $(seq 1 "$N"); do
  run_bounded 5 "$FLAG" -- sh -c 'exit 3'; rc=$?
  [ "$rc" = "3" ] || badrc=$((badrc+1))
  [ ! -e "$FLAG" ] || { flags=$((flags+1)); rm -f "$FLAG"; }
done
elapsed=$(( $(date +%s) - start ))
[ "$flags" = "0" ] && [ "$badrc" = "0" ]
check $? "${N}x exit 0 + ${N}x exit 3: $flags false timeout flags, $badrc wrong statuses (${elapsed}s)"

# 9. The bound does not depend on what the target does with inherited fds:
#    a target that closes 8 and 9 is still TERMed at the bound.
start=$(date +%s); run_bounded 1 "$FLAG" -- sh -c 'exec 8>&- 9>&-; exec sleep 30'; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "143" ] && [ -e "$FLAG" ] && [ "$elapsed" -ge 1 ] && [ "$elapsed" -le 2 ]
check $? "a target that closed fds 8 and 9 is still TERMed at the bound (rc=$rc, ${elapsed}s, flag=$([ -e "$FLAG" ] && echo present || echo absent))"
rm -f "$FLAG"

# 10. The caller's descriptor table is preserved: fds 8 and 9 it opened before
#     still write to the same files afterwards (the watcher keeps fd 9 for life).
exec 8> "$TMPDIR/fd8.out" 9> "$TMPDIR/fd9.out"
printf 'before\n' >&8; printf 'before\n' >&9
run_bounded 5 -- sh -c 'exit 0'
printf 'after\n' >&8 2>/dev/null; w8=$?
printf 'after\n' >&9 2>/dev/null; w9=$?
exec 8>&- 9>&-
[ "$w8" = "0" ] && [ "$w9" = "0" ] \
  && [ "$(tr '\n' ' ' < "$TMPDIR/fd8.out")" = "before after " ] \
  && [ "$(tr '\n' ' ' < "$TMPDIR/fd9.out")" = "before after " ]
check $? "the caller's fds 8 and 9 survive a run (writes rc=$w8/$w9; fd9 holds: $(tr '\n' ' ' < "$TMPDIR/fd9.out"))"

# 11. A zero-padded limit is decimal: 08 is eight seconds, not an error.
err="$(run_bounded 08 -- sh -c 'exit 5' 2>&1)"; rc=$?
[ "$rc" = "5" ] && [ -z "$err" ]
check $? "run_bounded 08 is accepted (rc=$rc, stderr: ${err:-<empty>})"

# 11b-11d. A malformed configured limit (unit suffix, non-digit, negative) falls
#     back to the 1 s floor silently and keeps the child's own exit status.
for bad in "5s" "x" "-3"; do
  err="$(run_bounded "$bad" -- sh -c 'exit 5' 2>&1)"; rc=$?
  [ "$rc" = "5" ] && [ -z "$err" ]
  check $? "a malformed limit ('$bad') falls back silently, child's own status kept (rc=$rc, stderr: ${err:-<empty>})"
done

# 11e. The fallback is exactly one second: a direct child that would exit 0 at
#     1.5s is TERMed first with the flag; a floor of 2 would let it finish.
for bad in "5s" "-3"; do
  start=$(date +%s); run_bounded "$bad" "$FLAG" -- sleep 1.5; rc=$?; elapsed=$(( $(date +%s) - start ))
  [ -e "$FLAG" ] && [ "$rc" -ge 128 ] && [ "$elapsed" -ge 1 ] && [ "$elapsed" -le 2 ]
  check $? "a 1.5s child under a malformed limit ('$bad') is TERMed at the 1s floor (rc=$rc, ${elapsed}s, flag=$([ -e "$FLAG" ] && echo present || echo absent))"
  rm -f "$FLAG"
done

# 12. Nothing is left behind in TMPDIR but the suite's own files.
leftovers="$(find "$TMPDIR" -type f ! -name 'fd8.out' ! -name 'fd9.out' | wc -l | tr -d ' ')"
[ "$leftovers" = "0" ]
check $? "nothing is left behind ($leftovers)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
