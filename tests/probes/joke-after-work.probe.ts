/**
 * Why does "tell me a joke" become a work call once a long task is running? The real gemini-3.8-live,
 * the real voice prompt and the real RelayAgent, with the core replaced by a timer. The same request
 * is made under several states, N times each:
 *
 *   none     — a fresh session, no task at all.
 *   open     — an app task whose work call is still OPEN (this PR: NON_BLOCKING, no result yet).
 *   open-q   — the same, but the submission reported a queue position (as live did: "N in line").
 *   closed   — an app task that finished 4 s ago: its call is closed, its result was spoken.
 *   ack-only — no open call: only the acknowledgement notice was injected (isolates the notice).
 *   open-no-ack — the open call without the acknowledgement notice (isolates the open call id).
 *   corrected — no task; the user first says "just tell me a joke, don't submit a work task" (as live did), then asks again.
 *   old      — main's tool: pendingMessage, not NON_BLOCKING, so the call closes at once while the task runs; no notice.
 *
 * Judged per run: did the joke request start a work call, or was it answered directly?
 *   set -a; . ./.env; set +a
 *   node node_modules/tsx/dist/cli.mjs tests/probes/joke-after-work.probe.ts [N] [state …]
 */
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { VoiceSession } from 'bodhi-realtime-agent';
import type { MainAgent } from 'bodhi-realtime-agent';
import { workTool } from '../../src/task-bridge.js';
import { RelayAgent, WORK_ACKNOWLEDGE, createVoiceTaskStore, relayAgentSubagentConfig } from '../../src/relay-agent.js';
import { buildInstructions } from '../../src/voice-agent-config.js';

const API_KEY = process.env.GEMINI_VOICE_API_KEY ?? process.env.GEMINI_API_KEY ?? '';
if (!API_KEY) { console.error('GEMINI_VOICE_API_KEY or GEMINI_API_KEY required'); process.exit(2); }
const MODEL = process.env.GEMINI_LIVE_MODEL || 'gemini-3.8-live';
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const N = Number(process.argv[2] || 5);
const STATES = process.argv.slice(3).length ? process.argv.slice(3) : ['none', 'open', 'open-q', 'closed', 'ack-only', 'open-no-ack', 'old', 'corrected'];
const APP = 'Can you create a calorie tracking app that uses image recognition to estimate calories from food photos?';
const JOKE = 'Can you tell me a joke?';
const OLD_PENDING = 'Task has been queued and is being processed. The result will be spoken when ready. Do NOT tell the user the task is done — say you are working on it.';

async function run(state: string): Promise<{ workCalls: string[]; joke: 'work' | 'direct' | 'silent'; spoken: string }> {
	const store = createVoiceTaskStore();
	const workCalls: string[] = [];
	let seq = 0;
	const transcript: string[] = [];
	let jokeAt = 0;
	let sessionRef: VoiceSession | null = null;
	const relay = new RelayAgent({
		store,
		submit: async (args) => {
			const task = String(args.task ?? '');
			const id = `task-${++seq}-${Date.now()}`;
			store.add(id, task);
			if (state === 'closed') setTimeout(() => relay.offerResult({ taskId: id, text: 'Your calorie tracker is built and working.' }), 4_000);
			return { status: 'pending', taskId: id, queuedAhead: state === 'open-q' ? 1 : 0, watcherOnline: true };
		},
		check: async () => ({ action: 'submit' }),
		notice: state === 'old' || state === 'open-no-ack' ? undefined : (text) => { sessionRef?.tryPublishSystemNotification(text); },
		log: () => {},
	});
	const agent: MainAgent = {
		name: 'main',
		instructions: () => buildInstructions({
			resolveCurrentMode: () => ({ mode: 'active', marker: '' }) as never, isMeetingActive: () => false, googleSearch: false,
			resetSessionGates: () => {}, resetNoteViewingDebounce: () => {}, getRecentConversation: () => '', getSecondsSinceLastTurn: () => null,
		}, { standIdentityJson: '{}', voiceContext: '', repoUrl: '', voiceAgentContext: '' }),
		tools: [state === 'old' ? (() => { const { behavior: _b, ...rest } = workTool as unknown as Record<string, unknown>; return { ...rest, pendingMessage: OLD_PENDING }; })() : workTool],
	} as unknown as MainAgent;
	const origLog = console.log;
	console.log = (...a: unknown[]) => {
		const line = a.map(String).join(' ');
		const m = /Background task started: work/.exec(line);
		if (m) workCalls.push(jokeAt ? 'after-joke' : 'before-joke');
	};
	const session = new VoiceSession({
		sessionId: `joke_${state}_${Date.now()}`, userId: 'probe_user', apiKey: API_KEY, agents: [agent], initialAgent: 'main',
		subagentConfigs: { work: relayAgentSubagentConfig(relay) },
		model: createGoogleGenerativeAI({ apiKey: API_KEY })('gemini-2.5-flash'), geminiModel: MODEL,
		clientSender: { sendAudio: () => {}, sendJson: () => {} }, inputAudioTranscription: true, orchestrationMode: 'actor',
		responseWatchdogMs: 60_000, speechConfig: { voiceName: 'Puck' },
	} as ConstructorParameters<typeof VoiceSession>[0]);
	sessionRef = session;
	try {
		await session.start();
		session.notifyClientConnected();
		const transport = (session as unknown as { transport: { onOutputTranscription?: (t: string) => void } }).transport;
		const prev = transport.onOutputTranscription;
		transport.onOutputTranscription = (t: string) => { transcript.push(t); prev?.(t); };
		await sleep(1500);
		if (['open', 'open-q', 'closed', 'old', 'open-no-ack'].includes(state)) {
			await session.injectText(APP, { mode: 'live' });
			const t0 = Date.now();
			while (Date.now() - t0 < 15_000 && !workCalls.length) await sleep(200);
			await sleep(state === 'closed' ? 12_000 : 6_000); // the ack is spoken; in "closed" the result too
		} else if (state === 'corrected') {
			await session.injectText(JOKE, { mode: 'live' });
			await sleep(8_000);
			await session.injectText("No, can you just tell me a joke and don't submit other work task?", { mode: 'live' });
			await sleep(8_000);
			workCalls.length = 0;
		} else if (state === 'ack-only') {
			session.tryPublishSystemNotification(WORK_ACKNOWLEDGE);
			await sleep(5_000);
		}
		const before = transcript.join('');
		jokeAt = Date.now();
		await session.injectText(JOKE, { mode: 'live' });
		while (Date.now() - jokeAt < 12_000 && !workCalls.includes('after-joke')) await sleep(200);
		await sleep(3_000);
		const said = transcript.join('').slice(before.length);
		const joke = workCalls.includes('after-joke') ? 'work' : said.trim() ? 'direct' : 'silent';
		return { workCalls, joke, spoken: said };
	} finally {
		console.log = origLog;
		await session.close('user_hangup').catch(() => {});
	}
}

const tally: Record<string, Record<string, number>> = {};
for (const state of STATES) {
	tally[state] = { work: 0, direct: 0, silent: 0 };
	for (let i = 0; i < N; i++) {
		try {
			const r = await run(state);
			tally[state][r.joke]++;
			console.log(`${state.padEnd(9)} #${i + 1}: joke → ${r.joke.padEnd(6)} | ${r.spoken.replace(/\s+/g, ' ').slice(0, 110)}`);
		} catch (err) { console.log(`${state} #${i + 1}: threw ${err instanceof Error ? err.message : String(err)}`); }
	}
}
console.log('\nstate      work  direct  silent');
for (const [s, t] of Object.entries(tally)) console.log(`${s.padEnd(9)} ${String(t.work).padStart(4)} ${String(t.direct).padStart(7)} ${String(t.silent).padStart(7)}`);
process.exit(0);
