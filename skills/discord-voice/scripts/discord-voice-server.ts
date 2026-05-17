#!/usr/bin/env npx tsx
/**
 * Discord Voice Server — discord.js + @discordjs/voice + bodhi VoiceSession
 * all in one TS process. No Python bridge.
 *
 * ## Audio chain
 *   Discord user → @discordjs/voice receiver (opus packets per speaking user)
 *     → prism opus.Decoder → PCM s16le 48k stereo
 *     → downsample48StereoTo16Mono → VoiceSession.handleAudioFromClient (PCM 16k mono)
 *
 *   Gemini Live → handleAudioOutput (base64 PCM 24k mono)
 *     → upsample24MonoTo48Stereo → PassThrough (PCM 48k stereo s16le)
 *     → @discordjs/voice AudioPlayer → opus-encoded out to voice connection
 *
 * Discord DAVE (E2EE) is supported first-party by @discordjs/voice via DAVESession.
 *
 * ## CLI
 *   tsx discord-voice-server.ts --guild <id> --channel <voice_channel_id>
 *
 * ## Env
 *   DISCORD_BOT_TOKEN  — bot token (~/.claude/channels/discord/.env)
 *   GEMINI_API_KEY     — required
 *   VOICE_MODEL / VOICE_NATIVE_AUDIO_MODEL — mirrors voice-agent.ts
 *   SUTANDO_WORKSPACE  — workspace root for tasks/results/data
 */

import { config as _dotenvConfig } from 'dotenv';
import { mkdirSync, writeFileSync, appendFileSync, existsSync, readFileSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';

_dotenvConfig({ path: new URL('../../../.env', import.meta.url).pathname, override: true });
_dotenvConfig({ path: join(process.env.HOME ?? '', '.claude/channels/discord/.env'), override: false });

import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { PassThrough } from 'node:stream';
import { VoiceSession, type ToolDefinition, type MainAgent } from 'bodhi-realtime-agent';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import { Client, GatewayIntentBits, ChannelType } from 'discord.js';
import {
	joinVoiceChannel,
	EndBehaviorType,
	createAudioPlayer,
	createAudioResource,
	StreamType,
	NoSubscriberBehavior,
	VoiceConnectionStatus,
	AudioPlayerStatus,
	entersState,
	type VoiceConnection,
	type AudioPlayer,
} from '@discordjs/voice';
import { Readable } from 'node:stream';
import prism from 'prism-media';
import {
	inlineTools,
	ownerOnlyTools,
	configurableTools,
	coreDocumentedSkills,
} from '../../../src/inline-tools.js';

// --- Config ---

const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? '';
const DISCORD_BOT_TOKEN = process.env.DISCORD_BOT_TOKEN ?? '';
const WORKSPACE_DIR =
	process.env.SUTANDO_WORKSPACE ||
	join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const DATA_DIR = join(WORKSPACE_DIR, 'data');
const RESULTS_DIR = process.env.DISCORD_VOICE_RESULTS_DIR || join(WORKSPACE_DIR, 'results');
const TASKS_DIR = join(WORKSPACE_DIR, 'tasks');
const TASK_POLL_INTERVAL_MS = 500;
const TASK_POLL_TIMEOUT_MS = 300_000;
const OWNER_NAME = process.env.owner ?? '';

const VOICE_MODEL = process.env.VOICE_MODEL || 'gemini-2.5-flash';
const VOICE_NATIVE_AUDIO_MODEL =
	process.env.VOICE_NATIVE_AUDIO_MODEL || 'gemini-3.1-flash-live-preview';

const TREAT_AS_OWNER = (process.env.DISCORD_VOICE_OWNER ?? 'true') !== 'false';

// CLI: --guild <id> --channel <voice_channel_id>
function getArg(name: string): string | undefined {
	const i = process.argv.indexOf(`--${name}`);
	return i >= 0 ? process.argv[i + 1] : undefined;
}
const GUILD_ID = getArg('guild');
const CHANNEL_ID = getArg('channel');

if (!GEMINI_API_KEY) { console.error('Error: GEMINI_API_KEY required'); process.exit(1); }
if (!DISCORD_BOT_TOKEN) { console.error('Error: DISCORD_BOT_TOKEN required'); process.exit(1); }
if (!GUILD_ID || !CHANNEL_ID) {
	console.error('Error: --guild <id> --channel <voice_channel_id> required');
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

// --- Audio conversion helpers ----------------------------------------------

/** PCM s16le 48k stereo → PCM s16le 16k mono (avg L+R, then decimate 3:1). */
function downsample48StereoTo16Mono(pcm: Buffer): Buffer {
	const inSamplePairs = pcm.length / 4; // 4 bytes per stereo sample
	const mono48 = new Int16Array(inSamplePairs);
	for (let i = 0; i < inSamplePairs; i++) {
		const l = pcm.readInt16LE(i * 4);
		const r = pcm.readInt16LE(i * 4 + 2);
		mono48[i] = (l + r) >> 1;
	}
	const outLen = Math.floor(mono48.length / 3);
	const mono16 = new Int16Array(outLen);
	for (let i = 0; i < outLen; i++) mono16[i] = mono48[i * 3];
	return Buffer.from(mono16.buffer, mono16.byteOffset, mono16.byteLength);
}

/** PCM s16le 24k mono → PCM s16le 48k stereo (sample-double upsample, mono→L=R). */
function upsample24MonoTo48Stereo(pcm: Buffer): Buffer {
	const mono24 = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.length / 2);
	const out = new Int16Array(mono24.length * 4); // 2× upsample × 2 channels
	for (let i = 0; i < mono24.length; i++) {
		const v = mono24[i];
		const off = i * 4;
		out[off] = v; out[off + 1] = v;
		out[off + 2] = v; out[off + 3] = v;
	}
	return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

// --- Active session ---------------------------------------------------------

interface DiscordVoiceSession {
	sessionId: string;
	connection: VoiceConnection;
	player: AudioPlayer;
	pcmOut: PassThrough;
	voiceSession: VoiceSession;
	guildId: string;
	channelId: string;
	startTime: number;
	transcript: { role: string; text: string }[];
	resultQueue: { text: string }[];
	pendingTasks: number;
	closing: boolean;
	taskResultCache?: Map<string, string>;
	_toolIdMap?: Map<string, string>;
	subscribedUsers: Set<string>;
	audioPending: Buffer[];
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
		`guild: ${s.guildId}\n` +
		`channel: ${s.channelId}\n` +
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
		const seen = new Set(tools.map(t => t.name));
		for (const t of inlineTools) {
			if (!seen.has(t.name)) { tools.push(t); seen.add(t.name); }
		}
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
		greeting: '',
	};
}

// --- Discord voice connection setup ---------------------------------------

// Gemini Live uses automatic VAD on the input stream — it waits for silence
// to mark turn-end. Discord only delivers opus packets while a user speaks,
// so after each utterance we send a brief silence burst to nudge Gemini's
// VAD past its silenceDurationMs threshold without flooding the WS.
const SILENCE_20MS_16K_MONO = Buffer.alloc(640); // 320 samples × 2 bytes
const SILENCE_BURST_FRAMES = 75; // ~1500ms — overshoot Gemini's silenceDurationMs default

function triggerSilenceBurst(s: DiscordVoiceSession): void {
	let n = 0;
	const handle = setInterval(() => {
		if (s.closing || n >= SILENCE_BURST_FRAMES) { clearInterval(handle); return; }
		try { (s.voiceSession as any).handleAudioFromClient(SILENCE_20MS_16K_MONO); } catch {}
		n++;
	}, 20);
}

// Silence ticker — BURST mode (2026-05-17 latency fix).
//
// HYPOTHESIS: Susan reported 30s gap between her utterance and Lucy's reply
// (2026-05-17 00:30 UTC). The earlier continuous-silence ticker (50fps of
// zero-PCM forever) appears to suppress Gemini Live's automatic VAD —
// Gemini sees a never-ending audio stream and never marks end-of-speech
// until its internal hard timeout (~25-30s).
//
// FIX: only send silence in a short BURST after Discord's
// EndBehaviorType.AfterSilence fires (i.e. user stopped speaking). The burst
// is ~250ms (12 frames × 20ms) which is enough to push Gemini past its
// silenceDurationMs (~1s default) when combined with the 200ms AfterSilence
// gap already provided by Discord, but doesn't flood the WS continuously.
//
// `triggerSilenceBurst(s)` is called from decoder.on('end') in subscribeUser.
function startAudioTicker(s: DiscordVoiceSession): void {
	(s as any)._noteSpoken = () => {}; // no-op now (kept for caller compat)
	(s as any)._tickHandle = null;
	console.log(`${ts()} [Ticker] BURST mode (silence sent only after AfterSilence)`);

	// Probe (optional): send synthetic text 5s after start to verify outbound.
	if (process.env.DISCORD_VOICE_PROBE === '1') {
		setTimeout(() => {
			console.log(`${ts()} [Probe] sending synthetic text to Gemini`);
			try {
				(s.voiceSession as any).transport.sendContent(
					[{ role: 'user', text: 'Say in English: hello from the discord voice probe' }],
					true,
				);
			} catch (e) { console.error(`${ts()} [Probe] failed:`, e); }
		}, 5000);
	}
}

function subscribeUser(s: DiscordVoiceSession, userId: string): void {
	if (s.subscribedUsers.has(userId)) return;
	s.subscribedUsers.add(userId);

	const opusStream = s.connection.receiver.subscribe(userId, {
		end: { behavior: EndBehaviorType.AfterSilence, duration: 200 },
	});
	const decoder = new prism.opus.Decoder({ frameSize: 960, channels: 2, rate: 48000 });
	// Resample 48k stereo s16le → 16k mono s16le via ffmpeg (anti-aliased).
	// -fflags nobuffer + -flush_packets 1 keep latency tight (no implicit batching).
	const resampler = new prism.FFmpeg({
		args: [
			'-fflags', 'nobuffer', '-flush_packets', '1',
			'-f', 's16le', '-ar', '48000', '-ac', '2', '-i', '-',
			'-f', 's16le', '-ar', '16000', '-ac', '1',
		],
	});
	opusStream.pipe(decoder).pipe(resampler);

	let chunks = 0;
	resampler.on('data', (pcm16Mono: Buffer) => {
		chunks++;
		try { (s.voiceSession as any).handleAudioFromClient(pcm16Mono); } catch {}
		(s as any)._noteSpoken?.();
		if (chunks === 1) console.log(`${ts()} [Voice] first chunk: ${pcm16Mono.length}B`);
	});
	resampler.on('end', () => {
		s.subscribedUsers.delete(userId);
		console.log(`${ts()} [Voice] user ${userId} stopped speaking (${chunks} chunks) — silence burst`);
		triggerSilenceBurst(s);
	});
	resampler.on('error', (e) => {
		console.error(`${ts()} [Voice] resampler error for ${userId}:`, e);
		s.subscribedUsers.delete(userId);
	});
	decoder.on('error', (e) => console.error(`${ts()} [Voice] decoder error for ${userId}:`, e));
	console.log(`${ts()} [Voice] subscribed to user ${userId} (ffmpeg resample)`);
}

async function createVoiceSession(connection: VoiceConnection): Promise<DiscordVoiceSession> {
	const bodhiPort = nextBodhiPort++;
	const sessionId = `discord_voice_${Date.now()}`;

	// Outbound audio: queue of PCM 48k stereo buffers. When Gemini sends a
	// chunk, push to queue. When player goes idle (or on first push), drain
	// the queue into a fresh AudioResource and play. This avoids the
	// outbound-silence-pump pattern (which buffered up and added latency on
	// every reconnect). Each Gemini burst becomes one resource.
	const pcmOut = new PassThrough({ highWaterMark: 1 << 20 }); // legacy, unused, kept for type compat
	const audioOutQueue: Buffer[] = [];
	const player = createAudioPlayer({
		behaviors: { noSubscriber: NoSubscriberBehavior.Play },
	});
	connection.subscribe(player);

	function flushAudioQueue(): void {
		if (audioOutQueue.length === 0) return;
		const merged = Buffer.concat(audioOutQueue.splice(0));
		const stream = Readable.from([merged]);
		const resource = createAudioResource(stream, { inputType: StreamType.Raw });
		player.play(resource);
	}

	(player as any)._pushAudio = (chunk: Buffer) => {
		audioOutQueue.push(chunk);
		if (player.state.status === AudioPlayerStatus.Idle) flushAudioQueue();
	};

	player.on(AudioPlayerStatus.Idle, () => {
		if (audioOutQueue.length > 0) flushAudioQueue();
	});

	player.on('stateChange', (oldS, newS) => {
		if (oldS.status !== newS.status) {
			console.log(`${ts()} [Player] ${oldS.status} → ${newS.status}`);
		}
	});
	player.on('error', (e) => console.error(`${ts()} [Player] error:`, e));

	const s: DiscordVoiceSession = {
		sessionId,
		connection,
		player,
		pcmOut,
		guildId: GUILD_ID!,
		channelId: CHANNEL_ID!,
		voiceSession: null as unknown as VoiceSession,
		startTime: Date.now(),
		transcript: [],
		resultQueue: [],
		pendingTasks: 0,
		closing: false,
		subscribedUsers: new Set(),
		audioPending: [],
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

	await attachVisionToSession(session);

	await session.start();
	console.log(`${ts()} [Bodhi] VoiceSession started on port ${bodhiPort} for ${sessionId}`);

	// [Outbound] Gemini PCM 24k mono → upsample to 48k stereo → pipe to AudioPlayer.
	const sessionAny = session as any;
	let outChunks = 0;
	sessionAny.handleAudioOutput = (data: string) => {
		sessionAny.notificationQueue?.markAudioReceived?.();
		try {
			const pcm24Mono = Buffer.from(data, 'base64');
			const pcm48Stereo = upsample24MonoTo48Stereo(pcm24Mono);
			(player as any)._pushAudio(pcm48Stereo);
			outChunks++;
			if (outChunks === 1 || outChunks % 50 === 0) {
				console.log(`${ts()} [Audio] outbound chunks: ${outChunks} (last=${pcm48Stereo.length}B)`);
			}
		} catch (err) {
			console.error(`${ts()} [Audio] outbound convert failed:`, err);
		}
	};

	// Transcript mirroring + result-queue drain
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
			} else if (item.role === 'assistant') {
				s.transcript.push({ role: 'sutando', text: item.content });
				s.events.push({ event: `sutando:${item.content}`, timestamp: new Date().toISOString() });
				logLine('assistant', item.content);
			}
		}
		lastProcessedIdx = items.length;

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

	sessionAny.handleClientConnected();

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

	// Subscribe to anyone currently speaking, and to anyone who starts.
	connection.receiver.speaking.on('start', (userId) => subscribeUser(s, userId));
	// Start the constant-rate ticker that flushes audio to Gemini every 20ms.
	startAudioTicker(s);

	// Outbound: no longer needs a silence pump. Audio is queued + played via
	// player.on(Idle) — see flushAudioQueue above.
	(s as any)._noteOut = () => {};
	(s as any)._outTickHandle = null;

	return s;
}

// --- Cleanup ----------------------------------------------------------------

function cleanupSession(s: DiscordVoiceSession): void {
	if (s.closing) return;
	s.closing = true;
	if (active === s) active = null;

	detachVisionFromSession();

	try { clearInterval((s as any)._tickHandle); } catch {}
	try { clearInterval((s as any)._outTickHandle); } catch {}
	try { s.player.stop(true); } catch {}
	try { s.pcmOut.end(); } catch {}
	try { s.connection.destroy(); } catch {}

	s.voiceSession.close('discord_voice_disconnect').catch(e =>
		console.error(`${ts()} [Bodhi] close error:`, e),
	);

	s.events.push({ event: 'session_ended', timestamp: new Date().toISOString() });
	const durationMs = Date.now() - s.startTime;
	const metrics = {
		timestamp: new Date().toISOString(),
		sessionId: s.sessionId,
		guildId: s.guildId,
		channelId: s.channelId,
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

// --- Bootstrap --------------------------------------------------------------

async function start(): Promise<void> {
	console.log(`${ts()} [Setup] logging in as Discord bot...`);

	const client = new Client({
		intents: [
			GatewayIntentBits.Guilds,
			GatewayIntentBits.GuildVoiceStates,
		],
	});

	await new Promise<void>((resolve, reject) => {
		client.once('ready', () => resolve());
		client.once('error', reject);
		client.login(DISCORD_BOT_TOKEN).catch(reject);
	});
	console.log(`${ts()} [Setup] logged in as ${client.user?.tag}`);

	const guild = await client.guilds.fetch(GUILD_ID!);
	const channel = await guild.channels.fetch(CHANNEL_ID!);
	if (!channel || (channel.type !== ChannelType.GuildVoice && channel.type !== ChannelType.GuildStageVoice)) {
		console.error(`Channel ${CHANNEL_ID} is not a voice channel`);
		process.exit(1);
	}
	console.log(`${ts()} [Setup] joining voice channel #${(channel as any).name} in guild ${guild.name}`);

	const connection = joinVoiceChannel({
		channelId: CHANNEL_ID!,
		guildId: GUILD_ID!,
		adapterCreator: guild.voiceAdapterCreator,
		selfDeaf: false,
		selfMute: false,
	});

	try {
		await entersState(connection, VoiceConnectionStatus.Ready, 30_000);
	} catch (e) {
		console.error(`${ts()} [Setup] voice connection failed:`, e);
		connection.destroy();
		process.exit(1);
	}
	console.log(`${ts()} [Setup] voice connection ready`);

	const session = await createVoiceSession(connection);
	active = session;
	console.log(`${ts()} [Setup] audio bridge live — speak in the channel`);

	connection.on(VoiceConnectionStatus.Disconnected, async () => {
		try {
			await Promise.race([
				entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
				entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
			]);
		} catch {
			console.log(`${ts()} [Voice] disconnected — cleaning up`);
			if (active) cleanupSession(active);
			process.exit(0);
		}
	});
}

process.on('SIGINT', () => { if (active) cleanupSession(active); process.exit(0); });
process.on('SIGTERM', () => { if (active) cleanupSession(active); process.exit(0); });
process.on('uncaughtException', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });
process.on('unhandledRejection', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });

start().catch(err => { console.error('Fatal:', err); process.exit(1); });
