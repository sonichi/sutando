// In meeting mode (bodhi transcription mode) the voice model must not speak. Text written
// straight to the Gemini transport bypasses bodhi's own check, so those paths hold it, and a
// task result that lands mid-meeting waits until the meeting ends.
// Run: npx tsx --test --test-force-exit tests/meeting-input-hold.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-meeting-hold-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { injectText, injectSilentContext } = await import('../src/browser-tools.js');
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

describe('meeting mode holds direct model input', () => {
	it('injectText and injectSilentContext send nothing in transcription mode', () => {
		const mode = { value: 'transcription' as 'agent' | 'transcription' };
		const s = fakeSession(mode);
		injectText(s, 'hello');
		assert.equal(injectSilentContext(s, 'context'), false);
		assert.deepEqual(s.sent, []);
		mode.value = 'agent';
		injectText(s, 'hello');
		assert.equal(injectSilentContext(s, 'context'), true);
		assert.deepEqual(s.sent, ['hello', 'context']);
	});

	it('a task result that lands during a meeting is delivered after it ends', async () => {
		const mode = { value: 'transcription' as 'agent' | 'transcription' };
		const s = fakeSession(mode);
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		wireDurableChannels(s as any);
		writeFileSync(join(TMP, 'results', 'task-1.txt'), 'Health check: all services up.');
		await tick(5_000);
		assert.deepEqual(s.sent, [], 'nothing reaches the model during the meeting');
		mode.value = 'agent';
		await tick(6_500);
		assert.equal(s.sent.length, 1);
		assert.match(s.sent[0], /Health check: all services up\./);
	});

	it('after a meeting, the summary, a held task result and a call result are spoken in turn, not over each other', async () => {
		const mode = { value: 'transcription' as 'agent' | 'transcription' };
		const s = fakeSession(mode);
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const durable = wireDurableChannels(s as any);
		// Straight into the queue: the watcher started by the test above would claim a result file.
		durable.enqueue({ text: 'Test result: the build finished successfully.', taskId: 'task-2' });
		durable.enqueue({ text: '[System: The phone call just completed.]\n\nCall transcript:\nThe dentist confirmed Tuesday at 3 pm.', framed: true });
		await tick(5_000);
		assert.deepEqual(s.sent, [], 'nothing reaches the model during the meeting');
		mode.value = 'agent';
		s.emit('turn.start');   // the meeting summary is being spoken
		await tick(6_000);
		assert.deepEqual(s.sent, [], 'nothing cuts into the summary');
		s.emit('turn.end');
		await tick(8_000);
		assert.equal(s.sent.length, 1, 'one hand-over, after a pause');
		assert.match(s.sent[0], /2 task results arrived together/);
		assert.match(s.sent[0], /the build finished successfully/);
		assert.match(s.sent[0], /The dentist confirmed Tuesday at 3 pm/);
	});
});

describe('the phone call-result poller', () => {
	it('leaves latest-result.json in place during a meeting instead of deleting it unsent', async () => {
		const { readFileSync } = await import('node:fs');
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		const poller = src.slice(src.indexOf("join(CALL_RESULTS_DIR, 'latest-result.json')"));
		const guard = poller.indexOf('meetingHoldsModel(session)');
		assert.ok(guard !== -1 && guard < poller.indexOf('unlinkSync(callResultFile)'), 'the meeting check returns before the file is deleted');
	});
});
