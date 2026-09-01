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

primary_rc=0
"$GH" pr list "$@" --limit "$CAP" --jq "$JQ" > "$OUT" 2>/dev/null || primary_rc=$?

# Count the real population separately: --limit is what hides the rest, so the
# same capped call can never report how much it dropped.
total="$("$GH" pr list "$@" --limit 1000 --json number --jq 'length' 2>/dev/null || echo "")"
# The two calls are independent, so the count can succeed while the capture failed;
# a footer built from $total alone then asserts rows the file does not hold.
rows="$(grep -c . "$OUT" 2>/dev/null)" || rows=0
primary_ok=1
[ "$primary_rc" -ne 0 ] && primary_ok=0
case "$total" in
	''|*[!0-9]*) ;;
	*) [ "$total" -gt 0 ] && [ "$rows" -eq 0 ] && primary_ok=0 ;;
esac

if [ "$primary_ok" -eq 0 ]; then
	echo "(capture may be incomplete — the listing call failed; this file may be truncated at $CAP)" >> "$OUT"
else
	case "$total" in
		''|*[!0-9]*) echo "(population unknown — could not count; this file may be truncated at $CAP)" >> "$OUT" ;;
		*) if [ "$total" -gt "$CAP" ]; then
			echo "($CAP of $total shown — capped for bundle size, $((total - CAP)) omitted)" >> "$OUT"
		fi ;;
	esac
fi
