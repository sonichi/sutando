// When the user's turn is over. Incidents from the 2026-10-21 17:38-17:46 session (voice-agent.log).
// Run: npx tsx --test --test-force-exit tests/relay-agent-turn-taking.test.ts
import { describe, it, after, afterEach, beforeEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-turn-taking-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const { RelayAgent, createVoiceTaskStore, createSpeechGate, createHandoffPacer, wireUserSpeech, QUIET_BEFORE_SUBMIT_MS, MAX_SUBMIT_HOLD_MS, USER_REPLY_WAIT_MS, HANDOFF_CONFIRM_MS } = await import('../src/relay-agent.js');
const { EventBus } = await import('bodhi-realtime-agent');

after(() => rmSync(TMP, { recursive: true, force: true }));

// A fake clock: Date.now and setTimeout move only when the test says so.
beforeEach(() => mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 }));
afterEach(() => mock.timers.reset());
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
/** Moves the clock to `t` ms, letting promises run on the way. */
async function at(t: number): Promise<void> {
	await settle();
	const d = t - Date.now();
	if (d > 0) mock.timers.tick(d);
	await settle();
}
const clock = () => Date.now();
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const publish = (bus: InstanceType<typeof EventBus>, event: string) => bus.publish(event as any, { sessionId: 's', atMs: Date.now(), source: 'client-vad' } as any);

describe('a speech start bodhi drops as a blip does not hold the user\'s turn open', () => {
	it('a work call waiting on the turn is released at the last real end + the quiet time, not at the 30 s cap', async () => {
		// 17:42:44.695 "User voice input started", 45.249 ignored (speechDuration=43ms): bodhi published
		// speech.user_aborted and no end. The work call at 45.408 was held until 17:43:15.4, the 30 s cap.
		const bus = new EventBus();
		const speech = createSpeechGate({ now: clock });
		wireUserSpeech(bus, speech);
		publish(bus, 'speech.user_started');
		await at(1_000);
		publish(bus, 'speech.user_ended'); // the request
		await at(1_200);
		publish(bus, 'speech.user_started'); // a blip
		await at(1_210);
		let released = 0;
		void speech.waitTurnOver().then(() => { released = Date.now(); });
		await at(1_243);
		publish(bus, 'speech.user_aborted'); // bodhi ignored it: no end follows
		await at(1_000 + QUIET_BEFORE_SUBMIT_MS - 1);
		assert.equal(released, 0, 'still inside the quiet time after the real end');
		await at(1_000 + QUIET_BEFORE_SUBMIT_MS);
		assert.equal(released, 1_000 + QUIET_BEFORE_SUBMIT_MS, `released at ${released}, the cap is ${1_210 + MAX_SUBMIT_HOLD_MS}`);
	});

	it('a result waiting for the user is handed over on the user-reply wait after the real end, not after the 60 s speaking cap', async () => {
		// 17:46:25: the pacer held a result past its hand-over while a dropped start stood open.
		const bus = new EventBus();
		const handed: number[] = [];
		const pacer = createHandoffPacer({ markSpoken: () => {}, now: clock });
		wireUserSpeech(bus, pacer);
		publish(bus, 'speech.user_started');
		await at(1_000);
		publish(bus, 'speech.user_ended');
		await at(3_000);
		publish(bus, 'speech.user_started'); // a blip
		await at(3_010);
		pacer.submit({ ids: ['task-1'], label: 'task-1', hand: () => { handed.push(Date.now()); return true; }, fallback: () => {} });
		await at(3_043);
		publish(bus, 'speech.user_aborted');
		await at(1_000 + USER_REPLY_WAIT_MS - 1);
		assert.deepEqual(handed, [], 'the model still has time to answer the user');
		await at(1_000 + USER_REPLY_WAIT_MS);
		assert.deepEqual(handed, [1_000 + USER_REPLY_WAIT_MS], `the speaking cap would hand it at ${3_000 + HANDOFF_CONFIRM_MS}`);
	});
});

describe('the turn check does not wait for an answer the model already gave', () => {
	it('a turn the model answered before the user barged in is checked at once, on its own words', async () => {
		// 17:38:39.6 the model began "Yes, that's correct…" to "Did you see it 923?"; 17:38:45.38 the user cut in with a
		// new request and kept talking; 17:38:45.6 the turn completed and reached the turn check, which waited for an
		// answer from then on and so for the user's quiet, until the 30 s cap (classified 17:39:15.6).
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ now: clock });
		const checked: Array<{ utterance: string; at: number }> = [];
		const relay = new RelayAgent({
			store,
			speech,
			classify: async (input) => { checked.push({ utterance: input.utterance, at: Date.now() }); return { action: 'none' }; },
			submit: async () => ({ status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }),
		});
		await at(30_000);
		speech.onUserSpeechStarted();
		await at(35_500);
		speech.onUserSpeechEnded();
		await at(36_360);
		relay.onUserTurn('No, that is the one with the diagram, right?', 36_360);
		await at(37_000);
		assert.equal(checked.length, 1, 'the earlier turn is checked once the user is quiet');
		await at(39_600);
		speech.onModelAudio(); // "Yes, that's correct…"
		await at(45_380);
		speech.onUserSpeechStarted(); // the user cuts in
		await at(45_600);
		relay.onUserTurn('Did you see it 923?', 45_600);
		// The user goes on with the next request, pausing under the quiet time.
		for (let t = 49_000; t < 76_000; t += 4_000) {
			await at(t);
			speech.onUserSpeechEnded();
			await at(t + 500);
			speech.onUserSpeechStarted();
		}
		await at(80_000);
		assert.equal(checked.length, 2);
		assert.ok(checked[1].at <= 45_600 + QUIET_BEFORE_SUBMIT_MS, `checked at ${checked[1].at}, the turn completed at 45600`);
		assert.equal(checked[1].utterance, 'Did you see it 923?', 'only that turn');
	});

	it('a turn the model has not answered yet still waits for the user to finish', async () => {
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ now: clock });
		const checked: Array<{ utterance: string; at: number }> = [];
		const relay = new RelayAgent({
			store,
			speech,
			classify: async (input) => { checked.push({ utterance: input.utterance, at: Date.now() }); return { action: 'none' }; },
			submit: async () => ({ status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }),
		});
		speech.onModelAudio(); // the greeting, before the first turn
		await at(1_000);
		relay.onUserTurn('Hello', 1_000);
		await at(2_000);
		assert.equal(checked.length, 1);
		await at(5_000);
		speech.onUserSpeechStarted();
		await at(6_000);
		relay.onUserTurn('Check the pull requests made by', 6_000); // cut off; the user goes on
		await at(7_000);
		speech.onUserSpeechEnded();
		await at(7_000 + QUIET_BEFORE_SUBMIT_MS - 1);
		assert.equal(checked.length, 1, 'no answer since the last turn: the user may not be finished');
		await at(7_000 + QUIET_BEFORE_SUBMIT_MS);
		assert.equal(checked.length, 2);
		assert.equal(checked[1].utterance, 'Check the pull requests made by');
	});

	it('a reply the model began while the user was still speaking does not end her turn', async () => {
		// The usual shape: the model speaks to a cut-off turn before it completes, so its audio is always in the turn.
		// Only audio the user then cut in on (17:38:45) answers it; audio over a sentence she is still saying does not.
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ now: clock });
		const checked: Array<{ utterance: string; at: number }> = [];
		const relay = new RelayAgent({
			store,
			speech,
			classify: async (input) => { checked.push({ utterance: input.utterance, at: Date.now() }); return { action: 'none' }; },
			submit: async () => ({ status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }),
		});
		speech.onUserSpeechStarted();
		await at(1_000);
		speech.onUserSpeechEnded();
		relay.onUserTurn('Hello', 1_000);
		await at(3_000);
		assert.equal(checked.length, 1);
		await at(5_000);
		speech.onUserSpeechStarted();
		await at(6_500);
		speech.onModelAudio(); // the model answers the half sentence
		await at(6_800);
		relay.onUserTurn('Check the pull requests made by', 6_800); // she goes on speaking
		await at(9_000);
		speech.onUserSpeechEnded();
		await at(9_000 + QUIET_BEFORE_SUBMIT_MS - 1);
		assert.equal(checked.length, 1, `checked at ${checked[1]?.at}: she had not finished`);
		await at(9_000 + QUIET_BEFORE_SUBMIT_MS);
		assert.equal(checked.length, 2);
	});
});

describe('a question from the turn check waits for the user to be quiet', () => {
	// Checked at once on a turn she cut in on (17:38:45), but said to her only once she stops: said as live input at
	// turn end it talks over her, and what she says next may answer it.
	function barged(verdicts: Array<Record<string, unknown>>) {
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ now: clock });
		const asked: Array<{ text: string; at: number }> = [];
		const checked: number[] = [];
		const relay = new RelayAgent({
			store,
			speech,
			classify: (async () => { checked.push(Date.now()); return verdicts.shift() ?? { action: 'none' }; }) as never,
			ask: (text: string) => { asked.push({ text, at: Date.now() }); },
			submit: async () => ({ status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }),
		} as never);
		return { relay, speech, asked, checked };
	}
	async function cutIn(s: ReturnType<typeof barged>) {
		s.speech.onUserSpeechStarted();
		await at(1_000);
		s.speech.onUserSpeechEnded();
		s.relay.onUserTurn('Hello', 1_000);
		await at(3_000);
		await at(4_000);
		s.speech.onModelAudio(); // the answer to Hello
		await at(6_000);
		s.speech.onUserSpeechStarted(); // she cuts in
		await at(6_200);
		s.relay.onUserTurn('Which one did you mean', 6_200);
		await at(6_300);
		assert.equal(s.checked.length, 2, 'checked at once');
	}

	it('an ask is said at her end plus the quiet time, not while she speaks', async () => {
		const s = barged([{ action: 'none' }, { action: 'ask', question: 'Which PR?' }]);
		await cutIn(s);
		await at(9_000);
		s.speech.onUserSpeechEnded();
		await at(9_000 + QUIET_BEFORE_SUBMIT_MS - 1);
		assert.deepEqual(s.asked, [], 'she is still speaking or just stopped');
		await at(9_000 + QUIET_BEFORE_SUBMIT_MS);
		assert.equal(s.asked.length, 1);
		assert.match(s.asked[0].text, /Which PR\?/);
	});

	it('an ask is dropped when she went on to a new turn: that turn is judged with her full words', async () => {
		const s = barged([{ action: 'none' }, { action: 'unbacked', claim: 'I have opened it' }, { action: 'none' }]);
		await cutIn(s);
		await at(8_000);
		s.relay.onUserTurn('the second one', 8_000);
		await at(8_500);
		s.speech.onUserSpeechEnded();
		await at(20_000);
		assert.deepEqual(s.asked, [], 'nothing from the half sentence');
		assert.equal(s.checked.length, 3, 'the new turn was checked');
	});
});
