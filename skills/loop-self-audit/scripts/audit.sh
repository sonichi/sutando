#!/usr/bin/env bash
# Loop self-audit. Reads build_log.md decision lines, reports patterns.
# Usage: bash skills/loop-self-audit/scripts/audit.sh [N]
#   N = window size (default 50 most recent decision lines)

set -euo pipefail

REPO="${SUTANDO_WORKSPACE:-$HOME/Desktop/sutando}"
BUILD_LOG="$REPO/build_log.md"
N="${1:-50}"
TODAY="$(date -u +%Y-%m-%d)"
OUT="$REPO/notes/loop-self-audit-$TODAY.md"

if [[ ! -f "$BUILD_LOG" ]]; then
  echo "build_log.md not found at $BUILD_LOG" >&2
  exit 1
fi

# Extract last N decision lines.
# Format expected: chose: <action> — category: <CAT> — reason: <text>
DECISION_LINES="$(grep "^chose:" "$BUILD_LOG" | tail -n "$N" || true)"

if [[ -z "$DECISION_LINES" ]]; then
  echo "no decision lines found in $BUILD_LOG (looking for ^chose:)" >&2
  exit 0
fi

TOTAL="$(echo "$DECISION_LINES" | wc -l | tr -d ' ')"

# Category distribution.
# Lines without explicit "category:" tag are pre-2506 — bucket as UNTAGGED.
CATEGORIES="$(echo "$DECISION_LINES" \
  | sed -nE 's/.*— category: ([^ —]+).*/\1/p' \
  | tr -d ' ' \
  || true)"
UNTAGGED="$(echo "$DECISION_LINES" | grep -cv "category:" || true)"
DIST="$(echo "$CATEGORIES" | sort | uniq -c | sort -rn || true)"

# 3-same-section detection: rolling window of 3.
# Skip UNTAGGED (pre-pass-2506 entries) — they're not real category runs.
THREESAME=""
PREV1=""
PREV2=""
while IFS= read -r line; do
  CAT="$(echo "$line" | sed -nE 's/.*— category: ([^ —]+).*/\1/p' | tr -d ' ')"
  [[ -z "$CAT" ]] && { PREV2=""; PREV1=""; continue; }  # UNTAGGED breaks the run, doesn't form one
  if [[ -n "$PREV1" && -n "$PREV2" && "$CAT" == "$PREV1" && "$PREV1" == "$PREV2" ]]; then
    THREESAME="$THREESAME$CAT (line: $line)\n"
  fi
  PREV2="$PREV1"
  PREV1="$CAT"
done <<< "$DECISION_LINES"

# Idle/wait rate. Match by category only — `chose: idle-*` action names (e.g.
# `chose: idle-rate-threshold-confirm`) on a non-WAITING category should NOT
# count as idle. Caught 2026-04-27 pass 2618 when a MAINTENANCE entry titled
# `chose: idle-rate-threshold-confirm` was wrongly counted as idle.
IDLE_COUNT="$(echo "$DECISION_LINES" | grep -cE "category: (WAITING|idle)" || true)"
IDLE_PCT=$(( IDLE_COUNT * 100 / TOTAL ))

# Repeated-reason detection: identical reason text across 3+ passes.
REPEAT_REASON="$(echo "$DECISION_LINES" \
  | sed -nE 's/.*— reason: (.*)$/\1/p' \
  | sort | uniq -c | sort -rn | awk '$1 >= 3 {print}' || true)"

# Compose report.
{
  echo "---"
  echo "title: Loop self-audit — $TODAY"
  echo "date: $TODAY"
  echo "tags: [audit, loop, self-improvement]"
  echo "---"
  echo
  echo "# Loop self-audit (window N=$TOTAL most recent decision lines)"
  echo
  echo "## Category distribution"
  echo
  echo '```'
  echo "$DIST"
  if [[ "$UNTAGGED" -gt 0 ]]; then
    echo "  $UNTAGGED UNTAGGED (decision lines without 'category:' field, pre-pass-2506)"
  fi
  echo '```'
  echo
  echo "## Idle/wait rate"
  echo
  echo "$IDLE_COUNT / $TOTAL passes ($IDLE_PCT%)"
  if [[ "$IDLE_PCT" -gt 20 ]]; then
    echo
    echo "⚠️ Above 20% threshold."
  fi
  echo
  echo "## 3-same-category windows"
  echo
  if [[ -z "$THREESAME" ]]; then
    echo "None detected."
  else
    echo '```'
    echo -e "$THREESAME"
    echo '```'
    echo
    echo "⚠️ Rule-2 (forced pivot) violations."
  fi
  echo
  echo "## Repeated-reason text (3+ passes)"
  echo
  if [[ -z "$REPEAT_REASON" ]]; then
    echo "None detected."
  else
    echo '```'
    echo "$REPEAT_REASON"
    echo '```'
    echo
    echo "⚠️ Lazy-reasoning signal."
  fi
} > "$OUT"

echo "Wrote $OUT"

# Anomaly summary to stdout. If any threshold crossed, route to proactive DM.
# Anomaly = the LATEST 3 are same-category (rule-2 ACTIVE), not just any 3-same in the window.
# Without this, every pass fires until the streak rolls out of the window — owner DM spam.
LATEST3="$(echo "$DECISION_LINES" | tail -3 | sed -nE 's/.*— category: ([^ —]+).*/\1/p' | tr -d ' ')"
LATEST3_COUNT="$(echo "$LATEST3" | wc -l | tr -d ' ')"
LATEST3_UNIQ="$(echo "$LATEST3" | sort -u | wc -l | tr -d ' ')"
ACTIVE_3SAME=""
if [[ "$LATEST3_COUNT" -ge 3 && "$LATEST3_UNIQ" -eq 1 && -n "$LATEST3" ]]; then
  ACTIVE_3SAME="$(echo "$LATEST3" | head -1)"
fi

ANOMALY=""
TYPES=""  # newline-separated set of anomaly types active this run
[[ -n "$ACTIVE_3SAME" ]] && { ANOMALY="${ANOMALY}rule-2 ACTIVE: last 3 = $ACTIVE_3SAME; "; TYPES="${TYPES}rule-2"$'\n'; }
[[ "$IDLE_PCT" -gt 20 ]] && { ANOMALY="${ANOMALY}idle-rate $IDLE_PCT% (>20%); "; TYPES="${TYPES}idle-rate"$'\n'; }
[[ -n "$REPEAT_REASON" ]] && { ANOMALY="${ANOMALY}reason-repetition; "; TYPES="${TYPES}reason-repetition"$'\n'; }

if [[ -n "$ANOMALY" ]]; then
  # Dedup by anomaly TYPE-SET, not KIND. Fire only when a NEW type appears
  # (e.g. rule-2 trips while idle-rate was already firing). If types stay
  # the same OR a subset clears (rule-2 turns off, idle-rate persists),
  # suppress — no new signal worth waking the owner for.
  # Sig file always reflects CURRENT types so stale entries don't permanently
  # mask re-appearance.
  STATE_DIR="$REPO/state"
  mkdir -p "$STATE_DIR"
  SIG_FILE="$STATE_DIR/last-loop-audit-anomaly.txt"
  CURRENT_SORTED="$(echo "$TYPES" | grep -v '^$' | sort -u)"
  LAST_SORTED=""
  [[ -f "$SIG_FILE" ]] && LAST_SORTED="$(sort -u "$SIG_FILE")"
  # NEW = types in current but not in last. If empty, suppress.
  NEW_TYPES="$(comm -23 <(echo "$CURRENT_SORTED") <(echo "$LAST_SORTED"))"
  if [[ -z "$NEW_TYPES" ]]; then
    echo "Anomaly types unchanged or subset of last run; not re-firing proactive ($ANOMALY)"
  else
    TS="$(date +%s)"
    cat > "$REPO/results/proactive-loop-audit-$TS.txt" <<EOF
**Loop self-audit anomalies (window N=$TOTAL):** $ANOMALY

New since last run: $(echo "$NEW_TYPES" | tr '\n' ',' | sed 's/,$//')

Full report: $OUT
EOF
    echo "Anomaly summary routed to results/proactive-loop-audit-$TS.txt (new: $(echo "$NEW_TYPES" | tr '\n' ',' | sed 's/,$//'))"
  fi
  # Always update sig to current set (whether fired or not), so cleared types
  # can re-fire when they reappear.
  echo "$CURRENT_SORTED" > "$SIG_FILE"
else
  # Anomaly cleared — wipe sig so next anomaly fires.
  STATE_DIR="$REPO/state"
  rm -f "$STATE_DIR/last-loop-audit-anomaly.txt" 2>/dev/null
fi
