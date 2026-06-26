#!/usr/bin/env bash
# review-pr <pr-number> [--max secs] [--stall secs] — read-only Codex review of a PR.
#
# Fetches the PR diff with `gh pr diff` — which is READ-ONLY: it pulls the diff via
# the GitHub API and does NOT check out the branch, so it never mutates git state
# and never fails on a dirty working tree (unlike `gh pr checkout`). The diff is
# inlined into `codex exec --sandbox read-only` (so the sandboxed agent needs no
# network for the diff and can't write anything), all wrapped in codex-bounded.sh
# (stall-watchdog + absolute cap) so a slow/wedged review can't grind unbounded.
#
#   bash skills/claude-codex/scripts/review-pr.sh 1754
#   bash skills/claude-codex/scripts/review-pr.sh 1754 --max 300
#
# Prints Codex's verdict to stdout. Exit 0 = verdict produced; non-zero = the
# review failed (gh error, or codex stalled=125 / hit cap=124 / errored).
#
# NOTE on timing: `codex exec` is agentic — even with the diff inlined it may
# explore related code, so a review can take 100s+ on a diff that touches wider
# subsystems (observed 147s on #1754). Keep --max generous (default 240); do NOT
# drop it near ~120 or you'll kill legitimate reviews. Speed is driven by how much
# codex explores, not diff size.
set -u

[[ $# -ge 1 && -n "${1:-}" ]] || { echo "usage: review-pr <pr-number> [--max secs] [--stall secs]" >&2; exit 2; }
PR="$1"; shift
MAX=240
STALL=60
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max)   MAX="${2:?--max needs a value}";   shift 2;;
        --stall) STALL="${2:?--stall needs a value}"; shift 2;;
        *)       echo "review-pr: unknown arg '$1'" >&2; exit 2;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DIFF="$(gh pr diff "$PR" 2>/dev/null)" || { echo "review-pr: \`gh pr diff $PR\` failed (bad PR number, or no gh auth/remote)" >&2; exit 2; }
[[ -n "$DIFF" ]] || { echo "review-pr: empty diff for #$PR (already merged with no changes, or not found)" >&2; exit 2; }

OUT="$(mktemp -t review-pr.XXXXXX)"
trap 'rm -f "$OUT"' EXIT   # clean up even on interrupt / non-zero exit, not just the happy path
bash "$HERE/codex-bounded.sh" --stall "$STALL" --max "$MAX" -- \
    codex exec --sandbox read-only -o "$OUT" -- "Concisely review this PR diff. List only real bugs, correctness issues, or security problems as bullets; if there are none, say 'no blocking issues'. Be specific (file + what's wrong).

$DIFF" < /dev/null
rc=$?

if [[ $rc -eq 0 && -s "$OUT" ]]; then
    cat "$OUT"
else
    echo "review-pr: no verdict for #$PR (codex exit $rc — 125=stalled, 124=hit --max, other=error)" >&2
fi
exit "$rc"
