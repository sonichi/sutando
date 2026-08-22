#!/bin/bash
# Session handoff — writes a summary for the next session to pick up.
# Called by PreCompact hook so context survives session restarts.
#
# Reads the transcript, extracts key signals, and writes to
# <workspace>/session-state.md. The incoming session reads this in CLAUDE.md
# or as part of the proactive loop.

# Ephemeral Team children do not own the live core's continuity snapshot.
[ "${SUTANDO_TEAM_RUNTIME:-}" = "1" ] && exit 0

# REPO resolves to: (1) $SUTANDO_REPO_DIR if set AND valid, (2) auto-detect
# from the script's own resolved location (symlink-safe), (3) common layout
# probes — each validated by _repo_ok. If nothing validates, the script exits
# loudly rather than trusting an unvalidated default (a bad REPO produces empty
# REPO-rooted output, the exact failure this guards against). SUTANDO_WORKSPACE
# intentionally NOT in the fallback (CLAUDE.md reserves it for the workspace
# dir; using it as a REPO alias would silently pick the wrong path).
#
# A set-but-stale SUTANDO_REPO_DIR is a real failure mode: long-lived parents
# (tmux, launchd, Sutando.app) cache the env across a repo move, so a fresh
# session after relocating the checkout gets empty REPO-rooted output (commits,
# health, session-state). Validate before trusting. (-e not -d for .git:
# submodule/worktree checkouts have a file, not a directory, at .git.)
# App-bundled engine checkouts ship WITHOUT .git, so requiring it rejected every
# candidate and no handoff ran there at all (#2756). src/ is the alternate
# checkout signal; CLAUDE.md + skills/ still carry the identification.
_repo_ok() { [ -f "$1/CLAUDE.md" ] && [ -d "$1/skills" ] && { [ -e "$1/.git" ] || [ -d "$1/src" ]; }; }
__SCRIPT_PARENT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P || echo "")"
if [ -n "${SUTANDO_REPO_DIR:-}" ] && _repo_ok "$SUTANDO_REPO_DIR"; then
    REPO="$SUTANDO_REPO_DIR"
else
    if [ -n "${SUTANDO_REPO_DIR:-}" ]; then
        echo "⚠ SUTANDO_REPO_DIR=\`$SUTANDO_REPO_DIR\` is not a valid Sutando checkout (stale after a repo move?) — probing instead." >&2
    fi
    REPO=""
    for _cand in "$__SCRIPT_PARENT" "$HOME/Desktop/sutando" "$HOME/Documents/sutando/sutando" "$HOME/Documents/sutando" "$HOME/sutando" "$(pwd)"; do
        if [ -n "$_cand" ] && _repo_ok "$_cand"; then
            REPO="$_cand"; break
        fi
    done
    # No validated candidate found. Do NOT fall back to an unvalidated default
    # (that just reintroduces the empty-REPO failure mode this script guards
    # against). Fail loud so the caller sees why the handoff was skipped.
    if [ -z "$REPO" ]; then
        echo "✗ session-handoff: could not locate a valid Sutando checkout (no candidate passed _repo_ok). Set SUTANDO_REPO_DIR to a valid checkout." >&2
        exit 1
    fi
fi
export PATH="/opt/homebrew/bin:$HOME/.nvm/versions/node/v24.14.1/bin:$PATH"
TRANSCRIPT="$1"  # Optional explicit path (manual invocations)
# Claude Code hooks pass transcript_path via stdin JSON ONLY — there is no
# $TRANSCRIPT_PATH env var, so on a stock hook config $1 expands empty and the
# conversation section silently degraded (john's #1909 review). Parse stdin
# when it's piped ([ ! -t 0 ]); interactive/manual runs skip this and either
# pass $1 or fall through to --latest at the extraction site below.
# NOTE (rebase over #2077): the pre-rebase branch also set
# STATE_FILE="$REPO/session-state.md" here — dropped; STATE_FILE is now
# derived from the resolved workspace below, per the workspace contract.
if [ -z "$TRANSCRIPT" ] && [ ! -t 0 ]; then
  TRANSCRIPT="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path") or "")' 2>/dev/null || true)"
fi

# Workspace resolves via the shared post-M0 helper (src/workspace_resolve.sh).
# Exports $WORKSPACE on success; exits non-zero with a diagnostic on failure
# (including empty-string returns — important because this script does NOT
# use `set -e`). See workspace-revamp M0 (PR #1395) + Mini's PR #1399 review.
# Defensive fallback for non-checkout installs where the helper isn't present.
# Helper resolution: prefer $REPO/src/, fall back to script-sibling (cross-
# checkout safety). Critical for session-handoff specifically because its
# REPO resolution above prefers $SUTANDO_REPO_DIR over script-parent, and
# that env can point to a sibling checkout (e.g. sutando-plus submodule pin)
# that hasn't yet pulled this newly-added file. Caught by E2E pass against
# PR #1399.
__HELPER="$REPO/src/workspace_resolve.sh"
[ -f "$__HELPER" ] || __HELPER="$(cd "$(dirname "$0")" && pwd)/workspace_resolve.sh"
if [ -f "$__HELPER" ]; then
  # shellcheck source=workspace_resolve.sh
  source "$__HELPER"
  resolve_workspace_or_die
else
  # Post-v0.8 (#1440 + Mini opinion-requested 2026-06-06): no env-var
  # fallback. `$SUTANDO_WORKSPACE` is no longer honored for workspace
  # resolution; if the M0 helper isn't reachable, session-handoff can't
  # save meaningful state. Fail loud rather than risk writing
  # session-state.md to the wrong workspace.
  echo "session-handoff: cannot resolve workspace — workspace_resolve.sh not found at \$REPO/src/ or alongside this script. Verify the sutando checkout has the M0 helper." >&2
  exit 1
fi
unset __HELPER
if [ -z "${WORKSPACE:-}" ]; then
  echo "session-handoff: workspace resolved to empty string. Refusing to derive paths under /." >&2
  exit 1
fi
WORKSPACE_DIR="$WORKSPACE"  # historical local name retained for the rest of this file

# session-state.md is per-user mutable state — workspace contract says it
# lives under <workspace>/, not the repo root. Writing to $REPO/ left the
# workspace copy permanently stale and re-tripped the legacy-state detector
# after every compaction (sutando-migrate classifies it newest-mtime).
STATE_FILE="$WORKSPACE_DIR/session-state.md"
# Staged beside the destination so the publish is a same-filesystem rename.
STATE_TMP="$(mktemp "${STATE_FILE}.tmp.XXXXXX" 2>/dev/null)" || STATE_TMP="${STATE_FILE}.tmp.$$"
# An interrupted hook must not leave its stage behind. Opt-out, not blanket rm:
# the publish-failure path below keeps the stage on purpose.
_handoff_keep_stage=0
_handoff_drop_stage() {
  [ "$_handoff_keep_stage" = 1 ] || rm -f "$STATE_TMP" 2>/dev/null
}
# A signal trap REPLACES the default action, so cleaning up and returning would
# let a cancelled hook run on; restore the default and re-raise to die correctly.
trap '_handoff_drop_stage' EXIT
trap '_handoff_drop_stage; trap - INT;  kill -INT  $$' INT
trap '_handoff_drop_stage; trap - TERM; kill -TERM $$' TERM
# Written last inside the capture block; the publish gate tests for it.
CAPTURE_END_MARKER="<!-- session-handoff: capture complete -->"
# A prior run killed before its rename leaves a stage behind; it is not state.
find "$(dirname "$STATE_FILE")" -maxdepth 1 -name "$(basename "$STATE_FILE").tmp.*" \
     ! -name "$(basename "$STATE_TMP")" -mmin +60 -delete 2>/dev/null || true

# JSON-escape one value. host/transcript/trigger are external input, and any
# raw control char or quote makes the whole line unparseable to every reader.
_ch_json_escape() {
    local s=${1//\\/\\\\}
    s=${s//\"/\\\"}
    printf '%s' "${s//[[:cntrl:]]/ }"
}

record_compaction_event() {
    local log="$WORKSPACE_DIR/state/compactions.jsonl" ts line lock tmp pend wip i=0 acquired=0
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$(dirname "$log")" 2>/dev/null || return 0
    line="$(printf '{"ts":"%s","epoch":%s,"host":"%s","transcript":"%s","trigger":"%s"}' \
        "$ts" "$(date +%s)" \
        "$(_ch_json_escape "${SUTANDO_HOST_LABEL:-$(hostname -s 2>/dev/null)}")" \
        "$(_ch_json_escape "$(basename "${1:-}" 2>/dev/null)")" \
        "$(_ch_json_escape "${2:-precompact}")")"
    # Trim-then-append is read-modify-write on one shared file, so overlapping
    # hooks drop events with no malformed line to show for it. One writer at a time.
    lock="$log.lock"
    while [ "$i" -lt 100 ]; do
        # Never block a PreCompact hook forever: a dead holder must not wedge it.
        if mkdir "$lock" 2>/dev/null; then acquired=1; break; fi
        i=$((i + 1))
        sleep 0.05
    done
    # The trim REPLACES the pathname, so an unlocked append lands in the old
    # inode and the mv discards it. Nothing may touch "$log" without the lock.
    if [ "$acquired" != 1 ]; then
        # Each give-up caller owns its OWN sidecar, published by rename. A shared
        # pathname loses records: an fd already opened on it follows the inode
        # through the holder's mv, so the write lands in a file already drained.
        wip="$(mktemp "${log}.wip.XXXXXX" 2>/dev/null)" || return 0
        if printf '%s\n' "$line" > "$wip" 2>/dev/null; then
            mv "$wip" "${log}.pending.${wip##*.}" 2>/dev/null || rm -f "$wip" 2>/dev/null
        else
            rm -f "$wip" 2>/dev/null
        fi
        return 0
    fi
    # A pre-fix writer, or one mid-upgrade, may have parked at the legacy shared
    # pathname. Absorb it too or an upgrade loses the very records this protects.
    if [ -f "$log.pending" ]; then
        pend="$(mktemp "${log}.pending.XXXXXX" 2>/dev/null)" || pend="${log}.pending.$$"
        mv "$log.pending" "$pend" 2>/dev/null || rm -f "$pend" 2>/dev/null
    fi
    # Absorb anything earlier give-up callers parked. Only completed sidecars are
    # named .pending.* — a writer builds in .wip.* and renames, so we never read a
    # partial line and never hold a pathname another writer still has open.
    for pend in "${log}".pending.*; do
        [ -e "$pend" ] || continue
        cat "$pend" >> "$log" 2>/dev/null || true
        rm -f "$pend" 2>/dev/null
    done
    if [ -f "$log" ] && [ "$(wc -l < "$log" 2>/dev/null || echo 0)" -ge 500 ]; then
        tmp="$(mktemp "${log}.tmp.XXXXXX" 2>/dev/null)" || tmp="${log}.tmp.$$"
        tail -n 499 "$log" > "$tmp" 2>/dev/null && mv "$tmp" "$log" 2>/dev/null
        rm -f "$tmp" 2>/dev/null
    fi
    printf '%s\n' "$line" >> "$log" 2>/dev/null || true
    rmdir "$lock" 2>/dev/null
    return 0
}
record_compaction_event "${TRANSCRIPT:-}" "${SUTANDO_HANDOFF_TRIGGER:-precompact}"

# Build state from available signals
{
  echo "---"
  echo "# Session State (auto-generated on compaction)"
  echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "---"
  echo ""

  # What's running
  echo "## System Status"
  # Status glyphs are assigned in src/health-check.py: ✓ ok · ⚠ warn · ✗ down /
  # missing / not_loaded · ♻ stale · ~ any other status. The previous pattern
  # listed only ✓⚠✗, so a stale or unrecognised check could not appear here at
  # all — and `~` is the catch-all for states nobody enumerated, which is
  # exactly the set most worth carrying into the next session.
  #
  # `head -15` then truncated a 29-check run to 15, dropping non-ok lines purely
  # by position. Both together meant this section could report an all-✓ system
  # while a check was failing. Non-ok lines are now uncapped; ok lines are
  # summarised as a count, since the successor needs the failures, not the roll.
  _hc=$(python3 "$REPO/src/health-check.py" 2>/dev/null)
  if [ -z "$_hc" ]; then
    echo "(health-check produced no output — status UNKNOWN, not healthy)"
  else
    printf '%s\n' "$_hc" | grep -E '^ *[⚠✗♻~] ' || true
    printf '%s\n' "$_hc" | grep -cE '^ *✓ ' | awk '{print "  (" $1 " checks ok)"}'
  fi
  unset _hc
  echo ""

  # Recent git activity (what was built)
  echo "## Recent Work (last 10 commits)"
  git -C "$REPO" log --oneline -10 2>/dev/null
  echo ""

  # Open PRs
  echo "## Open PRs"
  gh pr list --repo sonichi/sutando --state open --limit 5 2>/dev/null || echo "(couldn't fetch)"
  echo ""

  # Pending questions — per-host canonical home is <workspace>/hosts/<hostname>/
  # (post-#1717). personal_path() must receive the workspace root (WORKSPACE_DIR),
  # not REPO — passing REPO caused it to probe <repo>/hosts/<host>/ which doesn't
  # exist and fall back to the non-existent <repo>/pending-questions.md, silently
  # dropping the section from every session-state.md. Fallback echo uses
  # WORKSPACE_DIR for the same reason.
  PQ_PATH=$(SUTANDO_MEMORY_DIR="${SUTANDO_MEMORY_DIR:-}" SUTANDO_PRIVATE_DIR="${SUTANDO_PRIVATE_DIR:-}" python3 -c "
import sys; sys.path.insert(0, '$REPO/src')
from util_paths import personal_path
from pathlib import Path
print(personal_path('pending-questions.md', Path('$WORKSPACE_DIR')))
" 2>/dev/null || echo "$WORKSPACE_DIR/hosts/${SUTANDO_HOST_LABEL:-${SUTANDO_HOST_OVERRIDE:-$(scutil --get LocalHostName 2>/dev/null | grep . || hostname | sed 's/\..*//')}}/pending-questions.md")
  # Extract via the canonical parser (src/check-pending-questions.py) instead of
  # a second, weaker pattern here. Its `^## ` section split matches all three
  # heading formats in use — legacy `## Q1 — Title`, `## Title` + `**Status:**`,
  # and the free-form dated headings the proactive loop actually writes. The
  # previous `grep "^## Q"` matched only the legacy shape, so on a file using
  # either newer format the section rendered EMPTY.
  #
  # Empty is the dangerous part: it reads as "nothing pending" rather than "not
  # parsed", so the successor session is told there is nothing waiting on the
  # owner. Note this is the SECOND fix to this same section — the comment above
  # records a path-resolution fix, and repairing the path is what turned an
  # honest "None" (file not found) into a silent "" (found, matched nothing).
  # Hence the explicit parse-failure branch below: a broken extractor must never
  # be indistinguishable from an empty queue.
  echo "## Pending Questions"
  if [ -f "$PQ_PATH" ]; then
    pq_out=$(python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('cpq', '$REPO/src/check-pending-questions.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
qs = m.get_waiting_questions()
print('\n'.join('- ' + q['title'] for q in qs[:20]) if qs else 'None')
" 2>/dev/null)
    if [ -n "$pq_out" ]; then
      echo "$pq_out"
    else
      # Name both inputs so the failure is reproducible by hand. stderr is
      # dropped above to match the idiom of the sibling extractors in this
      # function, which makes this line the only handle an operator gets.
      echo "(could not parse $PQ_PATH via $REPO/src/check-pending-questions.py — section unavailable, NOT necessarily empty)"
    fi
  else
    echo "None"
  fi
  echo ""

  # Tasks in flight
  echo "## Tasks"
  # `ls … | head -5 || echo` never reached the fallback: `||` binds to the LAST
  # command of a pipeline, and `head` exits 0 on empty input however `ls` fared.
  # So "None pending" was unreachable and both "no tasks" and "the directory is
  # gone" rendered as an empty section.
  _tasks=$(ls "$WORKSPACE_DIR/tasks/"*.txt 2>/dev/null | head -5)
  if [ -n "$_tasks" ]; then
    printf '%s\n' "$_tasks"
  elif [ -d "$WORKSPACE_DIR/tasks" ]; then
    echo "None pending"
  else
    echo "(no tasks dir at $WORKSPACE_DIR/tasks — queue state UNKNOWN)"
  fi
  unset _tasks
  echo ""

  # Recent conversation — the PreCompact hook hands us $TRANSCRIPT but until
  # now nothing used it: conversation content died on every compaction and
  # only system status survived into the next session.
  echo "## Recent Conversation (before compaction)"
  if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    python3 "$REPO/src/context_resume.py" "$TRANSCRIPT" --turns 12 --chars 6000 2>/dev/null \
      || echo "(extraction failed — transcript at $TRANSCRIPT)"
  else
    # No exact path (manual run, or stdin JSON unavailable) — fall back to the
    # newest transcript for this project; context_resume ships --latest for
    # exactly this shape. Still fail-open: a one-line note, never a hard stop.
    python3 "$REPO/src/context_resume.py" --latest --turns 12 --chars 6000 2>/dev/null \
      || echo "(no transcript available — hook stdin empty and --latest found none)"
  fi
  echo ""

  # Quota (with reset times)
  echo "## Quota"
  # Quota state is per-user runtime state — canonical home is
  # <workspace>/state/quota-state.json (written by the credential proxy).
  # Reading an in-repo copy would pick up a stale shadow (see PR #970).
  QUOTA_FILE="$WORKSPACE_DIR/state/quota-state.json"
  if [ -f "$QUOTA_FILE" ]; then
    python3 -c "
import json
from datetime import datetime
d=json.load(open('$QUOTA_FILE'))
now=datetime.now()
r5=datetime.fromtimestamp(int(d['headers']['anthropic-ratelimit-unified-5h-reset']))
m5=int((r5-now).total_seconds()/60)
print(f'5h: {d[\"utilization_5h\"]:.0%} (resets in {m5}min at {r5.strftime(\"%I:%M %p\")}), 7d: {d[\"utilization_7d\"]:.0%}')
" 2>/dev/null
  fi
  echo ""

  # Stars
  echo "## Repo Stats"
  # Same unreachable-fallback shape as the tasks section: `||` bound to `awk`,
  # which exits 0 with no input records, so "(couldn't fetch)" never printed and
  # a failed API call rendered as an empty line.
  _stats=$(gh api repos/sonichi/sutando --jq '.stargazers_count, .forks_count' 2>/dev/null | tr '\n' ' ')
  if [ -n "${_stats// /}" ]; then
    printf '%s\n' "$_stats" | awk '{print $1 " stars, " $2 " forks"}'
  else
    echo "(couldn't fetch)"
  fi
  unset _stats

  # Relay notes — drain any unprocessed workspace/relay/*.md files written by
  # the proactive-loop (step 7) or /relay skill. These carry cross-session
  # narrative continuity that git log + build_log don't capture. Include them
  # here so the next session reads them as part of session-state.md, then
  # archive each one to relay/processed/ (mirrors catchup-after-startup's
  # original drain pattern — fixes issue #1738 where #1737 removed the only
  # consumer).
  # Capture (don't move yet). Retire the notes to processed/ only AFTER the
  # write to $STATE_FILE is confirmed below — otherwise an interrupt between
  # the mv and a durable write would retire a note that was never captured,
  # losing that context for the next session (issue #1738 reviewer note).
  RELAY_DIR="$WORKSPACE_DIR/relay"
  RELAY_PROCESSED="$RELAY_DIR/processed"
  unprocessed_relay=$(find "$RELAY_DIR" -maxdepth 1 -name 'relay-*.md' 2>/dev/null | sort)
  if [ -n "$unprocessed_relay" ]; then
    echo ""
    echo "## Relay Notes (from prior sessions)"
    while IFS= read -r relay_file; do
      echo ""
      echo "### $(basename "$relay_file")"
      cat "$relay_file"
    done <<< "$unprocessed_relay"
  fi

  # Terminal sentinel: gating on a SECTION pins a token, not a position, so
  # any section added after it silently narrows the gate. This is emitted last.
  echo ""
  echo "$CAPTURE_END_MARKER"
} > "$STATE_TMP" 2>/dev/null

# A complete-looking stage does not make the rename succeed; gate on both. The
# marker is the LAST line written, so a stage truncated anywhere fails here.
if [ ! -s "$STATE_TMP" ] || [ "$(tail -n 1 "$STATE_TMP" 2>/dev/null)" != "$CAPTURE_END_MARKER" ]; then
  rm -f "$STATE_TMP" 2>/dev/null
  echo "session-handoff: capture incomplete — kept the previous $STATE_FILE" >&2
  exit 1
fi
if ! mv "$STATE_TMP" "$STATE_FILE" 2>/dev/null; then
  # Stage is KEPT: it is the only copy of a capture that did complete, and the
  # destination still holds the last good snapshot.
  _handoff_keep_stage=1
  echo "session-handoff: publish failed — $STATE_FILE unchanged, capture kept at $STATE_TMP" >&2
  exit 1
fi
echo "Session state saved to $STATE_FILE"

# Retire relay notes to processed/ only now that session-state.md has been
# written. Confirm each note's content actually landed in $STATE_FILE (its
# header line is present) before moving it — if the capture failed or was
# interrupted, leave the note in place for the next run to retry.
if [ -n "$unprocessed_relay" ] && [ -s "$STATE_FILE" ]; then
  mkdir -p "$RELAY_PROCESSED"
  while IFS= read -r relay_file; do
    [ -n "$relay_file" ] || continue
    if grep -qF "### $(basename "$relay_file")" "$STATE_FILE" 2>/dev/null; then
      mv "$relay_file" "$RELAY_PROCESSED/" 2>/dev/null
    fi
  done <<< "$unprocessed_relay"
fi
