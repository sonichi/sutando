#!/usr/bin/env bash
# Health check for results/ directory backlog — flood-risk early warning.
#
# Run this:
#   - Pre-merge on any PR that changes a results/ delivery path or
#     restart/poll semantics (#349, #352, #394 class)
#   - On proactive-loop passes where budget allows (MEDIUM+)
#   - After any discord-bridge or task-bridge restart
#
# Exits 0 if clean. Exits 1 if any flood-risk signal found:
#   - >10 .txt files at top level of results/
#   - any zero-byte .txt files (HTTP 400 fodder for delivery loops)
#   - any .txt files older than 24h (would re-deliver after restart)
#
# Source: feedback_restart_semantics_check.md, post-mortem-dm-flood-2026-04-15.md.
#
# Usage: bash scripts/results-health.sh [--quiet]
#        - default: print summary + warning lines
#        - --quiet: print nothing on clean, only warn on signal

set -uo pipefail

QUIET=0
for arg in "$@"; do
	case "$arg" in
		-q|--quiet) QUIET=1 ;;
	esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="$REPO/results"

if [ ! -d "$RESULTS" ]; then
	[ "$QUIET" -eq 0 ] && echo "results/ missing — nothing to check"
	exit 0
fi

count=$(find "$RESULTS" -maxdepth 1 -type f -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')
zero_byte=$(find "$RESULTS" -maxdepth 1 -type f -name "*.txt" -size 0 2>/dev/null | wc -l | tr -d ' ')
stale=$(find "$RESULTS" -maxdepth 1 -type f -name "*.txt" -mmin +1440 2>/dev/null | wc -l | tr -d ' ')

issues=0

if [ "$count" -gt 10 ]; then
	echo "WARN: $count .txt files at top level of results/ (>10 = flood risk on restart)"
	issues=$((issues + 1))
fi
if [ "$zero_byte" -gt 0 ]; then
	echo "WARN: $zero_byte zero-byte .txt files in results/ (would HTTP 400 every poll cycle)"
	find "$RESULTS" -maxdepth 1 -type f -name "*.txt" -size 0 2>/dev/null | head -5 | sed 's/^/  /'
	issues=$((issues + 1))
fi
if [ "$stale" -gt 0 ]; then
	echo "WARN: $stale .txt files older than 24h (would re-deliver on restart)"
	find "$RESULTS" -maxdepth 1 -type f -name "*.txt" -mmin +1440 2>/dev/null | head -5 | sed 's/^/  /'
	issues=$((issues + 1))
fi

if [ "$issues" -eq 0 ]; then
	[ "$QUIET" -eq 0 ] && echo "results/ healthy: $count files, 0 zero-byte, 0 stale (>24h)"
	exit 0
fi

echo
echo "Action: archive stale results before next bridge restart:"
echo "  python3 src/archive-stale-results.py    # default RETENTION_HOURS=24"
echo "  # or for zero-byte files specifically:"
echo "  find results -maxdepth 1 -name '*.txt' -size 0 -delete"
exit 1
