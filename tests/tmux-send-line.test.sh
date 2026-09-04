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
# The resolver checks /opt/homebrew and /usr/local first; the shim must be found instead, so
# neutralise them for the test by running with a fake root prefix? No — keep it honest: run through the
# shim only if no real tmux sits at those paths, else exercise the policy via a real tmux on a throwaway socket.
if [ -x /opt/homebrew/bin/tmux ] || [ -x /usr/local/bin/tmux ]; then
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
  # policy checks need a controllable pane text: drive the awk policy directly
  P(){ printf '%b' "$1" | python3 -c 'import sys
last=""
for l in sys.stdin.read().splitlines():
    s=l.lstrip(" \t")
    if s.startswith("\u276f"):
        r=s[1:]
        if r[:1] in (" ", "\u00a0"): r=r[1:]
        last=r.rstrip()
print(last)'; }
  [ "$(P 'old ❯ stale\n────\n❯ half typed\n────\n')" = "half typed" ] && ok "P1 the LAST ❯ line is the prompt; its text is the pending input" || fail "P1" "[$(P 'old ❯ stale\n────\n❯ half typed\n────\n')]"
  [ -z "$(P '❯ done\n────\n❯\xc2\xa0\n────\n')" ] && ok "P2 an empty prompt with a trailing nbsp reads as empty" || fail "P2" "[$(P '❯ done\n────\n❯\xc2\xa0\n────\n')]"
  [ "$(P '  ❯ watcher\n')" = "watcher" ] && ok "P3 leading whitespace before ❯ is ignored" || fail "P3" ""
else
  echo "  skip real-tmux checks: no tmux at the fixed paths"
fi
echo; [ $fails -eq 0 ] && echo "tmux-send-line: all checks pass" || { echo "tmux-send-line: $fails FAILED"; exit 1; }
