#!/usr/bin/env bash
# One sender for lines typed into a core pane: session check, current-prompt
# read, queued-input policy, literal send + Enter. tmux is a PATH shim.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"; cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in *" has-session "*) [ -n "${TMUX_NO_SESSION:-}" ] && exit 1;; *" capture-pane "*) printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}";; esac
exit 0
SH
chmod +x "$T/bin/tmux"; export TMUX_LOG="$T/log"
# the script prefers the Homebrew path; make the shim win by shadowing lookup via PATH only when those are absent —
# so test the resolver through a private HOME-less environment: point both fixed paths away.
SEND="bash $HERE/scripts/tmux-send-line.sh"; fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
run(){ : > "$TMUX_LOG"; PATH="$T/bin:$PATH" $SEND "$@" > "$T/out" 2> "$T/err"; echo $?; }
# --- shim leg (always runs): policy and failure paths through the PATH shim, which the resolver finds first
cat > "$T/bin/tmux" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case " $* " in
  *" has-session "*) [ -n "${TMUX_NO_SESSION:-}" ] && exit 1;;
  *" capture-pane "*) [ -n "${TMUX_CAP_FAIL:-}" ] && exit 1; [ -n "${TMUX_CAP_DELAY:-}" ] && sleep "$TMUX_CAP_DELAY"; printf '%b' "${TMUX_PANE_TEXT:-────\n❯ \n────\n}";;
  *" send-keys "*) [ -n "${TMUX_SEND_DELAY:-}" ] && sleep "$TMUX_SEND_DELAY";;
esac
exit 0
SH
chmod +x "$T/bin/tmux"
rc=$(run probe hello --socket "$T/s.sock"); [ "$rc" = 0 ] && grep -q -- "send-keys -t probe -l hello" "$TMUX_LOG" && grep -q -- "send-keys -t probe Enter" "$TMUX_LOG" && ok "S1 shim: clear prompt → literal line then Enter" || fail "S1" "rc=$rc $(cat "$TMUX_LOG")"
rc=$(TMUX_PANE_TEXT='❯ half typed\n' run probe x --socket "$T/s.sock" --refuse-if-pending); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && ok "S2 shim: pending text → 5, nothing sent" || fail "S2" "rc=$rc"
rc=$(TMUX_PANE_TEXT='❯ watcher\n' run probe watcher --socket "$T/s.sock" --skip-if-queued watcher); [ "$rc" = 6 ] && ! grep -q send-keys "$TMUX_LOG" && ok "S3 shim: queued word → 6" || fail "S3" "rc=$rc"
rc=$(TMUX_NO_SESSION=1 run probe x --socket "$T/s.sock"); [ "$rc" = 3 ] && ok "S4 shim: no session → 3" || fail "S4" "rc=$rc"
rc=$(TMUX_CAP_FAIL=1 run probe x --socket "$T/s.sock" --refuse-if-pending); [ "$rc" = 7 ] && ! grep -q send-keys "$TMUX_LOG" && ok "S5 capture-pane fails → 7, NOT sent (fail-closed)" || fail "S5 capture fail" "rc=$rc $(cat "$T/err")"
# --- per-runtime prompt glyph: Codex draws › and a DIM placeholder on the empty composer
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m \033[2mImprove documentation in @filename\033[0m\n' run probe hello --socket "$T/s.sock" --runtime codex --refuse-if-pending); [ "$rc" = 0 ] && grep -q -- "capture-pane -e -p" "$TMUX_LOG" && grep -q -- "send-keys -t probe -l hello" "$TMUX_LOG" && ok "C1 codex: dim placeholder is NOT pending → sent (pane read with -e)" || fail "C1 codex placeholder" "rc=$rc $(cat "$T/err")"
rc=$(TMUX_PANE_TEXT='\033[1m›\033[0m half typed\n' run probe x --socket "$T/s.sock" --runtime codex --refuse-if-pending); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && grep -q "half typed" "$T/err" && ok "C2 codex: typed text after › → 5, quoted, nothing sent" || fail "C2 codex pending" "rc=$rc $(cat "$T/err")"
rc=$(TMUX_PANE_TEXT='  Select Model and Effort\n› 4. gpt-5.5 (current)  Proven previous-generation model\n' run probe x --socket "$T/s.sock" --runtime codex --refuse-if-pending); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && ok "C3 codex: an open picker's selected › row reads as pending → 5" || fail "C3 codex picker" "rc=$rc"
rc=$(TMUX_PANE_TEXT='❯ half typed\n' run probe x --socket "$T/s.sock" --runtime codex --refuse-if-pending); [ "$rc" = 0 ] && ok "C4 codex: a Claude ❯ line is not the Codex prompt (the runtime picks the glyph)" || fail "C4 glyph is per-runtime" "rc=$rc"
rc=$(run probe hello --socket "$T/s.sock"); grep -q -- "capture-pane -p -t probe" "$TMUX_LOG" && ! grep -q -- "capture-pane -e" "$TMUX_LOG" && ok "C5 claude (default): capture unchanged, no -e" || fail "C5 claude capture unchanged" "$(cat "$TMUX_LOG")"
# the delay between the literal line and Enter: a PATH-shimmed `sleep` logs its argument in sequence with the
# tmux calls instead of sleeping, so the check reads the script's own pause, not process-launch latency
cat > "$T/bin/sleep" <<'SH'
#!/bin/sh
printf 'sleep %s\n' "$*" >> "$TMUX_LOG"
SH
chmod +x "$T/bin/sleep"
seq(){ grep -E '^sleep |send-keys' "$TMUX_LOG" | sed -E 's/^-S [^ ]+ //' | tr '\n' '|'; }
rc=$(run probe hello --socket "$T/s.sock" --runtime codex); SEQ="$(seq)"
[ "$rc" = 0 ] && [ "$SEQ" = "send-keys -t probe -l hello|sleep 0.25|send-keys -t probe Enter|" ] && ok "C7 codex: literal line, sleep 0.25, then Enter (an Enter inside Codex's 120ms paste-burst window reads as a newline)" || fail "C7 codex enter delay" "rc=$rc seq=$SEQ"
rc=$(run probe hello --socket "$T/s.sock"); SEQ="$(seq)"
[ "$rc" = 0 ] && [ "$SEQ" = "send-keys -t probe -l hello|send-keys -t probe Enter|" ] && ok "C8 claude (default): no sleep between the literal line and Enter (the no-delay control)" || fail "C8 claude no delay" "rc=$rc seq=$SEQ"
rm -f "$T/bin/sleep"
rc=$(run probe x --socket "$T/s.sock" --runtime bogus); [ "$rc" = 2 ] && ! grep -q -- "capture-pane\|send-keys" "$TMUX_LOG" && ok "C6 unknown --runtime → 2 before any tmux call" || fail "C6 bogus runtime" "rc=$rc"
# the runtime is chosen by --runtime only: an ambient RUNTIME variable (callers that pass no flag inherit whatever
# the environment holds) must not switch the glyph, or a Claude draft reads as empty and gets written into
rc=$(RUNTIME=codex TMUX_PANE_TEXT='❯ half typed\n' run probe x --socket "$T/s.sock" --refuse-if-pending); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && ok "A1 ambient RUNTIME=codex is ignored: Claude ❯ pending text → 5, nothing sent" || fail "A1 ambient RUNTIME=codex" "rc=$rc $(cat "$T/err")"
rc=$(RUNTIME=bogus TMUX_PANE_TEXT='❯ half typed\n' run probe x --socket "$T/s.sock" --refuse-if-pending); [ "$rc" = 5 ] && ! grep -q send-keys "$TMUX_LOG" && ok "A2 ambient RUNTIME=bogus is ignored: still the Claude default → 5, not 2" || fail "A2 ambient RUNTIME=bogus" "rc=$rc $(cat "$T/err")"
printf '#!/bin/sh\nexit 1\n' > "$T/bin/python3"; chmod +x "$T/bin/python3"
rc=$(PATH="$T/bin:$PATH" SUTANDO_PY="$T/bin/python3" bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$T/s.sock" --refuse-if-pending > "$T/out" 2> "$T/err"; echo $?)
rm -f "$T/bin/python3"
[ "$rc" = 7 ] && ! grep -q send-keys "$TMUX_LOG" && ok "S6 parser interpreter fails → 7, NOT sent" || fail "S6 parser fail" "rc=$rc $(cat "$T/err")"
# overlapping callers: the lock must serialize inspect+send so payloads never interleave
: > "$TMUX_LOG"; (TMUX_CAP_DELAY=0.4 TMUX_SEND_DELAY=0.2 PATH="$T/bin:$PATH" bash "$HERE/scripts/tmux-send-line.sh" probe alpha --socket "$T/s.sock" >/dev/null 2>&1) & sleep 0.1; (PATH="$T/bin:$PATH" bash "$HERE/scripts/tmux-send-line.sh" probe beta --socket "$T/s.sock" >/dev/null 2>&1) & wait
SEQ="$(grep send-keys "$TMUX_LOG" | sed -E 's/.*-l (alpha|beta)$/lit:\1/; s/.*Enter$/enter/' | tr '\n' ' ')"
case "$SEQ" in "lit:alpha enter lit:beta enter "|"lit:beta enter lit:alpha enter ") ok "S7 two overlapping callers are serialized: $SEQ";; *) fail "S7 interleave" "$SEQ";; esac
# --lock-fd: a caller holding THIS pane's lock across a transaction lends its fd; any other file is refused
LOCKP="$(bash "$HERE/scripts/tmux-pane-lock.sh" "$T/s.sock" probe)"; : > "$TMUX_LOG"; rm -f "$T/rc"
(exec 8>"$LOCKP"; "$(bash "$HERE/scripts/sutando-config.sh" python-bin)" -c 'import fcntl; fcntl.flock(8, fcntl.LOCK_EX)'; PATH="$T/bin:$PATH" $SEND probe hello --socket "$T/s.sock" --lock-fd 8 > "$T/out" 2> "$T/err"; echo $? > "$T/rc") &
i=0; until [ -e "$T/rc" ] || [ $i -ge 100 ]; do sleep 0.1; i=$((i+1)); done; [ -e "$T/rc" ] || { kill %% 2>/dev/null; echo hang > "$T/rc"; }; wait
[ "$(cat "$T/rc")" = 0 ] && grep -q -- "send-keys -t probe -l hello" "$TMUX_LOG" && ok "L1 --lock-fd on the caller-held pane lock: sends inside it, no deadlock" || fail "L1 lock-fd held" "rc=$(cat "$T/rc") $(cat "$T/err")"
rc=$(: > "$TMUX_LOG"; exec 8>"$T/other.lock"; PATH="$T/bin:$PATH" $SEND probe hello --socket "$T/s.sock" --lock-fd 8 > "$T/out" 2> "$T/err"; echo $?)
[ "$rc" = 8 ] && ! grep -q send-keys "$TMUX_LOG" && grep -q "is not the pane lock" "$T/err" && ok "L2 --lock-fd on some OTHER file: refused (8), nothing sent" || fail "L2 wrong lock" "rc=$rc $(cat "$T/err")"
rc=$(run probe hello --socket "$T/s.sock" --lock-fd 8x); [ "$rc" = 2 ] && ! grep -q send-keys "$TMUX_LOG" && ok "L3 a non-integer --lock-fd: exit 2 before any tmux write" || fail "L3" "rc=$rc"
[ "$LOCKP" = "$(bash "$HERE/scripts/tmux-pane-lock.sh" "$T/s.sock" probe)" ] && [ "$LOCKP" != "$(bash "$HERE/scripts/tmux-pane-lock.sh" "$T/s.sock" other)" ] && ok "L4 tmux-pane-lock.sh is deterministic per socket+session and distinct across sessions" || fail "L4 lock path" "$LOCKP"
# --- real-tmux leg (optional): the same policy against a real server on a throwaway socket
if command -v tmux >/dev/null 2>&1 && [ "$(command -v tmux)" != "$T/bin/tmux" ]; then
  SOCKW="$T/w.sock"; OUTW="$T/pane.out"
  tmux -S "$SOCKW" new-session -d -s probe "cat > $OUTW"; sleep 0.4
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe "hello world" --socket "$SOCKW" > "$T/out" 2> "$T/err"; echo $?); sleep 0.4
  [ "$rc" = 0 ] && [ "$(tr -d '\r' < "$OUTW")" = "hello world" ] && ok "R1 real pane: literal line + Enter delivered" || fail "R1 real pane" "rc=$rc [$(cat "$OUTW")] $(cat "$T/err")"
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" nosuch x --socket "$SOCKW" > /dev/null 2> "$T/err"; echo $?)
  [ "$rc" = 3 ] && ok "R2 real tmux: missing session → exit 3" || fail "R2 no session" "rc=$rc"
  tmux -S "$SOCKW" kill-server 2>/dev/null
  tmux -S "$SOCKW" new-session -d -s probe 'printf "\xe2\x9d\xaf half typed"; sleep 30'; sleep 0.5
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$SOCKW" --refuse-if-pending > /dev/null 2> "$T/err"; echo $?)
  [ "$rc" = 5 ] && grep -q "half typed" "$T/err" && ok "R3 real pane with text after ❯: --refuse-if-pending exits 5 quoting it" || fail "R3 refuse" "rc=$rc $(cat "$T/err")"
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$SOCKW" --skip-if-queued "half typed" > /dev/null 2>&1; echo $?)
  [ "$rc" = 6 ] && ok "R4 --skip-if-queued matches the queued word: exit 6, nothing typed" || fail "R4 skip" "rc=$rc"
  tmux -S "$SOCKW" kill-server 2>/dev/null
  tmux -S "$SOCKW" new-session -d -s probe 'printf "\033[1m\xe2\x80\xba\033[0m \033[2mImprove documentation in @filename\033[0m"; sleep 30'; sleep 0.5
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$SOCKW" --runtime codex --refuse-if-pending --dry-run > "$T/out" 2> "$T/err"; echo $?)
  [ "$rc" = 0 ] && grep -q "pending: ''" "$T/out" && ok "R5 real Codex-shaped pane: dim placeholder after › reads as EMPTY" || fail "R5 real placeholder" "rc=$rc $(cat "$T/out" "$T/err")"
  tmux -S "$SOCKW" kill-server 2>/dev/null
  tmux -S "$SOCKW" new-session -d -s probe 'printf "\xe2\x80\xba half typed"; sleep 30'; sleep 0.5
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$SOCKW" --runtime codex --refuse-if-pending > /dev/null 2> "$T/err"; echo $?)
  [ "$rc" = 5 ] && grep -q "half typed" "$T/err" && ok "R6 real Codex-shaped pane with text after ›: --refuse-if-pending exits 5" || fail "R6 real codex refuse" "rc=$rc $(cat "$T/err")"
  rc=$(bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$SOCKW" --refuse-if-pending --dry-run > "$T/out" 2>&1; echo $?)
  [ "$rc" = 0 ] && grep -q "pending: ''" "$T/out" && ok "R7 the same pane read as claude (default) sees NO prompt — the pre-flag defect, now opt-out only" || fail "R7 default-runtime contrast" "rc=$rc $(cat "$T/out")"
  tmux -S "$SOCKW" kill-server 2>/dev/null
else
  echo "  skip real-tmux leg: no real tmux on this host"
fi
echo; [ $fails -eq 0 ] && echo "tmux-send-line: all checks pass" || { echo "tmux-send-line: $fails FAILED"; exit 1; }
