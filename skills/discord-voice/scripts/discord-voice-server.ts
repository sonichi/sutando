#!/usr/bin/env npx tsx
/**
 * Discord Voice Sidecar — bodhi VoiceSession over plain audio-chunk WebSocket
 *
 * Companion to `discord-voice-bridge.py`. The Python bridge owns the Discord
 * voice channel (capture + playback); this TS sidecar owns the Gemini Live
 * stack via bodhi's `VoiceSession`, exactly the same way
 * `skills/phone-conversation/scripts/conversation-server.ts` does for Twilio.
 *
 * ## Audio chain
 *
 *   Discord user → Python bridge → [PCM 16k mono base64 JSON WS] → this server
 *     → VoiceSession.handleAudioFromClient(buf) → Gemini Live
 *
 *   Gemini → VoiceSession.handleAudioOutput override → [PCM 24k mono base64 JSON WS]
 *     → Python bridge → Discord voice channel
 *
 * ## WS protocol (server <-> python bridge, one bridge per server instance)
 *
 *   client → server: {"type":"hello","guild":"...","channel":"..."}              (optional, logged)
 *   client → server: {"type":"audio","pcm":"<base64 PCM s16le 16kHz mono>"}     (per chunk)
 *   client → server: {"type":"bye"}                                              (clean shutdown)
 *   server → client: {"type":"audio","pcm":"<base64 PCM s16le 24kHz mono>"}     (Gemini speech)
 *   server → client: {"type":"transcript","role":"user|assistant","text":"..."}  (text mirror)
 *
 * Tool wiring (work / inlineTools / ownerOnlyTools / coreDocumentedSkills /
 * vision attach) mirrors conversation-server.ts. No Twilio-specific code:
 * no CallSession registry, no STIR/SHAKEN, no ngrok, no DTMF, no concurrent-call.
 */

// Load .env from the project root (3 levels up from this script), not cwd.
import { config as _dotenvConfig } from 'dotenv';
_dotenvConfig({ path: new URL('../../../.env', import.meta.url).pathname, override: true });

import { createServer } from 'node:http';
import { mkdirSync, writeFileSync, appendFileSync, existsSync, readFileSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { VoiceSession, type ToolDefinition, type MainAgent } from 'bodhi-realtime-agent';
import { WebSocketServer, WebSocket } from 'ws';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import {
	inlineTools,
	ownerOnlyTools,
	configurableTools,
	coreDocumentedSkills,
} from '../../../src/inline-tools.js';

// --- Config ---

const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? '';
const PORT = Number(process.env.DISCORD_VOICE_PORT) || 3200;
const WORKSPACE_DIR =
	process.env.SUTANDO_WORKSPACE ||
	join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const DATA_DIR = join(WORKSPACE_DIR, 'data');
const RESULTS_DIR = process.env.DISCORD_VOICE_RESULTS_DIR || join(WORKSPACE_DIR, 'results');
const TASKS_DIR = join(WORKSPACE_DIR, 'tasks');
const TASK_POLL_INTERVAL_MS = 500;
const TASK_POLL_TIMEOUT_MS = 300_000;
const OWNER_NAME = process.env.owner ?? '';

// Model configuration — match conversation-server.ts defaults so the same
// .env tuning applies to both surfaces.
const VOICE_MODEL = process.env.VOICE_MODEL || 'gemini-2.5-flash';
const VOICE_NATIVE_AUDIO_MODEL =
	process.env.VOICE_NATIVE_AUDIO_MODEL || 'gemini-3.1-flash-live-preview';

// Discord voice users always have OS access — voice channel membership is the
// gate (managed by the Python bridge / Discord permissions). The bridge will
// only connect for owner-authorized channels.
const TREAT_AS_OWNER = (process.env.DISCORD_VOICE_OWNER ?? 'true') !== 'false';

if (!GEMINI_API_KEY) {
	console.error('Error: GEMINI_API_KEY required');
	process.exit(1);
}

mkdirSync(DATA_DIR, { recursive: true });
mkdirSync(RESULTS_DIR, { recursive: true });
mkdirSync(TASKS_DIR, { recursive: true });

const ts = () => new Date().toISOString().slice(11, 23);
const google = createGoogleGenerativeAI({ apiKey: GEMINI_API_KEY });

// --- Lazy vision attach (mirrors conversation-server) -----------------------

let _setVisionSession: ((s: unknown) => void) | null = null;
let _priorVisionSession: unknown = undefined;
async function attachVisionToSession(session: unknown): Promise<void> {
	try {
		if (!_setVisionSession) {
			const m = await import('../../../src/vision-tools.js');
			_setVisionSession = m.setVisionSession;
			_priorVisionSession = null;
		}
		_setVisionSession(session);
	} catch {}
}
function detachVisionFromSession(): void {
	try { _setVisionSession?.(_priorVisionSession ?? null); } catch {}
}

// --- Conversation log -------------------------------------------------------

const LOG_PATH = join(
	DATA_DIR,
	`discord-voice-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`,
);

function logLine(role: 'user' | 'assistant' | 'system', text: string, extra: Record<string, unknown> = {}): void {
	try {
		appendFileSync(
			LOG_PATH,
			JSON.stringify({ timestamp: new Date().toISOString(), role, text, ...extra }) + '\n',
		);
	} catch {}
}

// --- Active session (Discord bridge is single-tenant: one voice channel
// at a time per sidecar process). If a second WS connects we close the
// older one to keep the model state coherent. -------------------------------

interface DiscordVoiceSession {
	sessionId: string;
	ws: WebSocket;
	voiceSession: VoiceSession;
	guild?: string;
	channel?: string;
	startTime: number;
	transcript: { role: string; text: string }[];
	resultQueue: { text: string }[];
	pendingTasks: number;
	closing: boolean;
	taskResultCache?: Map<string, string>;
	_toolIdMap?: Map<string, string>;
	toolCalls: { name: string; durationMs: number; timestamp: string }[];
	events: { event: string; timestamp: string }[];
}

let active: DiscordVoiceSession | null = null;
let nextBodhiPort = 9930;

// --- Task delegation (work tool) — same pattern as conversation-server -----

function delegateTask(s: DiscordVoiceSession, taskDescription: string): Promise<unknown> {
	const cached = s.taskResultCache?.get(taskDescription);
	if (cached) {
		console.log(`${ts()} [Task] cache hit for "${taskDescription}" — replaying`);
		s.resultQueue.push({
			text: `[Task result for "${taskDescription}"]\n${cached}\n\nReport this result to the user now.`,
		});
		return Promise.resolve({ status: 'cached', message: 'This was already completed — result is being replayed.' });
	}

	const taskId = `task-discord-voice-${Date.now()}`;
	const taskPath = join(TASKS_DIR, `${taskId}.txt`);
	const resultPath = join(RESULTS_DIR, `${taskId}.txt`);

	s.pendingTasks++;
	console.log(`${ts()} [Task] delegated: ${taskId} — "${taskDescription}" (pending: ${s.pendingTasks})`);
	s.events.push({ event: `task_delegated:${taskDescription.slice(0, 60)}`, timestamp: new Date().toISOString() });

	const fullTranscript = s.transcript.slice(-20)
		.map(t => `${t.role === 'sutando' ? 'Sutando' : 'User'}: ${t.text}`)
		.join('\n');
	const content =
		`id: ${taskId}\n` +
		`timestamp: ${new Date().toISOString()}\n` +
		`source: discord-voice\n` +
		`guild: ${s.guild ?? ''}\n` +
		`channel: ${s.channel ?? ''}\n` +
		`access_tier: ${TREAT_AS_OWNER ? 'owner' : 'other'}\n` +
		`task: ${taskDescription}\n` +
		`hint: Check ~/.claude/skills/ for a matching skill before using raw commands.\n` +
		`transcript:\n${fullTranscript}\n`;
	writeFileSync(taskPath, content);

	const startTime = Date.now();
	const poll = setInterval(() => {
		if (s.closing || s !== active) {
			clearInterval(poll);
			s.pendingTasks = Math.max(0, s.pendingTasks - 1);
			return;
		}
		if (existsSync(resultPath)) {
			clearInterval(poll);
			s.pendingTasks = Math.max(0, s.pendingTasks - 1);
			const result = readFileSync(resultPath, 'utf-8').trim();
			console.log(`${ts()} [Task] result ${taskId} (${Date.now() - startTime}ms): ${result.slice(0, 200)}`);
			s.events.push({ event: `task_result:${taskId}:${Date.now() - startTime}ms`, timestamp: new Date().toISOString() });
			try { unlinkSync(resultPath); } catch {}
			if (!s.taskResultCache) s.taskResultCache = new Map();
			s.taskResultCache.set(taskDescription, result);
			s.resultQueue.push({
				text: `[Task result for "${taskDescription}"]\n${result}\n\nReport this result to the user now.`,
			});
			return;
		}
		if (Date.now() - startTime > TASK_POLL_TIMEOUT_MS) {
			clearInterval(poll);
			s.pendingTasks = Math.max(0, s.pendingTasks - 1);
			console.log(`${ts()} [Task] timeout ${taskId}`);
			try {
				(s.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[Task "${taskDescription}" timed out — still being worked on. Let the user know.]` },
				], true);
			} catch {}
		}
	}, TASK_POLL_INTERVAL_MS);

	return Promise.resolve({
		status: 'delegated',
		taskId,
		message: 'Task submitted. Do NOT report any result to the user yet — wait for the actual task result before saying anything about it. You can continue the conversation on other topics.',
	});
}

// --- Build agent ------------------------------------------------------------

function buildAgent(s: DiscordVoiceSession): MainAgent {
	const isOwner = TREAT_AS_OWNER;

	let instructions: string;
	if (isOwner) {
		const repoUrl = (() => {
			try { return execSync('git remote get-url origin', { timeout: 2_000 }).toString().trim().replace(/\.git$/, ''); }
			catch { return ''; }
		})();
		instructions = [
			`You are Sutando, a personal AI assistant. You are in a Discord voice channel with your owner${OWNER_NAME ? ` ${OWNER_NAME}` : ''}.`,
			'YOU are Sutando — the AI assistant. The person speaking is your OWNER, a human. Do NOT confuse yourself with them.',
			'You have full capabilities — use the work tool for anything: check the screen, send emails, look things up, make calls, browse the web, or check results of previous tasks.',
			'',
			'## How to think',
			'Before acting, gather what you need. Before delegating, give them what they need.',
			'If you need info from multiple tools, call them in sequence — get results first, then act.',
			'',
			'## Tools',
			`These tools are instant (use them directly, NOT through work): ${inlineTools.map(t => t.name).join(', ')}. Use work for everything else.`,
			'TOOL EXCLUSIVITY: If an inline tool can handle the request, use ONLY the inline tool. NEVER also call work. They are mutually exclusive — calling both causes duplicate responses.',
			coreDocumentedSkills.length > 0
				? '## Documented skills (delegate via work)\n' + coreDocumentedSkills.map(sk => `- ${sk.name}: ${sk.description}`).join('\n')
				: '',
			'',
			'## Style',
			'Be natural, warm, and conversational. Keep responses to 1-2 sentences.',
			'Discord voice channels are persistent — do NOT say "goodbye" or try to hang up. Just stop speaking when you have nothing more to add.',
			'NEVER say "I\'m back", "Welcome back", "Working on it", or "task is queued". If the conversation resumes after a pause, just continue naturally.',
			'NEVER fabricate specific details. If you don\'t know it, use the work tool to look it up.',
			repoUrl ? `\n## Known info\nSutando GitHub repo: ${repoUrl}` : '',
		].filter(Boolean).join('\n');
	} else {
		instructions = [
			'You are Sutando, an AI assistant in a Discord voice channel.',
			'Be helpful and conversational. You can answer general knowledge questions, do translations, and have conversations.',
			'You cannot access files, control the screen, or delegate tasks.',
			'Keep responses to 1-2 sentences.',
		].join('\n');
	}

	const tools: ToolDefinition[] = [];

	if (isOwner) {
		tools.push({
			name: 'work',
			description:
				'Do the work. Call this for action requests — sending a message, looking something up, ' +
				'researching, editing files, generating images, video editing, scheduling. ' +
				'Do NOT use this for scrolling or switching apps — use the scroll and switch_app tools instead.',
			parameters: z.object({
				task: z.string().describe('Full description of the task to perform'),
			}),
			execution: 'inline',
			pendingMessage: 'The task is being processed. Wait silently for the result.',
			timeout: 120_000,
			async execute(args) {
				const { task } = args as { task: string };
				return delegateTask(s, task);
			},
		});
		// Dedup inline tools by name (Gemini 3.1 rejects duplicate function declarations).
		const seen = new Set(tools.map(t => t.name));
		for (const t of inlineTools) {
			if (!seen.has(t.name)) { tools.push(t); seen.add(t.name); }
		}
		// Owner-only + configurable layered on top (some may overlap inlineTools — seen guards).
		for (const t of [...ownerOnlyTools, ...configurableTools]) {
			if (!seen.has(t.name)) { tools.push(t); seen.add(t.name); }
		}
		tools.push({
			name: 'get_task_status',
			description: 'Check whether a delegated task is still in progress. Use when someone asks "are you still working on that?"',
			parameters: z.object({}),
			execution: 'inline',
			async execute() {
				return { inProgress: s.pendingTasks > 0, pendingCount: s.pendingTasks };
			},
		});
	}

	return {
		name: 'discord-voice',
		instructions,
		tools,
		googleSearch: true,
		// No automatic greeting — Discord voice is ambient; speak only when addressed.
		greeting: '',
	};
}

// --- Create VoiceSession for the active WS client --------------------------

async function createVoiceSession(ws: WebSocket, guild?: string, channel?: string): Promise<DiscordVoiceSession> {
	const bodhiPort = nextBodhiPort++;
	const sessionId = `discord_voice_${Date.now()}`;

	const s: DiscordVoiceSession = {
		sessionId,
		ws,
		guild,
		channel,
		voiceSession: null as unknown as VoiceSession,
		startTime: Date.now(),
		transcript: [],
		resultQueue: [],
		pendingTasks: 0,
		closing: false,
		toolCalls: [],
		events: [{ event: 'session_started', timestamp: new Date().toISOString() }],
	};

	const agent = buildAgent(s);

	const session = new VoiceSession({
		sessionId,
		userId: 'discord_voice_user',
		apiKey: GEMINI_API_KEY,
		agents: [agent],
		initialAgent: 'discord-voice',
		port: bodhiPort,
		host: '127.0.0.1',
		model: google(VOICE_MODEL),
		geminiModel: VOICE_NATIVE_AUDIO_MODEL,
		googleSearch: true,
		speechConfig: { voiceName: 'Aoede' },
		hooks: {
			onToolCall: (e) => {
				console.log(`${ts()} [Tool] ${e.toolName} (${e.execution})`);
				if (!s._toolIdMap) s._toolIdMap = new Map();
				s._toolIdMap.set(e.toolCallId, e.toolName);
				s.events.push({ event: `tool_call:${e.toolName}`, timestamp: new Date().toISOString() });
			},
			onToolResult: (e) => {
				const toolName = s._toolIdMap?.get(e.toolCallId) || 'unknown';
				console.log(`${ts()} [Tool] result: ${toolName} (${e.status}, ${e.durationMs}ms)`);
				s.toolCalls.push({ name: toolName, durationMs: e.durationMs, timestamp: new Date().toISOString() });
				s.events.push({ event: `tool_result:${toolName}:${e.durationMs}ms`, timestamp: new Date().toISOString() });
			},
			onError: (e) => console.error(`${ts()} [Error] ${e.component}: ${e.error.message} (${e.severity})`),
		},
	});

	s.voiceSession = session;

	// Vision attach — push-mode frames (Mentra glasses, Discord/Telegram photo
	// helper) reach Gemini in-stream while the voice channel is active.
	await attachVisionToSession(session);

	await session.start();
	console.log(`${ts()} [Bodhi] VoiceSession started on port ${bodhiPort} for ${sessionId}`);

	// [Outbound audio chain] override handleAudioOutput to forward Gemini PCM
	// directly to the Python bridge as base64 JSON (skip bodhi's internal WS).
	const sessionAny = session as any;
	sessionAny.handleAudioOutput = (data: string) => {
		sessionAny.notificationQueue?.markAudioReceived?.();
		if (ws.readyState !== WebSocket.OPEN) return;
		try {
			ws.send(JSON.stringify({ type: 'audio', pcm: data }));
		} catch (err) {
			console.error(`${ts()} [WS] send audio failed:`, err);
		}
	};

	// Transcript mirroring — emit transcript events to the Python bridge so
	// it can log + optionally surface caption text in Discord text channel.
	let lastProcessedIdx = 0;
	session.eventBus.subscribe('turn.end', () => {
		const items = session.conversationContext.items;
		if (items.length < lastProcessedIdx) lastProcessedIdx = 0;
		const lastText = s.transcript.length > 0 ? s.transcript[s.transcript.length - 1].text : null;
		for (const item of items.slice(lastProcessedIdx)) {
			if (item.content === lastText) continue;
			if (item.role === 'user') {
				s.transcript.push({ role: 'user', text: item.content });
				s.events.push({ event: `user:${item.content}`, timestamp: new Date().toISOString() });
				logLine('user', item.content);
				if (ws.readyState === WebSocket.OPEN) {
					try { ws.send(JSON.stringify({ type: 'transcript', role: 'user', text: item.content })); } catch {}
				}
			} else if (item.role === 'assistant') {
				s.transcript.push({ role: 'sutando', text: item.content });
				s.events.push({ event: `sutando:${item.content}`, timestamp: new Date().toISOString() });
				logLine('assistant', item.content);
				if (ws.readyState === WebSocket.OPEN) {
					try { ws.send(JSON.stringify({ type: 'transcript', role: 'assistant', text: item.content })); } catch {}
				}
			}
		}
		lastProcessedIdx = items.length;

		// Drain queued task results — inject after Gemini finishes speaking
		if (s.resultQueue.length > 0) {
			const queued = s.resultQueue.splice(0);
			for (const item of queued) {
				try {
					(s.voiceSession as any).transport.sendContent(
						[{ role: 'user', text: item.text }],
						true,
					);
				} catch (e) {
					console.log(`${ts()} [Task] inject failed: ${e}`);
				}
			}
		}
	});

	// Trigger client connected so VoiceSession starts the Gemini transport.
	sessionAny.handleClientConnected();

	// Auto-reconnect when Gemini transport closes — same pattern as conversation-server.
	const origHandleTransportClose = sessionAny.handleTransportClose.bind(sessionAny);
	sessionAny.handleTransportClose = (code?: number, reason?: string) => {
		console.log(`${ts()} [Voice] transport closed: code=${code} reason=${reason}`);
		origHandleTransportClose(code, reason);
		if (!s.closing && active === s) {
			setTimeout(() => {
				if (!s.closing && active === s) {
					console.log(`${ts()} [Voice] reconnecting Gemini for ${sessionId}`);
					sessionAny.handleClientConnected();
				}
			}, 1500);
		}
	};

	return s;
}

// --- Cleanup ----------------------------------------------------------------

function cleanupSession(s: DiscordVoiceSession): void {
	if (s.closing) return;
	s.closing = true;
	if (active === s) active = null;

	detachVisionFromSession();

	s.voiceSession.close('discord_voice_disconnect').catch(e =>
		console.error(`${ts()} [Bodhi] close error:`, e),
	);

	s.events.push({ event: 'session_ended', timestamp: new Date().toISOString() });
	const durationMs = Date.now() - s.startTime;
	const metrics = {
		timestamp: new Date().toISOString(),
		sessionId: s.sessionId,
		guild: s.guild,
		channel: s.channel,
		durationMs,
		transcriptLines: s.transcript.length,
		toolCalls: s.toolCalls,
		toolCount: s.toolCalls.length,
		pendingTasks: s.pendingTasks,
		events: s.events,
	};
	try {
		appendFileSync(join(DATA_DIR, 'discord-voice-metrics.jsonl'), JSON.stringify(metrics) + '\n');
	} catch {}
	console.log(`${ts()} [Voice] session finalized: ${s.sessionId} (${durationMs}ms, ${s.transcript.length} turns)`);
}

// --- HTTP + WebSocket server -----------------------------------------------

const server = createServer((req, res) => {
	const url = new URL(req.url ?? '', `http://localhost:${PORT}`);
	if (url.pathname === '/health' && req.method === 'GET') {
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({
			status: 'ok',
			active: !!active,
			sessionId: active?.sessionId,
			guild: active?.guild,
			channel: active?.channel,
			pendingTasks: active?.pendingTasks ?? 0,
		}));
		return;
	}
	res.writeHead(404, { 'Content-Type': 'application/json' });
	res.end(JSON.stringify({ error: 'not found' }));
});

const wss = new WebSocketServer({ server, path: '/voice' });

wss.on('connection', (ws: WebSocket) => {
	console.log(`${ts()} [WS] Discord bridge connected`);
	let session: DiscordVoiceSession | null = null;
	let mediaCount = 0;

	ws.on('message', async (data: Buffer) => {
		try {
			const msg = JSON.parse(data.toString());
			switch (msg.type) {
				case 'hello': {
					// Bridge announces guild + channel context.
					if (active && active.ws !== ws) {
						console.log(`${ts()} [WS] new bridge — closing previous session ${active.sessionId}`);
						const prev = active;
						try { prev.ws.close(1000, 'replaced'); } catch {}
						cleanupSession(prev);
					}
					session = await createVoiceSession(ws, msg.guild, msg.channel);
					active = session;
					console.log(`${ts()} [WS] session ready: guild=${msg.guild ?? '?'} channel=${msg.channel ?? '?'}`);
					try { ws.send(JSON.stringify({ type: 'ready', sessionId: session.sessionId })); } catch {}
					break;
				}
				case 'audio': {
					if (!session) {
						// Tolerate audio-before-hello: create implicitly.
						if (active && active.ws !== ws) {
							const prev = active;
							try { prev.ws.close(1000, 'replaced'); } catch {}
							cleanupSession(prev);
						}
						session = await createVoiceSession(ws);
						active = session;
					}
					mediaCount++;
					if (mediaCount === 1 || mediaCount % 500 === 0) {
						console.log(`${ts()} [WS] audio chunks: ${mediaCount}`);
					}
					const pcm = Buffer.from(msg.pcm, 'base64');
					try {
						(session.voiceSession as any).handleAudioFromClient(pcm);
					} catch (e) {
						if (mediaCount % 100 === 0) {
							console.error(`${ts()} [WS] handleAudioFromClient error:`, e);
						}
					}
					break;
				}
				case 'bye':
					console.log(`${ts()} [WS] bridge said bye`);
					if (session) cleanupSession(session);
					try { ws.close(1000, 'bye'); } catch {}
					break;
				default:
					console.log(`${ts()} [WS] ignoring message type: ${msg.type}`);
			}
		} catch (err) {
			console.error(`${ts()} [WS] message error:`, err);
		}
	});

	ws.on('close', () => {
		console.log(`${ts()} [WS] bridge disconnected`);
		if (session) cleanupSession(session);
	});
	ws.on('error', (err) => console.error(`${ts()} [WS] error:`, err));
});

// --- Port cleanup + startup ------------------------------------------------

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

async function start(): Promise<void> {
	killPortOccupant(PORT);
	await new Promise<void>(resolve => server.listen(PORT, '0.0.0.0', resolve));
	console.log(`\n╔════════════════════════════════════════════════════╗`);
	console.log(`║  Discord Voice Sidecar (bodhi VoiceSession)        ║`);
	console.log(`╠════════════════════════════════════════════════════╣`);
	console.log(`║  WS:    ws://localhost:${String(PORT).padEnd(28)}/voice ║`);
	console.log(`║  HTTP:  http://localhost:${String(PORT).padEnd(26)}/health║`);
	console.log(`║  Log:   ${LOG_PATH.slice(-43).padEnd(43)}║`);
	console.log(`╚════════════════════════════════════════════════════╝\n`);
}

process.on('SIGINT', () => { if (active) cleanupSession(active); process.exit(0); });
process.on('SIGTERM', () => { if (active) cleanupSession(active); process.exit(0); });
process.on('uncaughtException', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });
process.on('unhandledRejection', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });

start().catch(err => { console.error('Fatal:', err); process.exit(1); });
