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
#   bash scripts/review-checks.sh --allow-empty --diff pr.diff  # empty input OK
#
# Guide resolution: --guide wins; else <repo>/REVIEW.md. Missing
# guide -> generic fallback patterns + a stderr note (degrades safely).
#
# Exit: 0 = clean; 1 = a check flagged something; 2 = usage error, EMPTY input, or
#       scanner failure — all fail-closed, no PASS. --allow-empty makes empty = 0.
set -u

DIFF_FILE=""
GUIDE=""
ALLOW_EMPTY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --diff)  DIFF_FILE="${2:?--diff needs a path}"; shift 2;;
        --guide) GUIDE="${2:?--guide needs a path}";    shift 2;;
        --allow-empty) ALLOW_EMPTY=1; shift;;
        -h|--help) sed -n '2,20p' "$0"; exit 0;;
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
if [[ ! "$DIFF" =~ [^[:space:]] ]]; then
    # "Nothing was scanned" is not "nothing was found": exiting 0 here let every
    # wrapper (and every agent) read a no-op invocation as a clean gate.
    if [[ -n "$ALLOW_EMPTY" ]]; then
        echo "review-checks: empty diff — nothing to check (--allow-empty)." >&2
        exit 0
    fi
    echo "review-checks: ERROR — empty diff; nothing was scanned, so this is NOT a pass." >&2
    echo "  Pipe a diff, or pass --diff <file>; use --allow-empty to accept an empty input." >&2
    exit 2
fi

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
# Defaulted independently of the FLAGS fallback: the two go empty for different
# reasons, and sharing a condition left this one silently unscanned.
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
# Separate scanner: a stray root file is a diff HEADER, so the content scan
# above cannot see it whatever its patterns.
PROSE_CAP="$(sed -n 's/^ *prose_cap: *//p' "$GUIDE" | head -1)"
PROSE_CAP="${PROSE_CAP:-2}"
# Read from the guide like every other check. Hardcoding it made REVIEW.md's
# declaration decorative: a guide naming .ts still only ever got .py scanned.
PROSE_EXTS="$(sed -n 's/^ *prose_exts: *//p' "$GUIDE" 2>/dev/null | head -1 | tr -d "[]\"' ")"
[[ -n "$PROSE_EXTS" ]] || PROSE_EXTS="$(parse_list prose_exts | tr '\n' ',' | sed 's/,$//')"
PROSE_EXTS="${PROSE_EXTS:-.py}"
ROOT_HITS="$(printf '%s' "$DIFF" | RC_ROOT_ARTIFACT_GLOBS="$ROOT_GLOBS" python3 "$HERE/review-checks-root-artifacts.py")"
ROOT_RC=$?
if [[ $ROOT_RC -ne 0 ]]; then
    echo "review-checks: ERROR — root-artifacts scanner failed to run (exit $ROOT_RC); failing closed (NOT a pass)." >&2
    exit 2
fi

# --- scan ADDED lines for prose blocks over the physical-line cap -----------
# Separate scanner: COMMENT runs only, classified by tokenize over the post-image.
# Docstrings are out of scope — the written contract caps code comments.
PROSE_ERR="$(mktemp -t review-checks-prose.XXXXXX)"
trap 'rm -f "$PROSE_ERR"' EXIT
PROSE_HITS="$(printf '%s' "$DIFF" | RC_PROSE_CAP="$PROSE_CAP" RC_PROSE_EXTS="$PROSE_EXTS" python3 "$HERE/review-checks-prose-cap.py" 2>"$PROSE_ERR")"
PROSE_RC=$?
cat "$PROSE_ERR" >&2
# A diff detached from its tree (`gh pr diff`) leaves prose-cap nothing to read.
# Not a failure, but the verdict TOKEN must carry it — readers key on the first word.
PROSE_SCOPE="hardcoded-paths + root-artifacts + prose-cap clean"
VERDICT="PASS"
if grep -q '^prose-cap: SKIPPED' "$PROSE_ERR"; then
    PROSE_SCOPE="hardcoded-paths + root-artifacts clean; prose-cap SKIPPED — no post-image"
    VERDICT="PARTIAL"
elif grep -q '^prose-cap: no in-scope files' "$PROSE_ERR"; then
    PROSE_SCOPE="hardcoded-paths + root-artifacts clean; prose-cap had no in-scope files"
    VERDICT="PARTIAL"
fi
# Fail-closed is asserted AFTER the other scanners report. Exiting here would
# suppress real hardcoded-path/root-artifact findings already in hand.

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
if [[ "$PROSE_HITS" =~ [^[:space:]] ]]; then
    echo "review-checks: FAIL — prose-cap:" >&2
    printf '%s\n' "$PROSE_HITS" >&2
    echo "review-checks: $(printf '%s\n' "$PROSE_HITS" | grep -c '') block(s) over the cap. Keep the constraint in the code and move the narrative to the PR body." >&2
    FAILED=1
fi
if [[ $PROSE_RC -ne 0 ]]; then
    echo "review-checks: ERROR — prose-cap scanner failed to run (exit $PROSE_RC); failing closed (NOT a pass)." >&2
    exit 2
fi
[[ $FAILED -eq 1 ]] && exit 1
echo "review-checks: $VERDICT ($PROSE_SCOPE)"
exit 0
