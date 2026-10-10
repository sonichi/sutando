// The relay agent end to end, in its own process: a result file the watcher picks up goes back to the
// work call waiting for it, and only a result no call waits for is injected (one runtime per process).
// Run: npx tsx --test --test-force-exit tests/relay-agent-e2e.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-e2e-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

function fakeSession() {
	const sent: string[] = [];
	return {
		sent,
		eventBus: { subscribe: () => {} },
		getTranscriptionMode: () => 'agent',
		sessionManager: { isActive: true },
		clientConnected: true,
		transport: {
			session: { sendRealtimeInput: ({ text }: { text: string }) => sent.push(text) },
			sendContent: (turns: Array<{ text: string }>) => sent.push(turns[0].text),
		},
	};
}

/** A voice task the core is working on: its task file, and its row in the table. */
function voiceTask(id: string, text: string) {
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
}

describe('relay agent end to end', () => {
	const s = fakeSession();
	let submitted: Record<string, unknown> = {};
	const relay = new RelayAgent({ submit: async () => submitted, store: voiceTaskStore });
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	wireDurableChannels(s as any, { relay, reconcileMs: 60_000 });

	it('the core result for a waiting work call returns through that call, and is not injected', async () => {
		const id = 'task-1900000000001';
		voiceTask(id, 'draw a cat');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'draw a cat' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Here is the cat.');
		const result = await Promise.race([call, tick(8_000).then(() => 'timed out')]);
		assert.match(result, /TASK_RESULT_START[\s\S]*Here is the cat\./);
		await tick(2_500);
		assert.deepEqual(s.sent, [], 'returned through the call, never injected');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
	});

	it('a result whose call is gone (cancelled, session closed) is injected as before', async () => {
		const id = 'task-1900000000002';
		voiceTask(id, 'draw a dog');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const ctl = new AbortController();
		const call = relay.invoke('Execute tool: work', { task: 'draw a dog' }, ctl.signal);
		await tick(0);
		ctl.abort();
		await assert.rejects(call, /aborted/);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Here is the dog.');
		for (let i = 0; i < 40 && s.sent.length === 0; i++) await tick(250);
		assert.equal(s.sent.length, 1);
		assert.match(s.sent[0], /Here is the dog\./);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
	});
});
