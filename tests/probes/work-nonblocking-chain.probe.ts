/**
 * Chain probe — sutando's real `work` tool + RelayAgent on a bodhi VoiceSession against the REAL
 * gemini-3.8-live, with the core replaced by a timer. An earlier probe verified Gemini's side
 * (NON_BLOCKING + WHEN_IDLE); this one verifies sutando's side of the same contract:
 *
 *   S1 one call           — no response leaves until the result; one toolResponse, same id, WHEN_IDLE;
 *                           the model speaks the result; no interrupted turn.
 *   S2 out of order       — a slow call then a fast one: the fast result is spoken first; two ids.
 *   S3 re-issued call     — the same request again after its result: closed quietly, the core never
 *                           sees it twice (RelayAgent.recentlyAnswered).
 *   S4 reconnect mid-call — a forced reconnect here is a resume (triggerReconnect keeps the handle), so
 *                           the open call survives it and its reply is spoken in context; only a fresh
 *                           dial (session.reconnectBoundary) ends the call and sends the result the other way.
 *   S5 result mid-speech  — a result that lands while the model tells a story does not cut it off.
 *
 * Results are RECORDED (PASS/FAIL lines + a JSON log), judged from events and the output transcript.
 * Run on a host with a Gemini key:
 *   set -a; . /path/to/sutando/.env; set +a
 *   node node_modules/tsx/dist/cli.mjs tests/probes/work-nonblocking-chain.probe.ts [s1|s2|s3|s4|s5|all]
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { VoiceSession } from 'bodhi-realtime-agent';
import type { MainAgent } from 'bodhi-realtime-agent';
import { workTool } from '../../src/task-bridge.js';
import { RelayAgent, createVoiceTaskStore, relayAgentSubagentConfig } from '../../src/relay-agent.js';
import { frameTaskResult } from '../../src/inject-framing.js';

const API_KEY = process.env.GEMINI_VOICE_API_KEY ?? process.env.GEMINI_API_KEY ?? '';
if (!API_KEY) { console.error('GEMINI_VOICE_API_KEY or GEMINI_API_KEY required'); process.exit(2); }
const MODEL = process.env.GEMINI_LIVE_MODEL || 'gemini-3.8-live';
const which = (process.argv[2] ?? 'all').toLowerCase();

const t0 = Date.now();
const ts = () => `+${((Date.now() - t0) / 1000).toFixed(2)}s`;
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

type Ev = { at: number; kind: string; [k: string]: unknown };
const events: Ev[] = [];
const ev = (kind: string, data: Record<string, unknown> = {}) => { events.push({ at: Date.now() - t0, kind, ...data }); console.log(`${ts()} [${kind}] ${JSON.stringify(data)}`); };

// Capture bodhi's own log lines (RelayAgent, Dropped tool result, Background task …).
const captured: string[] = [];
const origLog = console.log.bind(console);
/** bodhi's own "Background task started" line is the source of truth for a work call taking the background path. */
let onBackgroundStarted: ((id: string, name: string) => void) | null = null;
console.log = (...a: unknown[]) => {
	const line = a.map(String).join(' ');
	captured.push(line);
	const m = /Background task started: (\w+) \(toolCallId=([^,)]+)/.exec(line);
	if (m) onBackgroundStarted?.(m[2], m[1]);
	origLog(...a);
};

let taskSeq = 0;
const submitted: string[] = [];

async function scenario(name: string, body: (ctx: Ctx) => Promise<string[]>): Promise<void> {
	origLog(`\n${ts()} ════ ${name} (model=${MODEL}) ════`);
	const ctx = await openSession(name);
	let failures: string[];
	try { failures = await body(ctx); }
	catch (err) { failures = [`threw: ${err instanceof Error ? err.message : String(err)}`]; }
	finally {
		ev('spoken', { scenario: name, transcript: ctx.spoken() });
		await ctx.session.close('user_hangup').catch(() => {});
	}
	ev(failures.length ? 'FAIL' : 'PASS', { scenario: name, failures });
}

interface Ctx {
	session: VoiceSession;
	relay: RelayAgent;
	/** Plan the fake core's answer for the NEXT submitted task. */
	nextResult: (text: string, delayMs: number) => void;
	/** The assistant's output transcript since session start. */
	spoken: () => string;
	/** First time (ms since t0) `needle` appeared in the output transcript, or null. */
	spokenAt: (needle: string) => number | null;
	/** Whether the output transcript produced since `sinceMs` matches `re` (the model paraphrases; judge on content words). */
	spokenAfter: (sinceMs: number, re: RegExp) => boolean;
	toolResponses: Array<{ at: number; id: string; scheduling?: string; preview: string }>;
	toolCalls: Array<{ at: number; id: string; name: string; task: string }>;
	interrupted: number;
	turnEnds: Array<{ at: number }>;
	injectedElsewhere: string[];
	say: (text: string) => Promise<void>;
	forceReconnect: () => Promise<void>;
}

async function openSession(label: string): Promise<Ctx> {
	const store = createVoiceTaskStore();
	const planned: Array<{ text: string; delayMs: number }> = [];
	const ctx: Partial<Ctx> & { transcript: string[]; transcriptAt: Array<{ at: number; text: string }> } = {
		transcript: [], transcriptAt: [], toolResponses: [], toolCalls: [], interrupted: 0, turnEnds: [], injectedElsewhere: [],
	};
	const relay = new RelayAgent({
		store,
		submit: async (args) => {
			const id = `task-${label}-${++taskSeq}`;
			const task = String(args.task ?? '');
			submitted.push(task);
			store.add(id, task);
			const plan = planned.shift() ?? { text: `DONE-${id}`, delayMs: 3000 };
			ev('core.submit', { id, task: task.slice(0, 60), delayMs: plan.delayMs });
			setTimeout(() => {
				const returned = relay.offerResult({ taskId: id, text: plan.text });
				ev('core.result', { id, returnedThroughCall: returned });
				if (!returned) {
					// The call is gone (ended at a reconnect): the result goes the other way, exactly as
					// live-agent-runtime's viaNotice does — a system notification the model speaks when idle.
					const ok = session.tryPublishSystemNotification(frameTaskResult(plan.text));
					ctx.injectedElsewhere!.push(id);
					ev('core.notice', { id, published: ok });
				}
			}, plan.delayMs);
			return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		},
		// A trivial request check: the same text as an open or just-answered task is a duplicate.
		check: async ({ task, openTasks }) => {
			const same = (openTasks ?? []).find((t) => t.text === task);
			return same ? { action: 'duplicate', of: same.id } : { action: 'submit' };
		},
		log: (m) => console.log(`${ts()} ${m}`),
	});
	const agent: MainAgent = {
		name: 'main',
		instructions:
			'You are a concise voice assistant. For anything that needs doing — drawing, looking something up, ' +
			'the weather, counting, generating — call the work tool with the request as the task. The call stays open ' +
			'while it runs: say in one short sentence that you are on it, keep talking with the user if they say more, ' +
			'and when a work call returns its result, say the result text out loud exactly, as the answer to that request. ' +
			'If asked for a story, tell a story of about twelve sentences without stopping.',
		tools: [workTool],
	};
	const google = createGoogleGenerativeAI({ apiKey: API_KEY });
	const session = new VoiceSession({
		sessionId: `probe_${label}_${Date.now()}`,
		userId: 'probe_user',
		apiKey: API_KEY,
		agents: [agent],
		initialAgent: 'main',
		subagentConfigs: { work: relayAgentSubagentConfig(relay) },
		model: google('gemini-2.5-flash'),
		geminiModel: MODEL,
		clientSender: { sendAudio: () => {}, sendJson: () => {} },
		inputAudioTranscription: true,
		// As sutando: only actor mode routes a background tool to its persistent subagent (the relay agent).
		orchestrationMode: 'actor',
		responseWatchdogMs: 60_000, // out of the way: the probe forces its own reconnect in S4
		speechConfig: { voiceName: 'Puck' },
		hooks: {
			onToolCall: (e: { toolCallId: string; toolName: string; execution: string }) => ev('hook.toolCall', { id: e.toolCallId, name: e.toolName, execution: e.execution }),
			onToolResult: (e: { toolCallId: string; status: string; durationMs: number; error?: string }) => ev('tool.result', { id: e.toolCallId, status: e.status, durationMs: e.durationMs, error: e.error }),
		},
	} as ConstructorParameters<typeof VoiceSession>[0]);

	session.eventBus.subscribe('turn.interrupted', () => { ctx.interrupted!++; ev('turn.interrupted'); });
	session.eventBus.subscribe('turn.end', () => { ctx.turnEnds!.push({ at: Date.now() - t0 }); ev('turn.end'); });
	session.eventBus.subscribe('session.stateChange', (e) => {
		const to = (e as { toState?: string }).toState;
		ev('session.state', { to });
		// What voice-agent.ts does: a reconnect ends every open work call.
		if (to === 'RECONNECTING') {
			const ended = relay.endAllCalls('continuing');
			ev('relay.endAllCalls', { ended });
		}
	});

	onBackgroundStarted = (id, name) => {
		if (ctx.toolCalls!.some((c) => c.id === id)) return;
		ctx.toolCalls!.push({ at: Date.now() - t0, id, name, task: '' });
		ev('tool.call', { id, name, path: 'background' });
	};
	await session.start();
	session.notifyClientConnected();

	// Observe what actually goes to Gemini for a tool, and the model's output transcript. bodhi may
	// re-wire or replace the transport on a reconnect, so the wrappers are re-applied on every ACTIVE.
	type Transport = { sendToolResult: (r: { id: string; scheduling?: string; result: unknown }) => void; onOutputTranscription?: (text: string) => void; __probeWrapped?: boolean };
	const wrapTransport = () => {
		const transport = (session as unknown as { transport: Transport }).transport;
		if (!transport || transport.__probeWrapped) return;
		transport.__probeWrapped = true;
		const send = transport.sendToolResult.bind(transport);
		transport.sendToolResult = (r) => {
			ctx.toolResponses!.push({ at: Date.now() - t0, id: r.id, scheduling: r.scheduling, preview: JSON.stringify(r.result).slice(0, 80) });
			ev('toolResponse', { id: r.id, scheduling: r.scheduling, preview: JSON.stringify(r.result).slice(0, 60) });
			send(r);
		};
		const prevOut = transport.onOutputTranscription;
		transport.onOutputTranscription = (text: string) => {
			ctx.transcript.push(text);
			ctx.transcriptAt.push({ at: Date.now() - t0, text });
			prevOut?.(text);
		};
		ev('probe.transportWrapped');
	};
	wrapTransport();
	session.eventBus.subscribe('session.stateChange', (e) => {
		if ((e as { toState?: string }).toState === 'ACTIVE') {
			// The session re-wires its callbacks on reconnect: wrap the (possibly new) transport again.
			const transport = (session as unknown as { transport: Transport }).transport;
			if (transport) transport.__probeWrapped = false;
			setTimeout(wrapTransport, 50);
		}
	});

	const full = (): Ctx => ({
		session, relay,
		nextResult: (text, delayMs) => planned.push({ text, delayMs }),
		spoken: () => ctx.transcript.join(''),
		spokenAt: (needle) => {
			let acc = '';
			for (const { at, text } of ctx.transcriptAt) { acc += text; if (acc.toLowerCase().includes(needle.toLowerCase())) return at; }
			return null;
		},
		spokenAfter: (sinceMs, re) => re.test(ctx.transcriptAt.filter((t) => t.at >= sinceMs).map((t) => t.text).join('')),
		toolResponses: ctx.toolResponses!, toolCalls: ctx.toolCalls!, get interrupted() { return ctx.interrupted!; },
		turnEnds: ctx.turnEnds!, injectedElsewhere: ctx.injectedElsewhere!,
		say: async (text) => { ev('user.say', { text }); await session.injectText(text, { mode: 'live' }); },
		forceReconnect: async () => {
			const i = session as unknown as { reconnector: { triggerReconnect(reason: string, elicit?: boolean): void }; sessionManager: { resumptionHandle: string | null } };
			const deadline = Date.now() + 15_000;
			while (!i.sessionManager.resumptionHandle && Date.now() < deadline) await sleep(250);
			ev('probe.forceReconnect', { handle: !!i.sessionManager.resumptionHandle });
			i.reconnector.triggerReconnect('probe', false);
		},
	});
	return full();
}

async function waitFor(pred: () => boolean, ms: number): Promise<boolean> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) { if (pred()) return true; await sleep(200); }
	return pred();
}

const responsesFor = (c: Ctx, id: string) => c.toolResponses.filter((r) => r.id === id);

// ── S1 ───────────────────────────────────────────────────────────────────────
async function s1(c: Ctx): Promise<string[]> {
	const f: string[] = [];
	c.nextResult('The apple picture is ready.', 8_000);
	await c.say('Draw me a picture of an apple.');
	if (!(await waitFor(() => c.toolCalls.length >= 1, 15_000))) return ['no work call within 15 s'];
	const call = c.toolCalls[0];
	await sleep(4_000);
	if (responsesFor(c, call.id).length) f.push(`a response left before the result (fake first answer): ${JSON.stringify(responsesFor(c, call.id))}`);
	await waitFor(() => responsesFor(c, call.id).length >= 1, 12_000);
	const rs = responsesFor(c, call.id);
	if (rs.length !== 1) f.push(`expected exactly one response for ${call.id}, got ${rs.length}`);
	if (rs[0] && rs[0].scheduling !== 'when_idle') f.push(`response scheduling is ${rs[0]?.scheduling}, not when_idle`);
	if (rs[0] && rs[0].at - call.at < 7_000) f.push(`response left ${rs[0].at - call.at} ms after the call; the result was due at 8 s`);
	const sentAt = rs[0]?.at ?? Date.now() - t0;
	if (!(await waitFor(() => c.spokenAfter(sentAt, /apple/i) && c.spokenAfter(sentAt, /ready|done|finished|complete|here/i), 20_000))) f.push(`result not spoken after the response; transcript: ${c.spoken().slice(-200)}`);
	if (c.interrupted) f.push(`${c.interrupted} interrupted turn(s)`);
	return f;
}

// ── S2 ───────────────────────────────────────────────────────────────────────
async function s2(c: Ctx): Promise<string[]> {
	const f: string[] = [];
	c.nextResult('The sunset picture is ready.', 14_000);
	c.nextResult('Tokyo is 17 degrees with light rain.', 3_000);
	await c.say('Draw me a picture of a sunset.');
	await waitFor(() => c.toolCalls.length >= 1, 15_000);
	await sleep(2_500);
	await c.say('Also, what is the weather in Tokyo right now?');
	if (!(await waitFor(() => c.toolCalls.length >= 2, 15_000))) return ['second work call never came (the model answered the weather itself, or waited)'];
	const [slow, fast] = c.toolCalls;
	await waitFor(() => c.spokenAt('17') !== null || c.spokenAt('rain') !== null, 25_000);
	await waitFor(() => c.spokenAt('sunset picture') !== null || c.spokenAt('sunset is') !== null || c.spokenAt('sunset drawing') !== null, 30_000);
	const wAt = c.spokenAt('17') ?? c.spokenAt('rain'); const sAt = c.spokenAt('sunset picture') ?? c.spokenAt('sunset is') ?? c.spokenAt('sunset drawing');
	if (wAt === null) f.push('weather token never spoken');
	if (sAt === null) f.push('sunset token never spoken');
	if (wAt !== null && sAt !== null && wAt > sAt) f.push(`the fast result was spoken after the slow one (weather ${wAt} ms, sunset ${sAt} ms)`);
	if (responsesFor(c, slow.id).length !== 1 || responsesFor(c, fast.id).length !== 1) f.push(`responses per id: slow=${responsesFor(c, slow.id).length} fast=${responsesFor(c, fast.id).length}`);
	if (c.toolResponses.some((r) => r.scheduling !== 'when_idle')) f.push(`a response without when_idle: ${JSON.stringify(c.toolResponses)}`);
	if (c.interrupted) f.push(`${c.interrupted} interrupted turn(s)`);
	return f;
}

// ── S3 ───────────────────────────────────────────────────────────────────────
async function s3(c: Ctx): Promise<string[]> {
	const f: string[] = [];
	const before = submitted.length;
	c.nextResult('The cat picture is ready.', 4_000);
	await c.say('Draw me a picture of a cat.');
	await waitFor(() => c.toolCalls.length >= 1, 15_000);
	await waitFor(() => c.spokenAt('cat picture') !== null || c.spokenAt('cat is') !== null || c.spokenAt('cat drawing') !== null, 20_000);
	await sleep(1_500);
	// The user (or a re-issuing model) asks the very same thing again, seconds after the answer.
	await c.say('Draw me a picture of a cat.');
	const got = await waitFor(() => c.toolCalls.length >= 2, 12_000);
	if (got) {
		const second = c.toolCalls[1];
		await waitFor(() => responsesFor(c, second.id).length >= 1, 8_000);
		const r = responsesFor(c, second.id)[0];
		if (!r) f.push('the re-issued call got no response');
		else if (!/answered a moment ago/i.test(r.preview) && !/Stay silent/i.test(r.preview)) f.push(`re-issued call was not closed as already answered: ${r.preview}`);
	} else {
		ev('note', { s3: 'the model did not re-issue a work call for the repeat; nothing to close' });
	}
	if (submitted.length - before !== 1) f.push(`the core saw the request ${submitted.length - before} time(s); expected once`);
	return f;
}

// ── S4 ───────────────────────────────────────────────────────────────────────
async function s4(c: Ctx): Promise<string[]> {
	const f: string[] = [];
	c.nextResult('Counting finished: all one million numbers counted.', 16_000);
	await c.say('Please count to one million for me.');
	if (!(await waitFor(() => c.toolCalls.length >= 1, 15_000))) return ['no work call within 15 s'];
	const call = c.toolCalls[0];
	await sleep(3_000);
	await c.forceReconnect();
	const ended = await waitFor(() => captured.some((l) => l.includes('endAllCalls') || l.includes('open work call')) || events.some((e) => e.kind === 'relay.endAllCalls' && (e.ended as string[]).length > 0), 15_000);
	if (!ended) f.push('the reconnect did not end the open work call');
	const mine = (e: Ev) => e.kind === 'core.result' && String(e.id).includes('S4');
	await waitFor(() => events.some(mine), 25_000);
	const res = events.find(mine);
	if (!res) f.push('the fake core result never fired');
	else if (res.returnedThroughCall !== false) f.push('the result was returned to the dead call instead of going the other way');
	if (!c.injectedElsewhere.length) f.push('the result was not delivered another way');
	const resAt = (res?.at as number | undefined) ?? Date.now() - t0;
	if (!(await waitFor(() => c.spokenAfter(resAt, /million|count/i), 25_000))) f.push(`result not spoken after the reconnect; transcript: ${c.spoken().slice(-200)}`);
	const dropped = captured.filter((l) => l.includes('Dropped tool result'));
	ev('note', { s4: { droppedAtFence: dropped.length, callId: call.id } });
	return f;
}

// ── S5 ───────────────────────────────────────────────────────────────────────
async function s5(c: Ctx): Promise<string[]> {
	const f: string[] = [];
	c.nextResult('The dog picture is ready.', 5_000);
	await c.say('Draw me a picture of a dog, and while it is being drawn, tell me a story of about twelve sentences without stopping.');
	if (!(await waitFor(() => c.toolCalls.length >= 1, 15_000))) return ['no work call within 15 s'];
	const call = c.toolCalls[0];
	await waitFor(() => responsesFor(c, call.id).length >= 1, 15_000);
	const r = responsesFor(c, call.id)[0];
	const interruptedBefore = c.interrupted;
	await waitFor(() => c.spokenAfter(r?.at ?? 0, /dog/i) && c.spokenAfter(r?.at ?? 0, /ready|done|finished|complete|here/i), 90_000);
	const spokeAt = c.spokenAfter(r?.at ?? 0, /dog/i) ? Date.now() - t0 : null;
	if (spokeAt === null) f.push(`result token never spoken; transcript tail: ${c.spoken().slice(-200)}`);
	if (c.interrupted > interruptedBefore) f.push(`${c.interrupted - interruptedBefore} turn(s) interrupted after the result landed`);
	if (r && spokeAt !== null) ev('note', { s5: { resultSentAt: r.at, spokenAt: spokeAt, waitedMs: spokeAt - r.at } });
	return f;
}

async function main() {
	const bodhiVersion = JSON.parse(readFileSync(new URL('../../node_modules/bodhi-realtime-agent/package.json', import.meta.url), 'utf-8')).version as string;
	origLog(`${ts()} work non-blocking chain probe (model=${MODEL}, bodhi=${bodhiVersion})`);
	const all: Array<[string, (c: Ctx) => Promise<string[]>]> = [['S1 one call', s1], ['S2 out of order', s2], ['S3 re-issued call', s3], ['S4 reconnect mid-call', s4], ['S5 result mid-story', s5]];
	const pick = which === 'all' ? all : all.filter(([n]) => n.toLowerCase().startsWith(which));
	for (const [name, body] of pick) await scenario(name, body);
	mkdirSync('probe-logs', { recursive: true });
	const out = `probe-logs/work-nonblocking-${MODEL}-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
	writeFileSync(out, JSON.stringify({ model: MODEL, events, captured }, null, 1));
	origLog(`\n${ts()} log: ${out}`);
	const verdicts = events.filter((e) => e.kind === 'PASS' || e.kind === 'FAIL');
	for (const v of verdicts) origLog(`${v.kind} ${v.scenario}${(v.failures as string[]).length ? ': ' + (v.failures as string[]).join(' | ') : ''}`);
	process.exit(verdicts.some((v) => v.kind === 'FAIL') ? 1 : 0);
}
main().catch((err) => { console.error(err); process.exit(1); });
