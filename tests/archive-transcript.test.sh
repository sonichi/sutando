#!/usr/bin/env bash
# Tests for src/archive-transcript.sh — the SCRIPT, not its registration.
#
# The registration tests assert the installer writes the right command string.
# Nothing exercised the script itself, and that is the half where a silent
# regression looks exactly like the defect this replaces: the original
# `cp "$TRANSCRIPT_PATH" …` also "passed" every registration check while
# archiving nothing on every compaction (#3999). So each exit path gets an arm,
# and two arms are POSITIVE — an all-red suite reads as vigilance when it is
# really a broken harness.
#
# Fixture paths contain spaces on purpose: this host's real install lives under
# "Library/Application Support/…", which is where unquoted expansions die.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../src/archive-transcript.sh"

pass=0; fail=0
ok() {  # ok <name> <condition-rc>
    if [ "$2" = 0 ]; then echo "ok   $1"; pass=$((pass+1))
    else echo "FAIL $1"; fail=$((fail+1)); fi
}

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/archive transcript test.XXXXXX")"
SRC="$ROOT/src"; mkdir -p "$SRC"
cp "$SCRIPT" "$SRC/archive-transcript.sh"
cp "$HERE/../src/hook_transcript_path.sh" "$SRC/hook_transcript_path.sh"
TRANSCRIPT="$ROOT/a transcript.jsonl"
printf '{"type":"user"}\n' > "$TRANSCRIPT"

run() {  # run <dest> [explicit] ; feeds $STDIN_JSON when set
    if [ -n "${STDIN_JSON:-}" ]; then
        printf '%s' "$STDIN_JSON" | bash "$SRC/archive-transcript.sh" "$@" 2>&1
    else
        bash "$SRC/archive-transcript.sh" "$@" </dev/null 2>&1
    fi
}

# --- POSITIVE: stdin JSON, the shape a real PreCompact hook sends ------------
D="$ROOT/dest stdin"
STDIN_JSON="$(printf '{"transcript_path":"%s"}' "$TRANSCRIPT")" run "$D" >/dev/null
ok "exit 0: stdin transcript_path archives" $?
ok "exit 0: exactly one file landed" \
   "$([ "$(ls "$D" 2>/dev/null | wc -l | tr -d ' ')" = 1 ] && echo 0 || echo 1)"
ok "exit 0: archived bytes match the source" \
   "$(cmp -s "$TRANSCRIPT" "$D/$(ls "$D")" && echo 0 || echo 1)"

# --- POSITIVE: explicit $2 (manual invocation), stdin never read ------------
D2="$ROOT/dest explicit"
STDIN_JSON="" run "$D2" "$TRANSCRIPT" >/dev/null
ok "exit 0: explicit \$2 archives with no stdin" $?

# --- explicit $2 WINS over stdin, and does not consume it -------------------
OTHER="$ROOT/other transcript.jsonl"; printf 'other\n' > "$OTHER"
D3="$ROOT/dest precedence"
STDIN_JSON="$(printf '{"transcript_path":"%s"}' "$TRANSCRIPT")" run "$D3" "$OTHER" >/dev/null
ok "precedence: explicit \$2 beats stdin JSON" \
   "$(cmp -s "$OTHER" "$D3/$(ls "$D3")" && echo 0 || echo 1)"

# --- exit 2: no destination -------------------------------------------------
STDIN_JSON="" run >/dev/null; ok "exit 2: no destination directory" \
   "$([ $? = 2 ] && echo 0 || echo 1)"

# --- exit 3: nothing on stdin and no $2 (the #3999 shape) -------------------
out="$(STDIN_JSON="" run "$ROOT/dest none")"; rc=$?
ok "exit 3: no transcript_path anywhere" "$([ $rc = 3 ] && echo 0 || echo 1)"
printf '%s' "$out" | grep -q "nothing archived"
ok "exit 3: says so on stderr rather than failing silently" $?

# --- exit 3: stdin JSON present but transcript_path absent/null -------------
STDIN_JSON='{"session_id":"x"}' run "$ROOT/dest nullpath" >/dev/null
ok "exit 3: JSON without transcript_path" "$([ $? = 3 ] && echo 0 || echo 1)"

# --- exit 4: path resolves but the file is gone -----------------------------
STDIN_JSON="$(printf '{"transcript_path":"%s"}' "$ROOT/absent.jsonl")" \
  run "$ROOT/dest missing" >/dev/null
ok "exit 4: transcript_path does not exist" "$([ $? = 4 ] && echo 0 || echo 1)"

# --- exit 7: the shared resolver is absent ----------------------------------
# Guards the extraction itself: without this, a missing helper would leave
# TRANSCRIPT empty and the script would report exit 3 — "no transcript_path",
# which is a TRUE statement about a WRONG cause and sends the reader hunting a
# hook payload that was fine.
mv "$SRC/hook_transcript_path.sh" "$SRC/hook_transcript_path.sh.hidden"
STDIN_JSON="$(printf '{"transcript_path":"%s"}' "$TRANSCRIPT")" \
  run "$ROOT/dest nohelper" >/dev/null
ok "exit 7: missing hook_transcript_path.sh is distinct from exit 3" \
   "$([ $? = 7 ] && echo 0 || echo 1)"
mv "$SRC/hook_transcript_path.sh.hidden" "$SRC/hook_transcript_path.sh"

rm -rf "$ROOT"
echo "---"
if [ "$fail" -gt 0 ]; then
    echo "FAILED — $fail of $((pass+fail)) checks"; exit 1
fi
echo "PASS — archive-transcript ($pass checks)"
