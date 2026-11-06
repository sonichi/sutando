// The relay agent end to end, in its own process with one live runtime (the result watcher is
// process-wide, so a second runtime would claim results meant for this one).
// Run: npx tsx --test --test-force-exit tests/relay-agent-e2e.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-e2e-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels } = await import('../src/live-agent-runtime.js');
const { RelayAgent, NOT_PICKED_MS } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener, voiceTaskRows, voiceTaskState } = await import('../src/task-bridge.js');
const { voiceTaskCounts } = await import('../src/inline-tools.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => {
	for (let w = 0; w < ms / 100 && !done(); w++) await tick(100);
};

const sent: string[] = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string, e: unknown = {}) => { for (const h of handlers[event] ?? []) h(e); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	tryPublishSystemNotification(text: string) {
		if (!this.sessionManager.isActive) return false;
		sent.push(text);
		return true;
	},
	transport: { sendContent: () => {} },
};
let submitted: Record<string, unknown> = {};
const relay = new RelayAgent({ submit: async () => submitted, store: voiceTaskStore });
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200 });

let seq = 0;
/** A voice task the core is working on: its task file, and its row in the table. */
function voiceTask(text: string): string {
	const id = `task-19000${String(++seq).padStart(8, '0')}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
	return id;
}
const heard = (id: string) => ['spoken', 'injected'].includes(voiceTaskStore.get(id)?.delivery ?? '');

describe('relay agent end to end', () => {
	it('the result returns through its waiting work call and is not injected', async () => {
		const id = voiceTask('draw a cat');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'draw a cat' });
		await tick(0);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Here is the cat.');
		const out = await Promise.race([call, tick(8_000).then(() => 'timed out')]);
		assert.match(out, /"draw a cat"[\s\S]*Here is the cat\./);
		await tick(1_000);
		assert.equal(sent.length, before, 'returned through the call, never injected');
		assert.ok(heard(id));
	});

	it('three requests the core answered in one result: all three named to the model, every row heard', async () => {
		const ids = ['5308', '5309', '5310'].map((pr) => voiceTask(`check PR ${pr}`));
		writeFileSync(join(TMP, 'results', `${ids[1]}.txt`), `[deduped: ${ids[0]}]`);
		writeFileSync(join(TMP, 'results', `${ids[2]}.txt`), `[deduped: ${ids[0]}]`);
		await until(() => ids.slice(1).every((id) => voiceTaskStore.get(id)?.answeredBy === ids[0]));
		writeFileSync(join(TMP, 'results', `${ids[0]}.txt`), 'All three: 5308 merged; 5309 ready; 5310 blocked.');
		const named = () => ['5308', '5309', '5310'].filter((pr) => sent.join('\n').includes(`"check PR ${pr}"`));
		await until(() => named().length === 3 && ids.every(heard), 15_000);
		assert.deepEqual(named(), ['5308', '5309', '5310'], 'every request named to the model');
		assert.ok(sent.every((t) => !t.includes('[deduped:')), 'a dedup marker is never spoken');
		assert.deepEqual(ids.map((id) => voiceTaskStore.get(id)?.answeredBy), [undefined, ids[0], ids[0]]);
		assert.ok(ids.every(heard));
	});

	it('a session that cannot take a result leaves one DM copy; a second fallback writes none', async () => {
		session.sessionManager.isActive = false;
		const id = voiceTask('check PR 9999');
		const copies = () => readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length;
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 9999 status.');
		await until(() => copies() === 1);
		assert.equal(copies(), 1);
		await until(() => voiceTaskStore.get(id)?.delivery === 'dm');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'dm');
		// The copy read back while the session is still down is not copied again.
		await tick(3_000);
		assert.ok(readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length <= 1);
		session.sessionManager.isActive = true;
		await until(() => sent.some((t) => t.includes('PR 9999 status.')), 15_000);
		assert.ok(sent.some((t) => t.includes('PR 9999 status.')), 'spoken once the session can');
		assert.ok(existsSync(join(TMP, 'results')));
	});
	it('no client connected: the result goes to the DM and its waiting call ends instead of hanging', async () => {
		session.clientConnected = false;
		const id = voiceTask('check PR 7777');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 7777' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 7777 status.');
		const out = await Promise.race([call, tick(8_000).then(() => 'timed out')]);
		assert.match(out, /delivered to the user separately/);
		assert.doesNotMatch(out, /PR 7777 status/, 'not handed to a session nobody hears');
		assert.equal(relay.isWaiting(id), false);
		await until(() => voiceTaskStore.get(id)?.delivery === 'dm');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'dm');
		assert.equal(readdirSync(join(TMP, 'results')).filter((f) => f.startsWith(`proactive-result-${id}-`)).length, 1);
		session.clientConnected = true;
	});
	it('the user is talking when a result lands: it goes back through its open call at once (Gemini picks the moment)', async () => {
		// WHEN_IDLE lets Gemini schedule the call's own response: no hold until the user's turn is over.
		const id = voiceTask('check PR 5167');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 5167' });
		await tick(0);
		session.emit('speech.user_started');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 5167 is open.');
		const out = await Promise.race([call, tick(5_000).then(() => 'timed out')]);
		assert.match(out, /PR 5167 is open/, 'returned while the user is still speaking');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'handed over, not yet heard');
		session.emit('speech.user_ended');
		session.emit('turn.start'); session.emit('response.first_audio'); session.emit('turn.end', { turnId: 't5167' } as never);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken', 'heard once the model has spoken after the hand-off');
	});

	it('a result handed to its call but lost to a fresh dial before it was spoken is handed over again', async () => {
		const id = voiceTask('check PR 6100');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 6100' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 6100 is merged.');
		assert.match(await Promise.race([call, tick(5_000).then(() => 'timed out')]), /PR 6100 is merged/);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
		session.emit('session.reconnectBoundary', {} as never);
		assert.equal(voiceTaskStore.get(id)?.delivery, undefined, 'not heard: reconcile hands it over again');
	});

	it('a resume (RECONNECTING then ACTIVE, no boundary) keeps the open call, and the result returns through it', async () => {
		const id = voiceTask('check PR 6200');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 6200' });
		await tick(0);
		session.emit('session.stateChange', { toState: 'RECONNECTING' } as never);
		session.emit('session.stateChange', { toState: 'ACTIVE' } as never);
		assert.equal(relay.isWaiting(id), true, 'a resumed connection still holds the call');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 6200 is merged.');
		assert.match(await Promise.race([call, tick(5_000).then(() => 'timed out')]), /PR 6200 is merged/, 'returned through the call');
	});

	it('a task the core ends with a skip-marked result is not pending for the user', async () => {
		// 2026-10-12 00:33 UTC: two tasks cleared with [no-send] results earlier stayed "done, not heard" in the
		// table; "how many pending" counted them and the user heard 6 for 4.
		const id = voiceTask('cleared later');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		void relay.invoke('Execute tool: work', { task: 'cleared later' });
		await tick(0);
		const mine = () => voiceTaskRows().filter((r) => r.id === id);
		assert.equal(voiceTaskCounts(mine()).pending, 1, 'queued: pending');
		writeFileSync(join(TMP, 'results', `${id}.txt`), '[no-send]');
		await until(() => voiceTaskStore.get(id)?.retired === true);
		assert.equal(voiceTaskStore.get(id)?.retired, true);
		assert.equal(mine()[0]?.retired, true);
		assert.equal(voiceTaskCounts(mine()).pending, 0, 'nothing to hear: not pending');
	});

	it('two results landing in the same moment: both go back through their calls at once, each heard after the model speaks', async () => {
		const [a, b] = ['5275', '5251'].map((pr) => voiceTask(`check PR ${pr}`));
		const calls = [a, b].map((id) => { submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; return relay.invoke('Execute tool: work', {}); });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${a}.txt`), 'PR 5275 is blocked on changes requested.');
		writeFileSync(join(TMP, 'results', `${b}.txt`), 'PR 5251 is waiting for review.');
		const [first, second] = await Promise.all(calls.map((c) => Promise.race([c, tick(8_000).then(() => 'timed out')])));
		assert.match(first, /PR 5275/);
		assert.match(second, /PR 5251/, 'not held behind the first: Gemini schedules each with WHEN_IDLE');
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't1' } as never);
		assert.equal(voiceTaskStore.get(a)?.delivery, 'spoken', 'recorded heard after the model spoke');
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken');
	});
	it('results waiting their turn are not replayed by reconcile (no log line, no replay counted)', async () => {
		const [a, b] = ['7001', '7002'].map((pr) => voiceTask(`check PR ${pr}`));
		const calls = [a, b].map((id) => { submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; return relay.invoke('Execute tool: work', {}); });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${a}.txt`), 'PR 7001 status.');
		writeFileSync(join(TMP, 'results', `${b}.txt`), 'PR 7002 status.');
		await calls[0];
		assert.match(await calls[1], /PR 7002/, 'returned at once, not queued behind the first');
		await tick(700); // two reconcile passes before the model speaks
		assert.equal(voiceTaskStore.get(b)?.replays ?? 0, 0, 'a result handed to its call is not a lost result');
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't9' } as never);
	});
	it('a corrected request: the old task\'s result is not said, its call ends, the new one is', async () => {
		const old = voiceTask('Check the status and details of PR #51167.');
		submitted = { status: 'pending', taskId: old, queuedAhead: 0, watcherOnline: true };
		const oldCall = relay.invoke('Execute tool: work', { task: 'PR 51167' });
		await tick(0);
		const fresh = voiceTask('Check the status and details of PR #5167.');
		submitted = { status: 'pending', taskId: fresh, queuedAhead: 0, watcherOnline: true };
		const freshCall = relay.invoke('Execute tool: work', { task: 'PR 5167', replaces: '51167' });
		assert.match(await oldCall, /delivered to the user separately/, 'the old call ends at the correction');
		assert.equal(voiceTaskStore.get(old)?.supersededBy, fresh);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `${old}.txt`), 'PR 51167 does not exist.');
		writeFileSync(join(TMP, 'results', `${fresh}.txt`), 'PR 5167 is blocked on review.');
		assert.match(await freshCall, /PR 5167 is blocked/);
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't20' } as never);
		await tick(1_200); // reconcile passes
		assert.ok(!sent.slice(before).some((t) => t.includes('51167')), 'the corrected request\'s result is never said');
	});

	it('a summary that names an open task\'s PR does not stand in for its result: the task\'s own answer is still said', async () => {
		// Review of #5348, B3: "[proactive] CI failed on PR 5302" made the task "check PR 5302" count as heard, and
		// its real answer was then dropped, not said and not sent to the DM.
		const a = voiceTask('check the review status of PR 5302');
		submitted = { status: 'pending', taskId: a, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', {});
		await tick(0);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `proactive-summary-${Date.now()}.txt`), 'CI failed on PR 5302 (lint).');
		await until(() => sent.slice(before).some((t) => t.includes('CI failed on PR 5302')));
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't21' } as never);
		assert.notEqual(voiceTaskStore.get(a)?.delivery, 'spoken', 'the task is not heard because a message mentioned its PR');
		writeFileSync(join(TMP, 'results', `${a}.txt`), 'PR 5302 waits for one approval.');
		assert.match(await call, /waits for one approval/, 'its own answer comes back through its call');
	});

	it('the core marks two requests deduped into a third before answering it: said once, all three named, nothing handed over again', async () => {
		const [a, b, c] = ['5302', '5229', '5167'].map((pr) => voiceTask(`check PR ${pr}`));
		const calls = [a, b, c].map((id) => { submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; return relay.invoke('Execute tool: work', {}); });
		await tick(0);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `${b}.txt`), `[deduped: ${a}]`);
		writeFileSync(join(TMP, 'results', `${c}.txt`), `[deduped: ${a}]`);
		await until(() => [b, c].every((id) => voiceTaskStore.get(id)?.answeredBy === a));
		writeFileSync(join(TMP, 'results', `${a}.txt`), 'Three PRs: 5302 waits for review; 5229 needs a rebase; 5167 waits for you.');
		const said = await calls[0];
		for (const pr of ['5302', '5229', '5167']) assert.ok(said.includes(`"check PR ${pr}"`), `request ${pr} named to the model`);
		for (const call of calls.slice(1)) assert.match(await call, /answered_together/);
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't22' } as never);
		for (const id of [a, b, c]) assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(1_200); // reconcile passes
		assert.equal(sent.length, before, 'nothing handed over again');
	});

	it('the deduped markers land after the combined result was said: the two inherit it and are not handed over again', async () => {
		const [a, b] = ['6302', '6229'].map((pr) => voiceTask(`check PR ${pr}`));
		const calls = [a, b].map((id) => { submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; return relay.invoke('Execute tool: work', {}); });
		await tick(0);
		const before = sent.length;
		writeFileSync(join(TMP, 'results', `${a}.txt`), 'Both PRs: 6302 waits; 6229 needs a rebase.');
		assert.match(await calls[0], /Both PRs/);
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't23' } as never);
		writeFileSync(join(TMP, 'results', `${b}.txt`), `[deduped: ${a}]`);
		assert.match(await calls[1], /delivered to the user separately/, 'its call ends with nothing more to say');
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken');
		await tick(1_200);
		assert.equal(sent.length, before, 'nothing handed over again');
	});

	it('a "not picked up" notice still waiting its turn is dropped once the task has been answered', async () => {
		const id = `task-19000${String(++seq).padStart(8, '0')}`;
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: is the fish good\n`);
		voiceTaskStore.add(id, 'is the fish good', Date.now() - NOT_PICKED_MS - 60_000);
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', {});
		await tick(0);
		const before = sent.length;
		// The user has just spoken and the model has not answered yet: nothing queued is handed over until it does
		// (the reply wait, 8 s by default). One explicit turn, no stream of events a loaded runner can delay (review B1).
		session.emit('speech.user_started');
		session.emit('speech.user_ended');
		await until(() => !!voiceTaskStore.get(id)?.notPickedNoticed, 3_000);
		assert.ok(voiceTaskStore.get(id)?.notPickedNoticed, 'the notice is queued');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The fish is a safe choice.');
		await until(() => voiceTaskState(id) !== 'queued', 4_000);
		// The model answers the user: the queue moves. The notice is no longer true and is dropped; the result goes.
		session.emit('turn.start');
		session.emit('response.first_audio');
		session.emit('turn.end', { turnId: 't-fish' } as never);
		assert.match(await call, /safe choice/, 'the answer goes back through its call');
		await tick(400);
		assert.ok(!sent.slice(before).some((t) => t.includes('has not been picked up')), 'the stale notice is not said');
	});
});
