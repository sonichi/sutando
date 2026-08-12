#!/bin/bash
# A compaction must leave a durable trace. Nothing on disk recorded one before,
# so "did context roll over just before that failure?" could not be answered.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SCRIPT="$REPO/src/session-handoff.sh"

pass=0; fail=0
ok()   { echo "ok   $1"; pass=$((pass+1)); }
bad()  { echo "FAIL $1"; fail=$((fail+1)); }

TMPD="$(mktemp -d -t handoff-compaction.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

run_handoff() {
    # Point the script at a throwaway workspace, never the real one.
    local ws="$1"; shift
    mkdir -p "$ws"
    cat > "$TMPD/sutando.config.local.json" <<JSON
{"workspace": "$ws"}
JSON
    ( cd "$REPO" && SUTANDO_REPO_DIR="$REPO" \
        SUTANDO_CONFIG_FILE="$TMPD/sutando.config.local.json" \
        SUTANDO_WORKSPACE_OVERRIDE="$ws" \
        bash "$SCRIPT" "${1:-/nonexistent/transcript.jsonl}" ) >/dev/null 2>&1
}

# --- the function exists and is CALLED, not merely defined --------------------
if grep -q "record_compaction_event()" "$SCRIPT"; then
    ok "record_compaction_event is defined"
else
    bad "record_compaction_event is not defined"
fi
# A definition with no call site is the defect this whole change is about.
if grep -qE '^record_compaction_event "' "$SCRIPT"; then
    ok "record_compaction_event is invoked at top level"
else
    bad "record_compaction_event is defined but never called"
fi

# --- it writes under state/, not the workspace root ---------------------------
if grep -q 'state/compactions.jsonl' "$SCRIPT"; then
    ok "log path is state/compactions.jsonl (workspace contract)"
else
    bad "log is not under state/"
fi

# --- the emitted line is valid JSON with the fields a reader needs ------------
LOG="$TMPD/probe.jsonl"
mkdir -p "$(dirname "$LOG")"
# Exercise the real function body rather than a re-typed copy of it.
WORKSPACE_DIR="$TMPD/ws"; mkdir -p "$WORKSPACE_DIR/state"
eval "$(sed -n '/^record_compaction_event() {/,/^}/p' "$SCRIPT")"
record_compaction_event "/some/path/transcript-abc.jsonl" "precompact"
OUT="$WORKSPACE_DIR/state/compactions.jsonl"
if [ -s "$OUT" ]; then
    ok "a line was written"
else
    bad "no line written to $OUT"
fi
if python3 -c "
import json,sys
d=json.loads(open('$OUT').read().strip().splitlines()[-1])
missing=[k for k in ('ts','epoch','host','transcript','trigger') if k not in d]
assert not missing, missing
assert d['transcript']=='transcript-abc.jsonl', d['transcript']
assert d['trigger']=='precompact', d['trigger']
assert isinstance(d['epoch'],int), type(d['epoch'])
" 2>/dev/null; then
    ok "line is valid JSON with ts/epoch/host/transcript/trigger"
else
    bad "line is not valid JSON or is missing fields: $(tail -1 "$OUT")"
fi

# --- append, not overwrite ----------------------------------------------------
record_compaction_event "/x/second.jsonl" "precompact"
if [ "$(wc -l < "$OUT")" -eq 2 ]; then
    ok "appends (2 events -> 2 lines)"
else
    bad "expected 2 lines, got $(wc -l < "$OUT")"
fi

# --- bounded: a long-lived core must not grow this forever --------------------
python3 - "$OUT" <<'PY'
import sys
p=sys.argv[1]
open(p,"w").write("".join('{"ts":"x","epoch":0}\n' for _ in range(600)))
PY
record_compaction_event "/x/third.jsonl" "precompact"
N=$(wc -l < "$OUT")
if [ "$N" -le 500 ]; then
    ok "bounded at 500 lines (got $N after seeding 600)"
else
    bad "unbounded: $N lines"
fi
if tail -1 "$OUT" | grep -q "third.jsonl"; then
    ok "the trim keeps the NEWEST event, not the oldest"
else
    bad "newest event lost by the trim: $(tail -1 "$OUT")"
fi

# --- never fatal: an unwritable state dir must not break the handoff ----------
WORKSPACE_DIR="$TMPD/ro-ws"; mkdir -p "$WORKSPACE_DIR"
: > "$WORKSPACE_DIR/state"   # a FILE where the dir must go -> mkdir fails
if record_compaction_event "/x/y.jsonl" "precompact"; then
    ok "returns success even when the log cannot be written"
else
    bad "a failed log write became a nonzero exit (would break PreCompact)"
fi

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
