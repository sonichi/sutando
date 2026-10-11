// The relay agent end to end, in its own process with one live runtime (the result watcher is
// process-wide, so a second runtime would claim results meant for this one).
// Run: npx tsx --test --test-force-exit tests/relay-agent-e2e.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-e2e-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => {
	for (let w = 0; w < ms / 100 && !done(); w++) await tick(100);
};

const sent: string[] = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string) => { for (const h of handlers[event] ?? []) h({}); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	tryPublishSystemNotification(text: string) {
		if (!this.sessionManager.isActive) return false;
		sent.push(text);
		return true;
	},
	transport: { sendContent: () => {} },
};
let submitted: Record<string, unknown> = {};
const relay = new RelayAgent({ submit: async () => submitted, store: voiceTaskStore });
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300 });

let seq = 0;
/** A voice task the core is working on: its task file, and its row in the table. */
function voiceTask(text: string): string {
	const id = `task-19000${String(++seq).padStart(8, '0')}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
	return id;
}
const heard = (id: string) => ['spoken', 'injected'].includes(voiceTaskStore.get(id)?.delivery ?? '');

describe('relay agent end to end', () => {
	it('the result returns through its waiting work call and is not injected', async () => {
		const id = voiceTask('draw a cat');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'draw a cat' });
		await tick(0);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Here is the cat.');
		const out = await Promise.race([call, tick(8_000).then(() => 'timed out')]);
		assert.match(out, /"draw a cat"[\s\S]*Here is the cat\./);
		await tick(1_000);
		assert.equal(sent.length, before, 'returned through the call, never injected');
		assert.ok(heard(id));
	});

	it('three requests the core answered in one result: all three named to the model, every row heard', async () => {
		const ids = ['5308', '5309', '5310'].map((pr) => voiceTask(`check PR ${pr}`));
		writeFileSync(join(TMP, 'results', `${ids[1]}.txt`), `[deduped: ${ids[0]}]`);
		writeFileSync(join(TMP, 'results', `${ids[2]}.txt`), `[deduped: ${ids[0]}]`);
		writeFileSync(join(TMP, 'results', `${ids[0]}.txt`), 'All three: 5308 merged; 5309 ready; 5310 blocked.');
		const named = () => ['5308', '5309', '5310'].filter((pr) => sent.join('\n').includes(`"check PR ${pr}"`));
		await until(() => named().length === 3 && ids.every(heard), 15_000);
		assert.deepEqual(named(), ['5308', '5309', '5310'], 'every request named to the model');
		assert.ok(sent.every((t) => !t.includes('[deduped:')), 'a dedup marker is never spoken');
		assert.deepEqual(ids.map((id) => voiceTaskStore.get(id)?.answeredBy), [undefined, ids[0], ids[0]]);
		assert.ok(ids.every(heard));
	});

	it('a session that cannot take a result leaves one DM copy; a second fallback writes none', async () => {
		session.sessionManager.isActive = false;
		const id = voiceTask('check PR 9999');
		const copies = () => readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length;
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 9999 status.');
		await until(() => copies() === 1);
		assert.equal(copies(), 1);
		await until(() => voiceTaskStore.get(id)?.delivery === 'dm');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'dm');
		// The copy read back while the session is still down is not copied again.
		await tick(3_000);
		assert.ok(readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length <= 1);
		session.sessionManager.isActive = true;
		await until(() => sent.some((t) => t.includes('PR 9999 status.')), 15_000);
		assert.ok(sent.some((t) => t.includes('PR 9999 status.')), 'spoken once the session can');
		assert.ok(existsSync(join(TMP, 'results')));
	});
	it('no client connected: the result goes to the DM and its waiting call ends instead of hanging', async () => {
		session.clientConnected = false;
		const id = voiceTask('check PR 7777');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 7777' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 7777 status.');
		const out = await Promise.race([call, tick(8_000).then(() => 'timed out')]);
		assert.match(out, /delivered to the user separately/);
		assert.doesNotMatch(out, /PR 7777 status/, 'not handed to a session nobody hears');
		assert.equal(relay.isWaiting(id), false);
		await until(() => voiceTaskStore.get(id)?.delivery === 'dm');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'dm');
		assert.equal(readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length, 1);
		session.clientConnected = true;
	});
});
