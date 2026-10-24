// A model, not a phrase pattern, decides whether a spoken turn told the user a result waiting to be heard. nbe2e
// 2026-10-21 (ws-115544 voice.log): "You merged 3 pull requests yesterday: number 5641, 5648 and 5650." was said in turn_10
// (18:56:52) as "Regarding your merged pull requests, you merged three yesterday."; the pattern judged it unsaid, the
// relay nudged at 18:57:05 and turn_13 said it again. Only exact evidence (a distinctive number said) is still decided by
// the pattern at once; every other turn goes to the heard check, and the nudge and release wait for its verdict.
// Own process (the result watcher is process-wide).
// Run: npx tsx --test --test-force-exit tests/relay-agent-heard-by-turn-check.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-heard-check-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const runtime = await import('../src/live-agent-runtime.js');
const check = await import('../src/work-request-check.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => { for (let w = 0; w < ms / 50 && !done(); w++) await tick(50); };

type HeardInput = { results: Array<{ id: string; request: string; result: string }>; assistantWords: string; cutOff: boolean };
/** The heard check's verdicts, as the test decides them; every input it was given. */
const inputs: HeardInput[] = [];
let judge: (i: HeardInput) => Promise<string[] | null> = async () => null;

const notices: string[] = [];
const items: Array<{ role: string; content: string; timestamp?: number }> = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string, e: unknown = {}) => { for (const h of handlers[event] ?? []) h(e); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	conversationContext: { items },
	tryPublishSystemNotification(text: string) { notices.push(text); return true; },
	transport: { sendContent: () => {} },
};
let submitted: Record<string, unknown> = {};
const relay = new RelayAgent({
	submit: async () => submitted,
	store: voiceTaskStore,
	heard: (i: HeardInput) => { inputs.push(i); return judge(i); },
});
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
const NUDGE_MS = 400;
const RELEASE_MS = 1_500;
const VERDICT_WAIT_MS = 2_500;
runtime.wireDurableChannels(session as never, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, returnNudgeMs: NUDGE_MS, returnReleaseMs: RELEASE_MS, heardVerdictWaitMs: VERDICT_WAIT_MS } as never);

let seq = 0;
const REQUEST = 'How many pull requests did I merge yesterday?';
/** A merge-count result with numbers of its own, so a turn that says one of them ends the test's result. */
const mergedResult = (n: number) => `You merged 3 pull requests yesterday: number ${n}, number ${n + 7} and number ${n + 9}.`;
/** Asks for each request, then returns all their results through the waiting calls at once. */
async function returnedTogether(asks: Array<{ result: string; request?: string }>): Promise<string[]> {
	const calls = asks.map(({ request = REQUEST }) => {
		const id = `task-19300${String(++seq).padStart(8, '0')}`;
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${request}\n`);
		voiceTaskStore.add(id, request);
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		return { id, call: relay.invoke('Execute tool: work', { task: request }) };
	});
	await tick(0);
	asks.forEach(({ result }, k) => writeFileSync(join(TMP, 'results', `${calls[k].id}.txt`), result));
	for (const { id, call } of calls) {
		const out = await Promise.race([call, tick(5_000).then(() => 'timed out')]);
		assert.notEqual(out, 'timed out');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'returned, not yet heard');
	}
	return calls.map((c) => c.id);
}
const returned = async (result: string, request = REQUEST): Promise<string> => (await returnedTogether([{ result, request }]))[0];
/** The model speaks one turn with these words. */
const modelSays = (text: string, turnId: string, opts: { cutOff?: boolean; at?: number } = {}) => {
	session.emit('turn.start');
	session.emit('response.first_audio');
	items.push({ role: 'assistant', content: text, ...(opts.at !== undefined ? { timestamp: opts.at } : {}) });
	if (opts.cutOff) session.emit('turn.interrupted', { turnId });
	session.emit('turn.end', { turnId });
};
const nudgesFor = (request = REQUEST) => notices.filter((n) => n.includes('came back through its work call') && n.includes(request.replace(/"/g, "'"))).length;
const deferred = () => { let resolve!: (v: string[] | null) => void; const p = new Promise<string[] | null>((r) => { resolve = r; }); return { p, resolve }; };

describe('the heard check decides whether a turn told the user a waiting result', () => {
	it('turn_10: "you merged three yesterday" told the result; it is heard, and no nudge follows', async () => {
		const id = await returned(mergedResult(5641));
		const before = notices.length;
		inputs.length = 0;
		judge = async (i) => i.results.map((r) => r.id);
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'a1');
		await tick(10);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		assert.equal(inputs.length, 1, 'one heard check for the turn');
		assert.deepEqual(inputs[0].results.map((r) => r.id), [id]);
		assert.match(inputs[0].results[0].request, /How many pull requests did I merge yesterday/);
		assert.equal(inputs[0].results[0].result, mergedResult(5641));
		assert.match(inputs[0].assistantWords, /you merged three yesterday/);
		assert.equal(inputs[0].cutOff, false);
		await tick(NUDGE_MS + 200);
		assert.equal(notices.length, before, 'no nudge: turn_13 "I\'ve confirmed that you merged exactly three" never comes');
	});

	it('the model\'s "not told" beats a pattern that would have taken it ("three of them")', async () => {
		const id = await returned(mergedResult(5741));
		judge = async () => [];
		modelSays('Regarding how many pull requests you merged yesterday, you merged three of them.', 'a2');
		await tick(10);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'the model judged it not told');
		await until(() => nudgesFor() > 0, 2_000);
		assert.ok(nudgesFor() > 0, 'still owed, so nudged');
		modelSays('You merged three pull requests yesterday: 5741, 5748 and 5750.', 'a2b');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		notices.length = 0;
	});

	it('a holding line that names the count before its noun is the model\'s call, not the pattern\'s', async () => {
		const id = await returned(mergedResult(5841));
		inputs.length = 0;
		judge = async () => [];
		modelSays('I\'m checking your three pull requests from yesterday now.', 'a3');
		await tick(10);
		assert.equal(inputs.length, 1);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
		modelSays('Those were 5841, 5848 and 5850.', 'a3b');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(NUDGE_MS);
		notices.length = 0;
	});

	it('a nudge that comes due while the verdict is pending waits for it: none when the turn told it', async () => {
		const id = await returned(mergedResult(5941));
		const v = deferred();
		judge = () => v.p;
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'a4');
		await tick(NUDGE_MS + 200);
		assert.equal(nudgesFor(), 0, 'no nudge while the verdict is pending');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
		v.resolve([id]);
		await tick(10);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(100);
		assert.equal(nudgesFor(), 0);
	});

	it('a nudge that came due while the verdict was pending goes once the verdict says not told', async () => {
		const id = await returned(mergedResult(6041));
		const v = deferred();
		judge = () => v.p;
		modelSays('Sure, here is a joke about atoms.', 'a5');
		await tick(NUDGE_MS + 200);
		assert.equal(nudgesFor(), 0);
		v.resolve([]);
		await tick(10);
		assert.equal(nudgesFor(), 1);
		modelSays('You merged 6041, 6048 and 6050 yesterday.', 'a5b');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		notices.length = 0;
	});

	it('the release waits for a pending verdict: a told result is never handed over again', async () => {
		const id = await returned(mergedResult(6141));
		const v = deferred();
		judge = () => v.p;
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'a6');
		await tick(RELEASE_MS + 200);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'not released to the reconcile pass while the verdict is pending');
		v.resolve([id]);
		await tick(10);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(700);
		assert.equal(notices.filter((n) => n.includes('6141')).length, 0, 'not handed over again');
		assert.equal(nudgesFor(), 0);
	});

	it('two results waiting, the turn told one: only that one is heard', async () => {
		const videoAsk = 'Watch the use case 03 video and summarize it';
		const [a, b] = await returnedTogether([{ result: mergedResult(6241) }, { result: 'The Use Case 03 video shows an agent booking a restaurant table and sending the confirmation to the owner on AG2 Space.', request: videoAsk }]);
		inputs.length = 0;
		judge = async () => [a];
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'a7');
		await tick(10);
		assert.equal(inputs.length, 1, 'one check for both');
		assert.deepEqual(inputs[0].results.map((r) => r.id).sort(), [a, b].sort());
		assert.equal(voiceTaskStore.get(a)?.delivery, 'spoken');
		assert.equal(voiceTaskStore.get(b)?.delivery, 'injected');
		judge = async () => [b];
		modelSays('Regarding the use case video, it shows an agent booking a restaurant table.', 'a7b');
		await tick(10);
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken');
		await tick(NUDGE_MS);
		notices.length = 0;
	});

	it('a stale "not told" for an earlier turn does not nudge a result a newer turn\'s verdict may still find told', async () => {
		const id = await returned(mergedResult(6341));
		const first = deferred();
		const second = deferred();
		const verdicts = [first, second];
		judge = () => verdicts.shift()!.p;
		modelSays('Regarding your merged pull requests,', 'a8', { cutOff: true });
		modelSays('you merged three yesterday.', 'a8b');
		await tick(NUDGE_MS + 200);
		first.resolve([]);
		await tick(10);
		assert.equal(nudgesFor(), 0, 'the newer turn\'s verdict is still coming');
		second.resolve([id]);
		await tick(10);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		assert.equal(nudgesFor(), 0);
	});

	it('the check sees every assistant line since the result came back, and whether the turn was cut off', async () => {
		const id = await returned(mergedResult(6441));
		inputs.length = 0;
		judge = async () => [];
		modelSays('Regarding your merged pull requests,', 'a9', { at: Date.now() + 1 });
		modelSays('you merged', 'a9b', { at: Date.now() + 2, cutOff: true });
		await tick(10);
		assert.equal(inputs.length, 2);
		assert.match(inputs[1].assistantWords, /Regarding your merged pull requests,[\s\S]*you merged/);
		assert.equal(inputs[1].cutOff, true);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
		modelSays('You merged 6441, 6448 and 6450.', 'a9c');
		await tick(NUDGE_MS);
		notices.length = 0;
	});

	it('a reconnect while the verdict is pending: a late "told" changes nothing, the result is handed over again', async () => {
		const id = await returned(mergedResult(6541));
		const v = deferred();
		judge = () => v.p;
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'a10');
		session.emit('session.reconnectBoundary');
		assert.equal(voiceTaskStore.get(id)?.delivery, undefined, 'unset at the boundary, as before');
		v.resolve([id]);
		await tick(10);
		assert.notEqual(voiceTaskStore.get(id)?.delivery, 'spoken');
		await until(() => notices.some((n) => n.includes('6541')), 3_000);
		modelSays('You merged 6541, 6548 and 6550.', 'a10b');
		await tick(NUDGE_MS);
		notices.length = 0;
	});
});

describe('exact evidence is decided at once; no verdict falls back to the rule as it was', () => {
	it('a distinctive number said verbatim is heard at once, with no check', async () => {
		const id = await returned(mergedResult(6641));
		inputs.length = 0;
		modelSays('You merged pull requests 6641, 6648 and 6650 yesterday.', 'b1');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken', 'in the same tick');
		assert.equal(inputs.length, 0);
	});

	it('the count right before its noun, in a turn that is not a holding line, is heard at once', async () => {
		const id = await returned(mergedResult(6741));
		inputs.length = 0;
		modelSays('Regarding the rechecked merge history, you merged three pull requests yesterday.', 'b2');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		assert.equal(inputs.length, 0);
	});

	it('the check failing or giving no answer: the rule as it was ("three yesterday" unheard, "three of them" heard)', async () => {
		const a = await returned(mergedResult(6841));
		judge = async () => { throw new Error('This operation was aborted'); };
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'b3');
		await tick(10);
		assert.equal(voiceTaskStore.get(a)?.delivery, 'injected');
		await until(() => nudgesFor() > 0, 2_000);
		assert.ok(nudgesFor() > 0);
		modelSays('You merged 6841, 6848 and 6850.', 'b3b');
		notices.length = 0;
		const b = await returned(mergedResult(6941));
		judge = async () => null;
		modelSays('Regarding how many pull requests you merged yesterday, you merged three of them.', 'b3c');
		await tick(10);
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken');
		await tick(NUDGE_MS);
		notices.length = 0;
	});

	it('no verdict in time: the rule as it was, and a verdict after that changes nothing', async () => {
		const id = await returned(mergedResult(7041));
		const v = deferred();
		judge = () => v.p;
		modelSays('Regarding your merged pull requests, you merged three yesterday.', 'b4');
		await tick(VERDICT_WAIT_MS + 50);
		assert.notEqual(voiceTaskStore.get(id)?.delivery, 'spoken', 'the rule as it was: unheard');
		// The release came due while the verdict was pending (RELEASE_MS < VERDICT_WAIT_MS): it goes now, and the
		// reconcile pass hands the result over again instead of a nudge as well.
		v.resolve([id]);
		await tick(10);
		assert.notEqual(voiceTaskStore.get(id)?.delivery, 'spoken', 'one decision per turn: the late verdict changes nothing');
		await until(() => notices.some((n) => n.includes('7041')), 3_000);
		assert.ok(notices.some((n) => n.includes('7041')), 'handed over again');
		assert.equal(nudgesFor(), 0, 'not nudged as well');
		modelSays('You merged 7041, 7048 and 7050.', 'b4b');
		await tick(NUDGE_MS);
		notices.length = 0;
	});
});

describe('the heard check\'s prompt and verdict', () => {
	const results = [{ id: 'task-1', request: REQUEST, result: mergedResult(5641) }];
	it('keeps only ids it was given; anything unreadable is no verdict', () => {
		assert.deepEqual(check.parseHeardVerdict('{"told":["task-1","task-9"]}', results), ['task-1']);
		assert.deepEqual(check.parseHeardVerdict('{"told":[]}', results), []);
		assert.equal(check.parseHeardVerdict('{"told":"task-1"}', results), null);
		assert.equal(check.parseHeardVerdict('not json', results), null);
	});

	it('states the rule, gives each result in full up to 600 characters, the words and the cut-off', () => {
		const long = `${'The video opens on the AG2 Space home page and walks through the room list. '.repeat(4)}The main answer: the agent books a table.`;
		assert.ok(long.length > 200 && long.length < 600);
		const p = check.heardCheckPrompt({ results: [...results, { id: 'task-2', request: 'summarize the video', result: long }], assistantWords: 'Regarding your merged pull requests, you merged three yesterday.', cutOff: true });
		assert.match(p, /A result is told when the words give the user its main answer, in any wording or language/);
		assert.match(p, /Saying it is checking or working on it, naming the request without its answer, or a different answer does not tell it\./);
		assert.match(p, /An answer cut off before its main point does not tell it\./);
		assert.ok(p.includes(long), 'not clipped at 200 characters');
		assert.match(p, /task-1: "How many pull requests did I merge yesterday\?" -> You merged 3 pull requests yesterday/);
		assert.match(p, /the last turn was cut off by the user/);
		assert.ok(p.indexOf('A result is told') < p.indexOf('Results waiting to be told'), 'rules, then input');
	});
});
