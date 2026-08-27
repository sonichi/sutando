#!/usr/bin/env bash
# Unit-test coverage gate — enforce >= 95% coverage on CHANGED Python lines.
#
# What it does:
#   1. Runs every standalone Python test (tests/**/*.test.py — same discovery
#      as ci.yml) under coverage.py instrumentation.
#   2. Prints the whole-tree coverage report (informational — the visibility
#      that lets us ratchet the global floor up over time).
#   3. Gates: `diff-cover` compares coverage.xml against the base branch and
#      FAILS if less than 95% of the added/changed executable Python lines
#      are exercised by the suite.
#   4. Writes coverage-summary.md (the numbers + per-file misses) for the PR
#      comment posted by .github/workflows/coverage-comment.yml, and appends
#      it to $GITHUB_STEP_SUMMARY when running in Actions.
#
# Why diff coverage and not a global floor: the tree's historical global
# coverage is far below 95% (large untested legacy surface: bridges, GUI
# automation, installers). A global 95% floor would hard-red every PR on
# day one and block all merges. The diff gate applies the full 95% bar to
# every line of NEW code, which converges the tree toward the target
# without freezing development. Flip GLOBAL_FLOOR on once the tree gets
# there (see docs/testing-coverage.md).
#
# Usage:
#   bash scripts/coverage-gate.sh                 # gate vs origin/main
#   BASE_REF=<ref> bash scripts/coverage-gate.sh  # gate vs another base
#   COVERAGE_GATE_FAIL_UNDER=90 bash scripts/coverage-gate.sh  # override bar
#
# Deps (dev-only, mirrors detect-secrets handling in ci.yml):
#   python3 -m pip install coverage diff-cover
# Locally on PEP-668 (Homebrew) pythons, use a venv and put it on PATH.

set -euo pipefail

# Tests must never emit product telemetry. The suite runs real code paths and
# an unstubbed one would hit PostHog from CI/dev; opt out for every test here.
export SUTANDO_TELEMETRY=0

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

BASE="${BASE_REF:-origin/main}"
FAIL_UNDER="${COVERAGE_GATE_FAIL_UNDER:-95}"
SUMMARY="coverage-summary.md"

# publish_summary <verdict-line> [extra-markdown-file]
# Writes the sticky-comment payload + mirrors it into the Actions job summary.
publish_summary() {
    {
        echo "## Coverage Gate"
        echo
        echo "$1"
        if [ -n "${2:-}" ] && [ -f "${2:-}" ]; then
            echo
            cat "$2"
        fi
    } > "$SUMMARY"
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        cat "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
    fi
}

if ! python3 -m coverage --version >/dev/null 2>&1; then
    echo "coverage-gate: coverage.py not importable by python3." >&2
    echo "  Install dev deps: python3 -m pip install coverage diff-cover" >&2
    exit 2
fi
if ! command -v diff-cover >/dev/null 2>&1; then
    echo "coverage-gate: diff-cover not on PATH." >&2
    echo "  Install dev deps: python3 -m pip install coverage diff-cover" >&2
    exit 2
fi

# Fast path: PRs that touch no Python have no diff to gate. (diff-cover
# would report 100% anyway; skipping saves the full instrumented suite run.)
if ! git diff --name-only "$BASE"...HEAD -- '*.py' | grep -q .; then
    echo "coverage-gate: no Python changes vs $BASE — nothing to gate. ✓"
    publish_summary "✅ **No Python changes** — nothing to gate (bar: ${FAIL_UNDER}% on changed lines)."
    exit 0
fi

echo "coverage-gate: running Python suite under instrumentation..."
rm -f .coverage coverage.xml diff-cover.md
find . -maxdepth 1 -name '.coverage.*' -delete

failed=0
# Skip accounting. The loop below captures each file's output and previously
# echoed it ONLY on failure, so a file whose every case skipped produced
# byte-identical output to one whose every case passed. A suite that silently
# stopped running — a missing toolchain, an absent optional dep, a
# host-specific guard that is false on the runner — was indistinguishable
# from a green one. unittest already reports this on stderr; we were
# discarding it.
skipped_total=0
fully_skipped=()
partly_skipped=()
# Each file writes its own .coverage.* fragment (`parallel = True`), merged
# by the `coverage combine` below.
COVGATE_WORKERS="${COVERAGE_GATE_WORKERS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"
RECDIR="$(mktemp -d)"
trap 'rm -rf "$RECDIR"' EXIT

# Per-file cap, mirroring ci.yml's twin loop. Without it ONE hung file burns
# the job's whole timeout-minutes budget and the job is CANCELED mid-suite —
# indistinguishable, from outside, from a suite that is merely slow.
# `timeout` is coreutils and absent on a stock macOS, where this script is
# also run by hand; degrade to no cap rather than failing the local path.
COVGATE_TIMEOUT=""
if command -v timeout >/dev/null 2>&1; then
    COVGATE_TIMEOUT="timeout -k 5 ${COVERAGE_GATE_FILE_TIMEOUT:-120}"
else
    echo "coverage-gate: no \`timeout\` binary — per-file cap disabled (local run)." >&2
fi
export RECDIR COVGATE_TIMEOUT

find tests -name '*.test.py' -not -path '*/node_modules/*' | sort > "$RECDIR/files"
# Key on the LINE INDEX: a name-derived key collides (`tr "/." "__"` maps
# tests/a/b and tests/a_b alike), letting a failing rc be overwritten.
_covgate_n="$(wc -l < "$RECDIR/files" | tr -d ' ')"

# shellcheck disable=SC2016
# Feed BARE INTEGERS: `xargs -I{}` rewrites a tab in the replaced string to a
# space, so a delimiter-in-argument scheme silently stops splitting.
seq 1 "$_covgate_n" | xargs -P "$COVGATE_WORKERS" -I{} bash -c '
    idx="$1"
    f="$(sed -n "${idx}p" "$RECDIR/files")"
    rec="$RECDIR/$idx"
    rc=0
    out="$(SUTANDO_TEST_SUBPROCESS_COVERAGE=1 $COVGATE_TIMEOUT python3 -m coverage run --rcfile=.coveragerc "$f" 2>&1)" || rc=$?
    printf "%s" "$out" > "$rec.out"
    printf "%s\n" "$rc" > "$rec.rc"
' _ {}

_covgate_idx=0
while IFS= read -r f; do
    _covgate_idx=$((_covgate_idx + 1))
    rec="$RECDIR/$_covgate_idx"
    rc="$(cat "$rec.rc" 2>/dev/null || echo 1)"
    output="$(cat "$rec.out" 2>/dev/null || true)"
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
            echo "✖ test TIMED OUT under instrumentation (>${COVERAGE_GATE_FILE_TIMEOUT:-120}s): $f"
        else
            echo "✖ test failed under instrumentation: $f"
        fi
        echo "$output"
        failed=1
        continue
    fi
    # "Ran N tests" / "OK (skipped=M)" — unittest's own summary.
    ran=$(printf '%s' "$output" | sed -nE 's/^Ran ([0-9]+) tests?.*/\1/p' | tail -1)
    skips=$(printf '%s' "$output" | sed -nE 's/.*skipped=([0-9]+).*/\1/p' | tail -1)
    [ -n "$ran" ] || ran=0
    [ -n "$skips" ] || skips=0
    skipped_total=$((skipped_total + skips))
    if [ "$ran" -gt 0 ] && [ "$skips" -eq "$ran" ]; then
        fully_skipped+=("$f ($ran/$ran skipped)")
    elif [ "$skips" -gt 0 ]; then
        partly_skipped+=("$f ($skips/$ran skipped)")
    fi
done < "$RECDIR/files"

if [ "$skipped_total" -gt 0 ]; then
    echo "coverage-gate: $skipped_total test case(s) SKIPPED in this environment."
    # Attribute them. A bare total tells you something is skipping but not
    # WHAT, so it cannot answer the question the total provokes: does this
    # environment skip different things than a developer's machine? Naming
    # the files makes a macOS-vs-Linux difference visible in the log instead
    # of requiring a local re-run to guess at.
    for entry in "${partly_skipped[@]}"; do echo "    - $entry"; done
fi
if [ "${#fully_skipped[@]}" -gt 0 ]; then
    echo "coverage-gate: ${#fully_skipped[@]} file(s) skipped EVERY case — they assert nothing here:"
    for entry in "${fully_skipped[@]}"; do echo "    - $entry"; done
    echo "coverage-gate: not a failure (optional deps / host toolchains are legitimately absent),"
    echo "coverage-gate: but a fully-skipped file is not a passing file. Verify that is intended."
fi
if [ "$failed" -ne 0 ]; then
    # A suite that fails here but passes locally is usually an environment gap.
    # /bin/bash is what the watcher suites spawn, so name it rather than $SHELL.
    echo "coverage-gate: env — $(/bin/bash --version | head -1) · $(python3 -V 2>&1) · $(uname -sr)" >&2
    publish_summary "❌ **Test suite failed under instrumentation** — coverage not measurable. See the job log."
    echo "coverage-gate: suite must be green before coverage is meaningful." >&2
    exit 1
fi

python3 -m coverage combine --quiet
python3 -m coverage xml --quiet

echo
echo "── whole-tree coverage (informational) ─────────────────────────"
python3 -m coverage report --skip-covered --sort=cover | tail -15
total="$(python3 -m coverage report --format=total 2>/dev/null || echo '?')"
echo "TOTAL: ${total}%"
echo "─────────────────────────────────────────────────────────────────"
echo

echo "coverage-gate: enforcing ${FAIL_UNDER}% on lines changed vs $BASE"
gate_rc=0
diff-cover coverage.xml --compare-branch="$BASE" --fail-under="$FAIL_UNDER" \
    --markdown-report diff-cover.md || gate_rc=$?

# diff-cover's markdown report carries the per-file table + missing lines;
# prepend the verdict + tree number so the PR comment answers "what
# percentage is it" at a glance.
if [ "$gate_rc" -eq 0 ]; then
    verdict="✅ **Diff coverage PASSES** the ${FAIL_UNDER}% bar. Whole-tree (informational): **${total}%**."
else
    verdict="❌ **Diff coverage BELOW the ${FAIL_UNDER}% bar** — uncovered changed lines listed below. Whole-tree (informational): **${total}%**."
fi

# diff-cover reports only on files IN coverage.xml, so a changed file outside
# `[run] source` is neither covered nor uncovered — the gate passes without
# having looked at it. Report, never fail: this names the silence, it does not
# move the bar.
report="diff-cover.md"
if unmeasured="$(python3 scripts/coverage_unmeasured.py "$BASE" coverage.xml)" \
   && [ -n "$unmeasured" ]; then
    echo
    echo "coverage-gate: NOT MEASURED — changed, but absent from coverage.xml:"
    printf '  %s\n' $unmeasured
    echo "coverage-gate: their changed lines are outside the bar, not inside and clean."
    {
        cat diff-cover.md
        echo
        echo "### ⚠️ Not measured"
        echo
        echo "Changed, but absent from \`coverage.xml\` — outside \`[run] source\` in"
        echo "\`.coveragerc\`, so **diff-cover never examined these lines**:"
        echo
        printf -- '-   `%s`\n' $unmeasured
    } > coverage-gate-report.md
    report="coverage-gate-report.md"
fi
publish_summary "$verdict" "$report"

exit "$gate_rc"
