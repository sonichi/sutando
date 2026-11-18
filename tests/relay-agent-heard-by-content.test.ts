// A result returned through its open call counts as heard only when a spoken turn carries its content: an
// unrelated sentence after the return ("Yes, I can hear you") is not it. Own process (the result watcher is process-wide).
// Run: npx tsx --test --test-force-exit tests/relay-agent-heard-by-content.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-heard-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels, resultEvidence, turnSaysResult, spokenNumberForms } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener, voiceTaskResultText } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => { for (let w = 0; w < ms / 100 && !done(); w++) await tick(100); };

const notices: string[] = [];
const items: Array<{ role: string; content: string }> = [];
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
const relay = new RelayAgent({ submit: async () => submitted, store: voiceTaskStore });
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, returnNudgeMs: 400, returnReleaseMs: 1_500 });

let seq = 0;
function voiceTask(text: string): string {
	const id = `task-19200${String(++seq).padStart(8, '0')}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
	return id;
}
/** The model speaks one turn with these words. */
const modelSays = (text: string, turnId: string) => {
	session.emit('turn.start');
	session.emit('response.first_audio');
	items.push({ role: 'assistant', content: text });
	session.emit('turn.end', { turnId });
};

describe('a returned result is heard only when the model says it', () => {
	it('a spoken turn about something else leaves it unheard; a nudge follows; the turn that says it marks it heard', async () => {
		const id = voiceTask('count the PRs merged in the last 3 days');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'count the PRs merged in the last 3 days' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Sixty-one pull requests merged in sonichi/sutando in the last three days.');
		assert.match(await Promise.race([call, tick(5_000).then(() => 'timed out')]), /Sixty-one/);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'returned, not yet heard');
		modelSays('Yes, I can hear you.', 't1');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'an unrelated sentence is not the result');
		modelSays('There are six pending tasks right now.', 't2');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'a status answer is not the result either');
		await until(() => notices.some((n) => /came back through its work call/.test(n)), 3_000);
		const nudge = notices.find((n) => /came back through its work call/.test(n)) ?? '';
		assert.match(nudge, /count the PRs merged in the last 3 days/, 'the nudge names the request');
		assert.equal(voiceTaskResultText(id), 'Sixty-one pull requests merged in sonichi/sutando in the last three days.', 'the status tool can read the result to say it');
		modelSays('Sixty-one pull requests were merged in the Sutando repository in the past three days.', 't3');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('a turn that carries a number from the result counts at once, with no nudge', async () => {
		const before = notices.length;
		const id = voiceTask('check PR 5617');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 5617' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 5617 is merged; it went in on Saturday.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Pull request 5617 was merged on Saturday.', 't4');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(600);
		assert.equal(notices.length, before, 'no nudge for a result that was said');
	});

	it('still unsaid after the release time: handed over again by the reconcile loop', async () => {
		const id = voiceTask('how many tasks are open in the tracker');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'how many tasks are open in the tracker' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The tracker lists 14 open items, 3 of them blocked.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Sure, what else can I do for you?', 't5');
		await until(() => voiceTaskStore.get(id)?.delivery === undefined || notices.filter((n) => n.includes('14 open items')).length > 0, 6_000);
		await until(() => notices.some((n) => n.includes('14 open items')), 6_000);
		assert.ok(notices.some((n) => n.includes('14 open items')), 'the result itself is handed over again as a notice');
	});
});

describe('resultEvidence / turnSaysResult', () => {
	it('numbers or two distinctive words; file markers and filler words are not evidence', () => {
		const e = resultEvidence('Here\'s your bird.\n[file: /x/y/generated-1792373489180.jpg]');
		assert.deepEqual(e.numbers, [], 'the file name\'s digits are not the result');
		assert.deepEqual(e.words, ['bird'], "'here's' and 'your' are filler");
		assert.ok(turnSaysResult('Your bird drawing is ready.', e), 'one distinctive word, and it was said');
		assert.equal(turnSaysResult('Yes, I can hear you.', e), false);
		const prs = resultEvidence('Sixty-one pull requests merged in sonichi/sutando in the last three days.');
		assert.equal(turnSaysResult('Yes, I can hear you.', prs), false);
		assert.equal(turnSaysResult('There are six pending tasks right now.', prs), false);
		assert.equal(turnSaysResult('Sixty-one pull requests were merged this week.', prs), true);
		const n = resultEvidence('PR 5617 is merged.');
		assert.equal(turnSaysResult('5617 went in on Saturday', n), true);
		assert.equal(turnSaysResult('It was merged.', n), false);
	});

	// The live transcript never writes digits: "98" is spoken as "ninetyeight" or "九十八", and a matcher that looks
	// for the digits nudges and re-hands a result the user has already heard.
	it('a number the transcript spells out counts: English, hyphen-free, pairwise for four digits, Chinese', () => {
		const prs = resultEvidence('98 PRs merged in sonichi/sutando in the last seven days — 2026-10-12 through today. Newest is #5717.');
		assert.equal(turnSaysResult('To follow up on the PRs, there were ninetyeight merged in the repository last week.', prs), true);
		assert.equal(turnSaysResult('关于上周合并的PR数量， 一共有九十八个合并到 Sutando 仓库里。', prs), true);
		assert.equal(turnSaysResult('Ninety-eight pull requests.', prs), true);
		assert.equal(turnSaysResult('The newest one is fifty-seven seventeen.', prs), true);
		assert.equal(turnSaysResult('Your disk is sixty percent free, with thirtyone gigabytes to clear.', prs), false, 'other numbers are not this result');
		assert.equal(turnSaysResult('Yes, I can hear you.', prs), false);
		const pct = resultEvidence('Disk is 60% free; 31 GB of caches can go.');
		assert.equal(turnSaysResult('Your disk is sixty percent free, and about thirtyone gigabytes of caches can be cleared.', pct), true);
		const dec = resultEvidence('It weighs 3.5 kg.');
		assert.equal(turnSaysResult('It weighs three point five kilograms.', dec), true);
		assert.equal(turnSaysResult('它重三点五公斤。', dec), true);
		for (const [n, f] of [['98', 'ninety eight'], ['98', '九十八'], ['5717', 'fifty seven seventeen'], ['2026', 'two thousand twenty six'], ['1105', '一千一百零五'], ['120', 'one hundred and twenty'], ['100', 'a hundred'], ['2500', 'twenty five hundred'], ['205', 'two oh five'], ['250', '两百五十'], ['3.50', 'three fifty']]) {
			assert.ok(spokenNumberForms(n).includes(f), `${n} → ${f}: ${spokenNumberForms(n).join(' | ')}`);
		}
		assert.ok(!spokenNumberForms('2005').includes('twenty five'), '2005 is never read as twenty-five');
	});

	// A spelled number must be whole words of the turn: "one" inside "done", "ten" inside "listening" or "attention",
	// and a date's day are not the result. Marked heard by one of these, a result was never handed over again.
	it('a number word inside another word, a small number, a date part, or the request\'s own number never count', () => {
		const pr = resultEvidence('PR 5691 has 2 approvals and 1 failing check.', 'check PR 5691');
		assert.deepEqual(pr.numbers, [], 'the request\'s number and the small counts are not evidence');
		for (const said of ["Don't worry, I'm on it.", 'Done!', 'Sure, I\'m listening.', 'Still checking 5691']) assert.equal(turnSaysResult(said, pr), false, said);
		assert.equal(turnSaysResult('Two approvals, one failing check.', pr), true, 'the distinctive words still count');
		const at10 = resultEvidence('The sync is set for 2026-10-20 at 10 tomorrow.');
		assert.deepEqual(at10.numbers, [], 'a date and a number under 13 are not evidence');
		assert.equal(turnSaysResult('People often ask me that.', at10), false);
		const prs = resultEvidence('98 PRs merged in sonichi/sutando in the last seven days.');
		assert.equal(turnSaysResult('Sure, I\'m listening.', prs), false);
		assert.equal(turnSaysResult('Ninety-eight were merged.', prs), true);
		assert.equal(turnSaysResult('Ninetyeight.', prs), true, 'a join inside the number is fine');
		assert.equal(turnSaysResult('The eightyninety eight.', prs), false, 'a join with the neighbouring word is not');
		const n100 = resultEvidence('There are 100 rows.');
		assert.equal(turnSaysResult('一共一百行。', n100), true);
		assert.equal(turnSaysResult('一共一百五十行。', n100), false, 'a longer numeral is a different number');
		for (const [n, said] of [['15', '合并了十五个。'], ['60', '六十个合并了。'], ['250', '两百五十个。'], ['250', '二百五十个。']]) assert.equal(turnSaysResult(said, resultEvidence(`${n} PRs merged.`)), true, said);
		assert.equal(turnSaysResult('Anything at all.', resultEvidence('OK', 'OK')), false, 'no evidence: never by content');
		const cn = resultEvidence('合并了15个PR。');
		assert.ok(cn.words.includes('合并'), 'CJK words are two-character grams');
		assert.equal(turnSaysResult('合并了十五个。', cn), true);
	});
});
