// The relay agent end to end, in its own process: one live runtime reconciles the global task
// table, as in production (other test files wire several runtimes in one process).
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

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

function fakeSession(mode: { value: 'agent' | 'transcription' }) {
	const sent: string[] = [];
	const handlers: Record<string, Array<() => void>> = {};
	return {
		sent,
		emit: (event: string) => { for (const h of handlers[event] ?? []) h(); },
		eventBus: { subscribe: (event: string, h: () => void) => { (handlers[event] ??= []).push(h); } },
		getTranscriptionMode: () => mode.value,
		sessionManager: { isActive: true },
		clientConnected: true,
		transport: {
			session: { sendRealtimeInput: ({ text }: { text: string }) => sent.push(text) },
			sendContent: (turns: Array<{ text: string }>) => sent.push(turns[0].text),
		},
	};
}

describe('relay agent end to end: results lost in a reconnect come back', () => {
	it('three PR results fall back to the DM while the session reconnects; once it can speak, all three are handed over', async () => {
		const { voiceTaskStore } = await import('../src/task-bridge.js');
		const month = new Date().toISOString().slice(0, 7);
		mkdirSync(join(TMP, 'results', 'archive', month), { recursive: true });
		mkdirSync(join(TMP, 'tasks', 'archive', month), { recursive: true });
		const mode = { value: 'agent' as 'agent' | 'transcription' };
		const s = fakeSession(mode);
		s.sessionManager.isActive = false;   // the reconnect outlasts the wait
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const durable = wireDurableChannels(s as any, { notReadyRetriesMs: [10, 10], reconcileMs: 200 });
		const ids = ['3509', '5140', '5167'].map((pr) => `task-19000000${pr}`);
		for (const [i, pr] of ['3509', '5140', '5167'].entries()) {
			const id = ids[i];
			writeFileSync(join(TMP, 'tasks', 'archive', month, `${id}.txt`), `id: ${id}\nsource: voice\ntask: check PR ${pr}\n`);
			writeFileSync(join(TMP, 'results', 'archive', month, `${id}.txt`), `PR ${pr} status.`);
			voiceTaskStore.add(id, `check PR ${pr}`);
			durable.enqueue({ text: `PR ${pr} status.`, taskId: id });
		}
		await tick(2_500);
		assert.deepEqual(s.sent, [], 'nothing spoken while the session is down');
		assert.deepEqual(ids.map((id) => voiceTaskStore.get(id)?.delivery), ['dm', 'dm', 'dm']);
		s.sessionManager.isActive = true;   // the reconnect completes
		await tick(6_000);                    // the watcher reads the DM copies back, then a gather and a pause
		assert.equal(s.sent.length, 1, 'one hand-over');
		for (const pr of ['3509', '5140', '5167']) assert.match(s.sent[0], new RegExp(`PR ${pr} status`));
		assert.match(s.sent[0], /did not hear this result/);
	});
});
