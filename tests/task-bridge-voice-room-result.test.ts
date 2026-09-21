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
	forwardVoiceResultToRoom, LEADING_REDIRECT_RE, _isDeliveredResult, _shouldFallthrough, _shouldRegisterTaskRow,
	forwardVoiceResultToOwnerDm, keepVoiceResultToDm, DM_ONLY_RE, DM_ONLY_DELIVERY_NOTE, voiceRoomTaskGuidance, startResultWatcher,
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

	it('a result that opens with its own [channel:] redirect keeps it: the docked room is not put in front', () => {
		const own = '\n[channel: !elsewhere:ag2.space]\nFor the other room.';
		const file = forwardVoiceResultToRoom('task-1700000000005', own, '!abc123:ag2.space', 1_800_000_005);
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), own, 'byte for byte: the first redirect the gateway reads is the core\'s');
		assert.match(file, /\.to-ag2space\.txt$/, 'still the gateway\'s file');
		assert.equal(_isDeliveredResult(file), true);
		const py = spawnSync('python3', ['-c', [
			'import sys; sys.path.insert(0, "src")',
			'from result_markers import parse_markers',
			`r = parse_markers(${JSON.stringify(own)})`,
			'print([a.value for a in r.actions if a.kind == "redirect"])',
		].join('\n')], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), "['!elsewhere:ag2.space']");
		// A redirect further down, or an empty one, is prose: the docked room leads.
		for (const [i, body] of ['Intro.\n[channel: !elsewhere:ag2.space]\nbody', '[channel: ]\nbody', '[channel:]\nbody'].entries()) {
			const f = forwardVoiceResultToRoom(`task-170000000001${i}`, body, '!abc123:ag2.space', 1_800_000_010 + i);
			assert.equal(readFileSync(join(RESULT_DIR, f), 'utf-8'), `[channel: !abc123:ag2.space]\n${body}`);
		}
		assert.match('  [channel: 123]', LEADING_REDIRECT_RE);
	});

	it('the marker is the shape the gateway accepts (no dm-only, marker alone on its line)', () => {
		const file = forwardVoiceResultToRoom('task-1700000000002', '[dm-only]\nprivate', '!abc123:ag2.space', 1_800_000_003);
		const body = readFileSync(join(RESULT_DIR, file), 'utf-8');
		// The writer is byte-for-byte: a dm-only body handed to it keeps that
		// declaration below the channel line, and parse_markers on the gateway
		// still suppresses the redirect. The drain never hands it one, though —
		// keepVoiceResultToDm routes a dm-only result before this is reached.
		assert.equal(body.split('\n')[0], '[channel: !abc123:ag2.space]');
		assert.equal(body.split('\n')[1], '[dm-only]');
	});
});

// Owner 2026-09-18, docked by voice in a customer room: "look into the mute
// bug" was answered IN that room. "it should only send messages that are
// RELEVANT to that room otherwise should go to the DM." The core marks such a
// result [dm-only] (conduct rule "Where replies go"); the bridge keeps it to
// the owner's DM and tells voice so.
describe('keepVoiceResultToDm — a [dm-only] result of a room-bound task goes to the owner DM, never the room', () => {
	const roomTask = 'task-1700000000700';
	const dmTask = 'task-1700000000701';
	const header = (id: string, room: string | null) =>
		`id: ${id}\ntimestamp: 2026-09-18T00:00:00Z\nsource: voice\ninteraction_type: realtime_audio\nmedia_form: live_stream\nchannel_id: ${room ?? 'local-voice'}\n` +
		(room ? `channel_kind: room\nsource_room_id: ${room}\n` : '') + `user_id: voice-local\naccess_tier: owner\npriority: urgent\ntask: mute bug\n`;

	after(() => { for (const id of [roomTask, dmTask]) { try { rmSync(join(TASK_DIR, `${id}.txt`), { force: true }); } catch {} } });

	it('DM_ONLY_RE detects the marker the way parse_markers does: anywhere in the body, any case', () => {
		assert.match('[dm-only]\nfindings', DM_ONLY_RE);
		assert.match('findings\n[DM-only]', DM_ONLY_RE);
		assert.match('- #2170 [dm-only]: closes the leak vector', DM_ONLY_RE, 'inline is still detected (Python parity), only the strip is line-anchored');
		assert.doesNotMatch('dm only, please', DM_ONLY_RE);
		assert.doesNotMatch('[channel: !r:s]\nbody', DM_ONLY_RE);
	});

	it('forwardVoiceResultToOwnerDm: gateway-tagged, no [channel:] line, [dm-only] on top, claimed at once', () => {
		const file = forwardVoiceResultToOwnerDm(roomTask, 'The mute bug is in the SDK.', 1_800_000_700);
		assert.equal(file, `proactive-result-${roomTask}-1800000700.to-ag2space.txt`, 'tagged: the room the session is docked in is a Matrix room, so that gateway is where the owner is');
		const body = readFileSync(join(RESULT_DIR, file), 'utf-8');
		assert.equal(body, '[dm-only]\nThe mute bug is in the SDK.');
		assert.ok(!body.includes('[channel:'), 'nothing for _proactive_route to redirect: the owner room is the default');
		assert.equal(_isDeliveredResult(file), true, 'claimed so the next drain tick never speaks it again');
		assert.equal(_shouldFallthrough(file), true, 'it would otherwise be spoken: the claim is the only thing stopping that');
		const py = spawnSync('python3', ['-c', [
			'import sys; sys.path.insert(0, "src")',
			'from proactive_routing import proactive_destination',
			'from result_markers import parse_markers',
			`p = parse_markers(${JSON.stringify(body)})`,
			`print(proactive_destination(${JSON.stringify(file)}), [a.kind for a in p.actions], repr(p.body))`,
		].join('\n')], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), `ag2space ['dm-only'] 'The mute bug is in the SDK.'`, 'the gateway claims it, sees dm-only and no redirect, strips the marker');
	});

	it('room-bound + dm-only → the DM file; room-bound + plain → null; DM task → null either way', () => {
		mkdirSync(TASK_DIR, { recursive: true });
		writeFileSync(join(TASK_DIR, `${roomTask}.txt`), header(roomTask, VERIFIED));
		writeFileSync(join(TASK_DIR, `${dmTask}.txt`), header(dmTask, null));
		const kept = keepVoiceResultToDm(roomTask, 'private findings', true, 1_800_000_701);
		assert.equal(kept, `proactive-result-${roomTask}-1800000701.to-ag2space.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, kept!), 'utf-8'), '[dm-only]\nprivate findings');
		assert.equal(keepVoiceResultToDm(roomTask, 'for the room', false, 1_800_000_702), null, 'a plain result is the room leg\'s to route');
		assert.equal(keepVoiceResultToDm(dmTask, 'private findings', true, 1_800_000_703), null, 'a DM-session task already answers in the DM: nothing to keep');
		assert.ok(!existsSync(join(RESULT_DIR, `proactive-result-${roomTask}-1800000702.to-ag2space.txt`)));
		assert.ok(!existsSync(join(RESULT_DIR, `proactive-result-${dmTask}-1800000703.to-ag2space.txt`)));
	});

	it('offline: a dm-only result of a room-bound task takes the DM shape even though the room would verify', async () => {
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		asked.length = 0;
		const file = await forwardOfflineVoiceResult(roomTask, 'private findings', 1_800_000_704, true);
		assert.equal(file, `proactive-result-${roomTask}-1800000704.to-ag2space.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8').split('\n')[0], '[dm-only]');
		assert.deepEqual(asked, [], 'no verdict is needed: the room is not a destination for a dm-only result');
		const plain = await forwardOfflineVoiceResult(roomTask, 'for the room', 1_800_000_705, false);
		assert.equal(readFileSync(join(RESULT_DIR, plain), 'utf-8').split('\n')[0], `[channel: ${VERIFIED}]`, 'a plain result still goes to the verified room');
		setVoiceRoomVerifier(null);
	});

	it('the delivery note names the DM and is not the result\'s own words', () => {
		assert.match(DM_ONLY_DELIVERY_NOTE, /went to their DM/);
		assert.match(DM_ONLY_DELIVERY_NOTE, /not the room/);
		assert.doesNotMatch(DM_ONLY_DELIVERY_NOTE, /\[dm-only\]/, 'voice never hears the marker');
	});
});

describe('the drain: a plain result of a room-bound task is posted in its room, a [dm-only] one is kept to the DM and voice is told', () => {
	const plainTask = 'task-1700000000800';
	const privateTask = 'task-1700000000801';
	const header = (id: string) =>
		`id: ${id}\ntimestamp: 2026-09-18T00:00:00Z\nsource: voice\ninteraction_type: realtime_audio\nmedia_form: live_stream\nchannel_id: ${VERIFIED}\nchannel_kind: room\nsource_room_id: ${VERIFIED}\nuser_id: voice-local\naccess_tier: owner\npriority: urgent\ntask: probe\n`;
	const spoken: Array<{ result: string; note?: string }> = [];
	const proactiveFor = (id: string) => readdirSync(RESULT_DIR).filter(f => f.startsWith(`proactive-result-${id}-`));
	const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise(r => setTimeout(r, 100)); return cond(); };

	after(() => {
		setVoiceRoomVerifier(null);
		for (const id of [plainTask, privateTask]) { try { rmSync(join(TASK_DIR, `${id}.txt`), { force: true }); } catch {} }
	});

	it('two results through one drain tick', async () => {
		mkdirSync(TASK_DIR, { recursive: true });
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		writeFileSync(join(TASK_DIR, `${plainTask}.txt`), header(plainTask));
		writeFileSync(join(TASK_DIR, `${privateTask}.txt`), header(privateTask));
		writeFileSync(join(RESULT_DIR, `${plainTask}.txt`), 'Three listings under $2k.');
		writeFileSync(join(RESULT_DIR, `${privateTask}.txt`), '[dm-only]\nThe mute bug is in the SDK.');
		startResultWatcher((result, note) => spoken.push({ result, note }), () => true);
		assert.ok(await until(() => proactiveFor(plainTask).length > 0 && proactiveFor(privateTask).length > 0, 8000), `drain did not deliver both: ${readdirSync(RESULT_DIR).join(', ')}`);
		assert.equal(_isDeliveredResult(`${plainTask}.txt`), true);
		assert.equal(_isDeliveredResult(`${privateTask}.txt`), true);

		const [roomFile] = proactiveFor(plainTask);
		assert.match(roomFile, /\.to-ag2space\.txt$/);
		assert.equal(readFileSync(join(RESULT_DIR, roomFile), 'utf-8'), `[channel: ${VERIFIED}]\nThree listings under $2k.`, 'posted in the room, as before');
		const [dmFile] = proactiveFor(privateTask);
		assert.match(dmFile, /\.to-ag2space\.txt$/);
		assert.equal(readFileSync(join(RESULT_DIR, dmFile), 'utf-8'), '[dm-only]\nThe mute bug is in the SDK.', 'kept to the owner DM: no [channel:] line');
		assert.ok(!readdirSync(RESULT_DIR).some(f => f.startsWith(`proactive-result-${privateTask}-`) && readFileSync(join(RESULT_DIR, f), 'utf-8').includes('[channel:')), 'nothing for that task addresses the room');
		assert.equal(_isDeliveredResult(roomFile), true);
		assert.equal(_isDeliveredResult(dmFile), true);

		const plainSpoken = spoken.find(s => s.result === 'Three listings under $2k.');
		const privateSpoken = spoken.find(s => s.result === 'The mute bug is in the SDK.');
		assert.ok(plainSpoken && privateSpoken, `spoken: ${JSON.stringify(spoken)}`);
		assert.equal(plainSpoken!.note, undefined, 'the room case carries no note: the docked notice already says the room');
		assert.equal(privateSpoken!.note, DM_ONLY_DELIVERY_NOTE, 'voice is told the copy went to the DM');
		assert.ok(!spoken.some(s => s.result.includes('[dm-only]')), 'the marker is stripped from what voice speaks, as on every text bridge');
		assert.equal(spoken.filter(s => s.result === 'The mute bug is in the SDK.').length, 1, 'spoken once: the DM file is claimed before the next tick');
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

	it('a room-bound task carries the room guidance as the body line right under task:, a DM task carries none', async () => {
		setVoiceSessionRoom(null);
		applySessionContextFrame(ROOM_A);
		const t = await delegate('room probe: guidance');
		written.push(t.taskId);
		const lines = readFileSync(join(TASK_DIR, `${t.taskId}.txt`), 'utf-8').split('\n');
		const at = lines.indexOf('task: room probe: guidance');
		assert.ok(at > 0, lines.join(' | '));
		assert.equal(lines[at + 1], voiceRoomTaskGuidance('!aaa:ag2.space'));
		assert.equal(voiceRoomTaskGuidance('!aaa:ag2.space'),
			'room_context: !aaa:ag2.space — post there only what its members are meant to read; for anything the owner asked for themselves start the result with [dm-only]');
		assert.ok(!lines.slice(0, at).some(l => l.startsWith('room_context:')), 'a body line, never a header key');
		applySessionContextFrame(DM);
		const dm = await delegate('room probe: no guidance');
		written.push(dm.taskId);
		assert.ok(!readFileSync(join(TASK_DIR, `${dm.taskId}.txt`), 'utf-8').includes('room_context:'));
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
		assert.match(sessionRoomNotice(entered.change, entered.room)!, /^You are docked in room "Commorai";/);
		assert.match(sessionRoomNotice(entered.change, entered.room)!, /answered there only when it is for the room's members, otherwise in the owner's DM; say where it went/);
		assert.doesNotMatch(sessionRoomNotice(entered.change, entered.room)!, /never "in your DM"/, 'a dm-only result IS in the DM, and voice must be free to say so');
		const dup = applySessionContextFrame(ROOM_A)!;
		assert.equal(dup.change, 'none', 'the same room again is a duplicate');
		assert.equal(sessionRoomNotice(dup.change, dup.room), null, 'a duplicate frame injects nothing');
		const renamed = applySessionContextFrame({ ...ROOM_A, room_name: 'Commorai HQ' })!;
		assert.equal(renamed.change, 'none', 'a name change alone is not a room change');
		assert.equal(getVoiceSessionRoom()?.name, 'Commorai HQ', 'but the newer name is kept');
		const moved = applySessionContextFrame(ROOM_B)!;
		assert.equal(moved.change, 'entered');
		assert.match(sessionRoomNotice(moved.change, moved.room)!, /^You are docked in room "Ops";/);
		const left = applySessionContextFrame(DM)!;
		assert.equal(left.change, 'left');
		assert.match(sessionRoomNotice(left.change, left.room)!, /^You are back in your DM\./);
		assert.deepEqual(applySessionContextFrame(DM), { change: 'none', room: null });
		assert.equal(applySessionContextFrame({ type: 'voice.retryUpstream' }), undefined, 'not a session.context frame');
		assert.equal(getVoiceSessionRoom(), null, 'a foreign frame leaves the binding alone');
	});

	it('an unnamed room reads by its id in the notice', () => {
		assert.match(sessionRoomNotice('entered', { id: '!x:s' })!, /^You are docked in room !x:s;/);
		assert.equal(sessionRoomNotice('entered', null), null);
	});
});

// The room notice asks for `turnComplete: false`. Whether that is silent is the
// transport's business: the pinned Gemini transport ignores the flag.
import { injectSilentContext } from '../src/browser-tools.js';

describe('room notices ask for an open turn', () => {
	it('injectSilentContext sends an open turn (turnComplete=false) and reports when it cannot', () => {
		const sent: Array<{ turns: unknown; turnComplete: unknown }> = [];
		const session = { transport: { sendContent: (turns: unknown, turnComplete: unknown) => sent.push({ turns, turnComplete }) } };
		assert.equal(injectSilentContext(session, '[System: hi]'), true);
		assert.deepEqual(sent, [{ turns: [{ role: 'user', text: '[System: hi]' }], turnComplete: false }]);
		assert.equal(injectSilentContext({ transport: { session: { sendRealtimeInput: () => {} } } }, 'x'), false,
			'without sendContent the notice is dropped, not sent as realtime input');
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

	it('room to room: while the new room\'s verdict is pending the session is on the DM, never the previous room', async () => {
		setVoiceSessionRoom(null);
		const OTHER = '!internal-ops:ag2.space';
		let release!: () => void;
		const gate = new Promise<void>(r => { release = r; });
		setVoiceRoomVerifier(async (room) => { if (room === OTHER) await gate; return verdictFor(room, true); });
		assert.equal((await bindSessionContextFrame(frame(VERIFIED)))?.change, 'entered');
		const moving = bindSessionContextFrame(frame(OTHER));
		assert.equal(getVoiceSessionRoom(), null, 'the previous room is released before the wait');
		const t = await delegate('room probe: spoken during the verdict window');
		written.push(t.taskId);
		const h = headerOf(t.taskId);
		assert.ok(h.includes('channel_id: local-voice'), h.join(' | '));
		assert.ok(!h.some(l => l.includes(VERIFIED)), 'the previous room appears nowhere in the header');
		release();
		assert.deepEqual(await moving, { change: 'entered', room: { id: OTHER, name: 'Forged' } });
		assert.equal(getVoiceSessionRoom()?.id, OTHER);
	});

	it('room to room refused: reported as left, once; the same room re-announced mid-wait keeps its binding', async () => {
		setVoiceSessionRoom(null);
		setVoiceRoomVerifier(vouchForVerifiedOnly);
		await bindSessionContextFrame(frame(VERIFIED));
		const refused = await bindSessionContextFrame(frame(FORGED));
		assert.equal(refused?.change, 'left');
		assert.equal((await bindSessionContextFrame(frame(FORGED)))?.change, 'none', 'a second refusal is not a second notice');
		await bindSessionContextFrame(frame(VERIFIED));
		let release!: () => void;
		const gate = new Promise<void>(r => { release = r; });
		setVoiceRoomVerifier(async (room) => { await gate; return verdictFor(room, true); });
		const again = bindSessionContextFrame(frame(VERIFIED));
		assert.equal(getVoiceSessionRoom()?.id, VERIFIED, 'a duplicate frame for the bound room releases nothing');
		release();
		assert.equal((await again)?.change, 'none');
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
		assert.equal(_isDeliveredResult(dm), false, 'left unclaimed: with no bridge to take it, the drain speaks it on reconnect');
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
