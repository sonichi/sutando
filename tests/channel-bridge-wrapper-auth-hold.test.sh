#!/usr/bin/env bash
# shellcheck disable=SC2329  # the counters are called indirectly through ge()
# channel-bridge-wrapper.sh: a rejected credential is held, not retried, and crash
# notices are rate limited.
#
#   exit-78 child  -> one auth-failed notice, no relaunch while held, no restart notice
#   one deliberate-restart stamp -> exactly one retry, even when it is future-dated
#   wrapper relaunched while held -> no second notice, no "restarted" alert
#   exit-1 child   -> 3 restart notices, then one "restarting repeatedly", then quiet
#
# Waits are driven by a sleep shim that logs each hold poll, never by wall-clock guesses.
# Run: bash tests/channel-bridge-wrapper-auth-hold.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
# shellcheck source=../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the wrapper auth-hold test")" || exit 1

check() {  # $1=description $2=actual $3=expected
  if [ "$2" = "$3" ]; then echo "  ok   $1 ($2)"; else echo "  FAIL $1: got $2, want $3"; fail=1; fi
}

POLL=0.2  # hold poll interval; the shim counts sleeps of exactly this length
setup() {  # $1 = exit code the stub bridge returns; prints the fixture dir
  local d; d=$(mktemp -d)
  mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$d/none.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  printf 'import sys\nopen("%s/launches", "a").write("x\\n")\nsys.exit(%s)\n' "$d" "$1" > "$d/src/discord-bridge.py"
  cp "$REPO/src/launchd/channel-bridge-wrapper.sh" "$d/src/launchd/"
  cp "$REPO/src/result_publish.py" "$d/src/"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  printf '#!/bin/bash\necho "$*" >> "%s/sleeps"\nexec /bin/sleep "$@"\n' "$d" > "$d/shims/sleep"
  chmod +x "$d/shims/osascript" "$d/shims/sleep"
  echo "$d"
}

start_wrapper() {  # $1 = fixture dir; prints the wrapper pid
  ( cd "$1" && PATH="$1/shims:$PATH" DISCORD_BOT_TOKEN=t SUTANDO_CHANNEL_BRIDGE_PYTHON="$PY" \
      SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY=1 SUTANDO_BRIDGE_ALERT_COOLDOWN_S=0 \
      SUTANDO_BRIDGE_AUTH_HOLD_POLL_S="$POLL" \
      exec bash src/launchd/channel-bridge-wrapper.sh discord >> "$1/o.log" 2>&1 ) &
  echo $!
}

stop_wrapper() { kill -TERM "$1" 2>/dev/null; wait "$1" 2>/dev/null; }
count() { [ -f "$1" ] && { grep -c -- "$2" "$1" || true; } || echo 0; }
launches() { count "$1/launches" x; }
polls() { [ -f "$1/sleeps" ] && { grep -cx -- "$POLL" "$1/sleeps" || true; } || echo 0; }
held() { count "$1/o.log" 'held: credential rejected'; }
retries() { count "$1/o.log" 'deliberate restart seen; retrying once'; }
notices() { find "$1/ws/results" -name "proactive-discord-bridge-$2-*.txt" | wc -l | tr -d ' '; }
wait_for() {  # $1=description, rest = condition command; 30 s ceiling, then FAIL
  local what="$1" i=0; shift
  until "$@"; do
    i=$((i + 1)); [ "$i" -lt 300 ] || { echo "  FAIL timed out waiting for $what"; fail=1; return 1; }
    /bin/sleep 0.1
  done
}
ge() { [ "$("$1" "$2")" -ge "$3" ]; }  # $1=counter $2=dir $3=min
settle() { local base; base=$(polls "$1"); wait_for "3 hold polls" ge polls "$1" $((base + 3)); }

echo "auth rejection (exit 78):"
d=$(setup 78)
# A frozen clock: every notice lands in the same second, so a name keyed on time alone collides.
# shellcheck disable=SC2016  # the shim body expands when it runs, not here
printf '#!/bin/bash\n[ "$1" = +%%s ] && { echo 1700000000; exit 0; }\nexec /bin/date "$@"\n' > "$d/shims/date"
chmod +x "$d/shims/date"
p=$(start_wrapper "$d")
wait_for "first hold" ge held "$d" 1 && settle "$d"
check "child launched once" "$(launches "$d")" 1
check "one auth-failed notice" "$(notices "$d" auth-failed)" 1
check "no restart notice" "$(notices "$d" restarted)" 0
check "notice names the vault key" \
  "$(grep -l 'DISCORD_BOT_TOKEN' "$d"/ws/results/proactive-discord-bridge-auth-failed-*.txt | wc -l | tr -d ' ')" 1
check "log names the fix" "$(count "$d/o.log" 'held: credential rejected (DISCORD_BOT_TOKEN); vault set')" 1
# Future-dated on purpose: a hold that compared against its own start time retried until the clock caught up.
echo $(( $(/bin/date +%s) + 30 )) > "$d/ws/state/channel-bridge-supervisor/deliberate-restart"
wait_for "the retry's hold" ge held "$d" 2 && settle "$d"
check "one stamp -> exactly one retry" "$(retries "$d")" 1
check "child launched twice in all" "$(launches "$d")" 2
check "the retry's rejection notifies once more" "$(notices "$d" auth-failed)" 2
stop_wrapper "$p"
p=$(start_wrapper "$d")
wait_for "the relaunch's hold" ge held "$d" 3 && settle "$d"
check "relaunched wrapper tries once" "$(launches "$d")" 3
check "relaunch while held: no retry" "$(retries "$d")" 1
check "relaunch while held: no new notice" "$(notices "$d" auth-failed)" 2
check "relaunch while held: no restart notice" "$(notices "$d" restarted)" 0
stop_wrapper "$p"; rm -rf "$d"

echo "genuine crash loop (exit 1):"
d=$(setup 1)
p=$(start_wrapper "$d")
wait_for "6 launches" ge launches "$d" 6
stop_wrapper "$p"
check "three restart notices" "$(notices "$d" restarted)" 3
check "one 'restarting repeatedly' notice" "$(notices "$d" restarting-repeatedly)" 1
check "it points at the log" \
  "$(cat "$d"/ws/results/proactive-discord-bridge-restarting-repeatedly-*.txt | grep -c 'see logs/discord-bridge.log')" 1
check "no auth-failed notice" "$(notices "$d" auth-failed)" 0
check "quiet stamp set" "$([ -s "$d/ws/state/channel-bridge-supervisor/discord.alert-quiet-until" ] && echo yes)" yes
rm -rf "$d"

[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
