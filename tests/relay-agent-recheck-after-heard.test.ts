// A re-check the user asks for right after a result was heard is a new request, not an orphan call. nbe2e 2026-10-21
// (ws-115544): turn_10 told "you merged three yesterday" (heard at turn end, ~18:56:53 now that the heard check judges it);
// the user said "I think I merged more than that" and the model called work at 18:56:55.4. Her line is logged only at
// that turn's end (18:56:58.8), so with a work check that answers in time the call read as "asked again after its result
// was said, no user turn since" and was closed silently: she heard "Let me double-check…" and never the answer.
// The user's own speech end (the client's microphone) counts as her turn.
// Run: npx tsx --test --test-force-exit tests/relay-agent-recheck-after-heard.test.ts
import { describe, it, mock } from 'node:test';
import assert from 'node:assert/strict';
import { RelayAgent, createVoiceTaskStore, createSpeechGate } from '../src/relay-agent.js';

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

function setup() {
	const store = createVoiceTaskStore(() => Date.now());
	const speech = createSpeechGate({ now: () => Date.now() });
	const submitted: string[] = [];
	const relay = new RelayAgent({
		store, speech,
		check: async ({ openTasks, answeredTasks }) => {
			const same = [...openTasks ?? [], ...answeredTasks ?? []].find((t) => t.id === 'task-1');
			return same ? { action: 'duplicate', of: 'task-1' } : { action: 'submit', lane: 'quick' };
		},
		submit: async (args) => { const id = `task-${submitted.push(String(args.task))}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
	});
	return { relay, store, speech, submitted };
}

describe('a work call after a heard result', () => {
	it('the user spoke after it was heard: sent as a new request, though her line is not logged yet', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
		try {
			const { relay, store, speech, submitted } = setup();
			void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
			await settle();
			assert.ok(relay.offerResult({ taskId: 'task-1', text: 'You merged 3 pull requests yesterday.' }));
			mock.timers.tick(5_000);
			store.set('task-1', 'spoken'); // turn_10's verdict
			mock.timers.tick(1_000);
			speech.onUserSpeechStarted(); // "I think I merged more than that."
			mock.timers.tick(1_500);
			speech.onUserSpeechEnded();
			const recheck = relay.invoke('work', { task: 'Check again how many pull requests I merged yesterday' });
			speech.onModelAudio(); // "Let me double-check your merge history…"
			await settle();
			assert.equal(submitted.length, 2, 'the re-check reaches the core');
			assert.ok(relay.isWaiting('task-2'));
			void recheck;
		} finally {
			mock.timers.reset();
		}
	});

	it('no user speech since it was heard: the model re-issued its own call, closed quietly as before', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
		try {
			const { relay, store, speech, submitted } = setup();
			speech.onUserSpeechStarted();
			mock.timers.tick(1_000);
			speech.onUserSpeechEnded(); // the original request
			mock.timers.tick(2_000);
			void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
			await settle();
			assert.ok(relay.offerResult({ taskId: 'task-1', text: 'You merged 3 pull requests yesterday.' }));
			mock.timers.tick(5_000);
			store.set('task-1', 'spoken');
			mock.timers.tick(1_000);
			const again = relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
			speech.onModelAudio();
			await settle();
			assert.match(await again, /Stay silent/);
			assert.equal(submitted.length, 1);
		} finally {
			mock.timers.reset();
		}
	});
});
