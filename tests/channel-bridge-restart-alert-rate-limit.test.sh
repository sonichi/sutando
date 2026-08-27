#!/usr/bin/env bash
# 50 restarts must not produce 50 owner alerts. Before the rate limit they did:
# ~945 alerts in five days retired the alert entirely.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export WORKSPACE="$TMP/ws"; STATE_DIR="$WORKSPACE/state/channel-bridge-supervisor"
mkdir -p "$STATE_DIR" "$WORKSPACE/results"
CHANNEL=testchan
# Load only the alerting block, with osascript stubbed out.
osascript() { :; }
# Extract through the END of emit_restart_alert (the 2nd column-0 brace), not the 1st.
BLOCK="$(awk '/^# One alert per flap EPISODE/{on=1} on{print} on&&/^}$/{n++; if(n==2) exit}' \
         "$REPO/src/launchd/channel-bridge-wrapper.sh")"
eval "$BLOCK"
type emit_restart_alert >/dev/null 2>&1 || { echo "  FAIL could not load emit_restart_alert"; exit 1; }
count_alerts() { ls -1 "$WORKSPACE/results/" 2>/dev/null | grep -c "bridge-restarted" || true; }
fail=0
# 1. a crashloop: 50 restarts back to back
for _ in $(seq 1 50); do emit_restart_alert; done
n="$(count_alerts)"
if [ "$n" -eq 1 ]; then echo "  ok   50 rapid restarts -> $n alert"; else echo "  FAIL 50 rapid restarts -> $n alerts (want 1)"; fail=1; fi
# 2. escalation fires once when the flap outlives the window
read -r f c lr la < "$STATE_DIR/$CHANNEL.flap"
printf '%s %s %s %s\n' "$f" "$c" "$(date +%s)" "$(( $(date +%s) - 2000 ))" > "$STATE_DIR/$CHANNEL.flap"
emit_restart_alert; emit_restart_alert
n2="$(count_alerts)"
if [ "$n2" -eq 2 ]; then echo "  ok   sustained flap -> exactly 1 escalation (total $n2)"; else echo "  FAIL sustained flap -> total $n2 (want 2)"; fail=1; fi
# 3. a NEW episode after quiet still alerts — the fix must not mute a real restart
printf '%s %s %s %s\n' "$f" "$c" "$(( $(date +%s) - 5000 ))" "$(( $(date +%s) - 5000 ))" > "$STATE_DIR/$CHANNEL.flap"
emit_restart_alert
n3="$(count_alerts)"
if [ "$n3" -eq 3 ]; then echo "  ok   restart after quiet period -> alerts again (total $n3)"; else echo "  FAIL after quiet -> total $n3 (want 3)"; fail=1; fi
# 4. corrupt state must not silence the alert
echo "garbage not numbers" > "$STATE_DIR/$CHANNEL.flap"
emit_restart_alert
n4="$(count_alerts)"
if [ "$n4" -eq 4 ]; then echo "  ok   corrupt state -> fails OPEN, still alerts (total $n4)"; else echo "  FAIL corrupt state -> total $n4 (want 4)"; fail=1; fi
[ "$fail" -eq 0 ] && echo "==== ALL RATE-LIMIT ASSERTIONS PASSED ====" || echo "==== FAILURES ===="
exit "$fail"
