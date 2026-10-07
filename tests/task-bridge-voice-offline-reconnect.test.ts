import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// A voice-only or Telegram-only install has no bridge that claims the untagged
// offline forward, so the drain's proactive-* fallthrough is its delivery path.

const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-offline-reconnect-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
const TASK_DIR = join(TMP, 'tasks');
mkdirSync(RESULT_DIR, { recursive: true });
mkdirSync(TASK_DIR, { recursive: true });

const { startResultWatcher, _isDeliveredResult, _forwardOfflineThenArchive } = await import('../src/task-bridge.js');

after(() => { try { rmSync(TMP, { recursive: true, force: true }); } catch {} });

const header = (id: string) =>
	`id: ${id}\ntimestamp: 2026-09-21T00:00:00Z\nsource: voice\ninteraction_type: realtime_audio\nmedia_form: live_stream\nchannel_id: local-voice\nuser_id: voice-local\naccess_tier: owner\npriority: urgent\ntask: capital of Peru\n`;
const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise(r => setTimeout(r, 100)); return cond(); };
const proactiveFor = (id: string) => readdirSync(RESULT_DIR).filter(f => f.startsWith(`proactive-result-${id}-`));

describe('offline voice result with no origin, no bridge: spoken when the client reconnects', () => {
	it('the forward is written unclaimed while offline, then the fallthrough speaks it on reconnect', async () => {
		const task = 'task-1700000001000';
		let connected = false;
		const spoken: string[] = [];
		writeFileSync(join(TASK_DIR, `${task}.txt`), header(task));
		writeFileSync(join(RESULT_DIR, `${task}.txt`), 'Lima is the capital of Peru.');
		startResultWatcher((result) => spoken.push(result), () => connected);

		assert.ok(await until(() => proactiveFor(task).length > 0, 8000), `no offline forward: ${readdirSync(RESULT_DIR).join(', ')}`);
		const [forward] = proactiveFor(task);
		assert.doesNotMatch(forward, /\.to-[a-z0-9_-]+\.txt$/, 'no origin: the untagged owner-DM shape');
		assert.equal(_isDeliveredResult(forward), false, 'unclaimed, so the drain still owns speaking it');
		assert.deepEqual(spoken, [], 'nothing is spoken with no client attached');

		connected = true;
		assert.ok(await until(() => spoken.length > 0, 8000), 'the reconnect drain spoke nothing');
		assert.deepEqual(spoken, ['Lima is the capital of Peru.'], 'spoken once: the originating result stays claimed');
		assert.equal(_isDeliveredResult(forward), true);
	});
});

describe('_forwardOfflineThenArchive — the originating result outlives a failed forward', () => {
	const stage = (task: string) => {
		writeFileSync(join(TASK_DIR, `${task}.txt`), header(task));
		writeFileSync(join(RESULT_DIR, `${task}.txt`), 'kept');
	};

	it('forward write fails: nothing is archived and the claim is released', async () => {
		const task = 'task-1700000001001';
		stage(task);
		const failing = async () => { throw Object.assign(new Error('ENOSPC: no space left on device'), { code: 'ENOSPC' }); };
		assert.equal(await _forwardOfflineThenArchive(task, `${task}.txt`, 'kept', false, failing, 20), false);
		await new Promise(r => setTimeout(r, 150));
		assert.ok(existsSync(join(RESULT_DIR, `${task}.txt`)), 'the undelivered result is still in results/');
		assert.ok(existsSync(join(TASK_DIR, `${task}.txt`)), 'and so is its task file');
		assert.equal(_isDeliveredResult(`${task}.txt`), false, 'released: the next drain tick retries or speaks it');
	});

	it('forward succeeds: the originating files are archived after the delay', async () => {
		const task = 'task-1700000001002';
		stage(task);
		assert.equal(await _forwardOfflineThenArchive(task, `${task}.txt`, 'kept', false, async () => 'written.txt', 20), true);
		assert.equal(_isDeliveredResult(`${task}.txt`), true);
		assert.ok(await until(() => !existsSync(join(RESULT_DIR, `${task}.txt`)) && !existsSync(join(TASK_DIR, `${task}.txt`)), 2000));
	});
});
