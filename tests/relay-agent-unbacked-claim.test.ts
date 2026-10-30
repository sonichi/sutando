// The voice model said it did something no tool did. 2026-10-21 (voice-agent.log): 17:39:23 "I've replaced the V2
// plan with Dot's designed workflow for V3", 17:40:00 "I've created a separate page … v3" and 17:40:14 "you might
// need to refresh", each in a turn with no tool call. The turn check now sees this turn's tool calls and the
// results delivered in it, and an unbacked claim is corrected.
// Run: npx tsx --test --test-force-exit tests/relay-agent-unbacked-claim.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-unbacked-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const relayMod = await import('../src/relay-agent.js') as Record<string, unknown> & typeof import('../src/relay-agent.js');
const { RelayAgent, createVoiceTaskStore, recentConversationLines } = relayMod;
const { parseTurnVerdict, turnClassifierPrompt } = await import('../src/work-request-check.js');
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('the unbacked verdict', () => {
	it('is parsed with its claim, and its core task when it has one', () => {
		assert.deepEqual(parseTurnVerdict('{"action":"unbacked","claim":"I\'ve created a separate page titled v3"}'),
			{ action: 'unbacked', claim: 'I\'ve created a separate page titled v3' });
		assert.deepEqual(parseTurnVerdict('{"action":"unbacked","claim":" I\'ve replaced the V2 plan ","task":" Replace the v2 page with Dot\'s plan "}'),
			{ action: 'unbacked', claim: 'I\'ve replaced the V2 plan', task: 'Replace the v2 page with Dot\'s plan' });
		assert.deepEqual(parseTurnVerdict('{"action":"unbacked","claim":"  "}'), { action: 'none' }, 'no claim, nothing to correct');
	});

	it('the prompt shows this turn\'s tools, defines unbacked, and puts the skills\' rules over the general split', () => {
		const p = turnClassifierPrompt({ utterance: 'Did you see the 9:23 one?', recentConversation: 'user: hi', thisTurn: 'write_page {"kind":"markdown"} -> ok' } as never, ['- none: a room rule.']);
		assert.match(p, /This turn, after the user spoke — the assistant's tool calls \(name, arguments, result\) and the results delivered to it:\nwrite_page \{"kind":"markdown"\} -> ok/);
		assert.match(p, /- unbacked: the assistant's reply to this turn \(its lines after the user's last line in the recent words\) says it already did something — did, created, wrote, updated, replaced, sent, deleted, opened — that no tool call this turn with "ok" did and no delivered result reports\./);
		assert.match(p, /\{"action":"unbacked","claim":"\.\.\.","task":"\.\.\."\}/);
		assert.match(p, /- A question about a message, video or picture at some time or place \("did you see the 9:23 one\?", "did you watch it\?"\), or about something the assistant has just answered, is none/);
		assert.match(p, /The rules below come from the assistant's own tools and take precedence over the split above: what they say the assistant does itself is none, even when it is about messages or notes\.\n- none: a room rule\./);
		assert.ok(p.indexOf('This turn, after the user spoke') > p.indexOf('Recent words (oldest first):'), 'the turn\'s tools are input, not instructions');
	});
});

describe('this turn\'s tool lines come from the conversation items', () => {
	const call = (toolCallId: string, toolName: string, args: Record<string, unknown>) => ({ role: 'tool_call', content: JSON.stringify({ toolCallId, toolName, args }) });
	const result = (toolCallId: string, toolName: string, r: unknown, error?: string) => ({ role: 'tool_result', content: JSON.stringify({ toolCallId, toolName, result: r, ...(error ? { error } : {}) }) });

	it('only the calls after this turn\'s user entry, each with its status, and the results delivered in it', () => {
		const thisTurnToolLines = relayMod.thisTurnToolLines as (items: unknown[], userText?: string) => string;
		assert.equal(typeof thisTurnToolLines, 'function');
		const items = [
			{ role: 'user', content: 'Add Dot\'s plan to the v2 page.' },
			call('c1', 'write_page', { kind: 'markdown-ycxdn2lw', text: 'old' }),
			result('c1', 'write_page', { ok: true, did: 'appended' }),
			{ role: 'assistant', content: 'I\'ve added it.' },
			{ role: 'user', content: 'I don\'t see us.' },
			call('c2', 'read_page', { kind: 'markdown-ycxdn2lw' }),
			result('c2', 'read_page', { ok: false, error: 'NOT_FOUND' }),
			call('c3', 'write_page', { kind: 'markdown-ycxdn2lw', text: 'x'.repeat(400) }),
			result('c3', 'write_page', null, 'timed out'),
			call('c4', 'search_commons', { query: 'v3' }),
			{ role: 'user', content: '[System: This answers the user\'s request: "build the injury app".]\n\n<TASK_RESULT_START>\nThe app is built.\n<TASK_RESULT_END>' },
			{ role: 'assistant', content: '[system] The room was switched to Susan, Chi.' },
			{ role: 'assistant', content: 'I\'ve created a separate page titled v3.' },
		];
		const lines = thisTurnToolLines(items, 'I don\'t see us.').split('\n');
		assert.deepEqual(lines.slice(0, 2), [
			'read_page {"kind":"markdown-ycxdn2lw"} -> error: NOT_FOUND',
			'write_page {"kind":"markdown-ycxdn2lw","text":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx… -> error: timed out',
		]);
		assert.equal(lines[2], 'search_commons {"query":"v3"} -> no result yet');
		assert.equal(lines[3], 'delivered: [System: This answers the user\'s request: "build the injury app".] <TASK_RESULT_START> The app is built. <TASK_RESULT_END>');
		assert.equal(lines[4], 'delivered: [system] The room was switched to Susan, Chi.');
		assert.equal(lines.length, 5, 'the earlier turn\'s write_page and the assistant\'s words are not tool lines');
		for (const l of lines) assert.ok(l.length <= 200, l);
		assert.equal(thisTurnToolLines([...items.slice(0, 4)], 'Add Dot\'s plan to the v2 page.'), 'write_page {"kind":"markdown-ycxdn2lw","text":"old"} -> ok: {"ok":true,"did":"appended"}');
		assert.equal(thisTurnToolLines([{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }], 'hi'), '');
	});

	it('a result for a call made in an earlier turn is a delivered line, not dropped', () => {
		// bodhi adds the work call's tool_call when the call starts and its tool_result when the core's answer returns,
		// often turns later: "is it merged yet?" answered by that result was judged with "(none)" and called unbacked.
		const thisTurnToolLines = relayMod.thisTurnToolLines as (items: unknown[], userText?: string) => string;
		const items = [
			call('A', 'work', { task: 'merge PR 5333' }),
			{ role: 'user', content: 'Is it merged yet?' },
			result('A', 'work', 'PR 5333 merged'),
			{ role: 'assistant', content: 'It\'s merged.' },
		];
		assert.equal(thisTurnToolLines(items, 'Is it merged yet?'), 'delivered: work result -> ok: PR 5333 merged');
		const failed = thisTurnToolLines([call('B', 'work', {}), { role: 'user', content: 'and?' }, result('B', 'work', null, 'core offline')], 'and?');
		assert.equal(failed, 'delivered: work result -> error: core offline');
		assert.ok(thisTurnToolLines([call('C', 'work', {}), { role: 'user', content: 'and?' }, result('C', 'work', 'y'.repeat(400))], 'and?').length <= 200);
	});

	it('the turn is bounded by time when the user line came after its tools', () => {
		// bodhi writes the user transcript at flushInput; when Gemini's transcription arrives after the tool call it
		// lands after the tool_call and tool_result, and "yes" -> write_page -> "I've added it" was judged unbacked.
		const thisTurnToolLines = relayMod.thisTurnToolLines as (items: unknown[], userText?: string, since?: number) => string;
		const t = (item: Record<string, unknown>, timestamp: number) => ({ ...item, timestamp });
		const items = [
			t({ role: 'user', content: 'Add Dot\'s plan to the v2 page.' }, 900),
			t(call('c0', 'write_page', { kind: 'old-turn' }), 950),
			t(result('c0', 'write_page', { ok: true }), 960),
			t({ role: 'assistant', content: 'Shall I add it?' }, 980),
			t(call('c1', 'write_page', { kind: 'markdown-v2' }), 1_100),
			t(result('c1', 'write_page', { ok: true, did: 'appended' }), 1_200),
			t({ role: 'user', content: 'Yes.' }, 1_300),
			t({ role: 'assistant', content: 'I\'ve added it.' }, 1_400),
		];
		assert.equal(thisTurnToolLines(items, 'Yes.', 1_000), 'write_page {"kind":"markdown-v2"} -> ok: {"ok":true,"did":"appended"}', 'the earlier turn\'s write is left out');
	});

	it('an injected "[system] " line is not the assistant speaking', () => {
		// bodhi's injectSystemMessage stores it as role assistant: read as speech it looks like a claim.
		assert.equal(recentConversationLines([{ role: 'user', content: 'hi' }, { role: 'assistant', content: '[system] The room was switched.' }, { role: 'assistant', content: 'Hello.' }], 8), 'user: hi\nassistant: Hello.');
	});
});

describe('relay agent: a claim no tool backs is corrected', () => {
	function agent(verdict: unknown) {
		const store = createVoiceTaskStore();
		const asked: string[] = [];
		const notices: string[] = [];
		const submitted: string[] = [];
		const inputs: Array<Record<string, unknown>> = [];
		const sinces: number[] = [];
		const relay = new RelayAgent({
			store,
			classify: (async (input: Record<string, unknown>) => { inputs.push(input); return verdict; }) as never,
			recentConversation: () => 'user: Delete it and add a new V3.\nassistant: I\'ve replaced the V2 plan with Dot\'s designed workflow for V3.',
			thisTurn: (u: string, since: number) => { sinces.push(since); return `(tools after "${u}")`; },
			ask: (t: string) => asked.push(t),
			notice: (t: string) => notices.push(t),
			log: () => {},
			submit: async (args: Record<string, unknown>) => { submitted.push(String(args.task)); store.add('task-u1', String(args.task)); return { status: 'pending', taskId: 'task-u1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		} as never);
		return { relay, asked, notices, submitted, inputs, sinces };
	}

	it('with no core task: the model is told it is not done, then to do it with its own tool, confirmed as usual', async () => {
		const { relay, asked, notices, submitted, inputs } = agent({ action: 'unbacked', claim: 'I\'ve created a separate page titled "v3"' });
		relay.onUserTurn('Yeah, I don\'t see it.');
		await tick(10);
		assert.equal(inputs[0].thisTurn, '(tools after "Yeah, I don\'t see it.")', 'the check saw this turn\'s tools');
		assert.deepEqual(submitted, []);
		assert.deepEqual(notices, []);
		assert.equal(asked.length, 1);
		assert.match(asked[0], /^\[System: You told the user "I've created a separate page titled 'v3'", but no tool did that this turn\. Tell the user in one short sentence that it is not done yet, then do it now: with your own tool, following its usual steps \(a write to a room is said back first and done after a clear yes\), or with work when only the core can do it\.\]$/);
	});

	it('the turn\'s tools are read from the end of the turn before it', async () => {
		const { relay, sinces } = agent({ action: 'none' });
		relay.onUserTurn('Add Dot\'s plan to the v2 page.', 1_000);
		await tick(10);
		relay.onUserTurn('Yes.', 2_000);
		await tick(10);
		assert.deepEqual(sinces, [0, 1_000]);
	});

	it('with a core task: it is submitted the way a work verdict is, and the model says it is not done yet but under way', async () => {
		const { relay, asked, notices, submitted } = agent({ action: 'unbacked', claim: 'I\'ve replaced the V2 plan with Dot\'s designed workflow for V3', task: 'In the room "Susan, Chi", make a v3 page from Dot\'s design that replaces the v2 plan' });
		relay.onUserTurn('So I need you to replace the current V2 with that plan. Delete it and add a new V3.');
		await tick(10);
		assert.deepEqual(submitted, ['In the room "Susan, Chi", make a v3 page from Dot\'s design that replaces the v2 plan']);
		assert.deepEqual(notices, [], 'one line to the model, not also the work verdict\'s notice');
		assert.equal(asked.length, 1);
		assert.match(asked[0], /You told the user "I've replaced the V2 plan with Dot's designed workflow for V3", but nothing had done it\. It has now been sent to the core: "In the room 'Susan, Chi', make a v3 page from Dot's design that replaces the v2 plan"\. Tell the user in one short sentence that it is not done yet and is being worked on now\. Do not call work for it\./);
	});

	it('the voice agent passes this turn\'s tools, and the voice prompt forbids a claim before an ok', () => {
		const agentSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(agentSrc, /thisTurn: \(userText, since\) => thisTurnToolLines\(sessionRef\?\.conversationContext\.items \?\? \[\], userText, since\)/);
		const configSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent-config.ts'), 'utf-8');
		assert.ok(configSrc.includes('- NEVER pretend you called a tool. NEVER say you did, created, updated, wrote, replaced, deleted or sent something unless a tool call this turn did it and returned ok, or a result told you the core did it; until then say you are doing it.'));
	});
});
