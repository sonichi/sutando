#!/usr/bin/env bash
# Capture a capped `gh pr list` into a bundle file, and say so when it capped.
#
# A file named prs-open.txt holding 20 of 117 open PRs reads as the whole set to
# every consumer; the cap is fine, the silence is the defect.
#
# Usage: capped-capture.sh <gh-binary> <outfile> <cap> <jq-expr> [gh pr list args...]
set -uo pipefail

GH="${1:?gh binary}"; OUT="${2:?outfile}"; CAP="${3:?cap}"; JQ="${4:?jq expr}"
shift 4

"$GH" pr list "$@" --limit "$CAP" --jq "$JQ" > "$OUT" 2>/dev/null || true

# Count the real population separately: --limit is what hides the rest, so the
# same capped call can never report how much it dropped.
total="$("$GH" pr list "$@" --limit 1000 --json number --jq 'length' 2>/dev/null || echo "")"
case "$total" in
	''|*[!0-9]*) echo "(population unknown — could not count; this file may be truncated at $CAP)" >> "$OUT" ;;
	*) if [ "$total" -gt "$CAP" ]; then
		echo "($CAP of $total shown — capped for bundle size, $((total - CAP)) omitted)" >> "$OUT"
	fi ;;
esac
