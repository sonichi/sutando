// A repeat of a request whose result is waiting for its heard verdict, and a re-check asked before that verdict came.
// nbe2e 2026-10-21 (ws-115544): turn_10 ended 18:56:52.2 saying "you merged three yesterday", which only the heard check
// can call told (about 1 s, up to its 4 s timeout; waited on up to HEARD_VERDICT_WAIT_MS). The user said "I think I
// merged more than that" and the model called work at 18:56:55.4. While the verdict was coming the row still read
// 'injected', so a work check naming the earlier task returned "say it to the user now": told meanwhile, it was said
// twice and her re-check was not sent. Once told, the row's last change was the verdict's time, so a re-check she said
// between the turn and the verdict read as asked before she heard it and was closed as an orphan.
// Own process (the result watcher is process-wide).
// Run: npx tsx --test --test-force-exit tests/relay-agent-heard-pending-repeat.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-heard-pending-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const runtime = await import('../src/live-agent-runtime.js');
const { RelayAgent, createSpeechGate } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const deferred = () => { let resolve!: (v: string[] | null) => void; const p = new Promise<string[] | null>((r) => { resolve = r; }); return { p, resolve }; };

let judge: () => Promise<string[] | null> = async () => null;
/** The work check's verdict: a repeat of this task, or a new request. */
let repeatOf: string | null = null;

const items: Array<{ role: string; content: string }> = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string, e: unknown = {}) => { for (const h of handlers[event] ?? []) h(e); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	conversationContext: { items },
	tryPublishSystemNotification() { return true; },
	transport: { sendContent: () => {} },
};
const speech = createSpeechGate({ quietMs: 20 });
let seq = 0;
const sent: string[] = [];
const relay = new RelayAgent({
	submit: async (args) => {
		const id = `task-19400${String(++seq).padStart(8, '0')}`;
		const task = String(args.task);
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${task}\n`);
		voiceTaskStore.add(id, task);
		sent.push(id);
		return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
	},
	store: voiceTaskStore,
	speech,
	check: async () => (repeatOf ? { action: 'duplicate', of: repeatOf } : { action: 'submit' }),
	heard: () => judge(),
});
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
runtime.wireDurableChannels(session as never, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, returnNudgeMs: 2_000, returnReleaseMs: 3_000, heardVerdictWaitMs: 3_000 } as never);

const REQUEST = 'How many pull requests did I merge yesterday?';
/** Asks, and returns the result through the waiting call; not heard yet. */
async function returned(n: number): Promise<string> {
	repeatOf = null;
	const call = relay.invoke('Execute tool: work', { task: REQUEST });
	await tick(50);
	const id = sent[sent.length - 1];
	writeFileSync(join(TMP, 'results', `${id}.txt`), `You merged 3 pull requests yesterday: number ${n}, number ${n + 7} and number ${n + 9}.`);
	assert.notEqual(await Promise.race([call, tick(5_000).then(() => 'timed out')]), 'timed out');
	assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
	return id;
}
/** turn_10: tells the result in words only the heard check can judge. */
const toldTurn = (turnId: string) => {
	session.emit('turn.start');
	session.emit('response.first_audio');
	speech.onModelAudio();
	items.push({ role: 'assistant', content: 'Regarding your merged pull requests, you merged three yesterday.' });
	session.emit('turn.end', { turnId });
};
const userSays = async () => { speech.onUserSpeechStarted(); await tick(30); speech.onUserSpeechEnded(); await tick(30); };

describe('a repeat while the heard verdict is coming', () => {
	it('the model re-issues its call; the verdict says told: closed quietly, not "say it now"', async () => {
		const id = await returned(5641);
		const v = deferred();
		judge = () => v.p;
		toldTurn('c1');
		await tick(20);
		repeatOf = id;
		const again = relay.invoke('Execute tool: work', { task: REQUEST });
		await tick(50);
		v.resolve([id]);
		const out = await Promise.race([again, tick(4_000).then(() => 'timed out')]);
		assert.doesNotMatch(out, /Say it to the user now/, 'told already: saying it now is the second time');
		assert.match(out, /Stay silent/);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('the user asks again after the turn; the verdict says told: her re-check is sent', async () => {
		const id = await returned(5741);
		const v = deferred();
		judge = () => v.p;
		toldTurn('c2');
		await tick(20);
		await userSays(); // "I think I merged more than that."
		repeatOf = id;
		const before = sent.length;
		const recheck = relay.invoke('Execute tool: work', { task: 'Check again how many pull requests I merged yesterday' });
		await tick(50);
		v.resolve([id]);
		await tick(50);
		assert.equal(sent.length, before + 1, 'the re-check reaches the core');
		assert.ok(relay.isWaiting(sent[sent.length - 1]));
		void recheck;
	});

	it('the verdict says not told: "say it now", as before', async () => {
		const id = await returned(5841);
		const v = deferred();
		judge = () => v.p;
		toldTurn('c3');
		await tick(20);
		repeatOf = id;
		const again = relay.invoke('Execute tool: work', { task: REQUEST });
		await tick(50);
		v.resolve([]);
		assert.match(await again, /Say it to the user now/);
	});
});

describe('a re-check said between the turn that told the result and its verdict', () => {
	it('is sent: heard counts from that turn\'s end, not from when the verdict came', async () => {
		const id = await returned(5941);
		const v = deferred();
		judge = () => v.p;
		toldTurn('d1');
		await tick(20);
		await userSays(); // she spoke after the turn, before the verdict
		v.resolve([id]);
		await tick(20);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		repeatOf = id;
		const before = sent.length;
		void relay.invoke('Execute tool: work', { task: 'Check again how many pull requests I merged yesterday' });
		await tick(50);
		assert.equal(sent.length, before + 1, 'not closed as an orphan');
	});
});
