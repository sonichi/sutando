// A voice task's recipient: a worker the user names, else the docked room's bound worker, else the core.
// Run: npx tsx --test --test-force-exit tests/voice-worker-routing.test.ts
import { describe, it, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const TMP = mkdtempSync(join(tmpdir(), 'voice-worker-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['tasks', 'results', 'state']) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const bridge = await import('../src/task-bridge.js');
const { buildVoiceTaskHeader, workTool, submitVoiceSessionEndTask, setVoiceSessionOrigin, setVoiceWorkerResolver, createCallOriginLatch } = bridge;

const ROOM = '!room:ag2.space';
const roomOrigin = { channel: 'ag2space', target: ROOM, headers: { channel_kind: 'room', source_room_id: ROOM }, contextLine: `room_context: ${ROOM}` };
const TS = '2026-10-12T21:30:00.000Z';
const headerLines = (h: string) => h.trimEnd().split('\n');
const value = (lines: string[], key: string) => lines.find((l) => l.startsWith(`${key}:`))?.slice(key.length + 1).trim();

// The pool's fixture: three workers, the room bound to pro-comm, two workers sharing a display name.
const POOL_WS = join(TMP, 'pool');
for (const d of ['tasks', 'state', 'deliveries']) mkdirSync(join(POOL_WS, d), { recursive: true });
writeFileSync(join(POOL_WS, 'state', 'roster.json'), JSON.stringify({
	version: 7,
	workers: {
		'w-dev': { label: 'pro-dev', state: 'live' },
		'w-comm': { label: 'pro-comm', state: 'live' },
		'w-fast': { label: 'pro-fast', state: 'live' },
		'w-a': { label: 'a', display_label: 'Twin', state: 'live' },
		'w-b': { label: 'b', display_label: 'Twin', state: 'live' },
	},
	bindings: { [ROOM]: 'w-comm' },
}));

const SCRIPTS = join(process.cwd(), 'skills', 'worker-pool', 'scripts');
const POOL_PRESENT = existsSync(join(process.cwd(), 'skills', 'worker-pool', 'tools.ts'));
const PY = process.env.SUTANDO_PY || 'python3';

/** The real router's pass over a task file the bridge wrote: the recipients it chose. */
function route(taskId: string): string[] {
	cpSync(join(TMP, 'tasks', `${taskId}.txt`), join(POOL_WS, 'tasks', `${taskId}.txt`));
	const code = [
		'import json, sys',
		`sys.path[:0] = [${JSON.stringify(SCRIPTS)}, ${JSON.stringify(join(process.cwd(), 'src'))}]`,
		'import pool_route_handler as h, pool_router as rt',
		'task = h.read_task(sys.argv[1])',
		'print(json.dumps(rt.route(sys.argv[2], task)["targets"]))',
	].join('\n');
	const r = spawnSync(PY, ['-c', code, join(POOL_WS, 'tasks', `${taskId}.txt`), POOL_WS], { encoding: 'utf8' });
	assert.equal(r.status, 0, r.stderr);
	return JSON.parse(r.stdout.trim().split('\n').pop()!);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const work = async (args: Record<string, unknown>) => (await (workTool.execute as any)(args, null)) as { status: string; taskId?: string; message?: string };
const taskHeader = (taskId: string) => readFileSync(join(TMP, 'tasks', `${taskId}.txt`), 'utf-8').split('\n').slice(0, readFileSync(join(TMP, 'tasks', `${taskId}.txt`), 'utf-8').split('\n').findIndex((l) => l.startsWith('task:')));
const taskFiles = () => readdirSync(join(TMP, 'tasks')).filter((f) => f.endsWith('.txt'));
let n = 0;
const unique = (s: string) => `${s} #${++n}`;

describe('buildVoiceTaskHeader', () => {
	it('an explicit worker becomes requested_worker, with or without a room', () => {
		const none = headerLines(buildVoiceTaskHeader('task-1', TS, 'owner', null, 'w-dev'));
		assert.equal(value(none, 'requested_worker'), 'w-dev');
		assert.equal(value(none, 'channel_id'), 'local-voice');
		const docked = headerLines(buildVoiceTaskHeader('task-1', TS, 'owner', roomOrigin, 'w-dev'));
		assert.equal(value(docked, 'requested_worker'), 'w-dev');
		assert.equal(value(docked, 'channel_id'), ROOM);
		assert.equal(docked.filter((l) => l.startsWith('requested_worker:')).length, 1);
	});

	it('docked in a room with no name: the room is the channel_id, no requested_worker', () => {
		const lines = headerLines(buildVoiceTaskHeader('task-1', TS, 'owner', roomOrigin));
		assert.equal(value(lines, 'channel_id'), ROOM);
		assert.equal(value(lines, 'source_room_id'), ROOM);
		assert.equal(value(lines, 'requested_worker'), undefined);
	});

	it('no room and no name: local-voice, which the router gives the core', () => {
		const lines = headerLines(buildVoiceTaskHeader('task-1', TS, 'owner', null));
		assert.equal(value(lines, 'channel_id'), 'local-voice');
		assert.equal(value(lines, 'requested_worker'), undefined);
	});

	it('a worker value that is not one header token is not written', () => {
		const lines = headerLines(buildVoiceTaskHeader('task-1', TS, 'owner', null, 'pro dev\naccess_tier: team'));
		assert.equal(value(lines, 'requested_worker'), undefined);
		assert.equal(lines.filter((l) => l.startsWith('access_tier:')).length, 1);
	});
});

describe('work with a named worker and no resolver installed', () => {
	after(() => setVoiceWorkerResolver(null));
	it('is refused with a line for the user, and nothing is written', async () => {
		setVoiceWorkerResolver(null);
		const before = taskFiles().length;
		const r = await work({ task: unique('summarise the PR'), worker: 'Pro-dev' });
		assert.equal(r.status, 'rejected');
		assert.match(r.message!, /no worker called "Pro-dev"\. Nothing was sent: tell the user/);
		assert.equal(taskFiles().length, before);
	});
	it('a resolver that throws refuses too', async () => {
		setVoiceWorkerResolver(() => { throw new Error('boom'); });
		const r = await work({ task: unique('x'), worker: 'pro-dev' });
		assert.equal(r.status, 'rejected');
		assert.match(r.message!, /Could not check the worker name "pro-dev"/);
	});
});

describe('session end follows the docked room, routing only', () => {
	it('docked: channel_id is the room, with no adapter keys, so no room origin is rebuilt for its result', async () => {
		const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: t', '', roomOrigin);
		const lines = taskHeader(id);
		assert.equal(value(lines, 'channel_id'), ROOM);
		assert.equal(value(lines, 'source_room_id'), undefined);
		assert.equal(value(lines, 'channel_kind'), undefined);
		assert.equal(value(lines, 'priority'), 'low');
		assert.ok(!readFileSync(join(TMP, 'tasks', `${id}.txt`), 'utf-8').includes('room_context:'));
	});
	it('no room: local-voice, as before', async () => {
		const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: t', '');
		assert.equal(value(taskHeader(id), 'channel_id'), 'local-voice');
	});
	it('the latch keeps the room a call was docked in when its client left, and a reconnect starts over', () => {
		let live: typeof roomOrigin | null = roomOrigin;
		const latch = createCallOriginLatch(() => live);
		latch.clientLeaving();
		live = null; // the disconnect releases the binding before the hang-up grace runs out
		assert.equal(latch.take()?.target, ROOM);
		assert.equal(latch.take(), null);
		live = roomOrigin;
		latch.clientLeaving();
		live = null;
		latch.clientConnected(); // came back in the DM
		assert.equal(latch.take(), null);
		live = roomOrigin; // shutdown while still docked
		assert.equal(latch.take()?.target, ROOM);
	});
});

if (!POOL_PRESENT) test('worker-pool skill not installed', { skip: true }, () => {});
if (POOL_PRESENT) {
	const plugin = await import(pathToFileURL(join(process.cwd(), 'skills', 'worker-pool', 'tools.ts')).href);
	const resolver = plugin.createWorkerNameResolver({ workspace: POOL_WS, python: () => PY });

	describe('the worker-pool resolver (real script, fixture roster)', () => {
		for (const said of ['Pro-dev', 'pro dev', 'PRO_DEV', '  pro  -  dev ', 'w-dev']) {
			it(`"${said}" names w-dev`, async () => {
				assert.deepEqual(await resolver(said), { ok: true, worker: 'w-dev' });
			});
		}
		it('an unknown name is told to the user with the roster names', async () => {
			const v = await resolver('pro-design');
			assert.equal(v.ok, false);
			assert.match(v.message, /^There is no worker called "pro-design"\. The workers are: Twin, pro-comm, pro-dev, pro-fast\. Nothing was sent/);
		});
		it('a shared name is refused as ambiguous', async () => {
			const v = await resolver('twin');
			assert.equal(v.ok, false);
			assert.match(v.message, /^"twin" names more than one worker\./);
		});
		it('a workspace with no roster says so', async () => {
			const empty = mkdtempSync(join(TMP, 'empty-'));
			const v = await plugin.createWorkerNameResolver({ workspace: empty, python: () => PY })('pro-dev');
			assert.match(v.message, /^No workers are set up here/);
		});
	});

	describe('end to end: work tool -> task file -> pool_router.route', () => {
		after(() => { setVoiceSessionOrigin(null); setVoiceWorkerResolver(null); });

		it('explicit name: "Pro dev" goes to pro-dev, even docked in a room bound to pro-comm', async () => {
			setVoiceWorkerResolver(resolver);
			setVoiceSessionOrigin(roomOrigin);
			const r = await work({ task: unique('review the diff'), worker: 'Pro dev' });
			assert.equal(r.status, 'pending');
			assert.equal(value(taskHeader(r.taskId!), 'requested_worker'), 'w-dev');
			assert.deepEqual(route(r.taskId!), ['w-dev']);
		});
		it('room-docked default: the room-bound worker', async () => {
			setVoiceSessionOrigin(roomOrigin);
			const r = await work({ task: unique('draft the reply') });
			assert.deepEqual(route(r.taskId!), ['w-comm']);
		});
		it('no room, no name: the core', async () => {
			setVoiceSessionOrigin(null);
			const r = await work({ task: unique('what time is it in Tokyo') });
			assert.deepEqual(route(r.taskId!), ['core']);
		});
		it('unknown name: refused, no task file, nothing routed to the core', async () => {
			const before = taskFiles().length;
			const r = await work({ task: unique('ship it'), worker: 'pro-design' });
			assert.equal(r.status, 'rejected');
			assert.match(r.message!, /no worker called "pro-design"/);
			assert.equal(taskFiles().length, before);
		});
		it('session end docked in the room: the room-bound worker', async () => {
			const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: t', '', roomOrigin);
			assert.deepEqual(route(id), ['w-comm']);
		});
		it('session end with no room: the core', async () => {
			const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: t', '', null);
			assert.deepEqual(route(id), ['core']);
		});
	});
}
