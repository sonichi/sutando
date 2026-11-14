// The supersede rule as a pure function: which earlier request a correction replaces, decided from a
// table's rows alone. RelayAgent applies its plan (tests/relay-agent.test.ts and the e2e suite pin that path).
// Run: npx tsx --test --test-force-exit tests/relay-agent-supersede.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { RelayAgent, SUPERSEDE_WINDOW_MS, createVoiceTaskStore, planSupersede, type SupersedeInput } from '../src/relay-agent.js';

// Far from the wall clock: a rule that read Date.now() would see every row as older than the window.
const NOW = 1_000_000_000_000;
const ago = (ms: number) => NOW - ms;
type Rows = SupersedeInput['rows'];
const plan = (replaces: string, rows: Rows, extra: Partial<SupersedeInput> = {}) =>
	planSupersede({ taskId: 'task-9000', replaces, rows, now: NOW, ...extra });
const picks = (id: string) => ({ action: 'supersede', id });
const nothing = (why: 'names_nothing' | 'no_match') => ({ action: 'none', why });
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('the supersede rule, over any table of requests', () => {
	it('a task id names exactly that row, not the newest one that looks like it', () => {
		const rows: Rows = [
			['task-1001', { text: 'Check PR 7001', submittedAt: ago(3_000) }],
			['task-1002', { text: 'Check PR 7001', submittedAt: ago(2_000) }],
		];
		assert.deepEqual(plan('task-1001', rows), picks('task-1001'));
		assert.deepEqual(plan('task-1002', rows), picks('task-1002'));
	});

	it('an id is never read as marks: one the table lacks matches nothing, whatever the texts carry', () => {
		const rows: Rows = [['task-1', { text: 'Check PR 4040', submittedAt: ago(1_000) }]];
		assert.deepEqual(plan('task-4040', rows), nothing('no_match'));
	});

	it('marks: every mark must be carried, and the most recent carrier wins', () => {
		const rows: Rows = [
			['task-a', { text: 'Check PR 5302 and "Release notes"', submittedAt: ago(3_000) }],
			['task-b', { text: 'Check PR 5302', submittedAt: ago(2_000) }],
		];
		assert.deepEqual(plan('5302', rows), picks('task-b'));
		assert.deepEqual(plan('5302 "release notes"', rows), picks('task-a'), 'the newer request lacks the name');
	});

	it('a mark is the whole number or the whole quoted name, never a part of one', () => {
		const rows: Rows = [['task-a', { text: 'Check PR #51167 and "Release notes v2" and release notes', submittedAt: ago(1_000) }]];
		assert.deepEqual(plan('1167', rows), nothing('no_match'));
		assert.deepEqual(plan('5116', rows), nothing('no_match'));
		assert.deepEqual(plan('"release notes"', rows), nothing('no_match'));
		assert.deepEqual(plan('"release notes v2"', rows), picks('task-a'));
		assert.deepEqual(plan('51167', rows), picks('task-a'));
	});

	it('a g or y idPattern is not advanced: repeated calls agree and the caller\'s pattern is untouched', () => {
		const rows: Rows = [
			['req-5302-a', { text: 'Summarize the budget', submittedAt: ago(3_000) }],
			['req-6001-b', { text: 'Check PR 5302', submittedAt: ago(2_000) }],
		];
		for (const flags of ['g', 'y']) {
			const idPattern = new RegExp('^req-\\d+-[a-z]$', flags);
			for (let k = 0; k < 4; k++) assert.deepEqual(plan('req-5302-a', rows, { idPattern }), picks('req-5302-a'), `${flags} call ${k}`);
			assert.equal(idPattern.lastIndex, 0);
		}
	});

	it('what names nothing is told apart from what nothing matches', () => {
		const rows: Rows = [['task-a', { text: 'Check PR 5302 for "pr" and "42"', submittedAt: ago(1_000) }]];
		for (const replaces of ['pr', '', '42', '"a"', 'the weather']) {
			assert.deepEqual(plan(replaces, rows), nothing('names_nothing'), JSON.stringify(replaces));
		}
		assert.deepEqual(plan('9999', rows), nothing('no_match'));
	});

	it('a heard request is never superseded; one handed over but unconfirmed, or sent to the DM, still is', () => {
		const text = 'Check PR 7001';
		const rows: Rows = [
			['task-1', { text, submittedAt: ago(3_000), delivery: 'injected' }],
			['task-2', { text, submittedAt: ago(2_000), delivery: 'dm' }],
			['task-3', { text, submittedAt: ago(1_000), delivery: 'spoken' }],
		];
		assert.deepEqual(plan('7001', rows), picks('task-2'));
		assert.deepEqual(plan('7001', rows.slice(0, 1)), picks('task-1'));
		assert.deepEqual(plan('task-3', rows), nothing('no_match'));
	});

	it('the correcting request is never its own target', () => {
		const rows: Rows = [
			['task-1', { text: 'Check PR 7001', submittedAt: ago(2_000) }],
			['task-9000', { text: 'Check PR 7001 again', submittedAt: ago(1_000) }],
		];
		assert.deepEqual(plan('7001', rows), picks('task-1'));
		assert.deepEqual(plan('task-9000', rows), nothing('no_match'));
	});

	it('a request that was already corrected is skipped', () => {
		const rows: Rows = [
			['task-1001', { text: 'Check PR 7001', submittedAt: ago(2_000) }],
			['task-1002', { text: 'Check PR 7001', submittedAt: ago(1_000), supersededBy: 'task-8000' }],
		];
		assert.deepEqual(plan('7001', rows), picks('task-1001'));
		assert.deepEqual(plan('task-1002', rows), nothing('no_match'));
	});

	it('a correction reaches back one window: the edge counts, a millisecond further does not, no time never', () => {
		const one = (submittedAt?: number): Rows => [['task-1', { text: 'Check PR 7001', submittedAt }]];
		for (const replaces of ['7001', 'task-1']) {
			assert.deepEqual(plan(replaces, one(ago(SUPERSEDE_WINDOW_MS))), picks('task-1'), `${replaces} at the edge`);
			assert.deepEqual(plan(replaces, one(ago(SUPERSEDE_WINDOW_MS + 1))), nothing('no_match'), `${replaces} just outside`);
			assert.deepEqual(plan(replaces, one(undefined)), nothing('no_match'), `${replaces} with no submission time`);
		}
	});

	it('a row with no text is skipped by marks, and can still be named by id', () => {
		const rows: Rows = [
			['task-1', { text: 'Check PR 7001', submittedAt: ago(2_000) }],
			['task-2', { submittedAt: ago(1_000) }],
		];
		assert.deepEqual(plan('7001', rows), picks('task-1'));
		assert.deepEqual(plan('task-2', rows), picks('task-2'));
	});

	it('by default an id is task-<digits>: anything else is read for marks, and these have none', () => {
		const rows: Rows = [['task-1001', { text: 'Check PR 7001', submittedAt: ago(1_000) }]];
		for (const replaces of ['task-b', 'task-', 'task-12a']) {
			assert.deepEqual(plan(replaces, rows), nothing('names_nothing'), replaces);
		}
	});

	it('the id form is the table\'s own: another form is named by id when the caller says so', () => {
		const rows: Rows = [
			['req-5302-a', { text: 'Summarize the budget', submittedAt: ago(3_000) }],
			['req-6001-b', { text: 'Check PR 5302', submittedAt: ago(2_000) }],
		];
		assert.deepEqual(plan('req-5302-a', rows, { idPattern: /^req-\d+-[a-z]$/ }), picks('req-5302-a'), 'named by id');
		// Left to the default (`task-<digits>`) the same words are a number, and the other row carries it.
		assert.deepEqual(plan('req-5302-a', rows), picks('req-6001-b'));
	});

	it('it decides and changes nothing: the rows are left as they were, and the same inputs give the same plan', () => {
		const row = Object.freeze({ text: 'Check PR 7001', submittedAt: ago(1_000) });
		const rows: Rows = Object.freeze([Object.freeze(['task-1', row] as const)]);
		const before = JSON.stringify(rows);
		const first = plan('7001', rows);
		assert.deepEqual(first, picks('task-1'));
		assert.deepEqual(plan('7001', rows), first);
		assert.equal(JSON.stringify(rows), before);
	});

	it('the voice task table feeds it as it is, oldest submission first', () => {
		const store = createVoiceTaskStore(() => NOW);
		store.add('task-b', 'Check PR 7001', ago(1_000));
		store.add('task-a', 'Check PR 7001', ago(2_000));
		store.add('task-c', 'Check PR 7002', ago(500));
		assert.deepEqual(planSupersede({ taskId: 'task-9000', replaces: '7001', rows: store.list(), now: NOW }), picks('task-b'));
		assert.equal(store.get('task-b')?.supersededBy, undefined, 'the table is the caller\'s to update');
	});
});

describe('RelayAgent applies the plan', () => {
	function agent() {
		const store = createVoiceTaskStore();
		const logs: string[] = [];
		let n = 0;
		const relay = new RelayAgent({
			store,
			log: (m) => logs.push(m),
			submit: async (args) => { const id = `task-new${++n}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		return { relay, store, logs };
	}

	it('a correction that no request carries is logged, and corrects nothing', async () => {
		const { relay, store, logs } = agent();
		store.add('task-1', 'Check PR 5302');
		void relay.invoke('w', { task: 'Check PR 5320', replaces: '9999' });
		await tick(5);
		assert.equal(store.get('task-1')?.supersededBy, undefined);
		assert.ok(logs.some((l) => l.includes('task-new1: no unheard request carries "9999"; nothing corrected')), logs.join('\n'));
	});

	it('it passes its own clock and task id: an old request is out of reach and a correction never supersedes itself', async () => {
		const { relay, store, logs } = agent();
		store.add('task-old', 'Check PR 7001', Date.now() - SUPERSEDE_WINDOW_MS - 60_000);
		void relay.invoke('w', { task: 'Check PR 7001, not the old one', replaces: '7001' });
		await tick(5);
		assert.equal(store.get('task-old')?.supersededBy, undefined);
		assert.equal(store.get('task-new1')?.supersededBy, undefined);
		assert.ok(logs.some((l) => l.includes('task-new1: no unheard request carries "7001"; nothing corrected')), logs.join('\n'));
	});

	it('the request the plan picks is marked, its call ends, and the log says so', async () => {
		const { relay, store, logs } = agent();
		const old = relay.invoke('w', { task: 'Check PR 51167' });
		await tick(5);
		void relay.invoke('w', { task: 'Check PR 5167', replaces: '51167' });
		assert.match(await Promise.race([old, tick(1_000).then(() => 'still waiting')]), /delivered to the user separately/);
		assert.equal(store.get('task-new1')?.supersededBy, 'task-new2');
		assert.ok(logs.some((l) => l.includes('task-new1: corrected by task-new2; its result will not be said')), logs.join('\n'));
	});
});
