// A result with no open work call, after a reconnect: handed as live input, not as a system notice
// (bodhi 0.4.6's notices stop reaching the model after a reconnect). Own process: the result watcher
// is process-wide, so this runtime cannot share one with relay-agent-e2e.
// Run: npx tsx --test --test-force-exit tests/relay-agent-live-fallback.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-live-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => { for (let w = 0; w < ms / 100 && !done(); w++) await tick(100); };

const notices: string[] = [];
const live: string[] = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string, e: unknown = {}) => { for (const h of handlers[event] ?? []) h(e); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	tryPublishSystemNotification(text: string) { notices.push(text); return true; },
	transport: { sendContent: () => {} },
};
const relay = new RelayAgent({ submit: async () => ({}), store: voiceTaskStore });
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, injectLive: (text) => live.push(text) });

let seq = 0;
/** A voice task whose call is gone: a row in the table and a task file, no call waiting. */
function callLessTask(text: string): string {
	const id = `task-19100${String(++seq).padStart(8, '0')}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
	return id;
}
const modelSpeaks = (turnId: string) => { session.emit('turn.start'); session.emit('response.first_audio'); session.emit('turn.end', { turnId }); };

describe('a result with no open call: notice before any reconnect, live input after one', () => {
	it('before a reconnect it goes as a system notice', async () => {
		const id = callLessTask('count the PRs');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Four PRs were merged this week.');
		await until(() => voiceTaskStore.get(id)?.delivery === 'injected');
		assert.equal(notices.filter((n) => n.includes('Four PRs')).length, 1, 'one notice');
		assert.equal(live.length, 0, 'not live input');
		modelSpeaks('t1');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('after a fresh dial it goes as live input, and the model\'s next spoken turn records it heard', async () => {
		session.emit('session.stateChange', { toState: 'RECONNECTING' });
		session.emit('session.reconnectBoundary', {});
		session.emit('session.stateChange', { toState: 'ACTIVE' });
		const id = callLessTask('build the trainer app');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Your trainer app is built and published.');
		await until(() => voiceTaskStore.get(id)?.delivery === 'injected');
		assert.equal(live.filter((n) => n.includes('trainer app is built')).length, 1, 'handed as live input');
		assert.equal(notices.filter((n) => n.includes('trainer app')).length, 0, 'no notice for it');
		assert.match(live[0], /TASK_RESULT_START/, 'framed as a task result, not as the user\'s words');
		modelSpeaks('t2');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('once the model has completed a spoken turn on the new connection, notices are trusted again', async () => {
		// The spoken turn t2 above completed after ACTIVE: the notification path is back.
		const id = callLessTask('send the file to Discord');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Sent hello world.txt to your Discord DM.');
		await until(() => voiceTaskStore.get(id)?.delivery === 'injected');
		assert.equal(notices.filter((n) => n.includes('hello world')).length, 1, 'a notice again');
		assert.equal(live.filter((n) => n.includes('hello world.txt')).length, 0, 'not live input');
		modelSpeaks('t3');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});
});
