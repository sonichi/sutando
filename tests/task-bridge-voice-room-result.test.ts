import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
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
} = await import('../src/task-bridge.js');
const TASK_DIR = join(TMP, 'tasks');
const { SESSION_CONTEXT_TYPE, buildSessionContextFrame } = await import('../src/web-voice-transport.js');

after(() => {
	setVoiceSessionRoom(null);
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

describe('forwardVoiceResultToRoom — a room-bound result reaches its room, and voice never speaks it twice', () => {
	it('writes results/proactive-result-<id>-<ts>.txt with [channel: <room>] as the first line', () => {
		const file = forwardVoiceResultToRoom('task-1700000000000', 'Three listings under $2k.\nSecond line.', '!abc123:ag2.space', 1_800_000_000);
		assert.equal(file, 'proactive-result-task-1700000000000-1800000000.txt');
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
		assert.equal(_isDeliveredResult('proactive-result-task-1700000000001-1800000002.txt'), false, 'only the file actually written');
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
