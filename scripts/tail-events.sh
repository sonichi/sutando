#!/usr/bin/env bash
# Tail today's structured event log with optional kind filter.
#
# Usage:
#   bash scripts/tail-events.sh                    # follow today's events
#   bash scripts/tail-events.sh bridge             # filter kinds matching /^bridge/
#   bash scripts/tail-events.sh -n 50              # show last 50 then follow
#   bash scripts/tail-events.sh --yesterday        # tail yesterday's file
#   bash scripts/tail-events.sh --no-follow        # cat + exit, no -f
#
# Requires: jq (for pretty output). Falls back to raw JSONL if jq missing.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATE="$(date +%Y-%m-%d)"
TAIL_N=20
FOLLOW=true
FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n) TAIL_N="$2"; shift 2 ;;
        --yesterday) DATE="$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d 'yesterday' +%Y-%m-%d)"; shift ;;
        --no-follow) FOLLOW=false; shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) FILTER="$1"; shift ;;
    esac
done

LOG_FILE="$REPO/logs/events-$DATE.jsonl"

if [[ ! -f "$LOG_FILE" ]]; then
    echo "tail-events: no file at $LOG_FILE" >&2
    exit 1
fi

format() {
    if command -v jq >/dev/null 2>&1; then
        jq -r '(.ts | todate) + " [" + .node + "] " + .kind + " " + (del(.ts,.node,.kind) | tostring)'
    else
        cat
    fi
}

filter() {
    if [[ -n "$FILTER" ]]; then
        grep -E "\"kind\":\"$FILTER"
    else
        cat
    fi
}

if $FOLLOW; then
    tail -n "$TAIL_N" -f "$LOG_FILE" | filter | format
else
    filter <"$LOG_FILE" | format
fi
