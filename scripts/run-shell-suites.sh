#!/usr/bin/env bash
# Runs the discovered shell suites: `lanes` = the pool through parallel-suite-lane.sh,
# `tail` = the process-table suites one at a time in the checkout, `all` = both.
# One copy, so two CI jobs cannot drift on the allowlist or the classifier.
set -euo pipefail
PART="${1:-all}"
case "$PART" in lanes|tail|all) ;; *) echo "usage: $0 [lanes|tail|all]" >&2; exit 2 ;; esac
failed=0
# Load the known-failures allowlist (suites that fail on ubuntu-latest
# due to macOS-specific tooling or timing). Allowlisted suites still run
# and their output is printed, but a failure does NOT set failed=1.
# New regressions in the other suites still fail CI immediately.
# Burn this list to zero: https://github.com/sonichi/sutando/issues/1934
known_failures_file="tests/shell-ci-known-failures.txt"
WORKERS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
RECDIR="$(mktemp -d)"
trap 'rm -rf "$RECDIR"' EXIT
find tests -name '*.test.sh' -not -path '*/node_modules/*' | sort > "$RECDIR/all"
# A suite that reads the host process table (pgrep/pkill, ps by pattern or
# by pid) sees sibling lanes' processes as its own; those run after the lanes,
# alone. Per file, grep's own status: 0 selects, 1 is "not one", anything above
# is an error that must not read as an empty list sending them into the lanes.
: > "$RECDIR/serial"
while IFS= read -r _f; do
  _g=0; grep -qE '\bpgrep\b|\bpkill\b|ps (-ef|ax|-A|-e |-p )' "$_f" || _g=$?
  case "$_g" in 0) echo "$_f" >> "$RECDIR/serial" ;; 1) ;; *) exit "$_g" ;; esac
done < "$RECDIR/all"
comm -23 "$RECDIR/all" "$RECDIR/serial" > "$RECDIR/files"
mkdir -p "$RECDIR/serial-rec"
# Same scheduler as the Python suite: one SERIAL worker per worktree, so
# no two suites ever share a cwd, and every suite's output + status land
# as records replayed in sorted order below — no suite's failure can
# abort the step or mask a later one.
#
# `timeout -k 5 300` bounds each suite: a hanging suite (e.g. a bounded-
# runner test whose --max backstop misfires under CI) would otherwise
# burn the whole job's timeout-minutes budget and get the job *canceled*
# mid-suite — masking every later suite AND this step's own diagnostics.
# On overrun timeout SIGTERMs (then SIGKILLs after 5s) and returns 124,
# so the suite fails loudly and in isolation instead of hanging CI.
# 300s, not 120s: graceful-restart.test.sh measures 73s on an idle dev
# box, leaving under 2x headroom on a slower, contended CI runner.
if [ "$PART" != tail ]; then
  bash scripts/parallel-suite-lane.sh "$WORKERS" "$RECDIR/files" "$RECDIR" \
    timeout -k 5 300 bash
fi
# Each part replays only what it ran; the other list is emptied, not skipped.
[ "$PART" = tail ] && : > "$RECDIR/files"
# The tail runs in the checkout itself, one suite at a time in sorted
# order: the environment these suites had before lanes existed.
_s=0
[ "$PART" = lanes ] && : > "$RECDIR/serial"
while IFS= read -r f; do
  _s=$((_s + 1)); _t0=$SECONDS
  # </dev/null: a suite that reads stdin must not eat the rest of the list.
  if timeout -k 5 300 bash "$f" < /dev/null > "$RECDIR/serial-rec/$_s.out" 2>&1; then
    echo 0 > "$RECDIR/serial-rec/$_s.rc"
  else
    echo $? > "$RECDIR/serial-rec/$_s.rc"
  fi
  echo $(( SECONDS - _t0 )) > "$RECDIR/serial-rec/$_s.time"
done < "$RECDIR/serial"
_idx=0; _rec="$RECDIR"
while IFS= read -r f; do
  if [ "$f" = "--serial--" ]; then _idx=0; _rec="$RECDIR/serial-rec"; continue; fi
  _idx=$((_idx + 1))
  echo "→ $f ($(cat "$_rec/$_idx.time" 2>/dev/null || echo '?')s)"
  rc="$(cat "$_rec/$_idx.rc" 2>/dev/null || echo 1)"
  output="$(cat "$_rec/$_idx.out" 2>/dev/null || true)"
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      echo "✖ shell test TIMED OUT (>300s): $f"
    else
      echo "✖ shell test failed: $f (exit $rc)"
    fi
    echo "$output"
    # Only gate CI if this suite is not in the known-failures allowlist.
    if ! grep -qxF "$f" "$known_failures_file" 2>/dev/null; then
      failed=1
    else
      echo "  (known failure — not gating CI; see $known_failures_file)"
    fi
  else
    echo "$output" | grep -E 'FAIL|^Summary' || true
    # A listed suite that PASSES is a stale entry, and a stale entry
    # un-gates a suite nobody thinks is un-gated. Fail so the list
    # cannot silently outlive the failures it excuses.
    if grep -qxF "$f" "$known_failures_file" 2>/dev/null; then
      echo "✖ stale allowlist entry: $f passed — delete its line from $known_failures_file"
      failed=1
    fi
  fi
done < <(cat "$RECDIR/files"; echo "--serial--"; cat "$RECDIR/serial")
exit "$failed"
