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
set -o pipefail   # so `cmd | grep ... || say "(none)"` actually fires when cmd fails

# REPO: env wins, else probe common layouts for a sutando checkout
# (CLAUDE.md + skills/ + .git signature, same heuristic session-handoff.sh
# uses), else fall back to the convention path. Probing means a checkout
# at $HOME/Desktop/sutando OR $HOME/Documents/sutando/sutando OR $(pwd)
# all Just Work without per-user env. (Was a hardcoded /Users/xueqingliu/...
# path pre-review; fixed per qingyun-wu + Mini's #1056 review.)
if [ -n "${SUTANDO_REPO_DIR:-}" ]; then
  REPO="$SUTANDO_REPO_DIR"
else
  REPO=""
  for _cand in "$HOME/Desktop/sutando" "$HOME/Documents/sutando/sutando" "$HOME/Documents/sutando" "$HOME/sutando" "$(pwd)"; do
    # -e not -d: in a submodule/worktree checkout `.git` is a file (gitdir
    # pointer), not a directory, so -d would reject an otherwise-valid repo.
    if [ -f "$_cand/CLAUDE.md" ] && [ -d "$_cand/skills" ] && [ -e "$_cand/.git" ]; then
      REPO="$_cand"; break
    fi
  done
  REPO="${REPO:-$HOME/Desktop/sutando}"   # falls through to the conventional path if probe finds nothing
fi
WS="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
# Hours window: positional arg wins (so /catchup-after-startup 12 works as
# documented), env var second, default 3h. Pre-review the script only read
# CATCHUP_HOURS — the documented `[hours]` arg was silently ignored (Mini #1).
HOURS="${1:-${CATCHUP_HOURS:-3}}"

print_section() { echo; echo "## $1"; echo; }
say() { echo "$@"; }

echo "# Catchup briefing — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
# REPO sanity: misconfigured REPO would silently empty out half the
# briefing (session-state.md / build_log.md / git log / health), which the
# skill's empty-vs-failed contract should make visible. Loud one-liner.
if [ ! -d "$REPO" ]; then
  echo
  echo "⚠ REPO not found at \`$REPO\` — REPO-rooted sections will be empty/unhelpful."
  echo "   Set SUTANDO_REPO_DIR to your Sutando checkout to fix."
fi
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

# 1b. Previous-session transcript (Claude Code project .jsonl)
#
# session-state.md is a 50-line summary written by session-handoff.sh, which
# only fires if the PreCompact / SessionStop hooks are wired AND succeed.
# Both have failed in practice (Mac Studio 2026-05-23 incident: hook
# pointed at a non-existent default path → session-state.md never written
# → catchup's most useful section was empty for weeks). The Claude Code
# project transcript .jsonl, by contrast, is ALWAYS there (Claude Code
# writes it natively, no hooks needed) and is richer (full user/assistant
# turns vs a summary). When session-state.md is missing or stale, this is
# the source of truth for "what was happening last session".
print_section "Previous-session transcript (last $HOURS h)"
proj_slug=$(echo "$REPO" | sed 's|/|-|g')
proj_dir="$HOME/.claude/projects/$proj_slug"
if [ -d "$proj_dir" ]; then
  # Most-recent .jsonl untouched in the last minute — skips the CURRENT
  # session's transcript (which the live process is still writing to).
  # BSD `find -mmin` requires integers; fractional silently matches zero
  # on macOS, so +1 is the tightest portable window.
  prev_jsonl=$(/usr/bin/find "$proj_dir" -name "*.jsonl" -mmin +1 -size +0c 2>/dev/null | xargs -I{} stat -f "%m %N" {} 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  if [ -n "$prev_jsonl" ]; then
    say "Source: $(basename "$prev_jsonl")"
    python3 <<PYEOF
import json, datetime as dt
cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=${HOURS})
turns = []
with open("$prev_jsonl") as f:
    for line in f:
        try: d = json.loads(line)
        except: continue
        ts_s = d.get("timestamp")
        if not ts_s: continue
        try:
            ts = dt.datetime.fromisoformat(ts_s.replace("Z","+00:00"))
        except: continue
        if ts < cut: continue
        role = d.get("type", "")
        if role not in ("user", "assistant"): continue
        msg = d.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
            content = " ".join(parts)
        content = str(content).strip()
        if not content: continue
        # Skip system reminders, task notifications, tool-result envelopes,
        # interrupt markers — they're noise, not human turns.
        if content.startswith("<"): continue
        if content.startswith("[Request interrupted"): continue
        if content.startswith("[{"): continue
        turns.append((ts.strftime("%H:%M"), "user" if role=="user" else "asst", content))
# Keep the LAST N turns in the window — those are most relevant to "what
# was happening just before this session".
for t, r, c in turns[-25:]:
    snippet = c[:240].replace("\n", " ")
    print(f"  {t} {r:4s}| {snippet}")
if not turns:
    print("  (no user/assistant turns in window)")
PYEOF
  else
    say "(no previous-session .jsonl in $proj_dir — fresh checkout?)"
  fi
else
  say "(no project transcript dir at $proj_dir)"
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
  # Filter: skip sections whose body explicitly marks resolution. Pre-review
  # this matched bare substrings 'DONE' and 'RESOLVED' anywhere in prose —
  # which over-matched (e.g. "this is not done yet" → dropped, qingyun #4).
  # Now require an anchored marker: header containing ✅/Resolved/Dismissed,
  # or a leading ✅ at the start of any body line.
  python3 <<PYEOF
import re
text = open("$pq").read()
sections = re.split(r'(?m)^(?=## )', text)
shown = 0
for s in sections:
    if not s.strip().startswith('## '): continue
    lines = s.splitlines()
    header = lines[0] if lines else ''
    # Header-level resolution markers
    if re.search(r'(✅|Resolved|Dismissed|DONE)', header):
        continue
    # Body-level ✅ at line start = explicit resolution marker
    if any(re.match(r'\s*✅', ln) for ln in lines[1:]):
        continue
    print(s.rstrip())
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
# Requires #1051's per-surface tables (voice/phone/discord_voice). On a db
# that pre-dates #1051 the query returns "(sqlite query failed)" — that's
# the expected signal that the operator's conversation-store is older.
print_section "Recent voice/phone/discord activity (last $HOURS h)"
if [ -f "$WS/data/conversation.sqlite" ]; then
  # Mini #2: pre-fix the inline `cmd | awk || say` pattern silently emitted
  # nothing on cmd failure because pipefail wasn't on; we set it now AND
  # capture into a variable so an empty-result-but-success case is distinct
  # from a failure case.
  sql_rows=$(sqlite3 -separator $'\t' "$WS/data/conversation.sqlite" "
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
  " 2>&1)
  sql_rc=$?
  if [ $sql_rc -ne 0 ]; then
    say "(sqlite query failed — db schema may pre-date #1051: $sql_rows)"
  elif [ -z "$sql_rows" ]; then
    say "(none)"
  else
    echo "$sql_rows" | awk -F'\t' '{printf "  [%s] %-13s %-10s %s\n", $1, $2, $3, $4}'
  fi
else
  say "(no conversation.sqlite)"
fi

# 7. Recent conversation.log (channel-bearing chat lines, last N h)
# With pipefail on, the whole pipeline exits non-zero if python fails;
# capturing into a variable + checking $? after the assignment is the
# cleanest way to distinguish empty-window from parse-failure under set -u
# (PIPESTATUS array isn't reliably present in command-substitution subshells).
print_section "Recent chat (logs/conversation.log, last $HOURS h)"
if [ -f "$WS/logs/conversation.log" ]; then
  log_rows=$(tail -200 "$WS/logs/conversation.log" | python3 -c "
import sys, datetime as dt
cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=${HOURS})
for line in sys.stdin:
    parts = line.split('|', 2)
    if len(parts) < 3: continue
    try:
        t = dt.datetime.fromisoformat(parts[0].replace('Z','+00:00'))
    except: continue
    if t < cut: continue
    print('  ' + line.rstrip())" 2>&1 | tail -40) || log_rows="__FAIL__"
  if [ "$log_rows" = "__FAIL__" ]; then
    say "(parse failed)"
  elif [ -z "$log_rows" ]; then
    say "(none)"
  else
    echo "$log_rows"
  fi
else
  say "(no conversation.log)"
fi

# 8. Recent commits across branches
print_section "Recent commits (this repo, last $HOURS h)"
# -e not -d: a submodule/worktree checkout has `.git` as a file, not a dir.
if [ -e "$REPO/.git" ]; then
  # Capture first, trim second. Piping `git log` straight into `head` lets
  # head close the pipe after 25 lines, which can SIGPIPE git (exit 141) and,
  # under `set -o pipefail`, trip a false "(git failed)" — a flaky race that
  # bites whenever the window holds >25 commits. Capturing the full output in
  # the `if` keeps git's real exit status as the only failure signal; the
  # later head runs on a plain string and cannot affect it.
  if git_rows=$(git -C "$REPO" log --all --since="${HOURS} hours ago" \
      --pretty='  %h %ad  %s (%an, %D)' --date=format:'%m-%d %H:%M' 2>&1); then
    git_rows=$(printf '%s\n' "$git_rows" | head -25)
    if [ -z "$git_rows" ]; then
      say "(none)"
    else
      echo "$git_rows"
    fi
  else
    say "(git failed)"
  fi
else
  say "(no .git at $REPO)"
fi

# 9. Build log — show the last 3 timestamped entries (## YYYY-MM-DDT…) regardless
#    of file age, plus a note if the most-recent entry is older than 24h.
print_section "build_log.md (last 3 entries)"
bl=""
[ -f "$WS/build_log.md" ] && bl="$WS/build_log.md"
[ -z "$bl" ] && [ -f "$REPO/build_log.md" ] && bl="$REPO/build_log.md"
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
