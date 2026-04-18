#!/usr/bin/env node
// Before/after POC for the voice-metrics reconnect fix.
//
// Mocks bodhi's `VoiceSession` state machine tightly enough to exercise
// our voice-agent's wrap logic. Two scenarios:
//
//   BEFORE:  wrap only handleClientDisconnected (the production code
//            pre-fix). 2 connect-disconnect cycles → onSessionStart
//            fires ONCE (because the `!startedAt` guard sticks), so
//            metricsWritten flips true after the first disconnect and
//            every subsequent writeVoiceMetrics() returns early.
//            Expected record count: 1.
//
//   AFTER:   wrap BOTH handleClientDisconnected AND
//            handleClientConnected. On the 2nd+ connect, we reset
//            metricsWritten=false + voiceEvents ourselves. Bypasses the
//            bodhi guard.
//            Expected record count: 2.
//
// This is a controlled experiment: the mock session is identical in
// both runs; only the wrap logic changes.

// (No imports needed — pure mock, no filesystem or module deps.)

// ---- Mock bodhi VoiceSession -------------------------------------------------
function makeSession({ onSessionStart, onSessionEnd }) {
  const state = { startedAt: null, sessionId: 'mock-session-id' };
  const api = {
    _state: 'CREATED',
    handleClientConnected() {
      // Mirrors bodhi:index.js:1219 — `newState==='ACTIVE' && !this.startedAt`
      this._state = 'ACTIVE';
      if (!state.startedAt) {
        state.startedAt = Date.now();
        onSessionStart?.({ sessionId: state.sessionId });
      }
    },
    handleClientDisconnected() {
      this._state = 'CLOSED';
      onSessionEnd?.({ sessionId: state.sessionId, reason: 'normal' });
      // NOTE: startedAt is NOT reset — matches bodhi behavior.
    },
  };
  return api;
}

// ---- Mock voice-agent bookkeeping + wrap factories --------------------------
function buildVoiceAgent({ applyReconnectFix }) {
  const records = [];
  const voiceEvents = [];
  let metricsWritten = false;
  let voiceSessionStart = 0;

  function writeVoiceMetrics() {
    if (metricsWritten) return;
    metricsWritten = true;
    records.push({
      timestamp: new Date().toISOString(),
      durationMs: Date.now() - voiceSessionStart,
      events: voiceEvents.slice(),
    });
  }

  const session = makeSession({
    onSessionStart: () => {
      voiceSessionStart = Date.now();
      metricsWritten = false;
      voiceEvents.length = 0;
      voiceEvents.push({ event: 'session_started' });
    },
    onSessionEnd: () => {
      voiceEvents.push({ event: 'session_ended' });
      writeVoiceMetrics();
    },
  });

  // Wrap 1 (always on, pre-existing in production): flush on disconnect.
  const origDisconnect = session.handleClientDisconnected.bind(session);
  session.handleClientDisconnected = () => {
    origDisconnect();
    writeVoiceMetrics();
  };

  // Wrap 2 (the fix, only applied in the AFTER run): reset state on reconnect.
  if (applyReconnectFix) {
    let clientHasConnectedOnce = false;
    const origConnect = session.handleClientConnected.bind(session);
    session.handleClientConnected = () => {
      if (clientHasConnectedOnce) {
        voiceSessionStart = Date.now();
        metricsWritten = false;
        voiceEvents.length = 0;
        voiceEvents.push({ event: 'session_started:client_reconnect' });
      }
      clientHasConnectedOnce = true;
      origConnect();
    };
  }

  return { session, records };
}

// ---- Scenario runner --------------------------------------------------------
function run({ applyReconnectFix }) {
  const { session, records } = buildVoiceAgent({ applyReconnectFix });
  // Two connect-disconnect cycles (user opens tab, talks, closes, reopens, talks, closes).
  session.handleClientConnected();
  session.handleClientDisconnected();
  session.handleClientConnected();
  session.handleClientDisconnected();
  return records;
}

// ---- Main -------------------------------------------------------------------
function pretty(records) {
  return records.map((r, i) => `  #${i + 1}: events=[${r.events.map(e => e.event).join(', ')}]`).join('\n') || '  (no records written)';
}

console.log('━━━ BEFORE (no reconnect fix) ━━━');
const before = run({ applyReconnectFix: false });
console.log(pretty(before));
console.log(`  → ${before.length} record(s) written\n`);

console.log('━━━ AFTER (reconnect fix applied) ━━━');
const after = run({ applyReconnectFix: true });
console.log(pretty(after));
console.log(`  → ${after.length} record(s) written\n`);

let failed = 0;
if (before.length === 1) {
  console.log('✓ BEFORE: bug reproduced — 2 sessions collapsed into 1 record');
} else {
  console.log(`✗ BEFORE: expected 1 record, got ${before.length} — mock doesn't reproduce the bug correctly`);
  failed++;
}
if (after.length === 2) {
  console.log('✓ AFTER: fix works — each session produces its own record');
} else {
  console.log(`✗ AFTER: expected 2 records, got ${after.length} — fix is incomplete`);
  failed++;
}
const hasReconnectMarker = after.some(r => r.events.some(e => e.event === 'session_started:client_reconnect'));
if (hasReconnectMarker) {
  console.log('✓ AFTER: reconnect record carries the synthetic marker for observability');
} else {
  console.log('✗ AFTER: missing session_started:client_reconnect marker — observability incomplete');
  failed++;
}

process.exit(failed > 0 ? 1 : 0);
