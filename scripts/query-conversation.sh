#!/usr/bin/env bash
# Thin sqlite3 CLI wrapper for data/conversation.sqlite (issue #603 slice 1).
#
# Usage:
#   query-conversation.sh "<like-term>"                    # search anywhere in text
#   query-conversation.sh "<like-term>" --since "1 day"    # time-bounded
#   query-conversation.sh "<like-term>" --role user        # filter by role
#   query-conversation.sh --last 20                        # latest N turns, no filter
#
# Output: tab-separated ts (ISO) | role | text (truncated to 200 chars)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB="${SUTANDO_CONVERSATION_DB:-$REPO_DIR/data/conversation.sqlite}"

if [[ ! -f "$DB" ]]; then
	echo "error: $DB does not exist yet — no conversations recorded" >&2
	exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
	echo "error: sqlite3 CLI not found in PATH" >&2
	exit 1
fi

term=""
role=""
since=""
last=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--since) since="$2"; shift 2 ;;
		--role)  role="$2"; shift 2 ;;
		--last)  last="$2"; shift 2 ;;
		--help|-h)
			sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
			exit 0 ;;
		*) term="$1"; shift ;;
	esac
done

where=()
[[ -n "$term" ]] && where+=("text LIKE '%${term//\'/\'\'}%'")
[[ -n "$role" ]] && where+=("role = '${role//\'/\'\'}'")
if [[ -n "$since" ]]; then
	# sqlite-friendly: e.g. "1 day", "2 hour" → strftime('%s', 'now', '-1 day')
	where+=("ts_unix > strftime('%s','now','-${since}')")
fi

if [[ ${#where[@]} -eq 0 ]] && [[ -z "$last" ]]; then
	echo "error: provide a search term, --role, --since, or --last" >&2
	exit 2
fi

where_clause=""
if [[ ${#where[@]} -gt 0 ]]; then
	where_clause="WHERE $(IFS=' AND '; echo "${where[*]}")"
fi

limit="${last:-200}"

# Per-surface transcript tables (voice/phone + any plugin-registered surface),
# discovered dynamically so this tool names no specific plugin. A surface table
# carries the ts_unix/kind/text/session_id schema and isn't a rollup/legacy table.
surface_tables=$(sqlite3 "$DB" "
  SELECT m.name FROM sqlite_master m
  WHERE m.type='table'
    AND m.name NOT IN ('sessions','session_events','conversation','tool_calls')
    AND (SELECT COUNT(*) FROM pragma_table_info(m.name) ti
         WHERE ti.name IN ('ts_unix','kind','text','session_id')) = 4;
")

union_sql=""
for t in $surface_tables; do
	[[ -n "$union_sql" ]] && union_sql="$union_sql UNION ALL "
	union_sql="$union_sql SELECT ts_unix, kind, text, session_id FROM $t"
done
if [[ -z "$union_sql" ]]; then
	echo "(no per-surface transcript tables found in $DB)"
	exit 0
fi

sqlite3 -separator $'\t' "$DB" "
SELECT datetime(ts_unix, 'unixepoch', 'localtime') AS ts,
       kind AS role,
       substr(text, 1, 200) AS text_preview
FROM (
  $union_sql
)
$where_clause
ORDER BY ts_unix DESC
LIMIT $limit;
"
