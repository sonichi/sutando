#!/usr/bin/env bash
# pane-observe-codex.sh drives Codex's two /model pickers by digit and reads acceptance from the
# pane: exact-id row match, effort row mapping, Escape when the row is absent, a blind capture is
# never a zero. tmux is the Codex pane shim (tests/fixtures/codex-pane-shim.sh); a real-tmux leg
# checks the › glyph and capture against a real server.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
OBS="$HERE/skills/model-switch/scripts/pane-observe-codex.sh"
mkdir -p "$T/bin"; cp "$HERE/tests/fixtures/codex-pane-shim.sh" "$T/bin/tmux"; chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH" TMUX_LOG="$T/tmux.log"
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
# open: type the bare /model the way the sender does, so the shim shows the model picker
open(){ : > "$TMUX_LOG"; rm -f "$TMUX_LOG.caps"; tmux -S "$T/s" send-keys -t probe -l /model; tmux -S "$T/s" send-keys -t probe Enter; }
wait_(){ bash "$OBS" probe --socket "$T/s" --wait --baseline "${BASE:-0}" --timeout 2 "$@" > "$T/out" 2> "$T/err"; echo $?; }
keys(){ grep send-keys "$TMUX_LOG" | sed -E 's/.* -l (.*)$/\1/; s/.* (Enter|Escape)$/\1/' | tail -n +3 | paste -sd' ' -; }

PRIOR='• Model changed to gpt-5.5 xhigh\n• Model changed to gpt-5.5-x medium\n• Model changed to gpt-5.6-luna medium\n'
: > "$TMUX_LOG"; n=$(TMUX_CODEX_PRIOR="$PRIOR" bash "$OBS" probe --socket "$T/s" --model gpt-5.5 --count); [ "$n" = 1 ] && ok "1 --count matches the exact id: gpt-5.5 counts once, gpt-5.5-x does not" || fail "1 count" "n=$n"
n=$(TMUX_CODEX_PRIOR="$PRIOR" bash "$OBS" probe --socket "$T/s" --model gpt-5 --count); [ "$n" = 0 ] && ok "2 ...and a prefix (gpt-5) counts nothing" || fail "2 prefix" "n=$n"
rm -f "$TMUX_LOG.caps"; out=$(TMUX_FAIL_CAPTURE_N=1 bash "$OBS" probe --socket "$T/s" --model gpt-5.5 --count; echo "rc=$?"); [ "$(echo $out)" = "CAPTURE-FAILED rc=12" ] && ok "3 a failed capture in --count is CAPTURE-FAILED (12), never 0" || fail "3 blind count" "$out"

open; rc=$(wait_ --model gpt-5.6-luna --effort xhigh); [ "$rc" = 0 ] && [ "$(cat "$T/out")" = "ACCEPTED xhigh" ] && [ "$(keys)" = "2 4" ] && ok "4 wait: row digit for the exact id (2), then the effort digit (Extra high = 4) -> 'ACCEPTED xhigh'" || fail "4" "rc=$rc keys='$(keys)' $(cat "$T/out" "$T/err")"
open; rc=$(wait_ --model gpt-5.5); [ "$rc" = 0 ] && [ "$(keys)" = "4 Enter" ] && [ "$(cat "$T/out")" = "ACCEPTED medium" ] && ok "5 no --effort: Enter confirms the reasoning picker's default and the OBSERVED effort (medium) is returned" || fail "5" "rc=$rc keys='$(keys)' $(cat "$T/out")"
open; rc=$(TMUX_ACCEPT_EFFORT_AS=medium wait_ --model gpt-5.5 --effort high); [ "$rc" = 15 ] && [ "$(cat "$T/out")" = "EFFORT-MISMATCH medium" ] && [ "$(keys)" = "4 3" ] && grep -q "requested effort 'high' but the CLI applied 'medium'" "$T/err" \
  && ok "5b --effort high, pane says 'Model changed to gpt-5.5 medium': EFFORT-MISMATCH (15), never ACCEPTED, both named" || fail "5b mismatch" "rc=$rc $(cat "$T/out" "$T/err")"
for e in "low 1" "medium 2" "high 3"; do set -- $e; open; rc=$(wait_ --model gpt-5.5 --effort "$1"); [ "$rc" = 0 ] && [ "$(keys)" = "4 $2" ] || fail "6 effort $1" "rc=$rc keys='$(keys)'"; done; ok "6 low/medium/high map to rows 1/2/3"
open; rc=$(wait_ --model gpt-5.5-x); [ "$rc" = 13 ] && [ "$(cat "$T/out")" = NOT-OFFERED ] && [ "$(keys)" = Escape ] && grep -q "Rows seen: gpt-5.6-sol,gpt-5.6-luna,gpt-5.6-mini,gpt-5.5" "$T/err" \
  && ok "7 requested id absent (gpt-5.5-x is not gpt-5.5): Escape, exit 13, rows named, no digit sent" || fail "7 not offered" "rc=$rc keys='$(keys)' $(cat "$T/err")"
ROWS2='  1. gpt-5.5-x  Variant
› 2. gpt-5.5 (current)  Base'
open; rc=$(TMUX_CODEX_ROWS="$ROWS2" wait_ --model gpt-5.5 --effort low); [ "$rc" = 0 ] && [ "$(keys)" = "2 1" ] && ok "8 with gpt-5.5-x listed FIRST, gpt-5.5 still selects its own row (2)" || fail "8 exact over prefix" "rc=$rc keys='$(keys)'"
open; rc=$(TMUX_CODEX_PRIOR='• Model changed to gpt-5.5 xhigh\n' TMUX_NO_ACCEPT=1 BASE=1 wait_ --model gpt-5.5 --effort xhigh); [ "$rc" = 11 ] && [ "$(cat "$T/out")" = TIMEOUT ] && ok "9 a stale acceptance line at the baseline is not a new one: TIMEOUT (11)" || fail "9 baseline" "rc=$rc $(cat "$T/out")"
open; rc=$(TMUX_NO_ACCEPT=1 wait_ --model gpt-5.5); [ "$rc" = 11 ] && ok "10 pickers driven but no acceptance line: TIMEOUT (11)" || fail "10" "rc=$rc"
: > "$TMUX_LOG"; rc=$(wait_ --model gpt-5.5); [ "$rc" = 11 ] && ! grep -q send-keys "$TMUX_LOG" && ok "11 no picker ever appears: TIMEOUT, nothing pressed" || fail "11" "rc=$rc $(grep send-keys "$TMUX_LOG")"
open; rc=$(TMUX_FAIL_CAPTURE_N=2 wait_ --model gpt-5.5 --effort high); [ "$rc" = 0 ] && [ "$(keys)" = "4 3" ] && ok "12 one blind capture mid-flow is retried, not read as an empty pane" || fail "12 blind poll" "rc=$rc keys='$(keys)'"
open; rc=$(TMUX_FAIL=1 wait_ --model gpt-5.5); [ "$rc" = 11 ] && [ "$(cat "$T/out")" = TIMEOUT ] && ok "13 every capture failing: TIMEOUT, never ACCEPTED" || fail "13" "rc=$rc $(cat "$T/out")"
open; rc=$(wait_ --model gpt-5.5 --effort turbo); [ "$rc" = 2 ] && [ "$(grep -c send-keys "$TMUX_LOG")" = 2 ] && ok "14 unknown --effort: exit 2 before touching the pane" || fail "14" "rc=$rc"
rc=$(bash "$OBS" probe --socket "$T/s" --cancel > "$T/out"; echo $?); [ "$rc" = 0 ] && grep -q -- "send-keys -t probe Escape" "$TMUX_LOG" && [ "$(cat "$T/out")" = CANCELLED ] && ok "15 --cancel sends Escape" || fail "15" "rc=$rc"

# A fresh shell: this one has already hashed the shim as `tmux`.
REAL="$(PATH="${PATH#"$T/bin:"}" bash -c 'command -v tmux' 2>/dev/null || true)"
if [ -n "$REAL" ]; then
  SOCKW="$T/real.sock"; export PATH="${PATH#"$T/bin:"}"; hash -r
  "$REAL" -S "$SOCKW" new-session -d -s probe 'printf "\xe2\x80\xa2 Model changed to gpt-5.5 xhigh\n\xe2\x80\xa2 Model changed to gpt-5.5-x medium\n  Select Model and Effort\n  1. gpt-5.6-sol (default)  Reliable\n\xe2\x80\xba 4. gpt-5.5 (current)  Proven\n"; sleep 30'; sleep 0.5
  n=$(bash "$OBS" probe --socket "$SOCKW" --model gpt-5.5 --count); [ "$n" = 1 ] && ok "R1 real pane: --count reads the exact id through a real capture" || fail "R1 real count" "n=$n"
  rc=$(bash "$OBS" probe --socket "$SOCKW" --model gpt-5.6-luna --wait --baseline 1 --timeout 2 > "$T/out" 2> "$T/err"; echo $?)
  [ "$rc" = 13 ] && grep -q "Rows seen: gpt-5.6-sol,gpt-5.5" "$T/err" && ok "R2 real pane: the › row parses (glyph dropped), an absent id exits 13 with the rows named" || fail "R2 real rows" "rc=$rc $(cat "$T/err")"
  "$REAL" -S "$SOCKW" kill-server 2>/dev/null
else
  echo "  skip real-tmux leg: no real tmux on this host"
fi
echo; [ $fails -eq 0 ] && echo "pane-observe-codex: all checks pass" || { echo "pane-observe-codex: $fails FAILED"; exit 1; }
