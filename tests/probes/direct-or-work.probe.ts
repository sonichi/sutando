/**
 * Direct answer or work? The real gemini-3.8-live with the real voice prompt, one fresh session per
 * request, N runs each. Half the requests should be answered directly (no tool, the model's own
 * knowledge); half need the core (a lookup, the user's data, an action). Judged on whether a work
 * call started.
 *   set -a; . ./.env; set +a
 *   node node_modules/tsx/dist/cli.mjs tests/probes/direct-or-work.probe.ts [N]
 */
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { VoiceSession } from 'bodhi-realtime-agent';
import type { MainAgent } from 'bodhi-realtime-agent';
import { workTool } from '../../src/task-bridge.js';
import { RelayAgent, createVoiceTaskStore, relayAgentSubagentConfig } from '../../src/relay-agent.js';
import { buildInstructions } from '../../src/voice-agent-config.js';

const API_KEY = process.env.GEMINI_VOICE_API_KEY ?? process.env.GEMINI_API_KEY ?? '';
if (!API_KEY) { console.error('GEMINI_VOICE_API_KEY or GEMINI_API_KEY required'); process.exit(2); }
const MODEL = process.env.GEMINI_LIVE_MODEL || 'gemini-3.8-live';
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const N = Number(process.argv[2] || 3);

// Not the prompt's examples: the point is whether the principle generalises.
const CASES: Array<{ say: string; expect: 'direct' | 'work' }> = [
	{ say: 'Can you tell me a joke?', expect: 'direct' },
	{ say: '给我讲个笑话', expect: 'direct' },
	{ say: 'Tell me a riddle.', expect: 'direct' },
	{ say: 'What does "serendipity" mean?', expect: 'direct' },
	{ say: 'Explain in two sentences how a hash map works.', expect: 'direct' },
	{ say: 'How do you say "where is the train station" in Japanese?', expect: 'direct' },
	{ say: 'Tell me a short bedtime story about a fox.', expect: 'direct' },
	{ say: 'What is the capital of Australia?', expect: 'direct' },
	{ say: 'Check the status of PR 5302 in sonichi sutando.', expect: 'work' },
	{ say: 'Send an email to Chi saying the demo is at 3.', expect: 'work' },
	{ say: 'What is on my calendar tomorrow?', expect: 'work' },
	{ say: 'Create a calorie tracking app that estimates calories from food photos.', expect: 'work' },
	{ say: 'What did we discuss yesterday about the Typst project?', expect: 'work' },
	{ say: 'How much free disk space do I have?', expect: 'work' },
	{ say: '帮我查一下今天旧金山的天气', expect: 'work' },
	{ say: 'Write a 2000-word essay on the history of Typst.', expect: 'work' },
];

async function run(say: string): Promise<{ verdict: 'work' | 'direct' | 'silent'; spoken: string }> {
	let workCalled = false;
	const transcript: string[] = [];
	const store = createVoiceTaskStore();
	const relay = new RelayAgent({
		store,
		submit: async (args) => { const id = `task-${Date.now()}`; store.add(id, String(args.task ?? '')); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true }; },
		check: async () => ({ action: 'submit' }),
		log: () => {},
	});
	const agent: MainAgent = {
		name: 'main',
		instructions: () => buildInstructions({
			resolveCurrentMode: () => ({ mode: 'active', marker: '' }) as never, isMeetingActive: () => false, googleSearch: false,
			resetSessionGates: () => {}, resetNoteViewingDebounce: () => {}, getRecentConversation: () => '', getSecondsSinceLastTurn: () => null,
		}, { standIdentityJson: '{}', voiceContext: '', repoUrl: '', voiceAgentContext: '' }),
		tools: [workTool],
	} as MainAgent;
	const origLog = console.log;
	console.log = (...a: unknown[]) => { if (/Background task started: work/.test(a.map(String).join(' '))) workCalled = true; };
	const session = new VoiceSession({
		sessionId: `dow_${Date.now()}`, userId: 'probe_user', apiKey: API_KEY, agents: [agent], initialAgent: 'main',
		subagentConfigs: { work: relayAgentSubagentConfig(relay) },
		model: createGoogleGenerativeAI({ apiKey: API_KEY })('gemini-2.5-flash'), geminiModel: MODEL,
		clientSender: { sendAudio: () => {}, sendJson: () => {} }, inputAudioTranscription: true, orchestrationMode: 'actor',
		responseWatchdogMs: 60_000, speechConfig: { voiceName: 'Puck' },
	} as ConstructorParameters<typeof VoiceSession>[0]);
	try {
		await session.start();
		session.notifyClientConnected();
		const transport = (session as unknown as { transport: { onOutputTranscription?: (t: string) => void } }).transport;
		const prev = transport.onOutputTranscription;
		transport.onOutputTranscription = (t: string) => { transcript.push(t); prev?.(t); };
		await sleep(1500);
		const t0 = Date.now();
		await session.injectText(say, { mode: 'live' });
		while (Date.now() - t0 < 12_000 && !workCalled) await sleep(200);
		await sleep(3_000);
		const spoken = transcript.join('');
		return { verdict: workCalled ? 'work' : spoken.trim() ? 'direct' : 'silent', spoken };
	} finally {
		console.log = origLog;
		await session.close('user_hangup').catch(() => {});
	}
}

let right = 0, total = 0;
const wrong: string[] = [];
for (const c of CASES) for (let i = 0; i < N; i++) {
	try {
		const r = await run(c.say);
		total++;
		const ok = r.verdict === c.expect;
		if (ok) right++; else wrong.push(`${c.say} → ${r.verdict} (expected ${c.expect})`);
		console.log(`${ok ? 'ok  ' : 'MISS'} ${c.expect.padEnd(6)} got ${r.verdict.padEnd(6)} | ${c.say.slice(0, 60).padEnd(60)} | ${r.spoken.replace(/\s+/g, ' ').slice(0, 90)}`);
	} catch (err) { total++; wrong.push(`${c.say} → threw`); console.log(`ERR  ${c.say}: ${err instanceof Error ? err.message : String(err)}`); }
}
console.log(`\n${right}/${total} right`);
for (const w of wrong) console.log(`  ${w}`);
process.exit(0);
