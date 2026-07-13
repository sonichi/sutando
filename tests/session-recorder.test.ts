// Unit coverage for the generalized SessionRecorder (step 5b-2): the injected
// ticker factory and the flush(extra) payload merge that let phone share the
// voice recorder without changing its stored observability shape.
//
// Seam: conversation-store honors SUTANDO_CONVERSATION_DB, so we point the
// recorder at a throwaway sqlite file and read the `sessions` row back. No
// collector endpoint is set, so the real usage tickers no-op on emit and their
// 30s interval never fires (flush clears it either way).
import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const DBP = join(mkdtempSync(join(tmpdir(), 'sutando-recorder-')), 'test.sqlite');

type Recorder = import('../src/live-agent-runtime.js').SessionRecorder;
let createSessionRecorder: (
	source: string,
	sessionId: string,
	opts?: { tickerFactory?: (model: string) => { stop: () => void } },
) => Recorder;

before(async () => {
	process.env.SUTANDO_CONVERSATION_DB = DBP;
	// Import AFTER the env is set — conversation-store reads DB_PATH at module init.
	({ createSessionRecorder } = await import('../src/live-agent-runtime.js'));
});

function fakeTicker() {
	const calls = { started: 0, stopped: 0, models: [] as string[] };
	const factory = (model: string) => {
		calls.started++;
		calls.models.push(model);
		return { stop: () => { calls.stopped++; } };
	};
	return { factory, calls };
}

function lastRow(where: string) {
	const db = new DatabaseSync(DBP);
	const row = db.prepare(`SELECT * FROM sessions WHERE ${where} ORDER BY rowid DESC LIMIT 1`).get() as Record<string, unknown>;
	db.close();
	return row;
}

test('phone flush: injected ticker is used, extra overrides base, row is phone-shaped', () => {
	const { factory, calls } = fakeTicker();
	// A real sessionId is passed to the constructor, but phone overrides it to
	// null in flush() — the row must keep session_id null (phone keys by call_sid).
	const rec = createSessionRecorder('phone', 'recorder-constructor-sid', { tickerFactory: factory });
	rec.startTicker('gemini-2.5-flash');
	assert.equal(calls.started, 1, 'injected factory drove startTicker');
	assert.deepEqual(calls.models, ['gemini-2.5-flash']);

	rec.events.push({ event: 'call_started', timestamp: new Date().toISOString() });
	rec.toolCalls.push({ name: 'hang_up', durationMs: 5, timestamp: new Date().toISOString() });

	rec.flush({
		sessionId: null,
		callSid: 'CA123',
		caller: '+15551234567',
		isOwner: true,
		isMeeting: false,
		durationMs: 4200,
		transcriptLines: 9,
		pendingTasks: 0,
	});
	assert.equal(calls.stopped, 1, 'flush stopped the injected ticker');

	const row = lastRow("call_sid='CA123'");
	assert.equal(row.source, 'phone');
	assert.equal(row.session_id, null, 'extra sessionId:null wins over constructor sessionId');
	assert.equal(row.call_sid, 'CA123');
	assert.equal(row.caller, '+15551234567');
	assert.equal(row.is_owner, 1);
	assert.equal(row.is_meeting, 0);
	assert.equal(row.duration_ms, 4200);
	assert.equal(row.transcript_lines, 9, 'transcriptLines from extra, not recorder.transcript (len 0)');
	assert.equal(row.tool_count, 1, 'tool_count from recorder.toolCalls');
	assert.equal(row.pending_tasks, 0);
});

test('flush is idempotent and stops the ticker before the write guard', () => {
	const { factory, calls } = fakeTicker();
	const rec = createSessionRecorder('phone', 'sid', { tickerFactory: factory });
	rec.startTicker('m');
	rec.flush({ sessionId: null, callSid: 'CAdup', durationMs: 1 });
	rec.flush({ sessionId: null, callSid: 'CAdup', durationMs: 1 });
	assert.equal(calls.stopped, 1, 'second flush neither re-stops nor re-emits');
	assert.equal(rec.wasFlushed, true);

	const db = new DatabaseSync(DBP);
	const n = db.prepare("SELECT COUNT(*) c FROM sessions WHERE call_sid='CAdup'").get() as { c: number };
	db.close();
	assert.equal(n.c, 1, 'exactly one row despite double flush');
});

test('voice flush(): no extra → base payload unchanged (session_id set, call_sid null)', () => {
	const { factory } = fakeTicker();
	const rec = createSessionRecorder('voice', 'voice-sess-1', { tickerFactory: factory });
	rec.startTicker('m');
	rec.transcript.push({ role: 'user', text: 'hi' });
	rec.transcript.push({ role: 'assistant', text: 'hello' });
	rec.toolCalls.push({ name: 'x', durationMs: 1, timestamp: new Date().toISOString() });
	rec.flush();

	const row = lastRow("source='voice' AND session_id='voice-sess-1'");
	assert.equal(row.session_id, 'voice-sess-1');
	assert.equal(row.call_sid, null, 'voice has no callSid');
	assert.equal(row.transcript_lines, 2, 'from recorder.transcript.length');
	assert.equal(row.tool_count, 1);
	assert.equal(row.pending_tasks, null, 'no pendingTasks for voice');
});

test('default ticker factory (no opts) is selected and flushes cleanly', () => {
	// Exercises the `opts.tickerFactory ?? <voice default>` branch. The default
	// wraps the real startVoiceTicker; with no collector endpoint it no-ops on
	// emit, and flush() clears the interval so the process does not hang.
	const rec = createSessionRecorder('voice', 'voice-default');
	rec.startTicker('m');
	rec.flush();
	assert.equal(rec.wasFlushed, true);
	const row = lastRow("session_id='voice-default'");
	assert.equal(row.source, 'voice');
});
