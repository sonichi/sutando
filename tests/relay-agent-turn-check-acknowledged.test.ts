// A request the turn check sends is acknowledged once. nbe2e 2026-10-21 (ws-113802 voice.log): turn_4 "Not Sonic China
// s o n i c h i" was answered "Understood, I'll make sure to check for pull requests by Sonicchi." with no work call;
// the turn check sent it (18:38:38.231) and its notice said "tell the user", so turn_5 added "I'm looking into those
// pull requests now." The turn check's verdict now says whether the reply already acknowledged the request, and
// then the submit is told to the model as context only.
// Run: npx tsx --test --test-force-exit tests/relay-agent-turn-check-acknowledged.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-turn-ack-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { RelayAgent, createVoiceTaskStore } = await import('../src/relay-agent.js');
const { parseTurnVerdict, turnClassifierPrompt } = await import('../src/work-request-check.js');
type Verdict = Awaited<ReturnType<import('../src/work-request-check.js').TurnClassifier>>;
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

const TURN_4 = 'Not Sonic China s o n i c h i s o n i c';
const TURN_4_REPLY = 'Understood, I\'ll make sure to check for pull requests by Sonicchi.';
const TASK = 'Check the pull requests made by sonicchi in the sutando repository about AG2 Sparrow and explain what they do.';

/** A relay whose turn check returns `verdict`; records what reaches the model and how. */
function relayWith(verdict: Verdict, withContext = true) {
	const store = createVoiceTaskStore();
	const notices: string[] = [];
	const context: string[] = [];
	const seen: string[] = [];
	let n = 0;
	const relay = new RelayAgent({
		store,
		notice: (t) => notices.push(t),
		...(withContext ? { context: (t: string) => context.push(t) } : {}),
		recentConversation: () => `user: ${TURN_4}\nassistant: ${TURN_4_REPLY}`,
		classify: async (input) => { seen.push(input.recentConversation); return verdict; },
		submit: async (args) => { const id = `task-${++n}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
	});
	return { relay, store, notices, context, seen };
}

describe('the turn check\'s work verdict carries whether the reply already acknowledged it', () => {
	it('is parsed only as a true flag', () => {
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK}","acknowledged":true}`), { action: 'work', task: TASK, acknowledged: true });
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK}","acknowledged":false}`), { action: 'work', task: TASK });
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK}","acknowledged":"yes"}`), { action: 'work', task: TASK });
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK}"}`), { action: 'work', task: TASK });
	});

	it('the prompt asks for it from the reply to this turn, and shows the work JSON with it', () => {
		const p = turnClassifierPrompt({ utterance: TURN_4, recentConversation: `user: ${TURN_4}\nassistant: ${TURN_4_REPLY}` });
		assert.match(p, /- With work, add "acknowledged":true when the assistant's reply to this turn \(its lines after the user's last line in the recent words\) already tells the user it is on this request/);
		assert.match(p, /Leave it out when that reply says nothing about doing it\./);
		assert.match(p, /\{"action":"work","task":"\.\.\.","acknowledged":true\}/);
		assert.ok(p.indexOf('acknowledged') < p.indexOf('Recent words (oldest first):'), 'a rule, not input');
	});
});

describe('a request the turn check sends is acknowledged once', () => {
	it('turn_4: the reply already said it is on it, so the submit is context only and asks for no second line', async () => {
		const { relay, store, notices, context, seen } = relayWith({ action: 'work', task: TASK, acknowledged: true });
		relay.onUserTurn(TURN_4);
		await tick(10);
		assert.equal(store.list().length, 1, 'sent by the turn check');
		assert.ok(seen[0].includes(`assistant: ${TURN_4_REPLY}`), 'the check judged with the reply in front of it');
		assert.deepEqual(notices, [], 'no notice: a notice is answered aloud ("I\'m looking into those pull requests now.")');
		assert.equal(context.length, 1);
		assert.match(context[0], /This request was sent to the core for the user: "Check the pull requests made by sonicchi/);
		assert.match(context[0], /You already told the user you are on it; say nothing more about it until its result comes\. Do not call work for it\./);
		assert.doesNotMatch(context[0], /Tell the user/);
	});

	it('a reply that said nothing about it: one spoken acknowledgement, by notice', async () => {
		const { relay, notices, context } = relayWith({ action: 'work', task: TASK });
		relay.onUserTurn(TURN_4);
		await tick(10);
		assert.deepEqual(context, []);
		assert.equal(notices.length, 1);
		assert.match(notices[0], /Tell the user in one short sentence that it is being looked into\. Do not call work for it\./);
	});

	it('with no context path the request is still told to the model by notice, not dropped', async () => {
		const { relay, notices } = relayWith({ action: 'work', task: TASK, acknowledged: true }, false);
		relay.onUserTurn(TURN_4);
		await tick(10);
		assert.equal(notices.length, 1);
	});

	it('a request the core did not take is not acknowledged either way', async () => {
		const store = createVoiceTaskStore();
		const notices: string[] = [];
		const context: string[] = [];
		const relay = new RelayAgent({
			store,
			notice: (t) => notices.push(t),
			context: (t) => context.push(t),
			classify: async () => ({ action: 'work', task: TASK, acknowledged: true }),
			submit: async () => ({ status: 'error', message: 'offline' }),
		});
		relay.onUserTurn(TURN_4);
		await tick(10);
		assert.deepEqual([notices, context], [[], []]);
	});
});
