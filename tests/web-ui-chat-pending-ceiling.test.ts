// Executes the shipped chat-pending code against stub globals with a fake clock;
// localStorage persists across both evaluations, which is what makes one a reload.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// The sandbox deliberately models browser globals loosely; this alias keeps that
// intent explicit instead of scattering `any`.
type Stub = ReturnType<typeof JSON.parse>;

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(repoRoot, 'src', 'web-client.ts'), 'utf8');

// Start at the storage-key declaration, not the timing constants: the pending
// helpers swallow errors, so a missing key is a silent no-op, not a ReferenceError.
const START = 'const PERSIST_KEY_CHAT_PENDING';
const END = '// ─── Text input ─';
const from = source.indexOf(START);
const to = source.indexOf(END);
assert.ok(from > 0 && to > from, 'chat-pending region not found in web-client.ts');
const REGION = source.slice(from, to);

const MINUTE = 60 * 1000;
// A real epoch, not 0: `begin = sentAt || Date.now()` short-circuits on a falsy
// timestamp, so a t=0 fixture cannot express the resumed-ceiling bug at all.
const T0 = 1_700_000_000_000;

function makeEl() {
	const classes = new Set<string>();
	return {
		children: [] as Stub[],
		_cls: '',
		set className(v: string) { this._cls = v; classes.clear(); v.split(/\s+/).forEach(c => c && classes.add(c)); },
		get className() { return this._cls; },
		textContent: '',
		innerHTML: '',
		classList: {
			add: (c: string) => classes.add(c),
			remove: (c: string) => classes.delete(c),
			contains: (c: string) => classes.has(c),
		},
		appendChild(c: Stub) { this.children.push(c); return c; },
	};
}

/** One page session over a shared localStorage. */
function session(store: Map<string, string>, opts: { now: number; result?: Stub }) {
	const state = {
		now: opts.now,
		fetches: [] as string[],
		timers: [] as Array<() => void>,
		transcript: makeEl(),
	};
	const sandbox: Record<string, Stub> = {
		localStorage: {
			getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
			setItem: (k: string, v: string) => void store.set(k, v),
		},
		JSON,
		Date: { now: () => state.now },
		location: { hostname: '127.0.0.1' },
		document: { createElement: () => makeEl() },
		$: () => state.transcript,
		window: {},
		scrollTranscript: () => {},
		addCopyBtn: () => {},
		setTimeout: (fn: () => void) => { state.timers.push(fn); return state.timers.length; },
		clearTimeout: () => {},
		fetch: (url: string) => {
			state.fetches.push(url);
			return Promise.resolve({ json: () => Promise.resolve(opts.result ?? { status: 'pending' }) });
		},
	};
	const names = Object.keys(sandbox);
	const factory = new Function(...names, REGION +
		'\nreturn { addPendingChatSend, loadPendingChatSends, resumePendingChatSends, pollChatReply, CHAT_POLL_MAX_MS, CHAT_PENDING_TTL_MS };');
	const api = factory(...names.map(n => sandbox[n]));
	return { api, state };
}

test('TTL outlives the poll ceiling, or a kept entry can never be recovered', () => {
	const { api } = session(new Map(), { now: T0 });
	assert.ok(
		api.CHAT_PENDING_TTL_MS > api.CHAT_POLL_MAX_MS,
		`TTL (${api.CHAT_PENDING_TTL_MS}) must exceed the poll ceiling (${api.CHAT_POLL_MAX_MS}); ` +
		'equal values GC exactly the entries the ceiling branch means to preserve',
	);
});

test('a reload past the ceiling still fetches and renders the late reply', async () => {
	const store = new Map<string, string>();

	// Session A: send, then leave the page while the task is still running.
	const a = session(store, { now: T0 });
	a.api.addPendingChatSend('task-1', 'do the slow thing');
	assert.equal(a.api.loadPendingChatSends().length, 1, 'send should persist');

	// 31 minutes later — past CHAT_POLL_MAX_MS — the user reloads and the task
	// has since completed.
	const late = T0 + 31 * MINUTE;
	const b = session(store, { now: late, result: { status: 'completed', result: 'here is your answer' } });

	assert.equal(
		b.api.loadPendingChatSends().length, 1,
		'entry must survive load past the ceiling — this is the GC/ceiling collision',
	);

	b.api.resumePendingChatSends();
	await new Promise(r => setImmediate(r));

	assert.deepEqual(
		b.state.fetches, ['http://127.0.0.1:7843/result/task-1'],
		'resume must actually call /result; measuring the ceiling from the original ' +
		'send made the first tick bail before any fetch',
	);

	const placeholder = b.state.transcript.children[1];
	assert.equal(placeholder.textContent, 'here is your answer', 'late reply must render into the placeholder');
	assert.equal(placeholder.classList.contains('t-working'), false, 'placeholder must leave the working state');
	assert.equal(b.api.loadPendingChatSends().length, 0, 'a rendered reply must clear its persisted entry');
});

test('a still-pending task past the ceiling keeps its entry for the next reload', async () => {
	const store = new Map<string, string>();
	session(store, { now: T0 }).api.addPendingChatSend('task-2', 'still going');

	const b = session(store, { now: T0 + 31 * MINUTE, result: { status: 'pending' } });
	b.api.resumePendingChatSends();
	await new Promise(r => setImmediate(r));

	assert.equal(b.state.fetches.length, 1, 'must probe /result even for an old send');
	assert.equal(b.api.loadPendingChatSends().length, 1, 'an unfinished task must stay recoverable');
});
