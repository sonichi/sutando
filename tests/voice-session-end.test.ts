// After a voice session closes, the core gets a task about it (bodhi's post-session pipeline contract).
// Run: npx tsx --test --test-force-exit tests/voice-session-end.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parse as parseDotenv } from 'dotenv';

const TMP = mkdtempSync(join(tmpdir(), 'session-end-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['tasks', 'results', 'state']) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { createSessionEndPipeline, createCallEndReporter, sessionEndTask, worthATask, SESSION_END_MAX_CHARS, callEndInstruction, DEFAULT_CALL_END_INSTRUCTION } = await import('../src/voice-session-end.js');
const { submitVoiceSessionEndTask, setVoiceSessionOrigin, snapshotVoiceSessionOrigin, endVoiceClientSession, setVoiceTaskOriginResolver, voiceTaskOrigin, forwardOfflineVoiceResult } = await import('../src/task-bridge.js');
const { roomOriginFromTaskHeader } = await import('../skills/ag2space-voice/room-binding.js');

const item = (role: string, content: string, timestamp = 0) => ({ role, content, timestamp }) as never;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
function input(items: unknown[], reason = 'client_disconnect') {
	return {
		sessionId: 's1', reason,
		build: () => ({
			snapshot: {
				sessionId: 's1', userId: 'user', initialAgentName: 'main', finalAgentName: 'main', transferPath: ['main'], reason,
				startedAt: Date.UTC(2026, 9, 10, 18, 0), endedAt: Date.UTC(2026, 9, 10, 18, 12), durationMs: 12 * 60_000,
				conversation: { items }, metrics: { turnCount: 4, toolCallCount: 1, agentTransferCount: 0 },
			},
			stores: {},
		}),
	} as never;
}

describe('session-end pipeline', () => {
	it('a closed session reaches the step once, with its facts and its items', async () => {
		const got: Array<{ reason: string; turnCount: number; items: unknown[] }> = [];
		const p = createSessionEndPipeline((s) => { got.push({ reason: s.reason, turnCount: s.turnCount, items: [...s.items] }); });
		const processed: string[] = [];
		p.events.onProcessed((r) => processed.push((r.results[0] as { status: string }).status));
		const run = p.dispatch(input([item('user', 'check PR 5308'), item('assistant', 'merged')]));
		assert.equal(run.outcome, 'accepted');
		const report = await run.report;
		assert.equal(got.length, 1);
		assert.equal(got[0].reason, 'client_disconnect');
		assert.equal(got[0].turnCount, 4);
		assert.equal(got[0].items.length, 2);
		assert.equal((report.results[0] as { status: string }).status, 'completed');
		assert.deepEqual(processed, ['completed']);
		assert.equal(p.stats().completed, 1);
	});

	it('a session the owner never spoke in is skipped', async () => {
		let calls = 0;
		const p = createSessionEndPipeline(() => { calls++; });
		const report = await p.dispatch(input([item('assistant', 'Hi, how can I help?'), item('user', '[System: greeting]')])).report;
		assert.equal(calls, 0);
		assert.equal((report.results[0] as { status: string }).status, 'skipped');
	});

	it('a failing step resolves its report as failed and never throws out of dispatch', async () => {
		const logs: string[] = [];
		const p = createSessionEndPipeline(() => { throw new Error('disk full'); }, (m) => logs.push(m));
		const report = await p.dispatch(input([item('user', 'hello')])).report;
		assert.equal((report.results[0] as { status: string }).status, 'failed');
		assert.ok(logs.some((l) => l.includes('disk full')));
		await p.drain();
	});
});

describe('the session-end task', () => {
	it('says when the session ran and how it ended, asks the core to do its follow-ups, and carries its spoken lines', async () => {
		const s = { sessionId: 's1', reason: 'client_disconnect', startedAt: Date.UTC(2026, 9, 10, 18, 0), endedAt: Date.UTC(2026, 9, 10, 18, 12),
			durationMs: 12 * 60_000, turnCount: 4, toolCallCount: 1,
			items: [item('user', 'check PR 5308'), item('tool_call', '{}'), item('assistant', 'It is merged.')] };
		assert.ok(worthATask(s));
		const { summary, transcript } = sessionEndTask(s);
		assert.match(summary, /^VOICE_SESSION_ENDED: the voice session s1 ended \(client_disconnect\)\. 2026-10-10T18:00:00\.000Z to 2026-10-10T18:12:00\.000Z, about 12 min, 4 turns, 1 tool calls\./);
		assert.match(summary, /not finished on the call \(a follow-up: .*\), do it now, or schedule it if it has a time, and reply with what you did\./);
		assert.match(summary, /If the call leaves nothing to do, answer with \[no-send\]\./);
		assert.equal(transcript, 'user: check PR 5308\nassistant: It is merged.');
		const id = await submitVoiceSessionEndTask(summary, transcript);
		const body = readFileSync(join(TMP, 'tasks', `${id}.txt`), 'utf-8');
		assert.match(body, /^source: voice$/m);
		assert.match(body, /^priority: low$/m);
		// The call-end lifecycle wrote it, not the owner: every header line above task:, as
		// tests/task-watch-owner.test.py's call-end fixture has them.
		const head = body.slice(0, body.indexOf('\ntask: ')).split('\n')
			.filter((l) => !/^(id|timestamp|local_time|envelope_hmac): /.test(l))
			.map((l) => l.replace(/^user_id: .+$/, 'user_id: *'));
		assert.deepEqual(head, ['source: voice', 'interaction_type: system_event', 'media_form: live_stream',
			'channel_id: local-voice', 'user_id: *', 'access_tier: owner', 'priority: low']);
		assert.doesNotMatch(body, /realtime_audio/);
		assert.match(body, /^task: VOICE_SESSION_ENDED:/m);
		assert.match(body, /--- the session's spoken lines[^\n]*---\n[\s\S]*user: check PR 5308/);
		assert.equal(readdirSync(join(TMP, 'tasks')).filter((f) => f.endsWith('.txt')).length, 1);
	});

	const fixture = () => ({ sessionId: 's1', reason: 'client_disconnect', startedAt: Date.UTC(2026, 9, 10, 18, 0), endedAt: Date.UTC(2026, 9, 10, 18, 12),
		durationMs: 12 * 60_000, turnCount: 4, toolCallCount: 1,
		items: [item('user', 'check PR 5308'), item('tool_call', '{}'), item('assistant', 'It is merged.')] });
	const facts = 'VOICE_SESSION_ENDED: the voice session s1 ended (client_disconnect). ' +
		'2026-10-10T18:00:00.000Z to 2026-10-10T18:12:00.000Z, about 12 min, 4 turns, 1 tool calls. ';

	it('the default instruction is today\'s text, byte for byte', () => {
		const today = 'If the user asked for, or agreed to, something that was not finished on the call (a follow-up: something to look up, ' +
			'send, write, remind them of, or do later), do it now, or schedule it if it has a time, and reply with what you did. ' +
			'Keep any durable fact from the call (a preference, a commitment, a follow-up) in memory. ' +
			'If the call leaves nothing to do, answer with [no-send].';
		assert.equal(DEFAULT_CALL_END_INSTRUCTION, today);
		assert.equal(sessionEndTask(fixture()).summary, `${facts}Read the spoken lines below. ${today}`);
	});

	it('a host instruction replaces only the instruction', () => {
		const { summary, transcript } = sessionEndTask(fixture(), 'List them.');
		assert.equal(summary, `${facts}Read the spoken lines below. List them.`);
		assert.equal(transcript, sessionEndTask(fixture()).transcript);
		assert.equal(transcript, 'user: check PR 5308\nassistant: It is merged.');
	});

	it('callEndInstruction: the host\'s variable when set, today\'s text when unset or blank', () => {
		assert.equal(callEndInstruction({}), DEFAULT_CALL_END_INSTRUCTION);
		assert.equal(callEndInstruction({ SUTANDO_VOICE_CALL_END_INSTRUCTION: '  ' }), DEFAULT_CALL_END_INSTRUCTION);
		assert.equal(callEndInstruction({ SUTANDO_VOICE_CALL_END_INSTRUCTION: ' do Y ' }), 'do Y');
	});

	it('.env.example\'s line, uncommented, reads the same in startup\'s bash source and in voice-agent\'s dotenv', () => {
		// startup-runtime.sh runs `set -a; source .env` under set -e: an unquoted space or apostrophe aborts startup.
		const example = readFileSync(join(import.meta.dirname, '..', '.env.example'), 'utf-8');
		const line = example.split('\n').find((l) => l.startsWith('# SUTANDO_VOICE_CALL_END_INSTRUCTION='));
		assert.ok(line, '.env.example documents the variable as a commented example');
		const file = join(TMP, 'call-end-example.env');
		const text = `A=1\n${line.slice(2)}\nB=after\n`;
		writeFileSync(file, text);
		const r = spawnSync('bash', ['-c', 'set -e; set -a; source "$1"; set +a; printf "%s|%s" "$SUTANDO_VOICE_CALL_END_INSTRUCTION" "$B"', '_', file], {
			encoding: 'utf-8', env: { PATH: process.env.PATH ?? '/usr/bin:/bin' },
		});
		assert.equal(r.status, 0, `bash could not source the uncommented line: ${r.stderr}`);
		const parsed = parseDotenv(text);
		assert.match(parsed.SUTANDO_VOICE_CALL_END_INSTRUCTION, /\[no-send\]/);
		assert.equal(r.stdout, `${parsed.SUTANDO_VOICE_CALL_END_INSTRUCTION}|after`);
		assert.equal(parsed.B, 'after');
		// The example itself holds nothing bash would expand inside double quotes.
		assert.doesNotMatch(line.slice(line.indexOf('=') + 1), /[$`\\]/);
	});

	// Both loaders, as startup and voice-agent run them: bash `set -a; source` and dotenv's parse.
	const loadBoth = (name: string, valueSource: string) => {
		const file = join(TMP, `${name}.env`);
		const text = `SUTANDO_VOICE_CALL_END_INSTRUCTION=${valueSource}\n`;
		writeFileSync(file, text);
		const r = spawnSync('bash', ['-c', 'set -e; set -a; source "$1"; set +a; printf "%s" "$SUTANDO_VOICE_CALL_END_INSTRUCTION"', '_', file], {
			encoding: 'utf-8', env: { PATH: process.env.PATH ?? '/usr/bin:/bin' },
		});
		assert.equal(r.status, 0, `bash could not source ${valueSource}: ${r.stderr}`);
		return { bash: r.stdout, dotenv: parseDotenv(text).SUTANDO_VOICE_CALL_END_INSTRUCTION };
	};

	it('single quotes keep $, ` and \\ literal in both startup\'s bash source and dotenv', () => {
		const literal = 'Pay $5 by `noon`, path C:\\x. Else [no-send].';
		const { bash, dotenv } = loadBoth('single-quoted', `'${literal}'`);
		assert.equal(bash, literal);
		assert.equal(dotenv, literal);
	});

	it('in double quotes bash expands a $ that dotenv keeps, and .env.example says what to use instead', () => {
		const { bash, dotenv } = loadBoth('double-quoted-dollar', '"Pay $5 now."');
		assert.equal(dotenv, 'Pay $5 now.');
		assert.notEqual(bash, dotenv, 'bash expands $5 inside double quotes');
		const example = readFileSync(join(import.meta.dirname, '..', '.env.example'), 'utf-8');
		const at = example.indexOf('# SUTANDO_VOICE_CALL_END_INSTRUCTION=');
		const block = example.slice(example.lastIndexOf('\n\n', at), at);
		assert.match(block, /single quotes/, '.env.example names single quotes for text with $, ` or \\');
		assert.match(block, /host's environment/, '.env.example names the host environment for text with both');
	});

	it('a long session keeps its end', () => {
		const long = Array.from({ length: 400 }, (_, i) => item(i % 2 ? 'assistant' : 'user', `line ${i} ${'x'.repeat(80)}`));
		const { transcript } = sessionEndTask({ sessionId: 's', reason: 'r', startedAt: 0, endedAt: 1, durationMs: 1, turnCount: 1, toolCallCount: 0, items: long });
		assert.ok(transcript.length <= SESSION_END_MAX_CHARS + 60);
		assert.match(transcript, /^\[… \d+ earlier characters\]/);
		assert.match(transcript, /line 399 x+$/);
	});

	it('voice-agent reports each call on hang-up, and a call in progress at shutdown through bodhi', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /postSessionPipeline: callEnd\.pipeline,/);
		assert.match(src, /drainPostSession: true,/, 'close() is followed by process.exit');
		assert.match(src, /onClientConnected: \(\) => callEnd\.clientConnected\(\),/);
		assert.match(src, /onClientDisconnected: \(\) => \{\n\t\t\tcallEnd\.clientDisconnected\(\);/);
		assert.match(src, /subscribe\('turn\.end', \(\) => callEnd\.collect\(\)\);/);
		assert.match(src, /beforeConversationClear = \(\) => callEnd\.collect\(\);/);
		assert.match(src, /sessionEndTask\(ended, callEndInstruction\(\)\)/);
		assert.match(src, /await submitVoiceSessionEndTask\(summary, transcript, ended\.origin \?\? null\);/);
	});
});

describe('a call ends when the user hangs up', () => {
	function call() {
		let t = 1000;
		const live: unknown[] = [];
		const ended: Array<{ reason: string; items: string[] }> = [];
		const r = createCallEndReporter({
			sessionId: 's1', items: () => live as never, graceMs: 30, now: () => t,
			onEnded: (s) => { ended.push({ reason: s.reason, items: s.items.map((i) => i.content) }); },
		});
		const say = (role: string, content: string) => { t += 10; live.push(item(role, content, t)); };
		return { r, live, ended, say, advance: (ms: number) => { t += ms; } };
	}

	it('no client back within the grace period: one task for the call', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'check PR 5308');
		c.say('assistant', 'merged');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, [{ reason: 'user_hangup', items: ['check PR 5308', 'merged'] }]);
	});

	it('a refresh or a blip that reconnects within the grace period is the same call', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'first');
		c.r.clientDisconnected();
		await tick(10);
		c.r.clientConnected();
		c.say('user', 'second');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, [{ reason: 'user_hangup', items: ['first', 'second'] }]);
	});

	it('a goodbye that clears the live conversation before the hang-up still reports what was said', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'remind me Friday');
		c.say('assistant', 'done');
		c.say('user', 'goodbye');
		c.r.collect(); // right before the clear
		c.live.length = 0;
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended[0].items, ['remind me Friday', 'done', 'goodbye']);
	});

	it('the next call reports only its own lines; earlier lines still in the live context are not repeated', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'call one');
		c.r.clientDisconnected();
		await tick(60);
		c.advance(100);
		c.r.clientConnected();
		c.say('user', 'call two');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended.map((e) => e.items), [['call one'], ['call two']]);
	});

	it('at shutdown a call in progress is reported once through bodhi\'s pipeline; an ended call is not reported again', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'in progress');
		const snapItems = [...c.live];
		await c.r.pipeline.dispatch(input(snapItems, 'user_hangup')).report;
		assert.deepEqual(c.ended.map((e) => e.items), [['in progress']]);
		await c.r.pipeline.dispatch(input(snapItems, 'user_hangup')).report;
		assert.equal(c.ended.length, 1, 'no call left to report');
	});

	it('a call the owner never spoke in sends nothing', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('assistant', 'Hi!');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, []);
	});
});

describe('the hang-up task carries the room the call was docked in', () => {
	const ROOM = '!roomA:s.space';
	const roomOrigin = (id: string) => ({
		channel: 'ag2space', target: id, headers: { channel_kind: 'room', source_room_id: id },
		contextLine: `room_context: ${id} — post there only what its members are meant to read`,
	});
	function call() {
		let t = 1000;
		const live: unknown[] = [];
		const ended: Array<{ items: string[]; origin: unknown }> = [];
		const r = createCallEndReporter({
			sessionId: 's1', items: () => live as never, graceMs: 30, now: () => t,
			origin: snapshotVoiceSessionOrigin,
			onEnded: (s) => { ended.push({ items: s.items.map((i) => i.content), origin: s.origin ?? null }); },
		});
		const say = (role: string, content: string) => { t += 10; live.push(item(role, content, t)); };
		// As the host does: the reporter hears the disconnect first, then the client's origin is released.
		const hangUp = () => { r.clientDisconnected(); endVoiceClientSession(); };
		return { r, live, ended, say, hangUp };
	}
	after(() => setVoiceSessionOrigin(null));

	it('the room bound during the call is on the report after the disconnect released it', async () => {
		const c = call();
		c.r.clientConnected();
		setVoiceSessionOrigin(roomOrigin(ROOM)); // the client announces its room after connecting
		c.say('user', 'remind me Friday');
		c.r.collect();
		c.hangUp();
		assert.equal(snapshotVoiceSessionOrigin(), null, 'the live origin is gone before the grace period ends');
		await tick(60);
		assert.equal(c.ended.length, 1);
		assert.equal((c.ended[0].origin as { target: string }).target, ROOM);
	});

	it('a call that moved to the DM before hanging up reports no room', async () => {
		const c = call();
		setVoiceSessionOrigin(roomOrigin(ROOM));
		c.r.clientConnected();
		c.say('user', 'hello');
		c.r.collect();
		setVoiceSessionOrigin(null);
		c.say('user', 'and now in the DM');
		c.hangUp();
		await tick(60);
		assert.equal(c.ended[0].origin, null);
	});

	it('a call with no docked room reports none, and the next call does not inherit an earlier room', async () => {
		const c = call();
		setVoiceSessionOrigin(roomOrigin(ROOM));
		c.r.clientConnected();
		c.say('user', 'in the room');
		c.hangUp();
		await tick(60);
		c.r.clientConnected();
		c.say('user', 'undocked');
		c.hangUp();
		await tick(60);
		assert.deepEqual(c.ended.map((e) => (e.origin as { target?: string } | null)?.target ?? null), [ROOM, null]);
	});

	it('at shutdown a call still attached reports its live room', async () => {
		const c = call();
		c.r.clientConnected();
		setVoiceSessionOrigin(roomOrigin(ROOM));
		c.say('user', 'in progress');
		await c.r.pipeline.dispatch(input([...c.live], 'user_hangup')).report;
		assert.equal((c.ended[0].origin as { target: string }).target, ROOM);
		setVoiceSessionOrigin(null);
	});

	it('the task names the docked room as voice_room_id only; its delivery headers are the undocked ones', async () => {
		const ROOM_ID = '!roomA:s.space';
		const header = (body: string) => body.slice(0, body.indexOf('\ntask:') + 1);
		const docked = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: x', 'user: hi', roomOrigin(ROOM_ID));
		const h = header(readFileSync(join(TMP, 'tasks', `${docked}.txt`), 'utf-8'));
		assert.match(h, new RegExp(`^voice_room_id: ${ROOM_ID}$`, 'm'));
		assert.match(h, /^channel_id: local-voice$/m);
		assert.match(h, /^source: voice$/m);
		assert.match(h, /^priority: low$/m);
		assert.doesNotMatch(h, /^(source_room_id|channel_kind):/m);
		assert.doesNotMatch(readFileSync(join(TMP, 'tasks', `${docked}.txt`), 'utf-8'), /room_context:/);
		await tick(2);
		const plain = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: y', 'user: hi');
		const plainBody = readFileSync(join(TMP, 'tasks', `${plain}.txt`), 'utf-8');
		assert.match(plainBody, /^channel_id: local-voice$/m);
		assert.doesNotMatch(plainBody, /^voice_room_id:/m);
	});

	it('its result is delivered as before: the room resolver finds no origin, and the offline result goes the owner-DM way', async () => {
		setVoiceTaskOriginResolver(roomOriginFromTaskHeader);
		try {
			const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: z', 'user: hi', roomOrigin('!roomA:s.space'));
			assert.equal(voiceTaskOrigin(id), null);
			const file = await forwardOfflineVoiceResult(id, 'did the follow-up');
			assert.match(file, /^proactive-result-task-\d+-\d+\.txt$/);
			assert.doesNotMatch(file, /\.to-/);
		} finally {
			setVoiceTaskOriginResolver(null);
		}
	});
});
