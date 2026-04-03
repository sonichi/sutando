#!/usr/bin/env npx tsx
/**
 * Phone Conversation Server — Twilio Media Streams + bodhi VoiceSession
 *
 * Uses bodhi's full VoiceSession for Gemini session management, tool execution,
 * reconnect, audio buffering — the same stack as the voice agent (voice-agent.ts).
 *
 * ## Audio chain (inbound — caller speaks)
 *
 *   Caller → Twilio → [mu-law 8kHz JSON WS] → Server (mulawTopcm16k)
 *     → [PCM 16kHz Buffer] → VoiceSession.handleAudioFromClient()
 *     → GeminiLiveTransport.sendAudio() → Gemini
 *
 * ## Audio chain (outbound — Gemini speaks)
 *
 *   Gemini → GeminiLiveTransport → VoiceSession.handleAudioOutput()
 *     → Server override (pcm24kToMulaw8k) → [mu-law 8kHz JSON WS] → Twilio → Caller
 *
 * ## Task chain (caller requests an action)
 *
 *   Caller speaks → Gemini invokes 'work' tool → delegateTask()
 *     → writes tasks/task-phone-{ts}.txt (resolves immediately)
 *     → Claude (fswatch) reads task, decides action, writes results/task-phone-{ts}.txt
 *     → Server polls result, injects into Gemini via sendContent → Gemini speaks result
 *
 * ## Concurrent call chain
 *
 *   Claude reads task "call Mary at +1..." → POST /concurrent-call API
 *     → Server creates child VoiceSession → Twilio calls Mary
 *     → Child Gemini has conversation with Mary
 *     → Child call ends → cleanupCall() injects transcript into parent Gemini
 *     → Parent Gemini speaks summary to caller
 *
 * ## Key design: voice is voice, Claude is the brain
 *   The server is a dumb audio pipe. It does not parse task descriptions,
 *   extract phone numbers, or decide what actions to take. All intelligence
 *   goes through Claude via task files. The server only handles:
 *   - Audio I/O (mu-law ↔ PCM conversion)
 *   - VoiceSession lifecycle (create, start, close)
 *   - API endpoints for Claude to call (/call, /concurrent-call, /hangup)
 *   - Goodbye detection (hang_up tool → Twilio hangup)
 *   - Transcript persistence
 */

import 'dotenv/config';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { mkdirSync, writeFileSync, appendFileSync, unlinkSync, existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync, spawn, type ChildProcess } from 'node:child_process';
import { VoiceSession, type ToolDefinition, type MainAgent } from 'bodhi-realtime-agent';
import { WebSocketServer, WebSocket } from 'ws';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import { inlineTools, anyCallerTools, ownerOnlyTools, configurableTools } from '../../../src/inline-tools.js';

// --- Config ---

const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? '';
const TWILIO_ACCOUNT_SID = process.env.TWILIO_ACCOUNT_SID ?? '';
const TWILIO_AUTH_TOKEN = process.env.TWILIO_AUTH_TOKEN ?? '';
const TWILIO_PHONE_NUMBER = process.env.TWILIO_PHONE_NUMBER ?? '';
const NGROK_AUTHTOKEN = process.env.NGROK_AUTHTOKEN ?? '';
const PORT = Number(process.env.PHONE_PORT) || 3100;
const WORKSPACE_DIR = process.env.SUTANDO_WORKSPACE || join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const RESULTS_DIR = process.env.PHONE_RESULTS_DIR || join(WORKSPACE_DIR, 'results');
const TASKS_DIR = join(WORKSPACE_DIR, 'tasks');
const TASK_POLL_INTERVAL_MS = 500;
const TASK_TIMEOUT_MS = 120_000;
const OWNER_NAME = process.env.owner ?? '';
const OWNER_NUMBER = process.env.OWNER_NUMBER ?? '';

/** Normalize phone number to digits only for comparison (strips +, -, spaces, parens) */
function normalizePhone(num: string): string {
	const digits = num.replace(/\D/g, '');
	// If 10 digits (US without country code), prepend 1
	return digits.length === 10 ? '1' + digits : digits;
}

/** Read recent conversation context, relabeled to avoid identity confusion */
function getSafeContext(lines = 5): string {
	try {
		const logPath = join(WORKSPACE_DIR, 'conversation.log');
		if (!existsSync(logPath)) return '';
		const entries = readFileSync(logPath, 'utf-8').trim().split('\n').slice(-lines);
		return entries.map(line => {
			const [, role, text] = line.split('|', 3);
			if (!role || !text) return '';
			if (role === 'user') return `Owner said: ${text}`;
			if (role === 'assistant') return `You (Sutando) replied: ${text}`;
			return '';
		}).filter(Boolean).join('\n');
	} catch { return ''; }
}


const VERIFIED_CALLERS_RAW = (process.env.VERIFIED_CALLERS ?? '').split(',').map(s => s.trim()).filter(Boolean);
const VERIFIED_CALLERS = new Set(VERIFIED_CALLERS_RAW.map(normalizePhone));
// Meeting IDs that grant the agent OS access (task delegation).
// Accepts Zoom meeting IDs, Google Meet PINs, or Meet codes (e.g. "gbn-otgn-dex").
// Unverified meetings: agent joins and takes notes only (no work tool).
const VERIFIED_MEETINGS = new Set(
	[
		...(process.env.VERIFIED_MEETINGS ?? '').split(','),
		...(process.env.VERIFIED_ZOOM_CALLERS ?? '').split(','),  // backward compat
	].map(s => s.trim()).filter(Boolean)
);

if (!GEMINI_API_KEY || !TWILIO_ACCOUNT_SID || !TWILIO_AUTH_TOKEN || !TWILIO_PHONE_NUMBER) {
	console.error('Error: GEMINI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER required');
	process.exit(1);
}
if (!NGROK_AUTHTOKEN) {
	console.error('Error: NGROK_AUTHTOKEN required for auto-tunnel');
	process.exit(1);
}

const CALLS_DIR = join(RESULTS_DIR, 'calls');
mkdirSync(CALLS_DIR, { recursive: true });
mkdirSync(TASKS_DIR, { recursive: true });

const ts = () => new Date().toISOString().slice(11, 23);
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const google = createGoogleGenerativeAI({ apiKey: GEMINI_API_KEY });

// --- Audio conversion (inbound + outbound audio chains) ---
// These functions convert between Twilio's mu-law 8kHz format and the PCM formats
// used by bodhi's VoiceSession (16kHz input, 24kHz output from Gemini).

const MULAW_DECODE = new Int16Array(256);
(() => {
	for (let i = 0; i < 256; i++) {
		const mu = ~i & 0xff;
		const sign = mu & 0x80 ? -1 : 1;
		const exponent = (mu >> 4) & 0x07;
		const mantissa = mu & 0x0f;
		const magnitude = ((mantissa << 1) + 33) * (1 << exponent) - 33;
		MULAW_DECODE[i] = sign * magnitude;
	}
})();

// [Outbound audio chain] Single PCM sample → mu-law byte
function pcmToMulaw(sample: number): number {
	const sign = sample < 0 ? 0x80 : 0;
	let magnitude = Math.min(Math.abs(sample), 32635);
	magnitude += 0x84;
	let exponent = 7;
	const expMask = 0x4000;
	for (let i = 0; i < 8; i++) {
		if (magnitude & (expMask >> i)) { exponent = 7 - i; break; }
	}
	const mantissa = (magnitude >> (exponent + 3)) & 0x0f;
	return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

// [Inbound audio chain] Twilio mu-law 8kHz → PCM 16kHz for VoiceSession.handleAudioFromClient()
function mulawTopcm16k(mulawBytes: Buffer): Buffer {
	const numSamples = mulawBytes.length;
	const out = Buffer.alloc(numSamples * 2 * 2);
	for (let i = 0; i < numSamples; i++) {
		const s0 = MULAW_DECODE[mulawBytes[i]];
		const s1 = i + 1 < numSamples ? MULAW_DECODE[mulawBytes[i + 1]] : s0;
		const mid = (s0 + s1) >> 1;
		out.writeInt16LE(s0, i * 4);
		out.writeInt16LE(mid, i * 4 + 2);
	}
	return out;
}

// [Outbound audio chain] Gemini PCM 24kHz → mu-law 8kHz for Twilio
// Called from the handleAudioOutput override in createCallSession()
function pcm24kToMulaw8k(pcmBuf: Buffer): Buffer {
	const numSamples = pcmBuf.length / 2;
	const outLen = Math.floor(numSamples / 3);
	const out = Buffer.alloc(outLen);
	for (let i = 0; i < outLen; i++) {
		const sample = pcmBuf.readInt16LE(i * 3 * 2);
		out[i] = pcmToMulaw(sample);
	}
	return out;
}

// --- Active call sessions ---

interface CallSession {
	callSid: string;
	streamSid: string;
	purpose: string;
	twilioWs: WebSocket;
	voiceSession: VoiceSession;
	bodhiPort: number;  // port for VoiceSession's ClientTransport (required but audio bypasses it)
	callerNumber: string;
	callerVerified: boolean;
	isOwner: boolean;
	isMeeting: boolean;
	meetingId?: string;
	passcode?: string;
	parentCallSid?: string;
	childCallSids: string[];
	hangingUp: boolean;
	pendingTasks: number;
	transcript: { role: string; text: string }[];
	resultQueue: { text: string }[];
	taskResultCache?: Map<string, string>;
}

const activeCalls = new Map<string, CallSession>();
const pendingMeetingJoins = new Set<string>(); // prevents duplicate near-simultaneous joins
let nextBodhiPort = 9910; // Dynamic ports for per-call VoiceSessions

// --- Goodbye detection ---
// Phone calls need explicit hangup (unlike browser which just disconnects).
// Instead of a separate classifier, the conversation model has a `hang_up` tool
// it can call when both sides have said goodbye. This eliminates extra API calls
// and gives the model full conversational context to decide when to end the call.

// --- Task delegation (task chain) ---
// Same pattern as task-bridge.ts: write task file → Claude processes → write result file.
// Resolves the tool call immediately so Gemini keeps talking.
// Polls for result file asynchronously and injects it into Gemini when it arrives.

// [Task chain] Gemini 'work' tool → write task file → resolve immediately → inject result later
// The tool resolves instantly so Gemini stays conversational while Claude works.
// When the result arrives, it's injected via sendContent.
function delegateTask(callSession: CallSession, taskDescription: string): Promise<unknown> {
	// Dedup: if same task was already completed this call, return cached result
	const cached = callSession.taskResultCache?.get(taskDescription);
	if (cached) {
		console.log(`${ts()} [Task] cache hit for "${taskDescription}" — replaying result`);
		callSession.resultQueue.push({
			text: `[Task result for "${taskDescription}"]\n${cached}\n\nReport this result to the caller now.`,
		});
		return Promise.resolve({ status: 'cached', message: 'This was already completed — result is being replayed.' });
	}

	const taskId = `task-phone-${Date.now()}`;
	const taskPath = join(TASKS_DIR, `${taskId}.txt`);
	const resultPath = join(RESULTS_DIR, `${taskId}.txt`);

	callSession.pendingTasks++;
	console.log(`${ts()} [Task] delegated: ${taskId} — "${taskDescription}" (pending: ${callSession.pendingTasks})`);

	const fullTranscript = callSession.transcript.slice(-20)
		.map(t => `${t.role === 'sutando' ? 'Sutando' : 'Caller'}: ${t.text}`)
		.join('\n');
	const content = `id: ${taskId}\ntimestamp: ${new Date().toISOString()}\ncallSid: ${callSession.callSid}\ncaller: ${callSession.callerNumber || 'unknown'}\ntask: ${taskDescription}\ntranscript:\n${fullTranscript}\n`;
	writeFileSync(taskPath, content);

	// Poll for result in background, inject when ready — don't block Gemini
	const POLL_TIMEOUT_MS = 300_000; // 5 min — watcher gaps can cause delays
	const startTime = Date.now();

	const poll = setInterval(() => {
		if (callSession.hangingUp || !activeCalls.has(callSession.callSid)) {
			clearInterval(poll);
			callSession.pendingTasks = Math.max(0, callSession.pendingTasks - 1);
			return;
		}
		if (existsSync(resultPath)) {
			clearInterval(poll);
			callSession.pendingTasks = Math.max(0, callSession.pendingTasks - 1);
			const result = readFileSync(resultPath, 'utf-8').trim();
			console.log(`${ts()} [Task] result for ${taskId} (${Date.now() - startTime}ms): ${result.slice(0, 200)}`);
			try { unlinkSync(resultPath); } catch {}
			// Cache result so duplicate requests get instant replay
			if (!callSession.taskResultCache) callSession.taskResultCache = new Map();
			callSession.taskResultCache.set(taskDescription, result);
			// Queue result — will be injected on next turn.end to avoid interrupting speech
			callSession.resultQueue.push({
				text: `[Task result for "${taskDescription}"]\n${result}\n\nReport this result to the caller now.`,
			});
			return;
		}
		if (Date.now() - startTime > POLL_TIMEOUT_MS) {
			clearInterval(poll);
			callSession.pendingTasks = Math.max(0, callSession.pendingTasks - 1);
			console.log(`${ts()} [Task] timeout for ${taskId}`);
			try {
				(callSession.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[Task "${taskDescription}" timed out — still being worked on. Let the caller know.]` },
				], true);
			} catch {}
		}
	}, TASK_POLL_INTERVAL_MS);

	// Resolve immediately so Gemini can keep talking
	return Promise.resolve({ status: 'delegated', taskId, message: 'Task submitted. Do NOT report any result to the caller yet — wait for the actual task result before saying anything about it. You can continue the conversation on other topics.' });
}

// --- Build bodhi agent + tools for a call ---
// Creates a MainAgent with system instructions and the 'work' tool.
// The 'work' tool is the same as task-bridge.ts — write task file, Claude handles it.

function buildAgent(callSession: CallSession): MainAgent {
	const isChildCall = !!callSession.parentCallSid;

	let instructions: string;
	if (callSession.isMeeting) {
		const ivrInstructions = [
			'You are dialing into a meeting. You will first hear an automated IVR system.',
			'CRITICAL IVR NAVIGATION: Listen carefully to the automated prompts and react to what you actually hear.',
			callSession.meetingId ? `If the IVR asks for a meeting ID, meeting number, conference ID, or PIN, immediately call send_dtmf with digits "${callSession.meetingId}#".` : '',
			callSession.passcode ? `If the IVR separately asks for a passcode or meeting passcode, immediately call send_dtmf with digits "${callSession.passcode}#".` : '',
			'If the IVR asks you to record a name, announce yourself briefly as "Sutando" unless there is an option to skip.',
			'If the IVR says "press # to skip", call send_dtmf with digits "#".',
			'Do NOT guess or front-run the menu. Wait for the prompt, then send the matching digits.',
			'Do NOT try to speak over DTMF-only prompts. Use send_dtmf for keypad interactions.',
			'Once you are in the meeting (you hear people talking, hold music ending, or silence), do NOT announce that you just joined or that you were dialing in. Just listen quietly until someone speaks to you or asks you something.',
		].filter(Boolean).join('\n');

		const meetingInstructions = callSession.callerVerified
			? 'You are Sutando, an AI assistant in a meeting. You have full capabilities — make calls, look things up, send messages, and perform tasks. Be natural, warm, and conversational. Keep responses to 1-2 sentences. Known URLs: "sutando agent repo" = https://github.com/sonichi/sutando'
			: 'You are Sutando, an AI note-taker in a meeting. Be natural, warm, and conversational. Keep responses to 1-2 sentences. You can listen, take notes, and answer questions about the discussion. You cannot make phone calls, send messages, or look things up — if asked, just say you can only help with notes today.';

		instructions = ivrInstructions + '\n\nAfter joining the meeting:\n' + meetingInstructions;
	} else if (isChildCall) {
		const availableTools = [
			...anyCallerTools.map(t => t.name),
			...(callSession.callerVerified ? configurableTools.map(t => t.name) : []),
			...(callSession.isOwner ? ownerOnlyTools.map(t => t.name) : []),
		];
		instructions = [
			`You are Sutando, a personal AI assistant. You are making a phone call on behalf of ${OWNER_NAME || 'your owner'}.`,
			`You are Sutando — NOT the person you are calling. When the person picks up, introduce yourself as Sutando.`,
			callSession.purpose ? `Purpose of this call: "${callSession.purpose}"` : '',
			'Be natural, warm, and conversational. Have a full conversation — do NOT rush to hang up.',
			'Ask follow-up questions to get complete information.',
			'When the conversation is done and both sides have said goodbye, call the hang_up tool to end the call.',
			'Keep responses to 1-2 sentences.',
			availableTools.length > 0 ? `You have these tools available: ${availableTools.join(', ')}. Use them when relevant to help the caller.` : '',
			'You can ONLY fulfill the stated purpose of this call. If the person asks you to do something outside your available tools, politely decline.',
		].filter(Boolean).join('\n');
	} else {
		const isInbound = callSession.purpose === 'inbound';
		// Load Stand identity (voice-context.txt excluded — can confuse phone identity)
		const standId = (() => { try { const si = JSON.parse(readFileSync('stand-identity.json', 'utf-8')); return si.name ? `Your Stand name is ${si.name}. When asked your name, say "I'm Sutando — ${si.name}."` : ''; } catch { return ''; } })();
		instructions = [
			'You are Sutando, a personal AI assistant.',
			standId,
			// Identity & greeting — based on owner vs verified vs unverified
			isInbound && callSession.isOwner
				? `Your owner${OWNER_NAME ? ` ${OWNER_NAME}` : ''} is calling you. YOU are Sutando — the AI assistant. The person on the phone is your OWNER, a human. Do NOT confuse yourself with the caller. You have full capabilities — use the work tool for anything: check the screen, send emails, look things up, make calls, browse the web, or check results of previous tasks. Say exactly: "Hi, this is Sutando. How can I help?" then WAIT for them to speak. Do NOT say anything else before they talk. Do NOT make up tasks, scenarios, or pretend you were doing something.${(() => { const ctx = getSafeContext(); return ctx ? `\n\nRecent voice conversation (for context — do NOT repeat or bring up unless asked):\n${ctx}` : ''; })()}`
				: isInbound && callSession.callerVerified
				? 'A verified caller is calling you. Be helpful and conversational. You can look up meeting IDs and check the time. You CAN answer general knowledge questions, do translations, and have conversations. You cannot access files, control the screen, or delegate tasks. Say "Hello, this is Sutando. How can I help?"'
				: isInbound
				? 'Someone is calling you. Be helpful and conversational. You CAN answer general knowledge questions, do translations, and have conversations — but you cannot access files, control the screen, or delegate tasks. Greet them with "Hello, this is Sutando. How can I help?"'
				: callSession.isOwner
				? `You are calling your owner${OWNER_NAME ? ` ${OWNER_NAME}` : ''}. The person who picks up IS your owner.`
				: 'You initiated this call on behalf of your owner.',
			callSession.purpose && !isInbound ? `Purpose of this call: "${callSession.purpose}"` : '',
			// Context only for outbound calls to owner
			!isInbound && callSession.isOwner ? (() => { const ctx = getSafeContext(); return ctx ? `\nRecent context (for reference — use only if relevant):\n${ctx}` : ''; })() : '',
			'Be natural, warm, and conversational. Keep responses to 1-2 sentences.',
			'NEVER say "I\'m back", "Welcome back", "Working on it", or "task is queued". If the conversation resumes after a pause, just continue naturally from where you left off.',
			'When the other person wants to wrap up, say a warm goodbye, then call the hang_up tool to end the call.',
		];

		// Owner-only sections
		if (callSession.isOwner) {
			instructions.push(
				'',
				'## How to think',
				'Before acting, gather what you need. Before delegating, give them what they need.',
				'call_contact makes a phone call — the message you pass is ALL the other person knows. If you pass no message or a vague one, they will be confused.',
				'If you need info from multiple tools, call them in sequence — get the results first, then act.',
				'',
				'## Tools',
				`These tools are instant (use them directly, NOT through work): ${inlineTools.map(t => t.name).join(', ')}. Use work for everything else.`,
				'TOOL EXCLUSIVITY: If an inline tool can handle the request, use ONLY the inline tool. NEVER also call work. They are mutually exclusive — calling both causes duplicate responses. Only use work when no inline tool fits.',
				'You can make concurrent calls — stay on the line while calling someone else.',
				'',
				'',
				'## Known info',
				(() => { try { const url = execSync('git remote get-url origin', { timeout: 2_000 }).toString().trim().replace(/\.git$/, ''); return `Sutando GitHub repo: ${url}`; } catch { return ''; } })(),
				'',
				'## Style',
				'Be natural, warm, and conversational. Keep responses to 1-2 sentences.',
				'When the conversation is done and both sides have said goodbye, call the hang_up tool.',
			);
		}

		instructions = instructions.filter(Boolean).join('\n');
	}

	// Grounding
	if (callSession.isOwner) {
		instructions += '\n\nNEVER fabricate specific details. If you don\'t know it, use the work tool to look it up.';
	}

	const tools: ToolDefinition[] = [];

	// All calls get hang_up — the model calls it when both sides have said goodbye.
	// Replaces the old separate Gemini classifier approach.
	tools.push({
		name: 'hang_up',
		description:
			'End this phone call. Call this ONLY when the CALLER has clearly and explicitly said goodbye ' +
			'(bye, talk to you later, have a good one, etc). Do NOT hang up if: ' +
			'(1) only you said goodbye but the caller did not, ' +
			'(2) the caller\'s speech is unclear or garbled, ' +
			'(3) you just completed an action — always report the result first and wait for the caller to respond, ' +
			'(4) the conversation is still going. When in doubt, do NOT hang up — ask if they need anything else.',
		parameters: z.object({}),
		execution: 'inline',
		async execute() {
			if (callSession.pendingTasks > 0) {
				console.log(`${ts()} [Phone] hang_up blocked — ${callSession.pendingTasks} task(s) still pending`);
				return { status: 'blocked', reason: `Cannot hang up — ${callSession.pendingTasks} task(s) still in progress. Wait for them to finish.` };
			}
			if (!callSession.hangingUp) {
				callSession.hangingUp = true;
				console.log(`${ts()} [Phone] hang_up tool called — ending ${callSession.callSid}`);
				setTimeout(() => {
					twilioHangup(callSession.callSid).catch(e =>
						console.error(`${ts()} [Phone] hangup error:`, e)
					);
				}, 2000); // 2s delay to let last audio flush
			}
			return { status: 'hanging up' };
		},
	});

	// Meeting calls get send_dtmf for IVR navigation — sends DTMF as audio through the WebSocket
	// (Used by Zoom; Google Meet uses TwiML <Play digits> via /twilio/meeting-ivr instead)
	if (callSession.isMeeting) {
		tools.push({
			name: 'send_dtmf',
			description:
				'Send DTMF tones (phone keypad digits) into the call. Use this to navigate automated phone menus (IVR). ' +
				'Send digits like "1234567890#" for a meeting ID, "919528#" for a passcode, or "#" to skip.',
			parameters: z.object({
				digits: z.string().describe('DTMF digits to send (0-9, #, *). Example: "1234567890#"'),
			}),
			execution: 'inline',
			async execute(args) {
				const { digits } = args as { digits: string };
				console.log(`${ts()} [DTMF] Sending via audio: ${digits} for ${callSession.callSid}`);
				try {
					// DTMF frequency pairs (Hz)
					const DTMF_FREQS: Record<string, [number, number]> = {
						'1': [697, 1209], '2': [697, 1336], '3': [697, 1477],
						'4': [770, 1209], '5': [770, 1336], '6': [770, 1477],
						'7': [852, 1209], '8': [852, 1336], '9': [852, 1477],
						'*': [941, 1209], '0': [941, 1336], '#': [941, 1477],
					};
					const SAMPLE_RATE = 8000; // mu-law 8kHz
					const TONE_MS = 200;      // duration per digit
					const GAP_MS = 100;       // silence between digits

					// Generate mu-law encoded DTMF audio for all digits
					const allSamples: number[] = [];
					for (const digit of digits) {
						const freqs = DTMF_FREQS[digit];
						if (!freqs) continue;
						const [f1, f2] = freqs;
						const toneSamples = Math.floor(SAMPLE_RATE * TONE_MS / 1000);
						const gapSamples = Math.floor(SAMPLE_RATE * GAP_MS / 1000);
						// Tone
						for (let i = 0; i < toneSamples; i++) {
							const t = i / SAMPLE_RATE;
							const sample = Math.floor(16000 * (Math.sin(2 * Math.PI * f1 * t) + Math.sin(2 * Math.PI * f2 * t)) / 2);
							allSamples.push(pcmToMulaw(sample));
						}
						// Gap (silence)
						for (let i = 0; i < gapSamples; i++) allSamples.push(0xFF); // mu-law silence
					}

					// Send as Twilio media message through the WebSocket
					const payload = Buffer.from(allSamples).toString('base64');
					const msg = JSON.stringify({
						event: 'media',
						streamSid: callSession.streamSid,
						media: { payload },
					});
					callSession.twilioWs.send(msg);
					console.log(`${ts()} [DTMF] Sent ${digits.length} digits (${allSamples.length} samples) via audio`);
					return { status: 'sent', digits };
				} catch (err) {
					console.error(`${ts()} [DTMF] error:`, err);
					return { error: `DTMF failed: ${err instanceof Error ? err.message : err}` };
				}
			},
		});
	}

	// --- 3-tier access control ---
	// Owner (isOwner): work tool + all inline tools + get_task_status
	// Verified (!isOwner + callerVerified): any-caller tools + configurable tools
	// Unverified: any-caller tools only (volume, brightness, time, toggle_tasks)
	// Access is determined entirely by the caller/callee phone number, not call type.

	// Any-caller tools available to everyone (including unverified)
	tools.push(...anyCallerTools);

	if (callSession.isOwner) {
		// Owner: full access
		tools.push({
			name: 'work',
			description:
				'Do the work. Call this for action requests — calling someone, looking something up, ' +
				'sending a message, scheduling, researching, editing files, generating images. ' +
				'Do NOT use this for scrolling or switching apps — use the scroll and switch_app tools instead.',
			parameters: z.object({
				task: z.string().describe('Full description of the task to perform'),
			}),
			execution: 'inline',
			pendingMessage: 'The task is being processed. Wait silently for the result.',
			timeout: 120_000,
			async execute(args) {
				const { task } = args as { task: string };
				return delegateTask(callSession, task);
			},
		});
		tools.push(...inlineTools);
		tools.push({
			name: 'get_task_status',
			description: 'Check whether a delegated task is still in progress. Use when someone asks "are you still working on that?"',
			parameters: z.object({}),
			execution: 'inline',
			async execute() {
				const REPO_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
				const tasksDir = join(REPO_DIR, 'tasks');
				const resultsDir = join(REPO_DIR, 'results');
				try {
					const taskFiles = readdirSync(tasksDir).filter(f => f.startsWith('task-phone-'));
					const pendingTasks = taskFiles.filter(f => !existsSync(join(resultsDir, f)));
					return {
						inProgress: pendingTasks.length > 0,
						pendingCount: pendingTasks.length,
						pendingTasks: pendingTasks.map(f => f.replace('.txt', '')).slice(0, 3),
					};
				} catch { return { inProgress: false, pendingCount: 0 }; }
			},
		});
	} else if (callSession.callerVerified) {
		// Verified caller: configurable tools (in addition to any-caller tools above)
		tools.push(...configurableTools);
	}

	return {
		name: 'phone',
		instructions,
		tools,
		googleSearch: true,
		greeting: callSession.isMeeting
			? ''  // No greeting for meetings — listen to IVR first
			: callSession.purpose === 'inbound'
			? 'Hi, this is Sutando. How can I help?'
			: 'Hi, this is Sutando calling.',
	};
}

// --- Create VoiceSession for a call ---
// Each Twilio call gets its own bodhi VoiceSession on a dynamic internal port.
// Audio bypasses ClientTransport's WebSocket — we override handleAudioOutput()
// and call handleAudioFromClient() directly for lower latency.

async function createCallSession(params: {
	callSid: string;
	streamSid: string;
	purpose: string;
	twilioWs: WebSocket;
	callerNumber: string;
	callerVerified: boolean;
	isOwner: boolean;
	isMeeting: boolean;
	meetingId?: string;
	passcode?: string;
	parentCallSid?: string;
}): Promise<CallSession> {
	const bodhiPort = nextBodhiPort++;

	const callSession: CallSession = {
		...params,
		voiceSession: null as unknown as VoiceSession,
		bodhiPort,
		childCallSids: [],
		hangingUp: false,
		pendingTasks: 0,
		transcript: [],
		resultQueue: [],
	};

	const agent = buildAgent(callSession);

	const session = new VoiceSession({
		sessionId: `phone_${params.callSid}`,
		userId: 'phone_user',
		apiKey: GEMINI_API_KEY,
		agents: [agent],
		initialAgent: 'phone',
		port: bodhiPort,
		host: '127.0.0.1',
		model: google('gemini-2.5-flash'),
		geminiModel: 'gemini-2.5-flash-native-audio-preview-12-2025',
		googleSearch: true,
		speechConfig: { voiceName: 'Aoede' },
		hooks: {
			onToolCall: (e) => console.log(`${ts()} [Tool] ${e.toolName} (${e.execution})`),
			onToolResult: (e) => console.log(`${ts()} [Tool] result: ${e.toolCallId} (${e.status}, ${e.durationMs}ms)`),
			onError: (e) => console.error(`${ts()} [Error] ${e.component}: ${e.error.message} (${e.severity})`),
		},
	});

	callSession.voiceSession = session;

	// Start VoiceSession (creates ClientTransport WebSocket server on bodhiPort)
	await session.start();
	console.log(`${ts()} [Bodhi] VoiceSession started on port ${bodhiPort} for ${params.callSid}`);

	// [Outbound audio chain] Override to send Gemini audio directly to Twilio
	// Bypasses ClientTransport's internal WebSocket for lower latency
	const sessionAny = session as any;
	let isReplaying = false; // suppress audio during reconnect replay
	let turnCountBeforeDisconnect = 0; // track turns to know when replay is done
	let _isRecordingMuted: (() => boolean) | null = null;
	import('../../../src/browser-tools.js').then(bt => { _isRecordingMuted = bt.isRecordingMuted; }).catch(() => {});
	sessionAny.handleAudioOutput = (data: string) => {
		sessionAny.notificationQueue?.markAudioReceived?.();
		if (isReplaying || _isRecordingMuted?.()) return;
		if (params.twilioWs.readyState === WebSocket.OPEN) {
			const pcmBuf = Buffer.from(data, 'base64');
			const mulawBuf = pcm24kToMulaw8k(pcmBuf);
			const CHUNK = 160;
			for (let offset = 0; offset < mulawBuf.length; offset += CHUNK) {
				const chunk = mulawBuf.subarray(offset, Math.min(offset + CHUNK, mulawBuf.length));
				params.twilioWs.send(JSON.stringify({
					event: 'media',
					streamSid: params.streamSid,
					media: { payload: chunk.toString('base64') },
				}));
			}
		}
	};

	// Set up recording hooks (tool trigger, narration push, etc)
	import('../../../src/browser-tools.js').then(bt => bt.setupRecordingHooks(session)).catch(() => {});

	// Track transcripts via event bus + run goodbye detection
	// Use a processed count per-session that resets on reconnect to avoid duplicates.
	let lastProcessedIdx = 0;
	session.eventBus.subscribe('turn.end', () => {
		const items = session.conversationContext.items;
		// Detect end of reconnect replay: when items catch up to pre-disconnect count
		if (isReplaying && items.length >= turnCountBeforeDisconnect) {
			console.log(`${ts()} [Phone] replay complete (${items.length}/${turnCountBeforeDisconnect} turns) — unmuting`);
			isReplaying = false;
			import('../../../src/browser-tools.js').then(bt => bt.onReconnect(session)).catch(() => {});
		}
		// Guard: if items shrunk (reconnect reset context), re-scan from start but skip already-seen text
		if (items.length < lastProcessedIdx) lastProcessedIdx = 0;
		const lastTranscriptText = callSession.transcript.length > 0
			? callSession.transcript[callSession.transcript.length - 1].text : null;
		for (const item of items.slice(lastProcessedIdx)) {
			// Skip if this exact text was the last thing we recorded (dedup across reconnects)
			if (item.content === lastTranscriptText) continue;
			if (item.role === 'user') {
				callSession.transcript.push({ role: 'caller', text: item.content });
			} else if (item.role === 'assistant') {
				callSession.transcript.push({ role: 'sutando', text: item.content });
			}
		}
		lastProcessedIdx = items.length;

		// Goodbye detection is handled by the model's hang_up tool — no classifier needed.

		// Drain queued task results — inject after Gemini finishes speaking
		if (callSession.resultQueue.length > 0) {
			const queued = callSession.resultQueue.splice(0);
			for (const item of queued) {
				try {
					(callSession.voiceSession as any).transport.sendContent([
						{ role: 'user', text: item.text },
					], true);
				} catch (e) {
					console.log(`${ts()} [Task] inject failed: ${e}`);
				}
			}
		}
	});

	// Trigger client connected (so VoiceSession sends greeting and starts Gemini)
	sessionAny.handleClientConnected();
	// Suppress greeting on reconnect — mute the first few seconds of audio after reconnect
	let firstGreetingSent = false;
	const origSendGreeting = sessionAny.sendGreeting?.bind(sessionAny);
	if (origSendGreeting) {
		sessionAny.sendGreeting = (...args: any[]) => {
			if (firstGreetingSent) {
				console.log(`${ts()} [Phone] suppressed reconnect greeting`);
				return;
			}
			firstGreetingSent = true;
			return origSendGreeting(...args);
		};
	}

	// Auto-reconnect when Gemini transport closes (e.g. 1008 crash).
	// We bypass ClientTransport, so VoiceSession's built-in reconnect won't trigger.
	// Override handleTransportClose (not transport.onClose) because transport.onClose
	// gets re-bound when transport.connect() is called during reconnection.
	const origHandleTransportClose = sessionAny.handleTransportClose.bind(sessionAny);
	sessionAny.handleTransportClose = (code?: number, reason?: string) => {
		console.log(`${ts()} [Phone] transport closed: code=${code} reason=${reason}`);
		// Call original (transitions state to CLOSED)
		origHandleTransportClose(code, reason);
		// Trigger reconnect
		if (!callSession.hangingUp && activeCalls.has(callSession.callSid)) {
			setTimeout(() => {
				if (!callSession.hangingUp && activeCalls.has(callSession.callSid)) {
					console.log(`${ts()} [Phone] reconnecting Gemini for ${callSession.callSid}`);
					isReplaying = true; // mute audio while Gemini replays history
					turnCountBeforeDisconnect = session.conversationContext.items.length;
					console.log(`${ts()} [Phone] replay suppression: ${turnCountBeforeDisconnect} turns to replay`);
					sessionAny.handleClientConnected();
					// Fallback: unmute after max(10s, 2s per turn) in case turn detection fails
					const fallbackMs = Math.max(10000, turnCountBeforeDisconnect * 2000);
					const fallbackTimer = setTimeout(() => {
						if (isReplaying) {
							console.log(`${ts()} [Phone] replay suppression fallback (${fallbackMs}ms)`);
							isReplaying = false;
							import('../../../src/browser-tools.js').then(bt => bt.onReconnect(session)).catch(() => {});
						}
					}, fallbackMs);
				}
			}, 1500);
		}
	};

	return callSession;
}

// --- Call cleanup: finalize transcript, inject child results into parent, cascade hangups ---
// [Concurrent call chain] When a child call ends, its transcript is injected into the parent
// Gemini session so it can summarize the results to the caller.

function cleanupCall(callSid: string): void {
	const session = activeCalls.get(callSid);
	if (!session) return;
	activeCalls.delete(callSid);
	if (session.meetingId) pendingMeetingJoins.delete(session.meetingId);

	import('../../../src/browser-tools.js').then(bt => bt.onCallEnd()).catch(() => {});

	// Close VoiceSession
	session.voiceSession.close('call_ended').catch(e =>
		console.error(`${ts()} [Bodhi] close error:`, e)
	);

	// Save transcript
	if (session.transcript.length > 0) {
		const formatted = session.transcript.map(t => {
			const label = t.role === 'sutando' ? 'Sutando' : 'Recipient';
			return `${label}: ${t.text}`;
		}).join('\n');
		const data = JSON.stringify({ callSid, transcript: formatted, timestamp: new Date().toISOString() });
		writeFileSync(join(CALLS_DIR, 'latest-result.json'), data);
		appendFileSync(join(CALLS_DIR, 'calls.jsonl'), data + '\n');
	}
	// Append to shared conversation.log for cross-agent context
	if (session.transcript.length > 0) {
		const logPath = join(WORKSPACE_DIR, 'conversation.log');
		const callType = session.meetingId ? `meeting-${session.meetingId}` : `call-${session.callerNumber || 'unknown'}`;
		for (const t of session.transcript) {
			const role = t.role === 'sutando' ? 'phone-agent' : 'phone-caller';
			const line = `${new Date().toISOString()}|${role}|[${callType}] ${t.text.replace(/\n/g, ' ').slice(0, 200)}\n`;
			try { appendFileSync(logPath, line); } catch { /* best effort */ }
		}
	}
	console.log(`${ts()} [Phone] call finalized: ${callSid}`);

	// Auto-scan the latest call for issues (async, best effort)
	try {
		const scanScript = join(WORKSPACE_DIR, 'src', 'scan-call-logs.py');
		spawn('python3', [scanScript, '--last', '1', '--json'], { stdio: 'pipe', detached: true })
			.on('close', (code) => { if (code === 0) console.log(`${ts()} [Phone] call scan complete`); });
	} catch { /* best effort */ }

	// If top-level call (no parent) with a transcript, write a summary task for Claude to pick up
	if (!session.parentCallSid && session.transcript.length > 0) {
		const summaryTaskId = `task-summary-${Date.now()}`;
		const formatted = session.transcript.map(t => {
			const label = t.role === 'sutando' ? 'Sutando' : 'Caller';
			return `${label}: ${t.text}`;
		}).join('\n');
		const isMeeting = session.meetingId != null;
		const taskLines = [
			`id: ${summaryTaskId}`,
			`timestamp: ${new Date().toISOString()}`,
			`callSid: ${callSid}`,
			`caller: ${session.callerNumber || 'unknown'}`,
			`task: Summarize this ${isMeeting ? 'meeting (ID: ' + session.meetingId + ')' : 'phone call'}.`,
			`instructions:`,
			`  1. Write a structured summary: ## Key Topics, ## Decisions, ## Action Items, ## Notable Quotes`,
			`  2. Save to notes/meetings/${summaryTaskId}.md with YAML frontmatter (title, date, tags: [meeting])`,
			`  3. Send a concise version (3-5 bullet points) to Discord DM (channel 1485370959870431433)`,
			`  4. Write result to results/${summaryTaskId}.txt so voice agent can speak it`,
			`transcript:`,
			formatted,
		];
		const summaryContent = taskLines.join('\n') + '\n';
		writeFileSync(join(TASKS_DIR, `${summaryTaskId}.txt`), summaryContent);
		console.log(`${ts()} [Summary] wrote summary task: ${summaryTaskId}`);
	}

	// If child call, inject transcript into parent
	if (session.parentCallSid) {
		const parent = activeCalls.get(session.parentCallSid);
		if (parent) {
			parent.childCallSids = parent.childCallSids.filter(s => s !== callSid);
			const transcriptText = session.transcript.length > 0
				? session.transcript.map(t => `${t.role === 'sutando' ? 'Sutando' : 'Other person'}: ${t.text}`).join('\n')
				: '(No conversation recorded.)';
			console.log(`${ts()} [Concurrent] child ${callSid} ended — injecting into parent ${session.parentCallSid}`);
			try {
				(parent.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[Concurrent call ended]\nThe call to ${session.callerNumber || 'the other person'} has ended:\n\n${transcriptText}\n\nReport the results directly. Say "they said..." not "I'll let the owner know...".` },
				], true);
			} catch (e) {
				console.log(`${ts()} [Concurrent] inject failed: ${e}`);
			}
		}
	}

	// If parent, clean up children
	for (const childSid of session.childCallSids) {
		twilioHangup(childSid).catch(e => console.error(`${ts()} [Concurrent] child hangup error:`, e));
	}
}

// --- Twilio REST API ---
// Used by goodbye detection and API endpoints to control calls.

// [Goodbye chain] Final step — tells Twilio to end the call
async function twilioHangup(callSid: string): Promise<void> {
	const auth = Buffer.from(`${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}`).toString('base64');
	await fetch(`https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${callSid}.json`, {
		method: 'POST',
		body: new URLSearchParams({ Status: 'completed' }).toString(),
		headers: { Authorization: `Basic ${auth}`, 'Content-Type': 'application/x-www-form-urlencoded' },
	});
}

// [Concurrent call chain] Creates outbound Twilio call — used by /call, /concurrent-call, /meeting
async function twilioCall(to: string, twimlUrl: string, sendDigits?: string): Promise<string> {
	const auth = Buffer.from(`${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}`).toString('base64');
	const body = new URLSearchParams({
		To: to, From: TWILIO_PHONE_NUMBER, Url: twimlUrl,
		StatusCallback: `${WEBHOOK_BASE_URL}/twilio/status`,
		StatusCallbackEvent: 'initiated ringing answered completed', Timeout: '60',
	});
	if (sendDigits) body.set('SendDigits', sendDigits);
	const res = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json`, {
		method: 'POST', body: body.toString(),
		headers: { Authorization: `Basic ${auth}`, 'Content-Type': 'application/x-www-form-urlencoded' },
	});
	if (!res.ok) throw new Error(`Twilio error ${res.status}: ${await res.text()}`);
	return ((await res.json()) as { sid: string }).sid;
}

// --- HTTP helpers ---

async function readBody(req: IncomingMessage): Promise<string> {
	const chunks: Buffer[] = [];
	for await (const chunk of req) chunks.push(chunk as Buffer);
	return Buffer.concat(chunks).toString('utf-8');
}

function json(res: ServerResponse, status: number, data: unknown): void {
	res.writeHead(status, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
	res.end(JSON.stringify(data));
}

function twimlResponse(res: ServerResponse, xml: string): void {
	res.writeHead(200, { 'Content-Type': 'text/xml' });
	res.end(xml);
}

// --- Port cleanup ---

function killPortOccupant(port: number): void {
	try {
		const output = execSync(`lsof -ti :${port}`, { encoding: 'utf-8' }).trim();
		if (output) {
			for (const pid of output.split('\n').filter(Boolean)) {
				if (pid !== String(process.pid)) {
					console.log(`${ts()} [Setup] killing PID ${pid} on port ${port}`);
					execSync(`kill -9 ${pid}`);
				}
			}
		}
	} catch {}
}

// --- ngrok ---

let ngrokProcess: ChildProcess | null = null;

async function startNgrokCli(port: number): Promise<string> {
	try { execSync('pkill -f "ngrok http"', { stdio: 'ignore' }); } catch {}
	await new Promise(r => setTimeout(r, 500));
	ngrokProcess = spawn('ngrok', ['http', String(port), '--log=stdout'], {
		stdio: ['ignore', 'pipe', 'pipe'],
		env: { ...process.env, NGROK_AUTHTOKEN },
	});
	ngrokProcess.stderr?.on('data', (d: Buffer) => {
		const line = d.toString().trim();
		if (line) console.error(`${ts()} [ngrok] ${line}`);
	});
	const deadline = Date.now() + 15_000;
	while (Date.now() < deadline) {
		await new Promise(r => setTimeout(r, 500));
		try {
			const resp = await fetch('http://127.0.0.1:4040/api/tunnels');
			const data = await resp.json() as { tunnels: Array<{ public_url: string; proto: string }> };
			const tunnel = data.tunnels.find(t => t.proto === 'https') ?? data.tunnels[0];
			if (tunnel?.public_url) return tunnel.public_url;
		} catch {}
	}
	throw new Error('ngrok tunnel did not start within 15s');
}

function cleanupNgrok(): void {
	if (ngrokProcess) { ngrokProcess.kill(); ngrokProcess = null; }
}

let WEBHOOK_BASE_URL = '';

async function waitForWebhook(): Promise<void> {
	if (WEBHOOK_BASE_URL) return;
	return new Promise(resolve => {
		const check = setInterval(() => {
			if (WEBHOOK_BASE_URL) { clearInterval(check); resolve(); }
		}, 100);
	});
}

// --- HTTP + WebSocket server ---
// Twilio requires an HTTP server for webhooks (call connect, status callbacks)
// and a WebSocket endpoint for real-time audio streaming (Media Streams).
// Claude also calls these endpoints to trigger actions (/call, /concurrent-call, /hangup).

const server = createServer(async (req, res) => {
	const url = new URL(req.url ?? '', `http://localhost:${PORT}`);
	const path = url.pathname;

	if (req.method === 'OPTIONS') {
		res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type' });
		res.end(); return;
	}

	try {
		if (path === '/health' && req.method === 'GET') {
			json(res, 200, { status: 'ok', activeCalls: activeCalls.size, webhookUrl: WEBHOOK_BASE_URL });

		} else if (path === '/call' && req.method === 'POST') {
			await waitForWebhook();
			const body = JSON.parse(await readBody(req)) as { to: string; message: string };
			if (!body.to || !body.message) { json(res, 400, { error: 'to and message required' }); return; }
			const twimlUrl = `${WEBHOOK_BASE_URL}/twilio/connect?purpose=${encodeURIComponent(body.message)}&to=${encodeURIComponent(body.to)}`;
			const sid = await twilioCall(body.to, twimlUrl);
			console.log(`${ts()} [Phone] call ${sid} → ${body.to}`);
			json(res, 200, { callSid: sid, to: body.to, status: 'calling' });

		} else if (path === '/concurrent-call' && req.method === 'POST') {
			await waitForWebhook();
			const body = JSON.parse(await readBody(req)) as { parentCallSid: string; to: string; purpose?: string };
			if (!body.parentCallSid || !body.to) { json(res, 400, { error: 'parentCallSid and to required' }); return; }
			const parent = activeCalls.get(body.parentCallSid);
			if (!parent) { json(res, 404, { error: 'Parent call not found' }); return; }
			const purpose = body.purpose ?? '';
			const twimlUrl = `${WEBHOOK_BASE_URL}/twilio/connect?purpose=${encodeURIComponent(purpose)}&to=${encodeURIComponent(body.to)}&parentCallSid=${encodeURIComponent(body.parentCallSid)}`;
			const childSid = await twilioCall(body.to, twimlUrl);
			parent.childCallSids.push(childSid);
			console.log(`${ts()} [Concurrent] child ${childSid} → ${body.to} (parent: ${body.parentCallSid})`);
			try {
				(parent.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[System: A concurrent call to ${body.to} is being made. Tell the caller the call is in progress.]` },
				], true);
			} catch (e) { console.log(`${ts()} [Concurrent] notify parent failed: ${e}`); }
			json(res, 200, { callSid: childSid, to: body.to, parentCallSid: body.parentCallSid, status: 'calling' });

		} else if (path === '/meeting-approve' && req.method === 'POST') {
			// Approve an active meeting call for task delegation (adds work tool mid-call).
			// Called by sutando-core after user confirms via Telegram/voice.
			const body = JSON.parse(await readBody(req)) as { callSid: string };
			if (!body.callSid) { json(res, 400, { error: 'callSid required' }); return; }
			const session = activeCalls.get(body.callSid);
			if (!session) { json(res, 404, { error: 'Call not found or already ended' }); return; }
			if (session.callerVerified) { json(res, 200, { status: 'already_verified' }); return; }

			session.callerVerified = true;
			VERIFIED_MEETINGS.add(session.meetingId ?? '');
			console.log(`${ts()} [Meeting] Approved: ${body.callSid} — adding work tool`);

			// Build the work tool and add it to the active session
			const workTool: ToolDefinition = {
				name: 'work',
				description:
					'Do the work. Call this for action requests — calling someone, looking something up, ' +
					'sending a message, scheduling, researching, editing files, generating images.',
				parameters: z.object({
					task: z.string().describe('Full description of the task to perform'),
				}),
				execution: 'inline',
				pendingMessage: 'The task is being processed. Wait silently for the result.',
				timeout: 120_000,
				async execute(args) {
					const { task } = args as { task: string };
					return delegateTask(session, task);
				},
			};

			// Register the work tool in bodhi's tool executor (so execute() fires)
			// and update Gemini's tool declarations via transport
			const sessionAny = session.voiceSession as any;
			try {
				if (sessionAny.toolExecutor) {
					sessionAny.toolExecutor.register([workTool]);
					console.log(`${ts()} [Meeting] Registered work tool in toolExecutor`);
				}
			} catch (e) { console.log(`${ts()} [Meeting] toolExecutor register failed: ${e}`); }

			// Update system instructions + tools on the transport (applied on next Gemini reconnect)
			const transport = (session.voiceSession as any).transport;
			const newInstructions = 'You are Sutando, an AI assistant in a meeting. You have full capabilities — make calls, look things up, send messages, take screenshots, and perform tasks using the work tool. Be natural, warm, and conversational. Keep responses to 1-2 sentences. Known URLs: "sutando agent repo" = https://github.com/sonichi/sutando';
			try {
				transport.updateSystemInstruction(newInstructions);
				// Also update tools on the transport for reconnect persistence
				const currentTools = transport.config?.tools ?? [];
				const hasWork = currentTools.some((t: any) => t.name === 'work');
				if (!hasWork) transport.updateTools([...currentTools, workTool]);
				console.log(`${ts()} [Meeting] Updated transport instructions + tools`);
			} catch (e) { console.log(`${ts()} [Meeting] transport update failed: ${e}`); }

			// Force a Gemini session update now (not just on reconnect)
			try {
				transport.sendContent([
					{ role: 'user', text: '[System: The meeting owner has approved task delegation. You now have the work tool. When someone asks you to do something — take a screenshot, make a call, look something up — use the work tool. You are no longer limited to notes. Do NOT say you cannot do something — use the work tool instead.]' },
				], true);
			} catch (e) { console.log(`${ts()} [Meeting] approve notify failed: ${e}`); }

			json(res, 200, { callSid: body.callSid, status: 'approved', meetingId: session.meetingId });

		} else if (path === '/hangup' && req.method === 'POST') {
			const body = JSON.parse(await readBody(req)) as { callSid: string };
			if (!body.callSid) { json(res, 400, { error: 'callSid required' }); return; }
			await twilioHangup(body.callSid);
			json(res, 200, { callSid: body.callSid, status: 'hanging_up' });

		} else if (path === '/meeting' && req.method === 'POST') {
			await waitForWebhook();
			const body = JSON.parse(await readBody(req)) as { meetingId: string; dialIn?: string; passcode?: string; platform?: string };
			if (!body.meetingId) { json(res, 400, { error: 'meetingId required' }); return; }
			// Prevent duplicate joins — check activeCalls and pending joins
			const meetingDigits = body.meetingId.replace(/\D/g, '');
			const existingMeeting = [...activeCalls.values()].find(c => c.isMeeting && c.meetingId === meetingDigits);
			if (existingMeeting || pendingMeetingJoins.has(meetingDigits)) {
				console.log(`${ts()} [Meeting] already in meeting ${body.meetingId} (${existingMeeting?.callSid ?? 'pending'}) — skipping`);
				json(res, 200, { callSid: existingMeeting?.callSid ?? 'pending', meetingId: body.meetingId, status: 'already_joined' });
				return;
			}
			pendingMeetingJoins.add(meetingDigits);
			setTimeout(() => pendingMeetingJoins.delete(meetingDigits), 30_000); // clear after 30s
			const dialIn = body.dialIn ?? '+12532158782';
			const digits = body.meetingId.replace(/\D/g, '');
			const passcode = body.passcode?.replace(/\D/g, '') ?? '';
			const platform = (body.platform ?? 'zoom').toLowerCase(); // 'zoom' | 'meet' | 'teams'
			const originalId = body.meetingId.trim();
			const connectUrl = `${WEBHOOK_BASE_URL}/twilio/connect?meeting=true&meetingId=${encodeURIComponent(originalId)}&passcode=${encodeURIComponent(passcode)}`;

			let sid: string;
			try {
			if (platform === 'meet' || platform === 'google-meet' || platform === 'google meet' || platform === 'gmeet') {
				// Google Meet: route through /twilio/meeting-ivr which plays DTMF via TwiML <Play digits>
				const ivrUrl = `${WEBHOOK_BASE_URL}/twilio/meeting-ivr?meetingId=${encodeURIComponent(digits)}&passcode=${encodeURIComponent(passcode)}`;
				console.log(`${ts()} [Meeting] Google Meet: dialing ${dialIn} — TwiML IVR for PIN ${digits}`);
				sid = await twilioCall(dialIn, ivrUrl);
			} else {
				// Zoom + others: agent hears IVR and sends DTMF via audio WebSocket
				console.log(`${ts()} [Meeting] ${platform}: dialing ${dialIn} — agent will navigate IVR for meeting ${digits}`);
				sid = await twilioCall(dialIn, connectUrl);
			}
			} catch (err) {
				pendingMeetingJoins.delete(meetingDigits);
				throw err;
			}

			// If not pre-verified, request approval via task (Telegram/voice)
			const preVerified = VERIFIED_MEETINGS.has(originalId) || VERIFIED_MEETINGS.has(digits);
			if (!preVerified) {
				const taskId = `task-approve-${Date.now()}`;
				const REPO_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
				const taskContent = `id: ${taskId}\ntimestamp: ${new Date().toISOString()}\ntask: Sutando joined meeting ${originalId || digits} (${platform}) — call SID ${sid}. Ask the user on Telegram whether to enable task delegation for this meeting. If approved, POST to http://localhost:3100/meeting-approve with {"callSid":"${sid}"}. If denied or no response within 2 minutes, do nothing (notes-only mode).\n`;
				writeFileSync(join(REPO_DIR, 'tasks', `${taskId}.txt`), taskContent);
				console.log(`${ts()} [Meeting] Approval requested: ${taskId}`);
			} else {
				VERIFIED_MEETINGS.add(originalId).add(digits);
			}

			json(res, 200, { callSid: sid, meetingId: digits, dialIn, platform, verified: preVerified, status: 'joining' });

		} else if (path === '/twilio/meeting-ivr' && req.method === 'POST') {
			// Meeting IVR navigation via TwiML <Play digits> (not SendDigits).
			// This fires AFTER the call connects, so the IVR is ready to receive DTMF.
			const meetingId = url.searchParams.get('meetingId') ?? '';
			const passcode = url.searchParams.get('passcode') ?? '';
			const isZoom = url.searchParams.get('isZoom') === 'true';

			// After IVR navigation, redirect to /twilio/connect to start the audio stream
			const connectUrl = `${WEBHOOK_BASE_URL}/twilio/connect?meeting=true&meetingId=${encodeURIComponent(meetingId)}`;

			let twiml: string;
			if (isZoom) {
				// Zoom IVR: "Welcome to Zoom..." (5s) → "Enter meeting ID" → pause → "Enter passcode" → pause → "Press # to skip"
				// Longer initial pause to let the welcome message finish before sending digits
				twiml = `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Pause length="6"/>
  <Play digits="${meetingId}#"/>
  <Pause length="10"/>
  <Play digits="${passcode}#"/>
  <Pause length="8"/>
  <Play digits="#"/>
  <Pause length="3"/>
  <Redirect method="POST">${esc(connectUrl)}</Redirect>
</Response>`;
			} else {
				// Google Meet: just enter the PIN
				twiml = `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Pause length="5"/>
  <Play digits="${esc(passcode || meetingId)}#"/>
  <Pause length="3"/>
  <Redirect method="POST">${esc(connectUrl)}</Redirect>
</Response>`;
			}

			console.log(`${ts()} [Meeting IVR] Sending DTMF via TwiML for ${meetingId} (zoom=${isZoom})`);
			twimlResponse(res, twiml);

		} else if (path === '/twilio/connect' && req.method === 'POST') {
			const body = await readBody(req);
			const form = new URLSearchParams(body);
			const purpose = url.searchParams.get('purpose') ?? '';
			const isMeeting = url.searchParams.get('meeting') === 'true';
			const meetingId = url.searchParams.get('meetingId') ?? '';
			const parentCallSid = url.searchParams.get('parentCallSid') ?? '';
			const toParam = url.searchParams.get('to') ?? '';
			const callerNumber = form.get('From') ?? '';
			const stirVerstat = form.get('StirVerstat') ?? '';
			console.log(`${ts()} [Connect] purpose=${purpose} from=${callerNumber} stirVerstat=${stirVerstat} callSid=${form.get('CallSid') ?? '?'}`);

			const params = [`<Parameter name="purpose" value="${esc(purpose)}" />`];
			if (callerNumber) params.push(`<Parameter name="callerNumber" value="${esc(callerNumber)}" />`);
			if (stirVerstat) params.push(`<Parameter name="stirVerstat" value="${esc(stirVerstat)}" />`);
			const passcodeParam = url.searchParams.get('passcode') ?? '';
			if (isMeeting) {
				params.push(`<Parameter name="isMeeting" value="true" />`);
				params.push(`<Parameter name="meetingId" value="${esc(meetingId)}" />`);
				if (passcodeParam) params.push(`<Parameter name="passcode" value="${esc(passcodeParam)}" />`);
			}
			if (parentCallSid) params.push(`<Parameter name="parentCallSid" value="${esc(parentCallSid)}" />`);
			if (toParam) params.push(`<Parameter name="to" value="${esc(toParam)}" />`);

			twimlResponse(res, `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://${new URL(WEBHOOK_BASE_URL).host}/media-stream">
      ${params.join('\n      ')}
    </Stream>
  </Connect>
</Response>`);

		} else if (path === '/twilio/status' && req.method === 'POST') {
			const form = new URLSearchParams(await readBody(req));
			const sid = form.get('CallSid') ?? '';
			const status = form.get('CallStatus') ?? '';
			console.log(`${ts()} [Status] ${sid} → ${status}`);
			if (['completed', 'failed', 'no-answer', 'busy'].includes(status)) cleanupCall(sid);
			res.writeHead(200); res.end();

		} else {
			json(res, 404, { error: 'not found' });
		}
	} catch (err) {
		console.error(`${ts()} [Error]`, err);
		json(res, 500, { error: (err as Error).message });
	}
});

// --- WebSocket server for Twilio Media Streams ---
// [Inbound audio chain entry point] Twilio connects here when a call starts.
// Each connection: parse stream events → create VoiceSession → bridge audio.

const wss = new WebSocketServer({ server, path: '/media-stream' });

wss.on('connection', (ws: WebSocket) => {
	console.log(`${ts()} [WS] Twilio Media Stream connected`);

	let callSid = '';
	let callSession: CallSession | null = null;
	let mediaEventCount = 0;

	ws.on('message', async (data: Buffer) => {
		try {
			const msg = JSON.parse(data.toString());

			switch (msg.event) {
				case 'connected':
					console.log(`${ts()} [WS] stream connected`);
					break;

				case 'start': {
					callSid = msg.start.callSid;
					const streamSid = msg.start.streamSid;
					const cp = msg.start.customParameters ?? {};
					console.log(`${ts()} [WS] customParameters: ${JSON.stringify(cp)}`);
					const recipientNumber = cp.to ?? '';
					const callerNumber = cp.callerNumber ?? cp.From ?? '';
					const personOnLine = recipientNumber || callerNumber;
					const isMeeting = cp.isMeeting === 'true';
					const meetingId = cp.meetingId ?? '';

					// Access control: verified callers get OS access (task delegation).
					// Child calls inherit parent's verification (the parent authorized this call).
					// For meetings: check meeting ID against VERIFIED_MEETINGS.
					// Accepts original ID (e.g. "gbn-otgn-dex"), numeric PIN, or Zoom meeting ID.
					const stirVerstat = cp.stirVerstat ?? '';
					let callerVerified: boolean;
					if (isMeeting) {
						// Meetings must be explicitly verified — no permissive default.
						// Unverified meetings get notes-only mode (no work tool / task delegation).
						const numericId = meetingId.replace(/\D/g, '');
						callerVerified = VERIFIED_MEETINGS.has(meetingId) || (numericId !== meetingId && VERIFIED_MEETINGS.has(numericId));
					} else {
						const normalizedPerson = normalizePhone(personOnLine);
						callerVerified = VERIFIED_CALLERS.size === 0 || VERIFIED_CALLERS.has(normalizedPerson);
					}

					// Owner detection: number match + STIR/SHAKEN verification.
					// A spoofed caller ID will fail STIR/SHAKEN (StirVerstat != TN-Validation-Passed-A).
					// If StirVerstat is present and NOT A-level, downgrade owner to verified-only.
					const ownerNumbers = OWNER_NUMBER ? OWNER_NUMBER.split(',').map(n => normalizePhone(n.trim())) : [];
					const numberMatchesOwner = ownerNumbers.length > 0 && ownerNumbers.includes(normalizePhone(personOnLine));
					let isOwner: boolean;
					if (numberMatchesOwner && stirVerstat && stirVerstat !== 'TN-Validation-Passed-A') {
						// Number matches but caller ID not cryptographically verified — possible spoof
						isOwner = false;
						console.log(`${ts()} [Security] Owner number matched but StirVerstat=${stirVerstat} (not A-level) — downgrading to verified`);
					} else {
						isOwner = numberMatchesOwner || (!OWNER_NUMBER && callerVerified);
					}

					console.log(`${ts()} [WS] stream started: ${callSid}, meeting: ${isMeeting}, verified: ${callerVerified}, owner: ${isOwner}, stirVerstat: ${stirVerstat}, personOnLine: ${personOnLine}, normalized: ${normalizePhone(personOnLine)}, verifiedSet: ${[...VERIFIED_CALLERS].join(',')}`);

					try {
						callSession = await createCallSession({
							callSid,
							streamSid,
							purpose: cp.purpose ?? '',
							twilioWs: ws,
							callerNumber: recipientNumber || callerNumber,
							callerVerified,
							isOwner,
							isMeeting,
							meetingId: meetingId ? meetingId.replace(/\D/g, '') : undefined,
							passcode: cp.passcode || undefined,
							parentCallSid: cp.parentCallSid || undefined,
						});
						activeCalls.set(callSid, callSession);

						// Notify parent if child call
						if (cp.parentCallSid) {
							const parent = activeCalls.get(cp.parentCallSid);
							if (parent) {
								try {
									(parent.voiceSession as any).transport.sendContent([
										{ role: 'user', text: `[Concurrent call connected to ${recipientNumber || 'the other person'}. Transcript will follow when it ends.]` },
									], true);
								} catch (e) { console.log(`${ts()} [Concurrent] notify parent failed: ${e}`); }
							}
						}
					} catch (err) {
						console.error(`${ts()} [WS] Failed to create call session:`, err);
						ws.close();
					}
					break;
				}

				case 'media': {
					if (!callSession?.voiceSession) break;
					mediaEventCount++;
					if (mediaEventCount === 1 || mediaEventCount % 500 === 0) {
						console.log(`${ts()} [WS] media events: ${mediaEventCount}`);
					}

					// mu-law 8kHz → PCM 16kHz → feed directly to VoiceSession (bypass internal WebSocket)
					const audioData = Buffer.from(msg.media.payload, 'base64');
					const pcm16k = mulawTopcm16k(audioData);
					try {
						(callSession.voiceSession as any).handleAudioFromClient(pcm16k);
					} catch (e) {
						if (mediaEventCount % 100 === 0) {
							console.error(`${ts()} [WS] handleAudioFromClient error:`, e);
						}
					}
					break;
				}

				case 'stop':
					console.log(`${ts()} [WS] stream stopped: ${callSid}`);
					cleanupCall(callSid);
					break;
			}
		} catch (err) {
			console.error(`${ts()} [WS] message error:`, err);
		}
	});

	ws.on('close', () => {
		console.log(`${ts()} [WS] Twilio connection closed`);
		if (callSid) cleanupCall(callSid);
	});
	ws.on('error', (err) => console.error(`${ts()} [WS] error:`, err));
});

// --- Startup ---

async function start(): Promise<void> {
	killPortOccupant(PORT);
	await new Promise<void>(resolve => server.listen(PORT, '0.0.0.0', resolve));
	console.log(`${ts()} [Server] listening on port ${PORT}`);

	try {
		WEBHOOK_BASE_URL = await startNgrokCli(PORT);
		console.log(`\n╔════════════════════════════════════════════════════╗`);
		console.log(`║  Phone Server (bodhi VoiceSession)                 ║`);
		console.log(`╠════════════════════════════════════════════════════╣`);
		console.log(`║  Local:    http://localhost:${String(PORT).padEnd(27)}║`);
		console.log(`║  Tunnel:   ${WEBHOOK_BASE_URL.slice(0, 40).padEnd(40)}║`);
		console.log(`║  Phone:    ${TWILIO_PHONE_NUMBER.replace(/\d(?=\d{4})/g, '*').padEnd(40)}║`);
		console.log(`╠════════════════════════════════════════════════════╣`);
		console.log(`║  POST /call              — outbound call           ║`);
		console.log(`║  POST /concurrent-call   — child call (for Claude) ║`);
		console.log(`║  POST /hangup            — hang up a call          ║`);
		console.log(`║  POST /meeting           — join Zoom meeting       ║`);
		console.log(`║  GET  /health            — status check            ║`);
		console.log(`╚════════════════════════════════════════════════════╝\n`);
	} catch (err) {
		console.error(`${ts()} [ngrok] Failed:`, err);
		process.exit(1);
	}
}

process.on('SIGINT', () => { cleanupNgrok(); process.exit(0); });
process.on('SIGTERM', () => { cleanupNgrok(); process.exit(0); });
process.on('uncaughtException', (err) => { console.error(`${ts()} [FATAL]`, err); cleanupNgrok(); process.exit(1); });
process.on('unhandledRejection', (err) => { console.error(`${ts()} [FATAL]`, err); cleanupNgrok(); process.exit(1); });

start().catch(err => { console.error('Fatal:', err); cleanupNgrok(); process.exit(1); });
