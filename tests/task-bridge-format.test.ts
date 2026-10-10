import { describe, it, before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from '../src/workspace_default.js';
import { buildVoiceTaskHeader, countQueuedAhead, queuedAheadInstruction, setVoiceSessionOrigin, getVoiceSessionOrigin, workTool } from '../src/task-bridge.js';
import { readQueueDepth } from '../src/inline-tools.js';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

// Integration test for PR #460's unified task-file schema. Every voice /
// work-tool task should emit the same set of fields the Discord bridge
// emits, so downstream consumers (Claude Code session, access-tier
// sandboxing) can treat all tasks uniformly.

// Task dir is wherever the bridge writes — `resolveWorkspace()/tasks/`. Was
// `<REPO_ROOT>/tasks/` pre-#821, when the bridge fell back to repo root.
const TASK_DIR = join(resolveWorkspace(), 'tasks');

function listTaskFiles(): string[] {
	if (!existsSync(TASK_DIR)) return [];
	return readdirSync(TASK_DIR).filter(f => f.startsWith('task-') && f.endsWith('.txt'));
}

describe('task-bridge workTool — PR #460 unified format', () => {
	let createdFiles: string[] = [];
	let baselineFiles: Set<string>;

	before(() => {
		mkdirSync(TASK_DIR, { recursive: true });
		baselineFiles = new Set(listTaskFiles());
	});

	afterEach(() => {
		// Clean up only the files we created; leave prod task files alone.
		for (const fn of createdFiles) {
			try { unlinkSync(join(TASK_DIR, fn)); } catch { /* already gone */ }
		}
		createdFiles = [];
	});

	after(() => {
		// Leak check: after all tests + cleanup, only baseline files should
		// remain. Anything extra means a test wrote a file but didn't track
		// it through createdFiles (e.g. a future test that bypasses
		// invokeWorkTool). Surfaces gaps in the cleanup harness early.
		const final = new Set(listTaskFiles());
		const leaked: string[] = [];
		for (const f of final) if (!baselineFiles.has(f)) leaked.push(f);
		assert.deepEqual(leaked, [], 'test leaked task files: ' + leaked.join(', '));
	});

	async function invokeWorkTool(task: string): Promise<string> {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const result = await (workTool.execute as any)({ task }, null) as { taskId?: string };
		assert.ok(result.taskId, 'workTool should return a taskId');
		const fn = result.taskId + '.txt';
		createdFiles.push(fn);
		return fn;
	}

	it('writes all 7 fields required by the Discord-bridge schema', async () => {
		const fn = await invokeWorkTool('Test task from format unit test');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		// Each of these is the schema locked in by PR #460.
		assert.match(content, /^id: task-\d+$/m, 'id field');
		assert.match(content, /^timestamp: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/m, 'ISO8601 timestamp');
		assert.match(content, /^task: Test task from format unit test$/m, 'task body');
		assert.match(content, /^source: voice$/m, 'source=voice for workTool');
		assert.match(content, /^channel_id: local-voice$/m, 'channel_id=local-voice');
		assert.match(content, /^user_id: \S+$/m, 'user_id (env or voice-local fallback)');
		assert.match(content, /^access_tier: owner$/m, 'access_tier=owner (voice is owner-only)');
	});

	it('does NOT emit the legacy "reminder:" field dropped in PR #460', async () => {
		const fn = await invokeWorkTool('Test task — no reminder');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		assert.doesNotMatch(content, /^reminder:/m, 'reminder field was dropped');
	});

	it('uses SUTANDO_DM_OWNER_ID when set, falls back to voice-local sentinel', async () => {
		// Case 1: default (env unset) → voice-local
		const fn1 = await invokeWorkTool('default fallback');
		const c1 = readFileSync(join(TASK_DIR, fn1), 'utf-8');
		if (!process.env.SUTANDO_DM_OWNER_ID) {
			assert.match(c1, /^user_id: voice-local$/m);
		} else {
			assert.match(c1, new RegExp(`^user_id: ${process.env.SUTANDO_DM_OWNER_ID}$`, 'm'));
		}
	});

	it('generates unique task IDs for back-to-back calls', async () => {
		const fn1 = await invokeWorkTool('first');
		const fn2 = await invokeWorkTool('second');
		assert.notEqual(fn1, fn2, 'task IDs must differ');
	});

	it('returns queuedAhead: how many owner tasks stood in tasks/ before this one', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const first = await (workTool.execute as any)({ task: 'queue probe one' }, null) as { taskId: string; queuedAhead: number; message: string };
		createdFiles.push(first.taskId + '.txt');
		assert.equal(typeof first.queuedAhead, 'number');
		assert.ok(first.queuedAhead >= 0);
		// The first file is still in tasks/, so the second call sees at least one ahead of it,
		// and its message carries the one sentence the voice agent is to say.
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const second = await (workTool.execute as any)({ task: 'queue probe two' }, null) as { taskId: string; queuedAhead: number; message: string };
		createdFiles.push(second.taskId + '.txt');
		assert.ok(second.queuedAhead >= first.queuedAhead + 1, `second saw ${second.queuedAhead}, first ${first.queuedAhead}`);
		const expected = second.queuedAhead === 1 ? "Got it, right after the one I'm on." : `Got it, ${second.queuedAhead} in line before this one.`;
		assert.ok(second.message.includes(expected), `message carries the spoken line: ${second.message}`);
		assert.ok(second.message.startsWith('Task has been '), 'the original instruction is kept in front');
	});
});

describe('queue depth helpers (pure, temp dirs)', () => {
	it('countQueuedAhead counts owner task files only, excluding this task and bookkeeping', () => {
		const dir = mkdtempSync(join(tmpdir(), 'queue-ahead-'));
		for (const f of ['task-1.txt', 'task-2.txt', 'task-chat-3.txt', 'task-cron-4.txt', 'task-bench-5.txt',
			'task-workstream-6.txt', 'task-project-grouping-7.txt', 'notes.md', 'task-8.json']) {
			writeFileSync(join(dir, f), 'id: x\ntask: y\n');
		}
		assert.equal(countQueuedAhead(dir, 'task-2'), 2, 'task-1 and task-chat-3');
		assert.equal(countQueuedAhead(dir, 'task-none'), 3);
		assert.equal(countQueuedAhead(join(dir, 'missing'), 'task-2'), 0, 'an unreadable dir is 0, never a throw');
		assert.equal(queuedAheadInstruction(0), '');
		assert.match(queuedAheadInstruction(1), /Got it, right after the one I'm on\./, 'one ahead reads as a person, not a queue');
		assert.match(queuedAheadInstruction(2), /Got it, 2 in line before this one\./);
	});

	it('readQueueDepth reads state/task-queue.json and treats a stale or absent snapshot as unknown', () => {
		const ws = mkdtempSync(join(tmpdir(), 'queue-depth-'));
		assert.equal(readQueueDepth(ws), null, 'absent');
		mkdirSync(join(ws, 'state'), { recursive: true });
		const now = 1_800_000_000;
		writeFileSync(join(ws, 'state', 'task-queue.json'), JSON.stringify({ ts: now - 30, depth: 3, pending: [] }));
		assert.equal(readQueueDepth(ws, now), 3);
		writeFileSync(join(ws, 'state', 'task-queue.json'), JSON.stringify({ ts: now - 601, depth: 3, pending: [] }));
		assert.equal(readQueueDepth(ws, now), null, 'older than 10 minutes');
		writeFileSync(join(ws, 'state', 'task-queue.json'), '{not json');
		assert.equal(readQueueDepth(ws, now), null, 'torn');
	});
});

// One header writer for the work tool and the cancel tool; an origin-bound task is
// addressed through `channel_id` plus whatever known keys its adapter supplies.
const ORIGIN = { channel: 'fakechan', target: 'place-42', label: 'The Place', headers: { channel_kind: 'place', source_room_id: 'place-42' }, contextLine: 'place_context: place-42 — be brief' };
describe('buildVoiceTaskHeader (pure) — both header shapes', () => {
	const TS = '2026-09-18T10:00:00.000Z';

	it('DM shape: channel_id: local-voice, no room keys, priority urgent, nothing at or after task:', () => {
		const header = buildVoiceTaskHeader('task-1', TS, 'owner-1', null);
		assert.equal(header, [
			'id: task-1',
			`timestamp: ${TS}`,
			'source: voice',
			'interaction_type: realtime_audio',
			'media_form: live_stream',
			'channel_id: local-voice',
			'user_id: owner-1',
			'access_tier: owner',
			'priority: urgent',
			'',
		].join('\n'));
		assert.doesNotMatch(header, /^task:/m, 'the header is everything ABOVE task: — the caller appends it');
		assert.doesNotMatch(header, /^(channel_kind|source_room_id):/m);
	});

	it('origin shape: channel_id is the target, the adapter\'s keys follow it, still voice and urgent', () => {
		const header = buildVoiceTaskHeader('task-2', TS, 'owner-1', ORIGIN);
		assert.equal(header, [
			'id: task-2',
			`timestamp: ${TS}`,
			'source: voice',
			'interaction_type: realtime_audio',
			'media_form: live_stream',
			'channel_id: place-42',
			'channel_kind: place',
			'source_room_id: place-42',
			'user_id: owner-1',
			'access_tier: owner',
			'priority: urgent',
			'',
		].join('\n'));
		assert.doesNotMatch(header, /local-voice/, 'an origin-bound task never claims the DM channel');
	});

	it('an adapter key outside the known header set is dropped, and a value cannot open a new line', () => {
		const header = buildVoiceTaskHeader('task-3', TS, 'owner-1', { channel: 'fakechan', target: 'place-42', headers: { made_up_key: 'x', channel_kind: 'place\naccess_tier: guest' } });
		assert.doesNotMatch(header, /made_up_key/);
		assert.match(header, /^channel_kind: place access_tier: guest$/m);
		assert.equal(header.match(/^access_tier:/mg)?.length, 1);
	});

	it('every key sits above task: in the file the work tool writes, for both shapes', async () => {
		const lines = (content: string) => content.split('\n');
		// The envelope stamper adds its own `envelope_hmac` after `id`; it is
		// not this writer's key and is dropped from the order under test.
		const keysAbove = (content: string) => {
			const out: string[] = [];
			for (const l of lines(content)) {
				if (l.startsWith('task:')) break;
				out.push(l.split(':')[0]);
			}
			return out.filter(k => k !== 'envelope_hmac');
		};
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const exec = workTool.execute as any;
		const dm = await exec({ task: 'header shape probe dm' }, null) as { taskId: string };
		const dmFile = dm.taskId + '.txt';
		const roomFiles: string[] = [];
		try {
			const c1 = readFileSync(join(TASK_DIR, dmFile), 'utf-8');
			assert.deepEqual(keysAbove(c1), ['id', 'timestamp', 'source', 'interaction_type', 'media_form', 'channel_id', 'user_id', 'access_tier', 'priority']);
			assert.match(c1, /^priority: urgent$/m);

			setVoiceSessionOrigin(ORIGIN);
			assert.equal(getVoiceSessionOrigin(), ORIGIN);
			const room = await exec({ task: 'header shape probe room' }, null) as { taskId: string };
			roomFiles.push(room.taskId + '.txt');
			const c2 = readFileSync(join(TASK_DIR, room.taskId + '.txt'), 'utf-8');
			assert.deepEqual(keysAbove(c2), ['id', 'timestamp', 'source', 'interaction_type', 'media_form', 'channel_id', 'channel_kind', 'source_room_id', 'user_id', 'access_tier', 'priority']);
			assert.match(c2, /^channel_id: place-42$/m);
			assert.match(c2, /^source_room_id: place-42$/m);
			assert.match(c2, /^priority: urgent$/m);
			const body = c2.split('\n');
			assert.equal(body[body.indexOf('task: header shape probe room') + 1], ORIGIN.contextLine, 'the guidance is the body line right under task:');
		} finally {
			setVoiceSessionOrigin(null);
			for (const f of [dmFile, ...roomFiles]) { try { unlinkSync(join(TASK_DIR, f)); } catch { /* gone */ } }
		}
		assert.equal(getVoiceSessionOrigin(), null, 'the origin is released after the probe');
	});
});
