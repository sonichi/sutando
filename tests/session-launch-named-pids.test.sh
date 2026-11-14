#!/usr/bin/env bash
# claude_named_pids (session-launch.sh) matches the session name as a whole token, in both
# argument forms: a sibling such as `<name>-worker` is another session, not this one.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; PIDS=""
trap 'for p in $PIDS; do kill "$p" 2>/dev/null; done; rm -rf "$T"' EXIT
fails=0; ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
command -v cc > /dev/null 2>&1 || { echo "session-launch-named-pids: skipped (no cc to build a process named claude)"; exit 0; }
printf '#include <unistd.h>\nint main(void){sleep(60);return 0;}\n' > "$T/c.c"
mkdir -p "$T/bin" && cc -o "$T/bin/claude" "$T/c.c" || { fail "build" "cc failed"; exit 1; }
spawn(){ "$T/bin/claude" "$@" < /dev/null > /dev/null 2>&1 & PIDS="$PIDS $!"; LAST=$!; }
S="slt-$$"; REPO="$HERE"; . "$HERE/src/agent/claude/cli/session-launch.sh"
spawn --name "$S-worker"; spawn "--name=$S-worker"; sleep 0.3
SESSION="$S"; [ -z "$(claude_named_pids)" ] && ! claude_named_process_running \
  && ok "1 siblings '--name $S-worker' and '--name=$S-worker' are not session $S" || fail "1" "pids=$(claude_named_pids | tr '\n' ' ')"
SESSION="$S-worker"; n=$(claude_named_pids | wc -l | tr -d ' ')
[ "$n" = 2 ] && ok "2 both forms match their own session exactly" || fail "2" "n=$n"
spawn --name "$S" --dangerously-skip-permissions; a=$LAST; spawn "--name=$S"; b=$LAST; sleep 0.3
SESSION="$S"; got="$(claude_named_pids | sort | tr '\n' ' ')"; want="$(printf '%s\n' "$a" "$b" | sort | tr '\n' ' ')"
[ "$got" = "$want" ] && ok "3 the session itself matches mid-argv and at the end, in both forms" || fail "3" "got=$got want=$want"
echo; [ $fails -eq 0 ] && echo "session-launch-named-pids: all checks pass" || { echo "session-launch-named-pids: $fails FAILED"; exit 1; }
