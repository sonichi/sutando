#!/usr/bin/env npx tsx
/**
 * Discord Voice Server — discord.js + @discordjs/voice + bodhi VoiceSession
 * all in one TS process. No Python bridge.
 *
 * ## Audio chain
 *   Discord user → @discordjs/voice receiver (opus packets per speaking user)
 *     → prism opus.Decoder → PCM s16le 48k stereo
 *     → ffmpeg s16le resample (48k stereo → 16k mono, anti-aliased)
 *     → VoiceSession.handleAudioFromClient (PCM 16k mono)
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
 *   GEMINI_API_KEY (or GEMINI_VOICE_API_KEY) — required; voiceApiKey()
 *   VOICE_MODEL — text/STT model; native-audio model + googleSearch +
 *                 owner_mode/channels live in the per-user config at
 *                 $SUTANDO_WORKSPACE/config/discord-voice.json — NOT a
 *                 committed repo file (the repo ships config.json.example
 *                 as a template; see src/voice-config.ts for the schema)
 *   SUTANDO_WORKSPACE  — workspace root for tasks/results/data + config
 */

import { config as _dotenvConfig } from 'dotenv';
import { mkdirSync, writeFileSync, copyFileSync, appendFileSync, existsSync, readFileSync, readdirSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { resolveWorkspace } from '../../../src/workspace_default.js';
import { recordConversation, recordSession, recordToolCall } from '../../../src/conversation-store.js';
import { resultBelongsTo, discordVoiceKey } from '../../../src/result-channel-key.js';
import { personalPath } from '../../../src/util_paths.js';
import { type Tier, loadAccessTiers, effectiveTier, toolAllowed, toolNeed } from './access-tier.js';

_dotenvConfig({ path: new URL('../../../.env', import.meta.url).pathname, override: true });
_dotenvConfig({ path: join(process.env.HOME ?? '', '.claude/channels/discord/.env'), override: false });

import { fileURLToPath } from 'node:url';
import { voiceApiKey } from '../../../src/voice-key.js';
import { loadVoiceConfig, resolveOwnerMode } from '../../../src/voice-config.js';
import { execSync, spawn } from 'node:child_process';
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

// Voice surfaces share the GEMINI_VOICE_API_KEY → GEMINI_API_KEY fallback
// chain via voiceApiKey() (src/voice-key.ts).
const GEMINI_API_KEY = voiceApiKey();
const DISCORD_BOT_TOKEN = process.env.DISCORD_BOT_TOKEN ?? '';
const WORKSPACE_DIR = resolveWorkspace();
// Operational/diagnostic log — the [Setup]/[Voice]/[Tool]/[VoiceSession]/
// [Dismiss] lines that otherwise only hit stdout. Mirrors discord-bridge.log
// and voice-agent.log so discord-voice's operational history survives a
// process exit. Tee'd from console.log/console.error below (fail-soft).
const DISCORD_VOICE_LOG = join(WORKSPACE_DIR, 'logs', 'discord-voice.log');
const DATA_DIR = join(WORKSPACE_DIR, 'data');
const RESULTS_DIR = process.env.DISCORD_VOICE_RESULTS_DIR || join(WORKSPACE_DIR, 'results');
const TASKS_DIR = join(WORKSPACE_DIR, 'tasks');
const TASK_POLL_INTERVAL_MS = 500;
const TASK_POLL_TIMEOUT_MS = 300_000;
const OWNER_NAME = process.env.owner ?? '';

const VOICE_MODEL = process.env.VOICE_MODEL || 'gemini-2.5-flash';
// Per-user voice config (native-audio model + googleSearch + owner_mode +
// channels) is data, not code: it lives in the workspace, NOT in the git repo.
//   live config: $SUTANDO_WORKSPACE/config/discord-voice.json
//   template:    skills/discord-voice/config.json.example (committed)
// On first run, if the workspace config is missing, the committed .example
// template is copied into place so the operator has a file to edit. If the
// copy fails (or the template is gone), loadVoiceConfig falls back to its
// built-in safe defaults. Schema + defaults: src/voice-config.ts.
const _discordSkillDir = dirname(dirname(fileURLToPath(import.meta.url)));
const DISCORD_VOICE_CONFIG_PATH = join(WORKSPACE_DIR, 'config', 'discord-voice.json');
if (!existsSync(DISCORD_VOICE_CONFIG_PATH)) {
	const _exampleConfigPath = join(_discordSkillDir, 'config.json.example');
	try {
		mkdirSync(dirname(DISCORD_VOICE_CONFIG_PATH), { recursive: true });
		if (existsSync(_exampleConfigPath)) {
			copyFileSync(_exampleConfigPath, DISCORD_VOICE_CONFIG_PATH);
			console.log(`${new Date().toISOString().slice(11, 23)} [discord-voice] seeded config from template → ${DISCORD_VOICE_CONFIG_PATH}`);
		}
	} catch (e) {
		console.warn(`${new Date().toISOString().slice(11, 23)} [discord-voice] could not seed config at ${DISCORD_VOICE_CONFIG_PATH}: ${(e as Error).message} — using built-in defaults`);
	}
}
const DISCORD_VOICE_CONFIG = loadVoiceConfig(DISCORD_VOICE_CONFIG_PATH);
const VOICE_NATIVE_AUDIO_MODEL = DISCORD_VOICE_CONFIG.model;
const DISCORD_VOICE_GOOGLE_SEARCH = DISCORD_VOICE_CONFIG.googleSearch;

// Comma-separated Discord user IDs of BOT accounts whose audio SHOULD be
// piped to Gemini despite User.bot=true. Defaults to empty — bots are auto-
// ignored. Set this when you genuinely want a peer bot's voice processed
// (rare; usually only for testing). Per #1096 — without this default-deny,
// the receiver would pipe peer-bot audio to Gemini and cause attribution
// errors like today's "the other speaker is a bot, not a human" misdiagnosis.
const ALLOWED_BOT_USER_IDS = new Set(
	(process.env.SUTANDO_ALLOWED_BOT_USER_IDS ?? '')
		.split(',').map(s => s.trim()).filter(Boolean)
);

// Username prefixes that identify a peer SUTANDO bot (distinct from any
// other Discord bot like a music bot or MEE6). Used by #1089 single-bot
// enforcement to decide who to refuse-join-against / leave-when-detected.
// Override via `SUTANDO_PEER_USERNAME_PATTERNS=Foo,Bar` if the naming
// convention drifts. Match: `username.startsWith(pattern)`, case-sensitive.
const SUTANDO_PEER_USERNAME_PATTERNS = (process.env.SUTANDO_PEER_USERNAME_PATTERNS ?? 'Sutando-,Sutando_,Lucy-,Lucy_,Maddy,Mini')
	.split(',').map(s => s.trim()).filter(Boolean);

// Disable #1089 single-bot enforcement (testing-only). Set to "1" to allow
// multiple sutando peers in the same voice channel without auto-leave. Defaults
// to enabled. NEVER set in production — bypassing defeats the defense-in-depth
// design where each peer self-declines AND the already-present peer auto-
// leaves if a peer joins anyway.
const SUTANDO_PEER_ENFORCEMENT_DISABLED = process.env.SUTANDO_PEER_ENFORCEMENT_DISABLED === '1';

// Hung-session watchdog threshold. A Gemini Live session can silently stall —
// audio keeps flowing in but it stops emitting turn.end, with no transport
// close event to trigger the reconnect path. If utterances have piled up
// since the last turn AND the user last stopped speaking longer ago than
// this, treat the session as hung and force a reconnect. Env-overridable.
const WATCHDOG_STALL_MS = Number(process.env.SUTANDO_WATCHDOG_STALL_MS) || 20000;

// --- Per-speaker access tier (owner / team / other) -------------------------
// Tier logic lives in ./access-tier.ts (pure + unit-tested). A Gemini Live
// session's tool list is fixed at session start, so the tier is enforced
// per-turn at tool execute() time, keyed off the last speaker.
const ACCESS = loadAccessTiers(process.env.HOME ?? '');

// CLI: --guild <id> --channel <voice_channel_id>
function getArg(name: string): string | undefined {
	const i = process.argv.indexOf(`--${name}`);
	return i >= 0 ? process.argv[i + 1] : undefined;
}
const GUILD_ID = getArg('guild');
const CHANNEL_ID = getArg('channel');

// Owner-mode (issue #1016) — resolved from the workspace config
// ($SUTANDO_WORKSPACE/config/discord-voice.json), NOT an env var and NOT a
// committed repo file. Resolution order:
//   1. config.channels[CHANNEL_ID].owner_mode  (per-channel override)
//   2. config.owner_mode                       (skill-wide default)
//   3. false                                   (safe default)
// Default false (safe): non-owner speakers in the voice channel get the
// read-only tool surface but NOT owner-tier work/file-edit/message-send.
// Set owner_mode=true (skill-wide or per-channel) to inherit owner privileges
// to every speaker — only safe in voice channels whose membership is fully
// trusted (single-operator Lounge, not community/public). See SKILL.md.
// resolveOwnerMode (src/voice-config.ts) is fail-closed: it grants ONLY on the
// boolean literal `true`, so a hand-edited config with a string `"true"` /
// `"false"` / null / number can't silently flip the trust boundary. It also
// preserves precedence — a channel that explicitly sets owner_mode:false still
// overrides a skill-wide owner_mode:true.
const TREAT_AS_OWNER = resolveOwnerMode(DISCORD_VOICE_CONFIG, CHANNEL_ID);

// Legacy env warning (issue #1016) — owner-mode used to be a coarse global
// env flag. It's now config-driven (`owner_mode` in the workspace config).
// If the old var is still set, warn once so the operator knows it's inert.
if (process.env.DISCORD_VOICE_OWNER !== undefined) {
	console.warn(
		'[discord-voice] DISCORD_VOICE_OWNER is set but no longer takes effect — ' +
		'owner-mode is now config-driven (`owner_mode` in the workspace config, ' +
		'$SUTANDO_WORKSPACE/config/discord-voice.json; see SKILL.md).',
	);
}

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
// discord-voice mirrors turns into conversation.sqlite (queryable) AND the
// shared logs/conversation.log text log — the same dual-write the phone path
// uses. conversation.log is the canonical source the reload importer rebuilds
// the sqlite `conversation` table from, so writing it keeps discord-voice rows
// recoverable after `import-conversation-log.py --reload`.
const CONVERSATION_LOG = join(WORKSPACE_DIR, 'logs', 'conversation.log');

function appendConversationLog(role: string, text: string): void {
	try {
		mkdirSync(dirname(CONVERSATION_LOG), { recursive: true });
		appendFileSync(CONVERSATION_LOG, `${new Date().toISOString()}|${role}|${text.replace(/\n/g, ' ')}\n`);
	} catch {}
}

// --- Operational log tee ----------------------------------------------------
// console.log/console.error still write to stdout exactly as before; each call
// is ALSO appended (ISO-timestamped) to logs/discord-voice.log. Mirrors the
// appendConversationLog pattern above — mkdirSync guard + fail-soft try/catch
// so a disk/permission error degrades silently to stdout-only and can NEVER
// crash the voice session.
function appendOperationalLog(level: string, args: unknown[]): void {
	try {
		const line = args
			.map((a) => (typeof a === 'string' ? a : a instanceof Error ? (a.stack ?? a.message) : String(a)))
			.join(' ');
		mkdirSync(dirname(DISCORD_VOICE_LOG), { recursive: true });
		appendFileSync(DISCORD_VOICE_LOG, `${new Date().toISOString()} ${level} ${line}\n`);
	} catch {}
}
{
	const _origLog = console.log.bind(console);
	const _origError = console.error.bind(console);
	console.log = (...args: unknown[]): void => {
		_origLog(...args);
		appendOperationalLog('LOG', args);
	};
	console.error = (...args: unknown[]): void => {
		_origError(...args);
		appendOperationalLog('ERR', args);
	};
}

// --- Audio conversion helpers ----------------------------------------------

/** PCM s16le 48k stereo → PCM s16le 16k mono (avg L+R, then decimate 3:1). */
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
	client: Client;
	// Cache of userId → isBot flag from User.bot. Populated lazily on first
	// speaking.start for each speaker. Used to auto-ignore bot accounts so
	// the receiver doesn't pipe peer-bot audio to Gemini.
	botFlagCache: Map<string, boolean>;
	// Every Discord user who contributed audio to the in-progress Gemini turn.
	// Added on speaking.start, cleared on turn.end. The tier gate reads this
	// set (not a live last-speaker pointer) so a tool call is attributed to
	// the turn that produced it, not to whoever spoke most recently.
	turnSpeakers: Set<string>;
	audioPending: Buffer[];
	toolCalls: { name: string; durationMs: number; timestamp: string }[];
	events: { event: string; timestamp: string }[];
}

// Effective tier of the in-progress turn — the gate owner/team tools check.
// Resolves across every speaker who contributed audio to this turn, failing
// closed to the least-privileged among them (see effectiveTier). TREAT_AS_OWNER
// (legacy DISCORD_VOICE_OWNER) overrides to owner.
function currentTier(s: DiscordVoiceSession): Tier {
	return effectiveTier(s.turnSpeakers, ACCESS, TREAT_AS_OWNER);
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
		`access_tier: ${currentTier(s)}\n` +
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

// Inject a system-role message into the live Gemini Live transport.
// Owns the `(... as any).transport.sendContent` cast in one place so future
// bodhi-realtime-agent versions that publicize this surface only need one
// edit. Used for Layer-2 peer-detected announcement, magic-word takeover,
// recent_context replays, and a few other system-side nudges.
//
// TODO: bodhi 1.x stability — `transport.sendContent` is an internal API on
// the VoiceSession; bodhi may rename/restructure it across minor versions.
// Keep all call sites going through this wrapper.
function injectSystemMessage(s: DiscordVoiceSession, text: string): void {
	(s.voiceSession as any).transport.sendContent(
		[{ role: 'user', text }],
		true,
	);
}

function buildAgent(s: DiscordVoiceSession): MainAgent {
	// Declare the full owner toolset whenever an owner is configured (access.json)
	// or the legacy flag is on; the per-speaker tier is then enforced at execute().
	const isOwner = TREAT_AS_OWNER || ACCESS.owner.size > 0;

	let instructions: string;
	if (isOwner) {
		const repoUrl = (() => {
			try { return execSync('git remote get-url origin', { timeout: 2_000 }).toString().trim().replace(/\.git$/, ''); }
			catch { return ''; }
		})();
		instructions = [
			`You are Sutando, a personal AI assistant. You are in a Discord voice channel with your owner${OWNER_NAME ? ` ${OWNER_NAME}` : ''}.`,
			'YOU are Sutando — the AI assistant. The person speaking is your OWNER, a human. Do NOT confuse yourself with them.',
			// Per-node Stand identity — mirrors src/voice-agent.ts:606 pattern.
			// `stand-identity.json` carries name + nameOrigin for the bot on
			// this machine (e.g. "Echo Act IV (Mini)" on the Mac mini, "Lucy"
			// on Susan's Mac Studio). Loading it here lets the discord-voice
			// agent answer "who are you" with the same Stand name the core
			// voice-agent already uses — single per-node identity contract
			// across surfaces, no parallel env var. Silent fall-through if
			// the file is absent (kept the generic "You are Sutando" framing).
			(() => { try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return si.name ? `Your Stand name is ${si.name}. Origin: ${si.nameOrigin || 'earned through use'}. When asked your name or who you are, say "I'm Sutando — ${si.name}."` : ''; } catch { return ''; } })(),
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
			// "Look it up" pointer — conditional on per-surface config.
			// Search on → native Web grounding (~2-3s, in-conversation);
			// search off → `work` tool fallback (round-trip ~8-15s).
			// Earlier code had both a permanent "use work" line + a soft
			// nudge; model read the imperative as imperative and the nudge
			// as optional. One conditional line so only one path appears.
			DISCORD_VOICE_GOOGLE_SEARCH
				? 'NEVER fabricate specific details. If you don\'t know it, use your built-in Web search to look it up — it\'s faster than delegating, and the answer stays in the conversation. If your built-in search returns nothing useful, OR the question needs deeper-than-one-lookup research (multi-step, multiple sources, file reading), call the work tool — it routes to the core agent which can do extensive research.'
				: 'NEVER fabricate specific details. If you don\'t know it, use the work tool to look it up.',
			repoUrl ? `\n## Known info\nSutando GitHub repo: ${repoUrl}` : '',
		].filter(Boolean).join('\n');
	} else {
		instructions = [
			'You are Sutando, an AI assistant in a Discord voice channel.',
			// Per-node Stand identity — same load as owner-tier block above. Non-
			// owner speakers also benefit from "this Sutando is named X" so
			// "Hi Lucy" / "Hi Mini" doesn't get the rigid "I'm Sutando, not X"
			// correction.
			(() => { try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return si.name ? `Your Stand name is ${si.name}. When asked your name, say "I'm Sutando — ${si.name}."` : ''; } catch { return ''; } })(),
			'Be helpful and conversational. You can answer general knowledge questions, do translations, and have conversations.',
			'You cannot access files, control the screen, or delegate tasks.',
			'Keep responses to 1-2 sentences.',
		].filter(Boolean).join('\n');
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
		// Skill-local override of `dismiss` — in a Discord voice context, the
		// generic core dismissTool (which runs Zoom AppleScript) is wrong; here
		// dismiss = SIGTERM self so cleanupSession() handler runs.
		// Pushed BEFORE the inline-tools merge loop so the dedupe-by-name
		// keeps THIS one and drops the core dismissTool.
		tools.push({
			name: 'dismiss',
			description:
				'Leave the current Discord voice channel and exit the voice session. ' +
				'Use when user says "dismiss", "leave", "leave discord", "log off", "bye", "end this", "退出", "下线", "你走吧". ' +
				'NOT for ending an in-progress task or hanging up a phone call.',
			parameters: z.object({}),
			execution: 'inline',
			async execute() {
				console.log(`${ts()} [Dismiss] Discord voice context — SIGTERM`);
				setTimeout(() => { try { process.kill(process.pid, 'SIGTERM'); } catch {} }, 400);
				return { status: 'left_discord_voice' };
			},
		});
		// Skill-local share_screen — full sub-2s path. The voice-server
		// directly spawns share-screen-modal.py (--full mode) which does ALL
		// 5 CGEvent clicks (Discord Share button + Entire Screen tab +
		// thumbnail + Share button) in ~0.7s. No MCP, no task-bridge, no
		// proactive-loop hop. Coords hard-coded in the python script —
		// re-derive via macos-use refresh_traversal on the MCP-Chrome main
		// PID if Discord/Chrome UI moves.
		const SHARE_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'share-screen-modal.py');
		const spawnShareScreen = (source: string, mode: 'full' | 'stop') => {
			const flag = mode === 'stop' ? '--stop' : '--full';
			const child = spawn('python3', [SHARE_SCRIPT, flag], { stdio: 'ignore', detached: true });
			child.on('error', (err) => console.log(`${ts()} [ShareScreen ${source}] spawn error:`, err));
			child.unref();
			console.log(`${ts()} [ShareScreen ${source} ${flag}] spawned PID ${child.pid}`);
			return { status: mode === 'stop' ? 'stop_share_clicked' : 'share_screen_clicked',
			         message: mode === 'stop' ? 'Stop-share click fired.' : 'Picker drive fired (sub-1s).' };
		};
		tools.push({
			name: 'share_screen',
			description:
				'STRONG MATCH for any "share screen" / "share my screen" / "screen share" / "show my screen" / "屏幕共享" / "分享屏幕" / "把屏幕分享" utterance — in a Discord voice channel this is ALWAYS this tool. ' +
				'Shares the owner\'s screen (Entire Screen mode, picker handled automatically by the proactive loop). ' +
				'Call again to re-share even if already shared (user wants a fresh share). ' +
				'DO NOT route share-screen utterances to switch_tab (that\'s for Chrome tab navigation) OR to summon / join_zoom (those open the Zoom desktop app — wrong app, user is in Discord). ' +
				'To stop, use stop_share_screen tool (NOT dismiss — dismiss leaves the whole voice session).',
			parameters: z.object({}),
			execution: 'inline',
			pendingMessage: 'Setting up screen share.',
			async execute() { return spawnShareScreen('share_screen', 'full'); },
		});
		// Skill-local override: the core `summon` tool opens Zoom.app — wrong
		// behavior when the user is in a Discord voice channel saying "summon"
		// or "share my screen". Redirect those utterances to share_screen.
		tools.push({
			name: 'summon',
			description:
				'In a Discord voice channel context, "summon" / "share my screen" / "start zoom" / "let me see your screen" / "show me your screen" all mean: share the Discord screen via share_screen. ' +
				'Call share_screen directly instead of this tool whenever possible. ' +
				'This override exists only because the core summon tool would otherwise open Zoom.app — wrong app when the user is in Discord.',
			parameters: z.object({}),
			execution: 'inline',
			async execute() { return spawnShareScreen('summon→share_screen', 'full'); },
		});
		// Skill-local stop_share_screen — same fast path. Single CGEvent
		// click on the Discord voice-strip button at (338, 809) which
		// morphs to "Stop Streaming" when a share is active.
		tools.push({
			name: 'stop_share_screen',
			description:
				'STRONG MATCH for any "stop share" / "stop sharing" / "stop screen share" / "unshare" / "停止分享" / "停止共享" / "别分享了" utterance. ' +
				'Stops the active Discord screen share by clicking the Stop Streaming button. Voice channel stays connected. ' +
				'No-op if not currently sharing.',
			parameters: z.object({}),
			execution: 'inline',
			pendingMessage: 'Stopping screen share.',
			async execute() { return spawnShareScreen('stop_share_screen', 'stop'); },
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

	// Per-speaker tier gate. The Gemini session's tool list is fixed at start,
	// so enforce the tier at execute() time, keyed off the last speaker.
	// toolNeed() classifies each tool (see access-tier.ts):
	//   owner-only — work, screen-share tools, ownerOnlyTools
	//   owner+team — configurableTools + dismiss (a teammate may end the
	//                session — owner can rejoin via DM)
	//   open       — inlineTools + get_task_status (read-only surface)
	const ownerOnlyNames = new Set<string>(ownerOnlyTools.map(t => t.name));
	const teamNames = new Set<string>(configurableTools.map(t => t.name));
	for (let i = 0; i < tools.length; i++) {
		const t = tools[i];
		const need: Tier | null = toolNeed(t.name, ownerOnlyNames, teamNames);
		if (!need) continue;
		const inner = t.execute.bind(t);
		tools[i] = {
			...t,
			execute: async (args: any) => {
				const tier = currentTier(s);
				const ok = toolAllowed(need, tier);
				if (!ok) {
					console.log(`${ts()} [Tier] '${t.name}' denied — speaker tier=${tier}, needs ${need}`);
					return { status: 'denied', message: `That needs ${need}-tier access; the current speaker is ${tier}-tier.` };
				}
				return inner(args);
			},
		};
	}

	return {
		name: 'discord-voice',
		instructions,
		tools,
		googleSearch: DISCORD_VOICE_GOOGLE_SEARCH,
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
	// In-flight guard so overlapping speakers (userA ends → burst starts;
	// userB ends within 1500ms) don't stack two intervals that both call
	// handleAudioFromClient at 20ms — Gemini would see doubled silence.
	// Per @qingyun-wu cold-review on PR #783.
	if ((s as any)._silenceBursting) return;
	(s as any)._silenceBursting = true;
	let n = 0;
	const handle = setInterval(() => {
		if (s.closing || n >= SILENCE_BURST_FRAMES) {
			clearInterval(handle);
			(s as any)._silenceBursting = false;
			return;
		}
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
// is ~1500ms (SILENCE_BURST_FRAMES = 75 frames × 20ms) — set high to overshoot
// Gemini Live's silenceDurationMs (~1s default) reliably even on a flaky WS,
// while still terminating instead of flooding silence forever.
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
	// Wrapped in try/catch — prism.FFmpeg's constructor calls getInfo() which
	// throws synchronously if the ffmpeg binary isn't on PATH. Without this
	// guard the throw escapes to process.on('uncaughtException') and tears
	// down the whole bot the first time anyone speaks (#1089-followup). With
	// the guard we drop this user's audio stream and keep the bot online.
	let resampler: prism.FFmpeg;
	try {
		resampler = new prism.FFmpeg({
			args: [
				'-fflags', 'nobuffer', '-flush_packets', '1',
				'-f', 's16le', '-ar', '48000', '-ac', '2', '-i', '-',
				'-f', 's16le', '-ar', '16000', '-ac', '1',
			],
		});
	} catch (e) {
		console.error(`${ts()} [Voice] ffmpeg not available — cannot subscribe ${userId}; bot stays online but audio is dropped:`, e);
		s.subscribedUsers.delete(userId);
		try { opusStream.destroy(); } catch {}
		try { decoder.destroy(); } catch {}
		return;
	}
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
		// Watchdog bookkeeping: an utterance just finished. A healthy Gemini
		// fires turn.end within seconds; these counters let the watchdog tell
		// a hang apart from a normal pause.
		(s as any).lastSpeakStopTs = Date.now();
		(s as any).utterancesSinceTurn = ((s as any).utterancesSinceTurn || 0) + 1;
		triggerSilenceBurst(s);
	});
	resampler.on('error', (e) => {
		console.error(`${ts()} [Voice] resampler error for ${userId}:`, e);
		s.subscribedUsers.delete(userId);
	});
	decoder.on('error', (e) => console.error(`${ts()} [Voice] decoder error for ${userId}:`, e));
	console.log(`${ts()} [Voice] subscribed to user ${userId} (ffmpeg resample)`);
}

async function createVoiceSession(connection: VoiceConnection, client: Client): Promise<DiscordVoiceSession> {
	const bodhiPort = nextBodhiPort++;
	// Encode guild + channel into the session id so channel-level diagnostics
	// survive into the sessions table (recordSession has no guild/channel field).
	const sessionId = `discord_voice_${GUILD_ID}_${CHANNEL_ID}_${Date.now()}`;

	// Outbound audio: queue of PCM 48k stereo buffers. When Gemini sends a
	// chunk, push to queue. When player goes idle (or on first push), drain
	// the queue into a fresh AudioResource and play. This avoids the
	// outbound-silence-pump pattern (which buffered up and added latency on
	// every reconnect). Each Gemini burst becomes one resource.
	const audioOutQueue: Buffer[] = [];
	const player = createAudioPlayer({
		behaviors: { noSubscriber: NoSubscriberBehavior.Play },
	});
	connection.subscribe(player);

	const flushAudioQueue = (): void => {
		if (audioOutQueue.length === 0) return;
		const merged = Buffer.concat(audioOutQueue.splice(0));
		const stream = Readable.from([merged]);
		const resource = createAudioResource(stream, { inputType: StreamType.Raw });
		player.play(resource);
	};

	const pushAudio = (chunk: Buffer): void => {
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
		guildId: GUILD_ID!,
		channelId: CHANNEL_ID!,
		voiceSession: null as unknown as VoiceSession,
		startTime: Date.now(),
		transcript: [],
		resultQueue: [],
		pendingTasks: 0,
		closing: false,
		subscribedUsers: new Set(),
		client,
		botFlagCache: new Map(),
		turnSpeakers: new Set(),
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
		googleSearch: DISCORD_VOICE_GOOGLE_SEARCH,
		speechConfig: { voiceName: 'Aoede' },
		// Shorten Gemini's end-of-speech silence wait so turn-end (and the
		// reply) is detected faster. Default ~1s+; env-overridable for tuning.
		vadConfig: { silenceDurationMs: Number(process.env.SUTANDO_VAD_SILENCE_MS) || 500 },
		hooks: {
			onToolCall: (e) => {
				console.log(`${ts()} [Tool] ${e.toolName} (${e.execution})`);
				if (!s._toolIdMap) s._toolIdMap = new Map();
				s._toolIdMap.set(e.toolCallId, e.toolName);
				// tool_call event push removed per #1052 — canonical record is
				// the discord_voice-table row written in onToolResult via
				// recordToolCall().
			},
			onToolResult: (e) => {
				const toolName = s._toolIdMap?.get(e.toolCallId) || 'unknown';
				console.log(`${ts()} [Tool] result: ${toolName} (${e.status}, ${e.durationMs}ms)`);
				s.toolCalls.push({ name: toolName, durationMs: e.durationMs, timestamp: new Date().toISOString() });
				// tool_result event push removed per #1052 — recordToolCall
				// below is the canonical write.
				recordToolCall('discord-voice', toolName, e.durationMs, s.sessionId);
			},
			onError: (e) => console.error(`${ts()} [Error] ${e.component}: ${e.error.message} (${e.severity})`),
			onTurnLatency: (e) => {
				console.log(`${ts()} [Latency] turn=${e.turnId} ${JSON.stringify(e.segments)}`);
			},
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
			pushAudio(pcm48Stereo);
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
		// Watchdog: a turn completed — clear the hang counters.
		(s as any).lastTurnActivityTs = Date.now();
		(s as any).utterancesSinceTurn = 0;
		// Tier gate: the turn is over — its speaker attribution no longer
		// applies. The next turn re-accumulates speakers from speaking.start.
		s.turnSpeakers.clear();
		const items = session.conversationContext.items;
		if (items.length < lastProcessedIdx) lastProcessedIdx = 0;
		const lastText = s.transcript.length > 0 ? s.transcript[s.transcript.length - 1].text : null;
		for (const item of items.slice(lastProcessedIdx)) {
			if (item.content === lastText) continue;
			if (item.role === 'user') {
				s.transcript.push({ role: 'user', text: item.content });
				// utterance event push removed per #1052 — canonical record is
				// the discord_voice-table row written by recordConversation
				// below. session_events keeps only lifecycle entries to stop
				// triple-encoding the same utterance.
				// conversation.log is the primary; write it before the sqlite
				// mirror so a row never exists in sqlite without a log line.
				appendConversationLog('discord-user', item.content);
				recordConversation('discord-user', item.content, s.sessionId);
			} else if (item.role === 'assistant') {
				s.transcript.push({ role: 'sutando', text: item.content });
				// utterance event push removed per #1052 — see comment above.
				appendConversationLog('discord-agent', item.content);
				recordConversation('discord-agent', item.content, s.sessionId);
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

	// In-flight guard so repeated transport flaps don't stack reconnect timers.
	let reconnectPending = false;
	const origHandleTransportClose = sessionAny.handleTransportClose.bind(sessionAny);
	sessionAny.handleTransportClose = (code?: number, reason?: string) => {
		console.log(`${ts()} [Voice] transport closed: code=${code} reason=${reason}`);
		origHandleTransportClose(code, reason);
		if (s.closing || active !== s || reconnectPending) return;
		reconnectPending = true;
		setTimeout(() => {
			reconnectPending = false;
			if (s.closing || active !== s) return;
			console.log(`${ts()} [Voice] reconnecting Gemini for ${sessionId}`);
			sessionAny.handleClientConnected();
		}, 1500);
	};

	// Hung-session watchdog. A healthy Gemini fires turn.end within seconds of
	// the user finishing an utterance. If >=2 utterances have piled up since
	// the last turn activity and the user last stopped speaking more than
	// WATCHDOG_STALL_MS ago, the session has silently stalled — force a
	// reconnect through the same path as a transport close. The >=2 guard
	// keeps a single Gemini-ignored micro-utterance from tripping it.
	(s as any).lastTurnActivityTs = Date.now();
	const watchdog = setInterval(() => {
		if (s.closing || active !== s || reconnectPending) return;
		const stop = (s as any).lastSpeakStopTs || 0;
		const turn = (s as any).lastTurnActivityTs || 0;
		const pile = (s as any).utterancesSinceTurn || 0;
		const idleMs = Date.now() - stop;
		if (stop > turn && pile >= 2 && idleMs > WATCHDOG_STALL_MS) {
			console.error(`${ts()} [Watchdog] Gemini session hung — ${pile} utterances / ${Math.round(idleMs / 1000)}s since last speech, no turn. Reconnecting.`);
			reconnectPending = true;
			setTimeout(() => {
				reconnectPending = false;
				if (s.closing || active !== s) return;
				// Clear the hang condition so the watchdog doesn't immediately re-fire.
				(s as any).lastTurnActivityTs = Date.now();
				(s as any).utterancesSinceTurn = 0;
				try {
					sessionAny.handleClientConnected();
				} catch (e) {
					console.error(`${ts()} [Watchdog] reconnect failed:`, e);
				}
			}, 500);
		}
	}, 10000);
	(s as any)._watchdogHandle = watchdog;

	// --- Per-channel pull path for non-delegated task results ---------------
	// Regular `work`-tool delegations land at `results/task-discord-voice-*.txt`
	// and are claimed by the per-task poll in delegateTask(). This separate
	// scan picks up the new scoped namespace — `results/<CHANNEL_ID>.task-*.txt`
	// — used when the core agent (or another tool) needs to deliver a result
	// to THIS voice channel without having delegated through the work tool
	// (e.g. context handoff from a different surface). Existing consumers
	// don't match the `<channel-id>.` prefix, so a file in this namespace is
	// invisible to them — only this scan and the matching phone scan claim it.
	//
	// Cadence is intentionally slower than the delegate poll (3s vs 500ms)
	// since this path is for cross-surface handoffs, not in-conversation
	// turn-taking. Read-and-delete mirrors delegateTask()'s fail-soft style.
	// Typed key constructor — keeps writer + consumer in sync on the
	// `dvoice-` prefix; prevents cross-consumer namespace collisions.
	const channelKey = discordVoiceKey(CHANNEL_ID!);
	// Safety-net against silent unlinkSync failures (the unlink below is wrapped
	// in try/catch so a failed delete won't surface — without this map we'd
	// re-deliver the same body every 3s). Stored as `name -> first-seen ms`
	// and pruned at 60s/tick so the map can't grow unbounded. Map (not Set) so
	// the prune is O(seen) per tick without a parallel structure.
	const channelScanSeen = new Map<string, number>();
	const CHANNEL_SCAN_TTL_MS = 60_000;
	const channelScan = setInterval(() => {
		if (s.closing || active !== s) return;
		// Prune entries older than the TTL so the map doesn't grow unbounded.
		const cutoff = Date.now() - CHANNEL_SCAN_TTL_MS;
		for (const [k, ts0] of channelScanSeen) {
			if (ts0 < cutoff) channelScanSeen.delete(k);
		}
		let entries: string[];
		try {
			entries = readdirSync(RESULTS_DIR);
		} catch {
			return;
		}
		for (const name of entries) {
			// .txt guard — never touch a writer's atomic-write temp
			// (`<key>.task-X.txt.tmp`, `.sending`, `.partial`, etc).
			// Belt-and-suspenders: `resultBelongsTo` also gates on .txt.
			if (!name.endsWith('.txt')) continue;
			if (channelScanSeen.has(name)) continue;
			if (!resultBelongsTo(name, channelKey)) continue;
			channelScanSeen.set(name, Date.now());
			const full = join(RESULTS_DIR, name);
			let body: string;
			try {
				body = readFileSync(full, 'utf-8').trim();
			} catch {
				continue;
			}
			if (!body) {
				try { unlinkSync(full); } catch {}
				continue;
			}
			console.log(`${ts()} [ChannelScan] picked up ${name} (${body.length}B)`);
			s.events.push({ event: `channel_result:${name}`, timestamp: new Date().toISOString() });
			// Inject through the same path the work-tool result-queue drain
			// uses: a role:user content event into the live Gemini transport.
			try {
				(s.voiceSession as any).transport.sendContent(
					[{ role: 'user', text: `[Channel result]\n${body}\n\nReport this result to the user now.` }],
					true,
				);
			} catch (e) {
				console.log(`${ts()} [ChannelScan] inject failed for ${name}: ${e}`);
			}
			// Read-and-delete so the scan doesn't re-deliver and so other
			// consumers can't pick the file up after we've claimed it.
			try { unlinkSync(full); } catch {}
		}
	}, 3000);
	(s as any)._channelScanHandle = channelScan;

	// Subscribe to anyone currently speaking, and to anyone who starts.
	connection.receiver.speaking.on('start', async (userId) => {
		// Attribute this speaker to the in-progress turn. The gate resolves
		// the turn's effective tier across the whole set (cleared on turn.end).
		s.turnSpeakers.add(userId);
		// Bot/human discrimination (#1096). Discord's gateway exposes `User.bot`;
		// without this check the receiver would happily pipe peer-bot audio to
		// Gemini, which both wastes API quota and causes attribution errors
		// (today: a peer-bot's utterance was misattributed to the owner,
		// triggering a misdiagnosis of "name-gate conflict from a second bot"
		// when in fact the other account was a human). Cached per-user so we
		// fetch once per speaker; degrades gracefully (subscribe anyway) if
		// the fetch fails so this can never *block* an owner from being heard.
		let isBot = s.botFlagCache.get(userId);
		if (isBot === undefined) {
			try {
				const user = await s.client.users.fetch(userId);
				isBot = !!user.bot;
			} catch {
				isBot = false;
			}
			s.botFlagCache.set(userId, isBot);
		}
		if (isBot && !ALLOWED_BOT_USER_IDS.has(userId)) {
			console.log(`${ts()} [Voice] ignoring bot user ${userId} (not in SUTANDO_ALLOWED_BOT_USER_IDS)`);
			return;
		}
		subscribeUser(s, userId);
	});
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
	try { clearInterval((s as any)._watchdogHandle); } catch {}
	try { clearInterval((s as any)._channelScanHandle); } catch {}
	try { s.player.stop(true); } catch {}
	try { s.connection.destroy(); } catch {}

	s.voiceSession.close('discord_voice_disconnect').catch(e =>
		console.error(`${ts()} [Bodhi] close error:`, e),
	);

	s.events.push({ event: 'session_ended', timestamp: new Date().toISOString() });
	const durationMs = Date.now() - s.startTime;
	recordSession({
		source: 'discord-voice',
		sessionId: s.sessionId,
		durationMs,
		transcriptLines: s.transcript.length,
		toolCount: s.toolCalls.length,
		pendingTasks: s.pendingTasks,
		toolCalls: s.toolCalls,
		events: s.events,
	});
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

	// #1089 single-bot enforcement, layer 1 (cooperative pre-join check). Scan
	// current channel members; if any sutando peer is already in, refuse to
	// join. Each peer self-declines so multiple instances never accidentally
	// share one voice room. Disable via SUTANDO_PEER_ENFORCEMENT_DISABLED=1
	// for testing the layer-2 path.
	const looksLikeSutandoPeer = (username: string, isBot: boolean, userId: string): boolean => {
		if (!isBot) return false;
		if (userId === client.user?.id) return false; // myself
		return SUTANDO_PEER_USERNAME_PATTERNS.some(p => username.startsWith(p));
	};
	if (!SUTANDO_PEER_ENFORCEMENT_DISABLED) {
		const members = (channel as any).members as Map<string, { user: { username: string; bot: boolean; id: string; tag: string } }>;
		const presentPeers: string[] = [];
		for (const [, m] of members) {
			if (looksLikeSutandoPeer(m.user.username, m.user.bot, m.user.id)) {
				presentPeers.push(m.user.tag);
			}
		}
		if (presentPeers.length > 0) {
			console.error(`${ts()} [Setup] #1089 refusing to join: sutando peer(s) already present: ${presentPeers.join(', ')}`);
			// Surface the refusal to the operator — without this they just see
			// "nothing happens" when inviting a second bot to a channel that
			// already has one. Drop a proactive result; the discord-bridge
			// polls results/ and DMs proactive-*.txt to the owner. Best-effort:
			// if the write fails the process still exits cleanly and
			// Sutando.app's checkWatcher will retry once the peer leaves.
			try {
				const proactivePath = join(WORKSPACE_DIR, 'results', `proactive-${Date.now()}.txt`);
				const channelName = (channel as any).name ?? CHANNEL_ID;
				writeFileSync(
					proactivePath,
					`Skipping voice join in #${channelName} — peer already present: ${presentPeers.join(', ')}. ` +
					`Single-bot enforcement (#1089); reinvite once they leave.\n`,
				);
			} catch (e) {
				console.error(`${ts()} [Setup] #1089 couldn't surface refusal to operator:`, e);
			}
			process.exit(0); // clean exit — operator (Sutando.app checkWatcher) will retry later when peer leaves
		}
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

	const session = await createVoiceSession(connection, client);
	active = session;
	console.log(`${ts()} [Setup] audio bridge live — speak in the channel`);

	// #1089 single-bot enforcement, layer 2 (adversarial post-join watcher).
	// If a sutando peer joins our channel despite layer 1 (race, env override,
	// compromised peer), leave the channel after a short audible announcement.
	//
	// Race-window note: when two peers race in nearly-simultaneously, both
	// observe each other via voiceStateUpdate and both exit. The watcher
	// (Sutando.app's checkWatcher) then respawns exactly one. Cooperative-
	// symmetric and eventually-consistent — chosen over earliest-join-wins
	// because the respawn cost is bounded (~seconds) and the symmetric path
	// avoids a tie-break/coordination protocol we'd otherwise have to invent.
	if (!SUTANDO_PEER_ENFORCEMENT_DISABLED) {
		// `client.once` (not `.on`) — once a peer is detected we exit the
		// process anyway, so registering as a one-shot listener avoids the
		// per-event cleanup dance and prevents handler-retention on the
		// Client instance for the lifetime of the process.
		client.once('voiceStateUpdate', (oldState, newState) => {
			const justJoinedOurChannel = newState.channelId === CHANNEL_ID && oldState.channelId !== CHANNEL_ID;
			if (!justJoinedOurChannel) return;
			const u = newState.member?.user;
			if (!u) return;
			if (!looksLikeSutandoPeer(u.username, u.bot, u.id)) return;
			console.error(`${ts()} [Setup] #1089 peer ${u.tag} joined while I was present — announcing + leaving`);
			// Best-effort audio announcement. The text-injection goes through
			// the Gemini Live transport so Lucy speaks before disconnecting.
			// We don't wait for the actual TTS to complete — Gemini might
			// reword the request — just give it a short window. Worst case
			// (TTS no-shows) Lucy still leaves; the disconnect is the
			// authoritative action.
			try {
				injectSystemMessage(
					session,
					`[System] Another Sutando bot (${u.tag}) just joined this voice channel. Say briefly: "I detected another Sutando bot — leaving." Then stop.`,
				);
			} catch (e) {
				console.error(`${ts()} [Setup] #1089 announcement injection failed:`, e);
			}
			setTimeout(() => {
				try { connection.destroy(); } catch {}
				process.exit(0);
			}, 3000);
		});
	}

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

// Give connection.destroy() ~1.5s to flush the voice-gateway disconnect frame
// before exiting; otherwise Discord keeps the bot pinned in the channel until
// its own heartbeat timeout (~60-90s).
function shutdownAfterFlush(code: number): void {
	if (active) { try { cleanupSession(active); } catch {} }
	setTimeout(() => process.exit(code), 1500);
}
process.on('SIGINT', () => shutdownAfterFlush(0));
process.on('SIGTERM', () => shutdownAfterFlush(0));
process.on('uncaughtException', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });
process.on('unhandledRejection', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });

start().catch(err => { console.error('Fatal:', err); process.exit(1); });
