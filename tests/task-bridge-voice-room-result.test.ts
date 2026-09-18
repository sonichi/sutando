import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Room-bound voice (Zerlinda 2026-09-17: "When talk to sutando in microphone,
// sends back to main conversation, talking but replying to the dm"). The
// client announces its room with a `session.context` frame (after
// session.config, then on every room change); the task bridge binds it, stamps
// every delegated task with it, and posts the result back into that room
// through the gateway's `[channel: <room>]` proactive marker.

// Fixtures use a tmp workspace, never the live queue: SUTANDO_TEST_MODE=1 must
// be set before the source-ordered `await import` binds the bridge's paths.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-room-result-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
mkdirSync(RESULT_DIR, { recursive: true });

const {
	forwardVoiceResultToRoom, _isDeliveredResult, _shouldFallthrough, _shouldRegisterTaskRow,
	parseSessionContextFrame, applySessionContextFrame, sessionRoomNotice,
	setVoiceSessionRoom, getVoiceSessionRoom, MATRIX_ROOM_ID_RE, ROOM_NAME_MAX_CHARS, workTool,
	bindSessionContextFrame, setVoiceRoomVerifier, resolveVoiceResultRoom, forwardOfflineVoiceResult,
	requestVoiceRoomVerdict, readVoiceRoomVerdict, voiceRoomCheckKey, VOICE_ROOM_CHECK_DIR, VOICE_ROOM_VERDICT_TTL_S,
} = await import('../src/task-bridge.js');
const TASK_DIR = join(TMP, 'tasks');
const { SESSION_CONTEXT_TYPE, SESSION_CONTEXT_ACK_TYPE, buildSessionContextFrame, buildSessionContextAckFrame } = await import('../src/web-voice-transport.js');

after(() => {
	setVoiceSessionRoom(null);
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

describe('forwardVoiceResultToRoom — a room-bound result reaches its room, and voice never speaks it twice', () => {
	it('writes results/proactive-result-<id>-<ts>.to-ag2space.txt with [channel: <room>] as the first line', () => {
		const file = forwardVoiceResultToRoom('task-1700000000000', 'Three listings under $2k.\nSecond line.', '!abc123:ag2.space', 1_800_000_000);
		assert.equal(file, 'proactive-result-task-1700000000000-1800000000.to-ag2space.txt');
		const path = join(RESULT_DIR, file);
		assert.ok(existsSync(path));
		const body = readFileSync(path, 'utf-8');
		const [first, ...rest] = body.split('\n');
		assert.equal(first, '[channel: !abc123:ag2.space]', 'the gateway routes on the first line');
		assert.equal(rest.join('\n'), 'Three listings under $2k.\nSecond line.', 'the result follows, byte for byte');
	});

	it('claims the filename in the delivered set at once — the proactive-* fallthrough passes it, so the claim is what stops a second narration', () => {
		const file = forwardVoiceResultToRoom('task-1700000000001', 'done', '!abc123:ag2.space', 1_800_000_001);
		assert.equal(_shouldFallthrough(file), true, 'the drain would otherwise speak this file on its next tick');
		assert.equal(_shouldRegisterTaskRow(file), false, 'and it is not a task row either way');
		assert.equal(_isDeliveredResult(file), true);
		assert.equal(_isDeliveredResult('proactive-result-task-1700000000001-1800000002.to-ag2space.txt'), false, 'only the file actually written');
	});

	it('the name tag is the claim grammar the bridges read: proactive_destination(name) == "ag2space", never another bridge\'s', () => {
		// The gateway claim gate reads the FILENAME (proactive_routing.py), not the
		// body: an untagged name falls through to last-owner-activity, and a
		// Discord-routed owner would have that bridge claim a Matrix room result.
		const file = forwardVoiceResultToRoom('task-1700000000004', 'tagged', '!abc123:ag2.space', 1_800_000_004);
		const py = spawnSync('python3', ['-c', [
			'import sys; sys.path.insert(0, "src")',
			'from proactive_routing import proactive_destination, fallback_claims_name, should_claim_proactive_file',
			'from pathlib import Path',
			`name = ${JSON.stringify(file)}`,
			'print(proactive_destination(name), fallback_claims_name(name, "discord"), fallback_claims_name(name, "ag2space"), should_claim_proactive_file(name, Path("/nonexistent"), "ag2space"), should_claim_proactive_file(name, Path("/nonexistent"), "discord"))',
		].join('\n')], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), 'ag2space False True True False');
		assert.equal(_shouldFallthrough(file), true, 'voice still recognises the tagged name for its own dedupe');
	});

	it('the marker is the shape the gateway accepts (no dm-only, marker alone on its line)', () => {
		const file = forwardVoiceResultToRoom('task-1700000000002', '[dm-only]\nprivate', '!abc123:ag2.space', 1_800_000_003);
		const body = readFileSync(join(RESULT_DIR, file), 'utf-8');
		// A result that declares itself dm-only keeps that declaration below the
		// channel line; parse_markers then suppresses the redirect and the gateway
		// delivers to the owner — the privacy guard wins over the room, by design.
		assert.equal(body.split('\n')[0], '[channel: !abc123:ag2.space]');
		assert.equal(body.split('\n')[1], '[dm-only]');
	});
});

describe('parseSessionContextFrame — the client frame that binds the room', () => {
	it('accepts the transport\'s own frame (type literal parity with SESSION_CONTEXT_TYPE)', () => {
		assert.equal(SESSION_CONTEXT_TYPE, 'session.context');
		const frame = buildSessionContextFrame({ roomId: '!abc123:ag2.space', roomName: 'Commorai' });
		assert.deepEqual(parseSessionContextFrame(frame as unknown as Record<string, unknown>), { id: '!abc123:ag2.space', name: 'Commorai' });
		const dm = buildSessionContextFrame(null);
		assert.equal(parseSessionContextFrame(dm as unknown as Record<string, unknown>), null, 'the DM shape releases the room');
	});

	it('returns undefined for anything that is not a session.context frame', () => {
		assert.equal(parseSessionContextFrame({ type: 'voice.retryUpstream', room_id: '!abc:s' }), undefined);
		assert.equal(parseSessionContextFrame({}), undefined);
		assert.equal(parseSessionContextFrame(null), undefined);
		assert.equal(parseSessionContextFrame(undefined), undefined);
	});

	it('rejects a room id that is not a Matrix room id (null, never a partial binding)', () => {
		for (const bad of ['', ' ', 'room', '#alias:server', '!nocolon', '! space:server', '!a:b c', 42, null, ['!a:b']]) {
			assert.equal(parseSessionContextFrame({ type: 'session.context', room_id: bad, room_name: 'x' }), null, `room_id=${JSON.stringify(bad)}`);
		}
		assert.match('!abc123:ag2.space', MATRIX_ROOM_ID_RE);
		assert.match('!x:localhost:8008', MATRIX_ROOM_ID_RE, 'a server with a port is still one token');
	});

	it('caps and flattens the room name, and drops an empty one', () => {
		const long = 'n'.repeat(ROOM_NAME_MAX_CHARS + 50);
		const r = parseSessionContextFrame({ type: 'session.context', room_id: '!a:s', room_name: long });
		assert.equal(r?.name?.length, ROOM_NAME_MAX_CHARS);
		assert.deepEqual(parseSessionContextFrame({ type: 'session.context', room_id: '!a:s', room_name: '  ' }), { id: '!a:s' });
		assert.deepEqual(parseSessionContextFrame({ type: 'session.context', room_id: '!a:s' }), { id: '!a:s' });
		assert.deepEqual(parseSessionContextFrame({ type: 'session.context', room_id: '!a:s', room_name: 'two\nlines' }), { id: '!a:s', name: 'two lines' },
			'a newline in a name can never open a new prompt line');
	});
});

describe('setVoiceSessionRoom / getVoiceSessionRoom — one binding per live client', () => {
	it('binds, reads back and releases', () => {
		assert.equal(getVoiceSessionRoom(), null);
		setVoiceSessionRoom({ id: '!abc123:ag2.space', name: 'Commorai' });
		assert.deepEqual(getVoiceSessionRoom(), { id: '!abc123:ag2.space', name: 'Commorai' });
		setVoiceSessionRoom(null);
		assert.equal(getVoiceSessionRoom(), null);
	});
});

// The client sends a frame after session.config and again on every room change
// (a DM frame when it leaves rooms), for the life of the session.
describe('applySessionContextFrame — last frame wins, a DM frame clears, notices only on change', () => {
	const ROOM_A = { type: 'session.context', version: 1, room_id: '!aaa:ag2.space', room_name: 'Commorai', surface: 'room' };
	const ROOM_B = { type: 'session.context', version: 1, room_id: '!bbb:ag2.space', room_name: 'Ops', surface: 'room' };
	const DM = buildSessionContextFrame(null) as unknown as Record<string, unknown>;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const delegate = async (task: string) => (await (workTool.execute as any)({ task }, null)) as { taskId: string };
	const headerOf = (taskId: string) => readFileSync(join(TASK_DIR, `${taskId}.txt`), 'utf-8').split('\n').filter(l => !l.startsWith('task:'));
	const written: string[] = [];

	after(() => {
		setVoiceSessionRoom(null);
		for (const id of written) { try { rmSync(join(TASK_DIR, `${id}.txt`), { force: true }); } catch {} }
	});

	it('two frames in one session: the task written after the second carries the second room', async () => {
		setVoiceSessionRoom(null);
		assert.deepEqual(applySessionContextFrame(ROOM_A), { change: 'entered', room: { id: '!aaa:ag2.space', name: 'Commorai' } });
		const t1 = await delegate('room probe: apartments in Austin');
		written.push(t1.taskId);
		assert.deepEqual(applySessionContextFrame(ROOM_B), { change: 'entered', room: { id: '!bbb:ag2.space', name: 'Ops' } });
		const t2 = await delegate('room probe: rotate the on-call schedule');
		written.push(t2.taskId);
		const h1 = headerOf(t1.taskId);
		const h2 = headerOf(t2.taskId);
		assert.ok(h1.includes('channel_id: !aaa:ag2.space') && h1.includes('source_room_id: !aaa:ag2.space'), h1.join(' | '));
		assert.ok(h2.includes('channel_id: !bbb:ag2.space') && h2.includes('source_room_id: !bbb:ag2.space'), h2.join(' | '));
		assert.ok(!h2.includes('source_room_id: !aaa:ag2.space'), 'the first room does not linger');
	});

	it('a DM frame after a room frame: the next task carries channel_id: local-voice and no room keys', async () => {
		setVoiceSessionRoom(null);
		applySessionContextFrame(ROOM_A);
		assert.deepEqual(applySessionContextFrame(DM), { change: 'left', room: null });
		assert.equal(getVoiceSessionRoom(), null);
		const t = await delegate('room probe: back in the dm');
		written.push(t.taskId);
		const h = headerOf(t.taskId);
		assert.ok(h.includes('channel_id: local-voice'), h.join(' | '));
		assert.ok(!h.some(l => l.startsWith('channel_kind:') || l.startsWith('source_room_id:')), 'no room keys in a DM task');
		assert.ok(readdirSync(TASK_DIR).includes(`${t.taskId}.txt`));
	});

	it('duplicate frames do not re-inject: only an actual change yields a notice', () => {
		setVoiceSessionRoom(null);
		assert.deepEqual(applySessionContextFrame(DM), { change: 'none', room: null }, 'DM while already in the DM is nothing');
		assert.equal(sessionRoomNotice('none', null), null);
		const entered = applySessionContextFrame(ROOM_A)!;
		assert.equal(entered.change, 'entered');
		assert.match(sessionRoomNotice(entered.change, entered.room)!, /^You are now in room "Commorai"\./);
		assert.match(sessionRoomNotice(entered.change, entered.room)!, /say "in this room", never "in your DM"/);
		const dup = applySessionContextFrame(ROOM_A)!;
		assert.equal(dup.change, 'none', 'the same room again is a duplicate');
		assert.equal(sessionRoomNotice(dup.change, dup.room), null, 'a duplicate frame injects nothing');
		const renamed = applySessionContextFrame({ ...ROOM_A, room_name: 'Commorai HQ' })!;
		assert.equal(renamed.change, 'none', 'a name change alone is not a room change');
		assert.equal(getVoiceSessionRoom()?.name, 'Commorai HQ', 'but the newer name is kept');
		const moved = applySessionContextFrame(ROOM_B)!;
		assert.equal(moved.change, 'entered');
		assert.match(sessionRoomNotice(moved.change, moved.room)!, /^You are now in room "Ops"\./);
		const left = applySessionContextFrame(DM)!;
		assert.equal(left.change, 'left');
		assert.match(sessionRoomNotice(left.change, left.room)!, /^You are back in your DM\./);
		assert.deepEqual(applySessionContextFrame(DM), { change: 'none', room: null });
		assert.equal(applySessionContextFrame({ type: 'voice.retryUpstream' }), undefined, 'not a session.context frame');
		assert.equal(getVoiceSessionRoom(), null, 'a foreign frame leaves the binding alone');
	});

	it('an unnamed room reads by its id in the notice', () => {
		assert.match(sessionRoomNotice('entered', { id: '!x:s' })!, /^You are now in room !x:s\./);
		assert.equal(sessionRoomNotice('entered', null), null);
	});
});

// The room notice must never be spoken. Realtime text input is answered out
// loud (every room switch said "Working on it.", owner 2026-09-18); an open
// clientContent turn is read and left unanswered until the user speaks.
import { injectSilentContext } from '../src/browser-tools.js';

describe('room notices go in silently', () => {
	it('injectSilentContext sends an open turn (turnComplete=false) and reports when it cannot', () => {
		const sent: Array<{ turns: unknown; turnComplete: unknown }> = [];
		const session = { transport: { sendContent: (turns: unknown, turnComplete: unknown) => sent.push({ turns, turnComplete }) } };
		assert.equal(injectSilentContext(session, '[System: hi]'), true);
		assert.deepEqual(sent, [{ turns: [{ role: 'user', text: '[System: hi]' }], turnComplete: false }]);
		assert.equal(injectSilentContext({ transport: { session: { sendRealtimeInput: () => {} } } }, 'x'), false,
			'realtime input is never used for a notice: it would be answered aloud');
	});

	it('the session.context handler uses the silent path and the notice asks for no reply', () => {
		const src = readFileSync(join(process.cwd(), 'src', 'voice-agent.ts'), 'utf8');
		const start = src.indexOf('function handleSessionContextFrame');
		const body = src.slice(start, src.indexOf('\n\t}\n', start));
		assert.ok(start > 0 && body.includes('injectSilentContext(session, line)'), 'notice goes through injectSilentContext');
		assert.ok(body.includes('await bindSessionContextFrame(message)'), 'the live frame binds only through the verified path');
		assert.ok(!body.includes('applySessionContextFrame('), 'the unverified apply is never on the live path');
		assert.ok(body.includes('buildSessionContextAckFrame(refused.id, false, refused.reason)'), 'a refusal is acked to the client');
		assert.ok(!body.includes('injectText('), 'no realtime-text fallback for the notice');
		assert.match(sessionRoomNotice('entered', { id: '!r:x', name: 'Ops' })!, /No reply is needed\.$/);
		assert.match(sessionRoomNotice('left', null)!, /No reply is needed\.$/);
	});
});

// ---------------------------------------------------------------------------
// Membership enforcement. The room id in a session.context frame is a claim a
// modified client can make for ANY well-formed room; it becomes a binding, a
// channel_id/source_room_id stamp or a [channel:] destination only on the
// gateway bridge's verdict (owner AND agent joined). Everything else is the DM.
// ---------------------------------------------------------------------------

import { writeFileSync } from 'node:fs';

const FORGED = '!forged:ag2.space';
const VERIFIED = '!verified:ag2.space';
const verdictFor = (room: string, verified: boolean, reason = verified ? 'agent and owner joined' : 'owner not joined') =>
	({ room_id: room, verified, reason, checked_at: Date.now() / 1000 });
/** A verifier that vouches for VERIFIED only, and records every question asked. */
const asked: string[] = [];
const vouchForVerifiedOnly = async (room: string) => { asked.push(room); return verdictFor(room, room === VERIFIED); };

describe('bindSessionContextFrame — a forged but valid room id never becomes a binding', () => {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const delegate = async (task: string) => (await (workTool.execute as any)({ task }, null)) as { taskId: string };
	const headerOf = (taskId: string) => readFileSync(join(TASK_DIR, `${taskId}.txt`), 'utf-8').split('\n').filter(l => !l.startsWith('task:'));
	const written: string[] = [];
	const frame = (room: string) => ({ type: 'session.context', version: 1, room_id: room, room_name: 'Forged', surface: 'room' });

	after(() => {
		setVoiceRoomVerifier(null);
		setVoiceSessionRoom(null);
		for (const id of written) { try { rmSync(join(TASK_DIR, `${id}.txt`), { force: true }); } catch {} }
	});

	it('refused verdict: the session stays on the DM, the task carries channel_id: local-voice and no room keys', async () => {
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		setVoiceSessionRoom(null);
		asked.length = 0;
		const bound = await bindSessionContextFrame(frame(FORGED));
		assert.deepEqual(asked, [FORGED], 'the verifier was asked about exactly the forged room');
		assert.deepEqual(bound, { change: 'none', room: null, refused: { id: FORGED, reason: 'owner not joined' } });
		assert.equal(getVoiceSessionRoom(), null, 'no binding');
		const t = await delegate('room probe: forged room');
		written.push(t.taskId);
		const h = headerOf(t.taskId);
		assert.ok(h.includes('channel_id: local-voice'), h.join(' | '));
		assert.ok(!h.some(l => l.includes(FORGED)), 'the forged id appears nowhere in the header');
		assert.ok(!h.some(l => l.startsWith('channel_kind:') || l.startsWith('source_room_id:')), 'no room keys');
	});

	it('a forged frame after a verified one releases the verified room too (the frame is the client\'s latest claim)', async () => {
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		setVoiceSessionRoom(null);
		const ok = await bindSessionContextFrame(frame(VERIFIED));
		assert.equal(ok?.change, 'entered');
		assert.equal(getVoiceSessionRoom()?.id, VERIFIED);
		const refused = await bindSessionContextFrame(frame(FORGED));
		assert.equal(refused?.change, 'left', 'the verified binding does not linger under a refused claim');
		assert.equal(refused?.refused?.id, FORGED);
		assert.equal(getVoiceSessionRoom(), null);
	});

	it('verified verdict: binds; a DM frame needs no verdict', async () => {
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		setVoiceSessionRoom(null);
		asked.length = 0;
		assert.deepEqual(await bindSessionContextFrame(frame(VERIFIED)), { change: 'entered', room: { id: VERIFIED, name: 'Forged' } });
		const t = await delegate('room probe: verified room');
		written.push(t.taskId);
		assert.ok(headerOf(t.taskId).includes(`source_room_id: ${VERIFIED}`));
		assert.deepEqual(await bindSessionContextFrame(buildSessionContextFrame(null) as unknown as Record<string, unknown>), { change: 'left', room: null });
		assert.deepEqual(asked, [VERIFIED], 'the DM frame asked nothing');
		assert.equal(await bindSessionContextFrame({ type: 'voice.retryUpstream' }), undefined);
	});

	it('a verifier that throws, or answers nothing, is a refusal (fail closed)', async () => {
		setVoiceSessionRoom(null);
		setVoiceRoomVerifier(async () => { throw new Error('gateway down'); });
		const r = await bindSessionContextFrame(frame(VERIFIED));
		assert.equal(r?.refused?.id, VERIFIED);
		assert.match(r?.refused?.reason ?? '', /verifier failed: gateway down/);
		assert.equal(getVoiceSessionRoom(), null);
	});

	it('the newest frame wins while an older verdict is still pending', async () => {
		setVoiceSessionRoom(null);
		let releaseFirst!: () => void;
		const first = new Promise<void>(r => { releaseFirst = r; });
		setVoiceRoomVerifier(async (room) => {
			if (room === '!slow:ag2.space') { await first; return verdictFor(room, true); }
			return verdictFor(room, true);
		});
		const slow = bindSessionContextFrame(frame('!slow:ag2.space'));
		const fast = await bindSessionContextFrame(frame(VERIFIED));
		assert.equal(fast?.change, 'entered');
		releaseFirst();
		const stale = await slow;
		assert.deepEqual(stale, { change: 'none', room: { id: VERIFIED, name: 'Forged' } }, 'the superseded frame changes nothing');
		assert.equal(getVoiceSessionRoom()?.id, VERIFIED);
	});
});

describe('the result leg re-checks the room: a forged source_room_id never becomes a [channel:] destination', () => {
	const forgedTask = 'task-1700000000900';
	const verifiedTask = 'task-1700000000901';
	const header = (id: string, room: string) =>
		`id: ${id}\ntimestamp: 2026-09-18T00:00:00Z\nsource: voice\ninteraction_type: realtime_audio\nmedia_form: live_stream\nchannel_id: ${room}\nchannel_kind: room\nsource_room_id: ${room}\nuser_id: voice-local\naccess_tier: owner\npriority: urgent\ntask: forged\n`;

	after(() => {
		setVoiceRoomVerifier(null);
		for (const id of [forgedTask, verifiedTask]) { try { rmSync(join(TASK_DIR, `${id}.txt`), { force: true }); } catch {} }
	});

	it('resolveVoiceResultRoom: refused → null, verified → the room', async () => {
		mkdirSync(TASK_DIR, { recursive: true });
		writeFileSync(join(TASK_DIR, `${forgedTask}.txt`), header(forgedTask, FORGED));
		writeFileSync(join(TASK_DIR, `${verifiedTask}.txt`), header(verifiedTask, VERIFIED));
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		assert.equal(await resolveVoiceResultRoom(forgedTask), null);
		assert.equal(await resolveVoiceResultRoom(verifiedTask), VERIFIED);
		assert.equal(await resolveVoiceResultRoom('task-does-not-exist'), null);
	});

	it('forwardOfflineVoiceResult: the forged task gets the owner-DM shape (no [channel:] line), the verified one its room', async () => {
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		const dm = await forwardOfflineVoiceResult(forgedTask, 'the answer', 1_800_000_100);
		assert.equal(dm, `proactive-result-${forgedTask}-1800000100.txt`, 'untagged: the DM shape follows the owner to whichever bridge they last used');
		const dmBody = readFileSync(join(RESULT_DIR, dm), 'utf-8');
		assert.equal(dmBody, 'the answer', 'no [channel:] marker: the gateway delivers it to the owner DM');
		assert.ok(!dmBody.includes(FORGED));
		assert.equal(_isDeliveredResult(dm), true, 'claimed so voice never speaks it as a second narration');
		const room = await forwardOfflineVoiceResult(verifiedTask, 'the answer', 1_800_000_101);
		assert.equal(room, `proactive-result-${verifiedTask}-1800000101.to-ag2space.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, room), 'utf-8').split('\n')[0], `[channel: ${VERIFIED}]`);
	});
});

describe('requestVoiceRoomVerdict — the file protocol with the gateway bridge', () => {
	const ROOM = '!proto:ag2.space';
	const key = voiceRoomCheckKey(ROOM);
	const verdictPath = join(VOICE_ROOM_CHECK_DIR, `${key}.verdict.json`);
	const requestPath = join(VOICE_ROOM_CHECK_DIR, `${key}.request.json`);

	after(() => { try { rmSync(VOICE_ROOM_CHECK_DIR, { recursive: true, force: true }); } catch {} });

	it('the key is filesystem-safe and collision-resistant', () => {
		assert.match(key, /^[A-Za-z0-9._-]+$/);
		assert.notEqual(voiceRoomCheckKey('!a/b:s'), voiceRoomCheckKey('!a_b:s'), 'ids that flatten alike keep distinct keys');
	});

	it('no bridge answer → unverified after the timeout, and the request stays on disk for the bridge', async () => {
		try { rmSync(VOICE_ROOM_CHECK_DIR, { recursive: true, force: true }); } catch {}
		const v = await requestVoiceRoomVerdict(ROOM, { timeoutMs: 250, pollMs: 20 });
		assert.equal(v.verified, false);
		assert.equal(v.reason, 'no verdict from the gateway bridge');
		assert.ok(existsSync(requestPath), 'the request file was written');
		assert.equal(JSON.parse(readFileSync(requestPath, 'utf-8')).room_id, ROOM);
		assert.equal((await requestVoiceRoomVerdict('not-a-room', { timeoutMs: 50 })).reason, 'not a matrix room id');
	});

	it('a bridge answer that lands while waiting is returned', async () => {
		try { rmSync(verdictPath, { force: true }); } catch {}
		setTimeout(() => writeFileSync(verdictPath, JSON.stringify({ room_id: ROOM, verified: true, reason: 'agent and owner joined', checked_at: Date.now() / 1000 })), 60);
		const v = await requestVoiceRoomVerdict(ROOM, { timeoutMs: 2000, pollMs: 20 });
		assert.equal(v.verified, true);
		assert.equal(v.room_id, ROOM);
	});

	it('readVoiceRoomVerdict: fresh answers stand, stale ones and answers about another room do not', () => {
		const now = Date.now() / 1000;
		writeFileSync(verdictPath, JSON.stringify({ room_id: ROOM, verified: true, reason: 'x', checked_at: now }));
		assert.equal(readVoiceRoomVerdict(ROOM)?.verified, true);
		assert.equal(readVoiceRoomVerdict(ROOM, now + VOICE_ROOM_VERDICT_TTL_S + 1), null, 'past the TTL the verdict is re-asked');
		writeFileSync(verdictPath, JSON.stringify({ room_id: '!other:ag2.space', verified: true, reason: 'x', checked_at: now }));
		assert.equal(readVoiceRoomVerdict(ROOM), null, 'a verdict naming another room is no verdict');
		writeFileSync(verdictPath, '{not json');
		assert.equal(readVoiceRoomVerdict(ROOM), null);
	});

	it('a fresh refused verdict on disk is returned without a new request', async () => {
		try { rmSync(requestPath, { force: true }); } catch {}
		writeFileSync(verdictPath, JSON.stringify({ room_id: ROOM, verified: false, reason: 'owner not joined', checked_at: Date.now() / 1000 }));
		const v = await requestVoiceRoomVerdict(ROOM, { timeoutMs: 50 });
		assert.deepEqual([v.verified, v.reason], [false, 'owner not joined']);
		assert.ok(!existsSync(requestPath), 'no request was written');
	});
});

describe('session.context.ack — what the client is told', () => {
	it('shapes: dm, room, refused', () => {
		assert.equal(SESSION_CONTEXT_ACK_TYPE, 'session.context.ack');
		assert.deepEqual(buildSessionContextAckFrame(null, false), { type: 'session.context.ack', version: 1, room_id: null, bound: false, surface: 'dm' });
		assert.deepEqual(buildSessionContextAckFrame(VERIFIED, true), { type: 'session.context.ack', version: 1, room_id: VERIFIED, bound: true, surface: 'room' });
		assert.deepEqual(buildSessionContextAckFrame(FORGED, false, 'owner not joined'),
			{ type: 'session.context.ack', version: 1, room_id: FORGED, bound: false, surface: 'refused', reason: 'owner not joined' });
	});
});
