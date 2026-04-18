#!/usr/bin/env bash
# POC for the voice-metrics reconnect bug fix (branch fix/voice-metrics-reset-on-reconnect).
#
# Bug:  bodhi fires `onSessionStart` only on the FIRST ACTIVE transition
#       (state-machine guard `!this.startedAt`, field never reset). Our
#       voice-agent relies on that hook to reset `metricsWritten=false`
#       and clear `voiceEvents`, so every client-reconnect in the same
#       process produces a lost metrics record.
#
# Fix:  wrap bodhi's `handleClientConnected` on the VoiceSession the
#       same way we already wrap `handleClientDisconnected`. On the
#       2nd+ connect, reset metrics bookkeeping ourselves — bypassing
#       bodhi's guard. First connect is left alone so the existing
#       onSessionStart path still runs normally.
#
# This script is a static-assertion POC (no live bodhi session). It
# proves the code is structured the way we claim, plus bounds the
# regression surface.

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✓ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ✗ $1"; }

SRC="src/voice-agent.ts"
BODHI="node_modules/bodhi-realtime-agent/dist/index.js"

# Phase A — confirm the bodhi bug surface exists (precondition for the fix)
echo "━━━ Phase A: bodhi state-machine guard ━━━"
grep -q 'newState === "ACTIVE" && !this.startedAt' "$BODHI" \
    && ok "A1: bodhi:index.js guards onSessionStart with !startedAt (first-active-only)" \
    || bad "A1: expected !startedAt guard not found — bodhi upgrade may have changed the shape"
a2_count=$(grep -c 'startedAt = null' "$BODHI")
[ "$a2_count" -le "1" ] \
    && ok "A2: bodhi never resets startedAt on CLOSED transition (init-only, count=$a2_count)" \
    || bad "A2: bodhi DOES reset startedAt (count=$a2_count) — fix may be unnecessary after all"

# Phase B — confirm the fix is in place
echo ""
echo "━━━ Phase B: voice-agent fix ━━━"
grep -q "clientHasConnectedOnce" "$SRC" \
    && ok "B1: reconnect-guard variable present in voice-agent.ts" \
    || bad "B1: clientHasConnectedOnce not found — fix missing or reverted"
grep -q "handleClientConnected = () =>" "$SRC" \
    && ok "B2: handleClientConnected is wrapped" \
    || bad "B2: handleClientConnected wrap missing"
grep -q 'session_started:client_reconnect' "$SRC" \
    && ok "B3: synthetic reconnect event emitted for observability" \
    || bad "B3: synthetic reconnect event marker missing"
b4_count=$(grep -c 'metricsWritten = false' "$SRC")
[ "$b4_count" -ge "2" ] \
    && ok "B4: metricsWritten reset happens in ≥2 sites (count=$b4_count)" \
    || bad "B4: metricsWritten reset count wrong (got $b4_count, expected ≥2)"
# First-connect is untouched: guard checks clientHasConnectedOnce BEFORE origConnect()
grep -B1 -A3 'handleClientConnected = () =>' "$SRC" | grep -q 'clientHasConnectedOnce' \
    && ok "B5: first connect falls through to origConnect without reset" \
    || bad "B5: first-connect fallthrough not visible in the wrap"

# Phase C — regression shape: the existing disconnect wrap is unchanged
echo ""
echo "━━━ Phase C: disconnect wrap unchanged ━━━"
grep -q "handleClientDisconnected = () =>" "$SRC" \
    && ok "C1: handleClientDisconnected wrap still present (pre-existing)" \
    || bad "C1: disconnect wrap was accidentally removed"
grep -q "origDisconnect()" "$SRC" && grep -q "writeVoiceMetrics();" "$SRC" \
    && ok "C2: disconnect still calls origDisconnect() + writeVoiceMetrics()" \
    || bad "C2: disconnect wrap body shape changed"

# Phase D — typescript compiles
echo ""
echo "━━━ Phase D: TypeScript ━━━"
if command -v npx >/dev/null 2>&1; then
    npx tsc --noEmit 2>&1 | grep -E "voice-agent" > /tmp/tsc-voice-agent.log || true
    if [ -s /tmp/tsc-voice-agent.log ]; then
        bad "D1: tsc --noEmit has errors in voice-agent.ts"
        cat /tmp/tsc-voice-agent.log | head -5
    else
        ok "D1: voice-agent.ts compiles clean (tsc --noEmit)"
    fi
    rm -f /tmp/tsc-voice-agent.log
else
    echo "  (skip D1 — npx not available)"
fi

echo ""
echo "━━━ Summary ━━━"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "Fix POC FAILED. Runtime verification still needed: connect browser"
    echo "to voice-agent, talk briefly, close tab, reopen, talk briefly again,"
    echo "close. Expect TWO distinct entries in data/voice-metrics.jsonl with"
    echo "different timestamps and at least one containing"
    echo "'session_started:client_reconnect' in events."
    exit 1
else
    echo ""
    echo "Static POC OK. Runtime next: start voice-agent, do two"
    echo "connect-disconnect cycles, confirm two new jsonl entries."
fi
