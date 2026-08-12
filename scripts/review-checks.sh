#!/usr/bin/env bash
# review-checks.sh — run a repo's machine-readable review checks over a unified diff.
#
# Reads the `checks:` block from the repo's REVIEW.md (the single
# source of truth — lessons + checks live there, NOT baked into this runner) and
# runs each check over the ADDED lines of a diff. Today the only check is
# `hardcoded-paths` (folds the per-review scanner from #2229 into one place that
# CI, review-pr.sh, and any agent all call).
#
# Usage:
#   git diff | bash scripts/review-checks.sh                 # scan a diff on stdin
#   bash scripts/review-checks.sh --diff pr.diff             # scan a diff file
#   bash scripts/review-checks.sh --guide path/to/REVIEW.md --diff pr.diff
#
# Guide resolution: --guide wins; else <repo>/REVIEW.md. Missing
# guide -> generic fallback patterns + a stderr note (degrades safely).
#
# Exit: 0 = clean; 1 = a check flagged something; 2 = usage error OR the scanner
#       failed to launch/run (fail-closed — NEVER print PASS in that case).
set -u

DIFF_FILE=""
GUIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --diff)  DIFF_FILE="${2:?--diff needs a path}"; shift 2;;
        --guide) GUIDE="${2:?--guide needs a path}";    shift 2;;
        -h|--help) sed -n '2,16p' "$0"; exit 0;;
        *) echo "review-checks: unknown arg '$1'" >&2; exit 2;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

if [[ -n "$DIFF_FILE" ]]; then
    [[ -r "$DIFF_FILE" ]] || { echo "review-checks: cannot read diff '$DIFF_FILE'" >&2; exit 2; }
    DIFF="$(cat "$DIFF_FILE")"
else
    DIFF="$(cat)"
fi
# Non-empty test via regex (NOT ${DIFF//…/} — that global substitution is
# O(pathological) on a large string under macOS bash 3.2 and effectively hangs).
[[ "$DIFF" =~ [^[:space:]] ]] || { echo "review-checks: empty diff — nothing to check." >&2; exit 0; }

[[ -n "$GUIDE" ]] || GUIDE="$REPO/REVIEW.md"

# --- parse the guide's checks: hardcoded-paths {flag,allow} lists -------------
parse_list() {  # $1 = flag|allow ; reads $GUIDE
    [[ -r "$GUIDE" ]] || return 0
    awk -v want="$1" '
        /^```yaml/ {y=1; next}
        /^```/     {y=0}
        !y {next}
        # Section keys are matched generically so a new list (e.g. allow_paired)
        # needs no parser change — previously only flag:/allow: were recognized
        # and every other key reset the state, silently dropping new sections.
        /^[[:space:]]*[A-Za-z_-]+:[[:space:]]*$/ {
            s=$0; sub(/^[[:space:]]*/,"",s); sub(/:[[:space:]]*$/,"",s); next
        }
        s==want && /^[[:space:]]*-[[:space:]]/ {
            v=$0; sub(/^[[:space:]]*-[[:space:]]*/,"",v)
            sub(/[[:space:]]+#.*$/,"",v)
            gsub(/^["'\'']|["'\'']$/,"",v)
            if (v!="") print v
        }
    ' "$GUIDE"
}
FLAGS="$(parse_list flag)"
# flag_exact: patterns that must match the WHOLE path token, not a substring.
# Needed for full executable paths — '/usr/bin/swift' as a substring also
# rejects the real, separate '/usr/bin/swift-inspect' binary (#2474 review).
FLAGS_EXACT="$(parse_list flag_exact)"
ALLOWS="$(parse_list allow)"
ALLOW_PAIRED="$(parse_list allow_paired)"
ROOT_GLOBS="$(parse_list root_artifact_glob)"
NOTE=""
ROOT_NOTE=""
# Defaulted INDEPENDENTLY of the FLAGS fallback below, because the two go empty
# for different reasons: a guide can parse fine for hardcoded-paths and simply
# not carry this key. Tying them together left ROOT_GLOBS empty on that path,
# and the scan then reported "root-artifacts clean" without ever running.
if [[ -z "${ROOT_GLOBS//[$' \t\r\n']/}" ]]; then
    ROOT_GLOBS=$'prbody*\npr-body*\npr_body*\nreply*.md\ncomment*.md\ndraft*.md\n*.patch\n*.diff\n*.orig\n*.rej\nnohup.out'
    ROOT_NOTE="no root_artifact_glob in ${GUIDE#$REPO/}; used generic root-artifact defaults"
fi
if [[ -z "${FLAGS//[$' \t\r\n']/}" ]]; then
    FLAGS=$'/Users/\n/home/'
    ALLOWS=$'/nonexistent\n/usr/fake\n/tmp/\nexample.com'
    ALLOW_PAIRED=''
    NOTE="no repo review guide (or no checks: block) at ${GUIDE#$REPO/}; used generic defaults"
fi

# --- scan ADDED diff lines for flagged hardcoded paths -----------------------
# The scan itself is a sibling Python file (robust string handling; and keeping
# it out of a $() heredoc dodges macOS bash 3.2's heredoc-in-$() mis-parse).
# Patterns pass via env (small); the diff is STREAMED via stdin — never argv/env
# — so a large PR diff (~8MB) can't hit 'Argument list too long' and make the
# scanner fail to launch while we blindly print PASS (#2281). `printf` is a bash
# builtin, so piping the whole diff carries no exec-size limit.
HITS="$(printf '%s' "$DIFF" | RC_FLAGS="$FLAGS" RC_FLAGS_EXACT="$FLAGS_EXACT" RC_ALLOWS="$ALLOWS" RC_ALLOW_PAIRED="$ALLOW_PAIRED" python3 "$HERE/review-checks.py")"
SCAN_RC=$?
# Fail closed: if the scanner didn't run to completion (exec failure, crash),
# its exit is non-zero. Do NOT interpret an empty stdout as "clean" — error out.
if [[ $SCAN_RC -ne 0 ]]; then
    echo "review-checks: ERROR — hardcoded-paths scanner failed to run (exit $SCAN_RC); failing closed (NOT a pass)." >&2
    exit 2
fi

[[ -n "$NOTE" ]] && echo "review-checks: $NOTE" >&2
[[ -n "$ROOT_NOTE" ]] && echo "review-checks: $ROOT_NOTE" >&2

# --- scan ADDED FILE PATHS for PR-draft artifacts at the repo root -----------
# Separate scanner: a stray root file is a diff HEADER, never an added line, so
# the content scanner above cannot see it however its patterns are written.
ROOT_HITS="$(printf '%s' "$DIFF" | RC_ROOT_ARTIFACT_GLOBS="$ROOT_GLOBS" python3 "$HERE/review-checks-root-artifacts.py")"
ROOT_RC=$?
if [[ $ROOT_RC -ne 0 ]]; then
    echo "review-checks: ERROR — root-artifacts scanner failed to run (exit $ROOT_RC); failing closed (NOT a pass)." >&2
    exit 2
fi

FAILED=0
if [[ "$HITS" =~ [^[:space:]] ]]; then
    echo "review-checks: FAIL — hardcoded-paths:" >&2
    printf '%s\n' "$HITS" >&2
    echo "review-checks: $(printf '%s\n' "$HITS" | grep -c '') violation(s). Resolve via workspace/config helpers, or add a scoped allow to REVIEW.md if it's a genuine fixture." >&2
    FAILED=1
fi
if [[ "$ROOT_HITS" =~ [^[:space:]] ]]; then
    echo "review-checks: FAIL — root-artifacts:" >&2
    printf '%s\n' "$ROOT_HITS" >&2
    echo "review-checks: $(printf '%s\n' "$ROOT_HITS" | grep -c '') artifact(s) at the repo root. Delete them from the branch, or add the name to root-artifacts in REVIEW.md if it is genuinely source." >&2
    FAILED=1
fi
[[ $FAILED -eq 1 ]] && exit 1
echo "review-checks: PASS (hardcoded-paths + root-artifacts clean)"
exit 0
