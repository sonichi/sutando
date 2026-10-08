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
	return {
		sent,
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
		await tick(4_500);
		assert.equal(s.sent.length, 1);
		assert.match(s.sent[0], /Health check: all services up\./);
	});
});
