// A session-end result reaches the owner's DM after the call; it is never spoken into a later call.
// Run: npx tsx --test --test-force-exit tests/voice-session-end-delivery.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'session-end-delivery-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
const TASK_DIR = join(TMP, 'tasks');
for (const d of [RESULT_DIR, TASK_DIR, join(TMP, 'state')]) mkdirSync(d, { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { startResultWatcher, submitVoiceSessionEndTask } = await import('../src/task-bridge.js');

const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 100)); return cond(); };
const copiesOf = (id: string) => readdirSync(RESULT_DIR).filter((f) => f.startsWith(`proactive-result-${id}-`));

describe('session-end result delivery', () => {
	it('held while a later call is live, then forwarded to the owner DM once no client is attached', async () => {
		let connected = true;
		const spoken: string[] = [];
		startResultWatcher((r) => spoken.push(r), () => connected);

		// An ordinary voice task's result is still spoken into the live call: the hold is session-end only.
		const work = 'task-1700000002000';
		writeFileSync(join(TASK_DIR, `${work}.txt`), `id: ${work}\nsource: voice\nchannel_id: local-voice\naccess_tier: owner\ntask: capital of Peru\n`);
		writeFileSync(join(RESULT_DIR, `${work}.txt`), 'Lima.');
		assert.ok(await until(() => spoken.includes('Lima.'), 8000), 'control: a work result is spoken while connected');

		const id = await submitVoiceSessionEndTask('VOICE_SESSION_ENDED: test', 'user: remind me Friday');
		writeFileSync(join(RESULT_DIR, `${id}.txt`), 'Scheduled the Friday reminder.');
		await new Promise((r) => setTimeout(r, 5000));
		assert.deepEqual(spoken, ['Lima.'], 'the session-end result is not spoken into the live call');
		assert.ok(existsSync(join(RESULT_DIR, `${id}.txt`)), 'and stays queued');
		assert.deepEqual(copiesOf(id), []);

		connected = false;
		assert.ok(await until(() => copiesOf(id).length > 0, 8000), `no DM forward: ${readdirSync(RESULT_DIR).join(', ')}`);
		const [copy] = copiesOf(id);
		assert.doesNotMatch(copy, /\.to-[a-z0-9_-]+\.txt$/, 'the untagged owner-DM shape');
		assert.equal(readFileSync(join(RESULT_DIR, copy), 'utf-8'), 'Scheduled the Friday reminder.');
		assert.deepEqual(spoken, ['Lima.']);
	});
});
