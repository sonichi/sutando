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
#   bash skills/claude-codex/scripts/review-pr.sh sonichi/sutando-skills#657
#
# A BARE number resolves against the cwd repo's remote, so the same number in a
# sibling repo is a different PR and the review reads as confident and clean about
# the wrong object. Qualify it (`owner/repo#N`, or `--repo owner/repo`) whenever the
# request did not come from this checkout. Either way the resolved repo and title are
# printed after the marker, so the verdict names what it actually reviewed.
#
# stdout is NOT the verdict: take only what follows the LAST `VERDICT-MARKER:` line.
# The stream, or its tail, picks up codex trace or truncates the checks. See SKILL.md.
#
# NOTE on timing: `codex exec` is agentic — even with the diff inlined it may
# explore related code, so a review can take 100s+ on a diff that touches wider
# subsystems (observed 147s on #1754). Keep --max generous (default 240); do NOT
# drop it near ~120 or you'll kill legitimate reviews. Speed is driven by how much
# codex explores, not diff size.
set -u

[[ $# -ge 1 && -n "${1:-}" ]] || { echo "usage: review-pr <pr-number|owner/repo#N> [--repo owner/repo] [--max secs] [--stall secs]" >&2; exit 2; }
PR="$1"; shift
MAX=240
STALL=60
REPO=""
# `owner/repo#N` carries its own repo; a bare number does not and defers to cwd.
if [[ "$PR" == */*#* ]]; then
    REPO="${PR%%#*}"
    PR="${PR##*#}"
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max)   MAX="${2:?--max needs a value}";   shift 2;;
        --stall) STALL="${2:?--stall needs a value}"; shift 2;;
        --repo)  REPO="${2:?--repo needs a value}";  shift 2;;
        *)       echo "review-pr: unknown arg '$1'" >&2; exit 2;;
    esac
done
[[ "$PR" =~ ^[0-9]+$ ]] || { echo "review-pr: '$PR' is not a PR number" >&2; exit 2; }
GH_R=()
[[ -n "$REPO" ]] && GH_R=(-R "$REPO")

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DIFF="$(gh pr diff "${GH_R[@]+"${GH_R[@]}"}" "$PR" 2>/dev/null)" || { echo "review-pr: \`gh pr diff ${REPO:+-R $REPO }$PR\` failed (bad PR number, or no gh auth/remote)" >&2; exit 2; }
# Identify what was actually resolved, so a wrong-repo review is visible in the verdict
# rather than only in the caller's assumption.
SUBJECT="$(gh pr view "${GH_R[@]+"${GH_R[@]}"}" "$PR" --json url,title \
    --jq '"\(.url) — \(.title)"' 2>/dev/null || true)"
[[ -n "$DIFF" ]] || { echo "review-pr: empty diff for #$PR (already merged with no changes, or not found)" >&2; exit 2; }

OUT="$(mktemp -t review-pr.XXXXXX)"
# Per-RUN nonce, announced on line 1. A fixed literal is unsafe here: the marker would
# be inlined by any diff that mentions it, and a verdict quoting it defeats the split.
VERDICT_NONCE="$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 12)"
[[ -n "$VERDICT_NONCE" ]] || VERDICT_NONCE="$$$(date +%s)"   # never an empty marker
VERDICT_MARKER="===CODEX-VERDICT-${VERDICT_NONCE}==="
# FIRST line of stdout, before codex runs: the consumer reads the token here. codex is
# never shown the nonce, so neither its trace nor the diff-derived verdict can contain it.
printf 'VERDICT-MARKER: %s\n' "$VERDICT_MARKER"
trap 'rm -f "$OUT"' EXIT   # clean up even on interrupt / non-zero exit, not just the happy path

# Captured now, emitted AFTER the marker: a consumer keeps only post-marker text,
# so printing here would drop a FAIL. Patterns live in REVIEW.md, not here.
CHECKS_SH="$(cd "$HERE/../../.." && pwd)/scripts/review-checks.sh"
MECH=""
if [[ -x "$CHECKS_SH" ]]; then
    MECH="$(printf '%s' "$DIFF" | bash "$CHECKS_SH" 2>&1 || true)"
fi
bash "$HERE/codex-bounded.sh" --stall "$STALL" --max "$MAX" -- \
    codex exec --sandbox read-only -o "$OUT" -- "Concisely review this PR diff. List only real bugs, correctness issues, or security problems as bullets; if there are none, say 'no blocking issues'. Be specific (file + what's wrong).

$DIFF" < /dev/null
rc=$?

# codex's trace shares this stdout and --stall watches it, so it cannot be
# silenced; the marker is what separates that trace from the review output.
printf '%s\n' "$VERDICT_MARKER"
# What was resolved, not what was asked for: a bare number silently follows cwd, so
# the verdict has to carry its own subject or a wrong-repo review reads as clean.
[[ -n "$SUBJECT" ]] && printf 'Reviewed: %s\n\n' "$SUBJECT"
[[ -n "$MECH" ]] && printf 'Mechanical checks (review-checks.sh):\n%s\n\n' "$MECH"
if [[ $rc -eq 0 && -s "$OUT" ]]; then
    cat "$OUT"
else
    echo "review-pr: no verdict for #$PR (codex exit $rc — 125=stalled, 124=hit --max, other=error)" >&2
fi
exit "$rc"
