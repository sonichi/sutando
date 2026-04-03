#!/bin/bash
# Session handoff — writes a summary for the next session to pick up.
# Called by PreCompact hook so context survives session restarts.
#
# Reads the transcript, extracts key signals, and writes to session-state.md.
# The incoming session reads this in CLAUDE.md or as part of the proactive loop.

REPO="$HOME/Desktop/sutando"
STATE_FILE="$REPO/session-state.md"
TRANSCRIPT="$1"  # Passed by PreCompact hook as $TRANSCRIPT_PATH

# Build state from available signals
{
  echo "---"
  echo "# Session State (auto-generated on compaction)"
  echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "---"
  echo ""

  # What's running
  echo "## System Status"
  python3 "$REPO/src/health-check.py" 2>/dev/null | grep -E "✓|⚠|✗" | head -15
  echo ""

  # Recent git activity (what was built)
  echo "## Recent Work (last 10 commits)"
  git -C "$REPO" log --oneline -10 2>/dev/null
  echo ""

  # Open PRs
  echo "## Open PRs"
  gh pr list --repo sonichi/sutando --state open --limit 5 2>/dev/null || echo "(couldn't fetch)"
  echo ""

  # Pending questions
  echo "## Pending Questions"
  if [ -f "$REPO/pending-questions.md" ]; then
    grep -A1 "^## Q" "$REPO/pending-questions.md" | head -20
  else
    echo "None"
  fi
  echo ""

  # Tasks in flight
  echo "## Tasks"
  ls "$REPO/tasks/"*.txt 2>/dev/null | head -5 || echo "None pending"
  echo ""

  # Quota
  echo "## Quota"
  if [ -f "$REPO/quota-state.json" ]; then
    python3 -c "import json; d=json.load(open('$REPO/quota-state.json')); print(f'5h: {d[\"utilization_5h\"]:.0%}, 7d: {d[\"utilization_7d\"]:.0%}')" 2>/dev/null
  fi
  echo ""

  # Stars
  echo "## Repo Stats"
  gh api repos/sonichi/sutando --jq '.stargazers_count, .forks_count' 2>/dev/null | tr '\n' ' ' | awk '{print $1 " stars, " $2 " forks"}' || echo "(couldn't fetch)"

} > "$STATE_FILE" 2>/dev/null

echo "Session state saved to $STATE_FILE"
