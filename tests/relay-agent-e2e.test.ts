// The relay agent end to end, in its own process: one live runtime reconciles the global task
// table, as in production (other test files wire several runtimes in one process).
// Run: npx tsx --test --test-force-exit tests/relay-agent-e2e.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-e2e-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');

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
		tryPublishSystemNotification(text: string) {
			if (!this.sessionManager.isActive) return false;
			sent.push(text);
			return true;
		},
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
		const relay = new RelayAgent({ submit: async () => ({}), store: voiceTaskStore });
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const durable = wireDurableChannels(s as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 200 });
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
		// The watcher reads the DM copies back; whatever is waiting together goes in one hand-over per turn.
		const heard = () => ['3509', '5140', '5167'].filter((pr) => s.sent.join('\n').includes(`PR ${pr} status`));
		for (let w = 0; w < 80 && heard().length < 3; w++) {
			await tick(100);
			if (w % 5 === 4) s.emit('turn.end');
		}
		assert.deepEqual(heard(), ['3509', '5140', '5167'], 'all three handed over');
		assert.match(s.sent.join('\n'), /did not hear this result/);
		// One DM copy per result: a result already sent to the DM is not copied again while voice is down.
		const copies = readdirSync(join(TMP, 'results', 'archive', month)).filter((f) => f.startsWith('proactive-result-'));
		assert.ok(copies.length <= 3, `at most one copy per result, got ${copies.length}`);
	});

	it('a result already sent to the DM is not copied again when it falls back a second time', async () => {
		const { voiceTaskStore } = await import('../src/task-bridge.js');
		const mode = { value: 'agent' as 'agent' | 'transcription' };
		const s = fakeSession(mode);
		s.sessionManager.isActive = false;
		const relay = new RelayAgent({ submit: async () => ({}), store: voiceTaskStore });
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const durable = wireDurableChannels(s as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 600_000 });
		const id = 'task-1900000009999';
		voiceTaskStore.add(id, 'check PR 9999');
		const copiesOf = () => readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length;
		durable.enqueue({ text: 'PR 9999 status.', taskId: id });
		for (let i = 0; i < 100 && copiesOf() === 0; i++) await tick(50);
		assert.equal(copiesOf(), 1);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'dm');
		durable.enqueue({ text: 'PR 9999 status.', taskId: id });
		await tick(3_500);
		assert.equal(copiesOf(), 1, 'the second fallback writes no second copy');
	});
});
