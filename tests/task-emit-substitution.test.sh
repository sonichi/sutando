#!/bin/bash
# A shutdown emit invoked one command-substitution deep must reach the REAL
# stdout. The mutation arm is part of the test; rationale is in the PR body.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="$REPO/src/task-emit.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

# Establish fd 9 as a dup of real stdout, source the unit, then invoke the
# emitter INSIDE a substitution -- where fd 1 is the capture pipe and fd 9 is not.
run_arm() {
	bash -c '
		exec 9>&1
		# shellcheck source=/dev/null
		source "$1"
		swallowed="$(emit_task_file "probe-task.txt"; echo settled)"
		[ "$swallowed" = "settled" ] || printf "ARM_NOTE: capture held %s\n" "$swallowed" >&2
	' _ "$1" 2>/dev/null
}

# --- arm 1: the shipped unit -------------------------------------------------
fixed_out="$(run_arm "$UNIT")"
case "$fixed_out" in
	*"TASK_FILE: probe-task.txt"*) : ;;
	*) fail "shipped unit: emit did not reach real stdout (got: '$fixed_out')" ;;
esac

# --- arm 2: mutation -- drop the fd-9 redirect, keep everything else ---
MUTANT="$TMP/task-emit-mutant.sh"
sed 's/ >&9 / /' "$UNIT" > "$MUTANT"
grep -q '>&9' "$MUTANT" && fail "mutation did not apply -- mutant still redirects to fd 9"
grep -q 'printf .TASK_FILE' "$MUTANT" || fail "mutation removed the emit itself, so arm 2 is vacuous"

mutant_out="$(run_arm "$MUTANT")"
case "$mutant_out" in
	*"TASK_FILE: probe-task.txt"*)
		fail "mutant reached real stdout -- this test cannot detect the bug it exists for" ;;
	*) : ;;
esac

printf 'ok: emit survives one command-substitution deep (shipped)\n'
printf 'ok: without >&9 the same call is swallowed by the capture (mutant)\n'
printf 'ALL PASS\n'
