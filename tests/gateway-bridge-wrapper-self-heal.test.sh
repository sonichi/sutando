#!/usr/bin/env bash
# gateway-bridge-wrapper.sh must supervise the bridge like channel-bridge-wrapper.sh
# does, so a bridge that exits cleanly (restart.sh's pkill -> exit 0) is relaunched
# instead of leaving the launchd job idle (its KeepAlive is crash-only).
#
#   exit-0 child  -> restarted   (the 2026-09-25 outage: a clean exit sat down for 27 min)
#   exit-1 child  -> restarted
#   exit-75 child -> NOT restarted, wrapper ends (the bridge's own stand-down)
#   deliberate window -> restart happens but no alert / no proactive result file
#   no token      -> wrapper exits 0 at once (no child); startup-runtime's idle-job
#                    kickstart owns that case, and a live idle PID would read as running
#
# Run: bash tests/gateway-bridge-wrapper-self-heal.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway wrapper self-heal test")" || exit 1

# $1 = exit code the stub bridge returns; $2 = "deliberate" to pre-stamp a fresh
# deliberate-restart marker; $3 = "notoken" to run without a token.
# Prints: <restart-count> <alert-count> <proactive-files> <wrapper-alive-at-end>
run_case() {
  local code="$1" mode="${2:-}" tok="${3:-}" d
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  # sutando-config.sh: workspace -> the scratch ws; claude-home-path -> a missing env file
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$d/none.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  echo "import sys; sys.exit($code)" > "$d/src/remote-gateway-bridge.py"
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"
  # the wrapper calls the literal `python3`; shim it to the resolved interpreter
  printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  chmod +x "$d/shims/python3" "$d/shims/osascript"
  [ "$mode" = "deliberate" ] && date +%s > "$d/ws/state/channel-bridge-supervisor/deliberate-restart"
  local envtok="REMOTE_TASK_TOKEN=t"; [ "$tok" = "notoken" ] && envtok="REMOTE_TASK_TOKEN="
  ( cd "$d"; env -u AG2_REMOTE_TOKEN PATH="$d/shims:$PATH" $envtok \
      SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 & p=$!
    sleep 5
    if kill -0 $p 2>/dev/null; then alive=1; else alive=0; fi
    kill -TERM $p 2>/dev/null; wait $p 2>/dev/null
    echo "$alive" > alive.txt )
  # Prints: <restarts> <alert-files> <alive-at-end> <waiting-idle-lines>
  local restarts alerts alive idle
  restarts=$(grep -c "automatically restarting" "$d/o.log")
  alerts=$(ls "$d/ws/results"/proactive-gateway-bridge-restarted-* 2>/dev/null | wc -l | tr -d ' ')
  alive=$(cat "$d/alive.txt")
  idle=$(grep -c "nothing to run; exiting cleanly" "$d/o.log")
  echo "$restarts $alerts $alive $idle"
  rm -rf "$d"
}

read -r r0 a0 alive0 _ <<<"$(run_case 0)"
read -r r1 a1 _ _ <<<"$(run_case 1)"
read -r r75 _ alive75 _ <<<"$(run_case 75)"
read -r rd ad _ _ <<<"$(run_case 0 deliberate)"
read -r rn _ aliven idlen <<<"$(run_case 0 "" notoken)"

echo "exit-0  child -> $r0 restart(s), $a0 alert file(s)   (want >0 restarts: the outage case)"
echo "exit-1  child -> $r1 restart(s)                       (want >0)"
echo "exit-75 child -> $r75 restart(s), wrapper alive=$alive75 (want 0 and 0: stand-down)"
echo "deliberate    -> $rd restart(s), $ad alert file(s)    (want >0 restarts, 0 alerts)"
echo "no token      -> $rn restart(s), wrapper alive=$aliven  (want 0 restarts, alive=0: exits 0 at once, no child)"

[ "$r0" -gt 0 ] || { echo "FAIL: a clean exit-0 bridge was NOT relaunched (the outage would recur)"; fail=1; }
[ "$a0" -gt 0 ] || { echo "FAIL: a non-deliberate restart raised no alert"; fail=1; }
[ "$r1" -gt 0 ] || { echo "FAIL: a crashed bridge was NOT relaunched"; fail=1; }
[ "$r75" -eq 0 ] || { echo "FAIL: a declared stand-down (rc 75) was respawned"; fail=1; }
[ "$alive75" -eq 0 ] || { echo "FAIL: wrapper stayed alive after a stand-down"; fail=1; }
[ "$rd" -gt 0 ] || { echo "FAIL: deliberate window suppressed the RESTART, not just the alert"; fail=1; }
[ "$ad" -eq 0 ] || { echo "FAIL: a restart inside the deliberate window still alerted"; fail=1; }
[ "$rn" -eq 0 ] || { echo "FAIL: launched the bridge with no token"; fail=1; }
[ "$aliven" -eq 0 ] || { echo "FAIL: no-token wrapper stayed alive (an idle PID would read as a running bridge)"; fail=1; }
[ "$idlen" -gt 0 ] || { echo "FAIL: no-token path did not log 'nothing to run; exiting cleanly'"; fail=1; }

# Token rotation on a LEGACY install (.env carries only AG2_REMOTE_TOKEN): the
# child must see the rotated value on relaunch, and a removed token must stop the
# loop cleanly rather than relaunch with the stale export.
rotation_case() {
  local d; d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; claude-home-path) echo "$d/relay.env" ;; *) echo "$d/none.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  echo "AG2_REMOTE_TOKEN=first" > "$d/relay.env"
  # each child records the token it was launched with, then exits 0 (relaunch path)
  printf 'import os,sys\nopen("%s/seen.log","a").write(os.environ.get("REMOTE_TASK_TOKEN","<unset>")+"\\n")\nsys.exit(0)\n' "$d" > "$d/src/remote-gateway-bridge.py"
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"
  printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"; printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
  chmod +x "$d/shims/python3" "$d/shims/osascript"
  ( cd "$d"; env -u REMOTE_TASK_TOKEN -u AG2_REMOTE_TOKEN PATH="$d/shims:$PATH" SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
      bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 & p=$!
    sleep 1.5; echo "AG2_REMOTE_TOKEN=second" > "$d/relay.env"
    sleep 2.5; : > "$d/relay.env"          # token removed
    sleep 3
    if kill -0 $p 2>/dev/null; then echo 1 > alive.txt; else echo 0 > alive.txt; fi
    kill -TERM $p 2>/dev/null; wait $p 2>/dev/null )
  echo "$(grep -c '^first$' "$d/seen.log") $(grep -c '^second$' "$d/seen.log") $(grep -c 'token removed' "$d/o.log") $(cat "$d/alive.txt")"
  rm -rf "$d"
}
read -r seen_first seen_second removed_lines alive_after <<<"$(rotation_case)"
echo "legacy rotation -> first=$seen_first second=$seen_second removed-stop=$removed_lines alive=$alive_after (want first>0, second>0, removed-stop>0, alive=0)"
[ "$seen_first" -gt 0 ] || { echo "FAIL: first child never saw the legacy AG2_REMOTE_TOKEN mapping"; fail=1; }
[ "$seen_second" -gt 0 ] || { echo "FAIL: a rotated token in .env never reached a relaunched child (stale export won)"; fail=1; }
[ "$removed_lines" -gt 0 ] && [ "$alive_after" -eq 0 ] || { echo "FAIL: a removed token did not stop the loop cleanly"; fail=1; }

[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
