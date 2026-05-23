#!/usr/bin/env bash
# Catchup briefing — assemble "what was happening before this session started"
# from everything persisted to disk: session-state.md, recent conversation.log,
# open PRs, in-flight tasks/results, pending questions, recent voice/phone/
# discord activity, recent commits, build_log tail, health.
#
# Designed to run as the first action of a fresh Sutando session so the
# conversation buffer has context before the user types anything.
#
# Quiet on empty sections so output stays scannable.
set -u

REPO="${SUTANDO_REPO_DIR:-/Users/xueqingliu/Documents/sutando/sutando}"
WS="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
HOURS="${CATCHUP_HOURS:-3}"

print_section() { echo; echo "## $1"; echo; }
say() { echo "$@"; }

echo "# Catchup briefing — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
echo "Reconstructed from disk (last ${HOURS}h window where applicable). Issue #1032 — recall half."

# 1. Last session checkpoint
print_section "Last session checkpoint (session-state.md)"
if [ -f "$REPO/session-state.md" ]; then
  ts=$(grep -m1 -i '^timestamp:' "$REPO/session-state.md" 2>/dev/null | awk '{print $2}')
  say "Captured: ${ts:-unknown}"
  echo '```'
  sed -n '1,60p' "$REPO/session-state.md"
  echo '```'
elif [ -f "$WS/session-state.md" ]; then
  echo '```'
  sed -n '1,60p' "$WS/session-state.md"
  echo '```'
else
  say "(no session-state.md — last session may have exited without compacting; rely on logs below)"
fi

# 2. Open PRs (mine — the shared liususan091219 identity)
print_section "Open PRs on sonichi/sutando (liususan091219)"
gh pr list --repo sonichi/sutando --state open --author liususan091219 \
  --json number,title,updatedAt,reviewDecision \
  --jq '.[] | "  #\(.number) [\(.reviewDecision // "no-review")] \(.title) (updated \(.updatedAt[:10]))"' 2>/dev/null \
  || say "(gh not available or no PRs)"

# 3. In-flight tasks
print_section "In-flight tasks (workspace/tasks/)"
tasks=$(ls -lt "$WS/tasks/"task-*.txt 2>/dev/null | head -8 | awk '{print "  "$NF" ("$6" "$7" "$8")"}')
[ -n "$tasks" ] && echo "$tasks" || say "(none)"

# 4. Pending results (delivered or not)
print_section "Recent results (last $HOURS h)"
results=$(/usr/bin/find "$WS/results" -maxdepth 1 -name 'task-*.txt' -mmin -$((HOURS*60)) 2>/dev/null | head -6 | awk '{print "  "$0}')
[ -n "$results" ] && echo "$results" || say "(none)"

# 5. Pending questions — show only UN-resolved entries
print_section "Pending questions (un-resolved only)"
pq=""
[ -f "$REPO/pending-questions.md" ] && pq="$REPO/pending-questions.md"
[ -z "$pq" ] && [ -f "$WS/pending-questions.md" ] && pq="$WS/pending-questions.md"
if [ -n "$pq" ]; then
  # Split on '## ' headers, drop any section whose body contains a resolution
  # marker (✅, 'RESOLVED', or 'DONE') so old-but-archived items don't dominate.
  python3 <<PYEOF
import re
text = open("$pq").read()
sections = re.split(r'(?m)^(?=## )', text)
shown = 0
for s in sections:
    if not s.strip().startswith('## '): continue
    body = s
    if '✅' in body or 'RESOLVED' in body or '## Resolved' in body or 'DONE' in body or '## Dismissed' in body:
        continue
    print(body.rstrip())
    print()
    shown += 1
    if shown >= 5: break
if shown == 0:
    print("  (no un-resolved entries)")
PYEOF
else
  say "(no pending-questions.md found)"
fi

# 6. Recent voice/phone/discord activity (sqlite, last N h)
print_section "Recent voice/phone/discord activity (last $HOURS h)"
if [ -f "$WS/data/conversation.sqlite" ]; then
  sqlite3 -separator $'\t' "$WS/data/conversation.sqlite" "
    SELECT datetime(ts_unix,'unixepoch','localtime') AS time,
           'voice' AS surface, kind, substr(text,1,80) AS text
    FROM voice WHERE ts_unix > strftime('%s','now')-${HOURS}*3600
    UNION ALL
    SELECT datetime(ts_unix,'unixepoch','localtime'), 'phone', kind, substr(text,1,80)
    FROM phone WHERE ts_unix > strftime('%s','now')-${HOURS}*3600
    UNION ALL
    SELECT datetime(ts_unix,'unixepoch','localtime'), 'discord_voice', kind, substr(text,1,80)
    FROM discord_voice WHERE ts_unix > strftime('%s','now')-${HOURS}*3600
    ORDER BY 1 DESC LIMIT 20;
  " 2>/dev/null | awk -F'\t' '{printf "  [%s] %-13s %-10s %s\n", $1, $2, $3, $4}' \
    || say "(sqlite query failed)"
else
  say "(no conversation.sqlite)"
fi

# 7. Recent conversation.log (channel-bearing chat lines, last N h)
print_section "Recent chat (logs/conversation.log, last $HOURS h)"
if [ -f "$WS/logs/conversation.log" ]; then
  # filter to entries within window (rough: keep last 200 lines, then python-filter)
  tail -200 "$WS/logs/conversation.log" | python3 -c "
import sys, datetime as dt
cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=${HOURS})
for line in sys.stdin:
    parts = line.split('|', 2)
    if len(parts) < 3: continue
    try:
        t = dt.datetime.fromisoformat(parts[0].replace('Z','+00:00'))
    except: continue
    if t < cut: continue
    print('  ' + line.rstrip())" 2>/dev/null | tail -40 \
    || say "(parse failed)"
else
  say "(no conversation.log)"
fi

# 8. Recent commits across branches
print_section "Recent commits (this repo, last $HOURS h)"
git -C "$REPO" log --all --since="${HOURS} hours ago" \
  --pretty='  %h %ad  %s (%an, %D)' --date=format:'%m-%d %H:%M' 2>/dev/null \
  | head -25 \
  || say "(git not available)"

# 9. Build log — show the last 3 timestamped entries (## YYYY-MM-DDT…) regardless
#    of file age, plus a note if the most-recent entry is older than 24h.
print_section "build_log.md (last 3 entries)"
bl=""
[ -f "$REPO/build_log.md" ] && bl="$REPO/build_log.md"
[ -z "$bl" ] && [ -f "$WS/build_log.md" ] && bl="$WS/build_log.md"
if [ -n "$bl" ]; then
  python3 <<PYEOF
import re, os, time
text = open("$bl").read()
# Sections begin with '## ' (any header); pick those with a timestamp prefix.
sections = re.split(r'(?m)^(?=## )', text)
ts_sections = [s for s in sections if re.match(r'## \d{4}-\d{2}-\d{2}', s)]
last3 = ts_sections[-3:]
for s in last3:
    body = s.rstrip()
    # truncate any single section over 60 lines to keep briefing scannable
    lines = body.splitlines()
    if len(lines) > 60:
        body = "\n".join(lines[:60]) + "\n  ... (truncated, $bl has more)"
    print(body)
    print()
# Staleness note
mtime = os.path.getmtime("$bl")
age_h = (time.time() - mtime) / 3600
if age_h > 24:
    print(f"  ⚠ build_log.md last updated {age_h:.0f}h ago — proactive-loop may not be appending")
PYEOF
else
  say "(no build_log.md)"
fi

# 10. Health one-liner — call by file presence (script may not be +x)
print_section "Health"
if [ -f "$REPO/src/health-check.py" ]; then
  health=$(python3 "$REPO/src/health-check.py" 2>/dev/null | grep -E '✓|⚠|✗' | head -10)
  [ -n "$health" ] && echo "$health" || say "(health-check returned no ✓/⚠/✗ lines)"
else
  say "(health-check.py not found at $REPO/src/health-check.py)"
fi

echo
echo "---"
echo "End of catchup briefing. Treat above as recovered context for the new session."
