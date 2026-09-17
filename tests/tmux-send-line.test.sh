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
printf '#!/bin/sh\nexit 1\n' > "$T/bin/python3"; chmod +x "$T/bin/python3"
rc=$(PATH="$T/bin:$PATH" SUTANDO_PY="$T/bin/python3" bash "$HERE/scripts/tmux-send-line.sh" probe x --socket "$T/s.sock" --refuse-if-pending > "$T/out" 2> "$T/err"; echo $?)
rm -f "$T/bin/python3"
[ "$rc" = 7 ] && ! grep -q send-keys "$TMUX_LOG" && ok "S6 parser interpreter fails → 7, NOT sent" || fail "S6 parser fail" "rc=$rc $(cat "$T/err")"
# overlapping callers: the lock must serialize inspect+send so payloads never interleave
: > "$TMUX_LOG"; (TMUX_CAP_DELAY=0.4 TMUX_SEND_DELAY=0.2 PATH="$T/bin:$PATH" bash "$HERE/scripts/tmux-send-line.sh" probe alpha --socket "$T/s.sock" >/dev/null 2>&1) & sleep 0.1; (PATH="$T/bin:$PATH" bash "$HERE/scripts/tmux-send-line.sh" probe beta --socket "$T/s.sock" >/dev/null 2>&1) & wait
SEQ="$(grep send-keys "$TMUX_LOG" | sed -E 's/.*-l (alpha|beta)$/lit:\1/; s/.*Enter$/enter/' | tr '\n' ' ')"
case "$SEQ" in "lit:alpha enter lit:beta enter "|"lit:beta enter lit:alpha enter ") ok "S7 two overlapping callers are serialized: $SEQ";; *) fail "S7 interleave" "$SEQ";; esac
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
else
  echo "  skip real-tmux leg: no real tmux on this host"
fi
echo; [ $fails -eq 0 ] && echo "tmux-send-line: all checks pass" || { echo "tmux-send-line: $fails FAILED"; exit 1; }
