#!/usr/bin/env bash
# Collect observable state from the last <window> into a temp directory.
# Usage: gather.sh [window]       # e.g. gather.sh 24h, gather.sh 3d
# Default window: 24h.
# Prints the output directory path on stdout as the last line.

set -euo pipefail

WINDOW="${1:-24h}"
# REPO resolution: prefer SUTANDO_ROOT env, then $PWD if it looks like a
# Sutando workspace (has build_log.md), else walk up from $0 looking for
# build_log.md (handles invocation via the userSettings hardlink at
# ~/.claude/skills/... where the original 3-level dirname-walk landed at
# ~/.claude/ instead of the workspace). Caught 2026-05-05 when /self-diagnose
# silently ran against ~/.claude/ on Mini, producing empty
# git-log/build_log/health.txt.
REPO=""
if [ -n "${SUTANDO_ROOT:-}" ] && [ -f "${SUTANDO_ROOT}/build_log.md" ]; then
	REPO="$SUTANDO_ROOT"
elif [ -f "$PWD/build_log.md" ]; then
	REPO="$PWD"
else
	# Walk up from $0 looking for build_log.md (max 5 levels)
	DIR="$(cd "$(dirname "$0")" && pwd)"
	for _ in 1 2 3 4 5; do
		if [ -f "$DIR/build_log.md" ]; then
			REPO="$DIR"
			break
		fi
		DIR="$(dirname "$DIR")"
		[ "$DIR" = "/" ] && break
	done
	# Last-resort fallback: original 3-level dirname-walk
	[ -z "$REPO" ] && REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
fi
TS="$(date +%s)"
OUT="/tmp/sutando-diagnose-$TS"
mkdir -p "$OUT"

# Convert window to seconds for log filtering
case "$WINDOW" in
	*h) SECONDS_AGO=$((${WINDOW%h} * 3600)) ;;
	*d) SECONDS_AGO=$((${WINDOW%d} * 86400)) ;;
	*w) SECONDS_AGO=$((${WINDOW%w} * 604800)) ;;
	*) echo "Unknown window format: $WINDOW (use h/d/w)" >&2; exit 1 ;;
esac
SINCE_EPOCH=$(( $(date +%s) - SECONDS_AGO ))
SINCE_ISO="$(date -r $SINCE_EPOCH +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -d "@$SINCE_EPOCH" +%Y-%m-%dT%H:%M:%S)"

echo "window: $WINDOW (since $SINCE_ISO)" > "$OUT/meta.txt"
echo "repo: $REPO" >> "$OUT/meta.txt"

# 1) Git activity
git -C "$REPO" log --since="$SINCE_ISO" --pretty=format:'%h %ad %s' --date=short > "$OUT/git-log.txt" 2>/dev/null || true
git -C "$REPO" status --short > "$OUT/git-status.txt" 2>/dev/null || true

# 2) Open PRs + recently merged (last 14d) — cheap, already cached by gh
if command -v gh >/dev/null; then
	gh pr list --state open --limit 20 --json number,title,mergeable,headRefName,author,updatedAt \
		--jq '.[] | "#\(.number) \(.headRefName) [@\(.author.login)] \(.title) — \(.mergeable)"' \
		> "$OUT/prs-open.txt" 2>/dev/null || true
	gh pr list --state merged --search "merged:>$(date -v -14d +%Y-%m-%d 2>/dev/null || date -d '14 days ago' +%Y-%m-%d)" \
		--limit 30 --json number,title,mergedAt,author \
		--jq '.[] | "#\(.number) \(.mergedAt[:10]) [@\(.author.login)] \(.title)"' \
		> "$OUT/prs-recent-merged.txt" 2>/dev/null || true
fi

# 3) Build log tail + pending questions + cold-review log (small files, copy whole)
tail -150 "$REPO/build_log.md" > "$OUT/build_log-tail.md" 2>/dev/null || true
cp "$REPO/pending-questions.md" "$OUT/pending-questions.md" 2>/dev/null || true
cp "$REPO/notes/cold-review-log.md" "$OUT/cold-review-log.md" 2>/dev/null || true

# 4) Voice-agent log — filter to window, grep for signal lines, keep it bounded.
# Signals: transport closes (1006/1011/1007/1008), errors, GoAway, setup complete, 1006/1011 numeric.
VLOG="$REPO/logs/voice-agent.log"
if [ -f "$VLOG" ]; then
	awk -v since="$SINCE_ISO" '
		# Approximate filter: log lines start with HH:MM:SS — we can'"'"'t easily compare dates,
		# so we simply take the last ~5000 lines and filter by signal inside that window.
		{ buf[NR % 5000] = $0 }
		END { for (i = (NR>=5000?NR-4999:1); i <= NR; i++) print buf[i % 5000] }
	' "$VLOG" 2>/dev/null | grep -E "code=1006|code=1011|code=1007|code=1008|code=4000|GoAway|Transport error|Transport closed|setup complete|Gemini disconnected|reconnect|Error: " \
		> "$OUT/voice-agent-signals.txt" || true
	wc -l "$VLOG" > "$OUT/voice-agent-size.txt"
fi

# 5) Discord bridge log — last 200 non-dm-fallback lines
DLOG="$REPO/logs/discord-bridge.log"
if [ -f "$DLOG" ]; then
	grep -v "\[dm-fallback\]" "$DLOG" 2>/dev/null | tail -200 > "$OUT/discord-bridge-recent.txt" || true
fi

# 6) Health check current state
if [ -f "$REPO/src/health-check.py" ]; then
	python3 "$REPO/src/health-check.py" 2>&1 | tail -40 > "$OUT/health.txt" || true
fi

# 7) Recent result files — what did the agent actually reply to?
# Use -mmin against SECONDS_AGO (not `-newer meta.txt` — meta.txt was created
# at gather-start, so that would only match files written DURING the gather,
# not files in the last $WINDOW).
find "$REPO/results" -maxdepth 1 -type f -name "*.txt" -mmin "-$((SECONDS_AGO/60))" 2>/dev/null | head -20 > "$OUT/results-recent-paths.txt" || true

# 8) Quota state
if [ -f "$HOME/.claude/skills/quota-tracker/scripts/read-quota.py" ]; then
	python3 "$HOME/.claude/skills/quota-tracker/scripts/read-quota.py" 2>&1 | head -10 > "$OUT/quota.txt" || true
fi

# Print size summary to stderr and path to stdout
echo "Gathered to $OUT:" >&2
du -h "$OUT"/* 2>/dev/null | sort -rh | head -15 >&2
echo "$OUT"
