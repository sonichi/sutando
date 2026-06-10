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
import { mkdirSync, writeFileSync, copyFileSync, appendFileSync, createWriteStream, existsSync, readFileSync, readdirSync, unlinkSync } from 'node:fs';
import type { WriteStream } from 'node:fs';
import { join, dirname } from 'node:path';
import { resolveWorkspace } from '../../../src/workspace_default.js';
import { recordConversation, recordEvent, recordSession, recordToolCall } from '../../../src/conversation-store.js';
import { resultBelongsTo, discordVoiceKey } from '../../../src/result-channel-key.js';
import { personalPath } from '../../../src/util_paths.js';
import { type Tier, loadAccessTiers, effectiveTier, toolAllowed, toolNeed, shouldLeaveOnOwnerExit, breakSilenceAllowed } from './access-tier.js';
import { type ActionLease, mintLease, leaseValid } from './action-lease.js';
import { makeSendDiscordMessageTool, openGithubUrlTool, makeSwitchModeTools, makeDismissTool, shareScreenTool } from './discord-voice-tools.js';
import { sttGateDecision } from './stt-gate.js';
import { createGate, decideForTurn, isStandby, isWakePhrase, normalizeSpoken, type GateState } from './name-gate.js';
import { addressingClassifierPrompt, decideSpeak, isStopWord, regimeFor, shouldResilenceAtTurnEnd, shouldRestoreActiveOnReconnect } from './speak-gate.js';

_dotenvConfig({ path: new URL('../../../.env', import.meta.url).pathname, override: true });
_dotenvConfig({ path: join(process.env.HOME ?? '', '.claude/channels/discord/.env'), override: false });

import { fileURLToPath } from 'node:url';
import { voiceApiKey } from '../../../src/voice-key.js';
import { loadVoiceConfig, resolveOwnerMode } from '../../../src/voice-config.js';
import { execSync, execFileSync, spawn } from 'node:child_process';
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
// #1427 (Susan 2026-06-09, code-enforced — not agent-memory): if a delegated
// task has no result after this long, the SERVER prompts the model to check in
// with the user (restate the task, say it's still running, ask keep-waiting vs
// finish-offline). The 25-min review incident: core worked silently, voice said
// "idle", the user concluded the task was lost and left before the result came.
// Fixed constant — no env knob (Susan: don't introduce new env variables).
const TASK_CHECKIN_MS = 120_000;
const OWNER_NAME = process.env.owner ?? '';

// Speak-gate (name-gate, reused from sutando-skills PR #16 name-gate.ts).
// This bot's stand name (from stand-identity.json) + peer bots' names. When a
// peer is configured, the gate stays silent (default meeting-mode) and only
// breaks silence for turns ADDRESSED to this bot by name (local match, no LLM).
// Empty peer list = gate disabled = behaves like single-bot (always responds).
const STAND_NAME: string = (() => {
	// Env override lets a second gated identity run on the same machine (e.g.
	// testing a peer bot + this bot on one host without two checkouts). Falls back to
	// the workspace stand-identity.json name.
	if (process.env.SUTANDO_STAND_NAME) return process.env.SUTANDO_STAND_NAME.trim();
	try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return (si.name as string) || ''; }
	catch { return ''; }
})();
// Spoken-form aliases for the stand name (ASR variants of the stand
// name). Read BOTH env names: `SUTANDO_STAND_ALIASES` (what the code has
// always read) and `SUTANDO_NAME_ALIASES` (the name the per-machine .env
// convention actually used). They diverged silently, so aliases set under
// `SUTANDO_NAME_ALIASES` were ignored — and in meeting-mode that meant the
// gate never matched an ASR-mangled name, leaving the bot permanently
// audio-suppressed (generates text, no speech).
const STAND_NAME_ALIASES = (() => {
	// Aliases may come from env OR the per-node stand-identity.json `aliases` array
	// (#1427: single-file identity — name + aliases + nameOrigin in one place, so a
	// machine like this bot's host needs only that file, no env juggling for the gate to match
	// ASR variants). Env takes precedence; both are merged.
	let fromJson: string[] = [];
	try {
		const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8'));
		if (Array.isArray(si.aliases)) fromJson = si.aliases.map((a: unknown) => String(a));
	} catch { /* no file / no aliases */ }
	return [process.env.SUTANDO_STAND_ALIASES, process.env.SUTANDO_NAME_ALIASES, ...fromJson]
		.filter(Boolean).join(',').split(',').map(s => s.trim()).filter(Boolean);
})();
const PEER_NAMES = (process.env.SUTANDO_PEER_NAMES ?? '')
	.split(',').map(s => s.trim()).filter(Boolean)
	.filter(n => n.toLowerCase() !== STAND_NAME.toLowerCase());

// Meeting-buddy mode (single bot, multiple humans — PR #1427). Opt-in via
// SUTANDO_MEETING_MODE=1. When on, the name-gate starts SILENT, stays active
// even with no peer bots (own name is the only wake), and honors the standby
// phrases below as an explicit "go quiet but stay in the channel" command.
// Default off → legacy behavior is byte-for-byte unchanged.
const SUTANDO_MEETING_MODE = process.env.SUTANDO_MEETING_MODE === '1' || (() => {
	// #1427: also honor `meetingMode: true` in stand-identity.json so a node (e.g.
	// this bot) can default to gated WITHOUT the launcher sourcing .env (the env var
	// doesn't reach the process there — same gotcha as aliases). When on, the bot
	// JOINS name-gated/silent and only speaks when addressed by name.
	try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return si.meetingMode === true; } catch { return false; }
})();
// Tightened to DELIBERATE standby commands only. Removed
// conversational fillers 'hold on'/'wait'/'one sec' — the controller says those constantly in
// normal speech and they were false-triggering meeting mode (caught via the new
// sqlite mode-switch log). Meeting mode = an explicit command, not a filler.
const STANDBY_PHRASES = (process.env.SUTANDO_STANDBY_PHRASES ?? 'standby,stand by,待命,你待命')
	.split(',').map(s => s.trim()).filter(Boolean);
// #1427/#1456: the SINGLE Discord user id allowed to CONTROL
// meeting mode (enter via standby, wake/exit). When set, enter/wake cues only fire
// when the turn's speaker is this id — so a relay account (a peer bot via a relay user-id), a peer, or
// the bot's own echo can NEVER flip the bot's mode; only the controller can. Read from env or
// stand-identity.json `controller`. Empty → no controller gate (legacy: anyone's cue).
const VOICE_CONTROLLER: string = process.env.SUTANDO_VOICE_CONTROLLER || (() => {
	try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return String(si.controller || ''); }
	catch { return ''; }
})();

// Loud startup warning for the exact trap that cost a full night (2026-06-04):
// meeting-mode SUPPRESSES audio output until the bot is addressed by name, but
// ASR routinely mangles the name (e.g. a name's ASR variant). With no aliases, the
// gate never matches, so the bot generates text but stays permanently silent —
// indistinguishable from a model bug. Make the misconfig visible at boot.
if (SUTANDO_MEETING_MODE && STAND_NAME && STAND_NAME_ALIASES.length === 0) {
	console.warn(`[NameGate] ⚠ meeting-mode ON but NO aliases for "${STAND_NAME}" — the gate will only break silence on the EXACT word "${STAND_NAME}". ASR variants (e.g. "${STAND_NAME}ie") will NOT wake it and audio stays suppressed. Set SUTANDO_STAND_ALIASES.`);
}

// Single source of truth for the audio OUTPUT gate: "should this turn's audio be
// emitted?". This is DISTINCT from summon/mode name-control (the tool name-gate +
// meeting-enter gates), which stays name-gated — only conversation output is
// any-human. Unifies the four conditions that were inlined in handleAudioOutput.
// Post bodhi #20 this same function is handed to the VoiceSession as
// `config.shouldEmitAudio`, letting the handleAudioOutput monkey-patch be deleted.
// Policy (Susan 2026-06-06): in ACTIVE mode answer ANY human — bots are filtered
// at the input layer (speaking.start, before s.lastSpeaker is set), so a set
// lastSpeaker is always a human; the latch keeps an in-progress reply going. In
// MEETING mode stay silent UNLESS summoned by name (s.gate.lastAddressedToMe).
function shouldEmitAudio(s: any, nowMs: number): boolean {
	if (s.allowAckAudible) return true;                       // mode-switch ack — always heard
	if (nowMs < (s._forceAudibleUntil || 0)) return true;     // #1456 force-audible window after a mode switch
	if (VOICE_CONTROLLER) {
		// Controller mode: the precise per-stream gate (unchanged).
		if (!s.meetingMode) return s._turnAudioAllowed || !!s.lastSpeaker;
		return s.gate?.lastAddressedToMe === true;
	}
	// Legacy (no controller): population-aware speak gate (#1427, Susan's
	// 2026-06-09 spec). solo (≤1 human) keeps today's behavior bit-for-bit:
	// active → answer any human, meeting → addressed-only. group (≥2 humans):
	// addressed-or-sticky required in BOTH modes — with several people talking,
	// "utterance ended" no longer implies "answer me". The decision is stashed
	// for the per-turn speak_decision audit row (written at turn start in
	// handleAudioOutput, NOT here — this runs per audio chunk).
	const d = decideSpeak({
		humanCount: (s as any)._humanCount ?? 1,
		meetingMode: !!s.meetingMode,
		addressedToMe: s.gate?.lastAddressedToMe === true,
		allowAck: false,        // handled above
		forceAudible: false,    // handled above
	});
	(s as any)._lastSpeakDecision = d;
	return d.speak;
}

// Meeting mode — suppresses bot audio output while keeping transcription + sqlite running.
// Mirrors src/voice-agent.ts `meetingActive` behaviour for the discord-voice surface.
// Manual: poll state/voice-mode.txt (same file the menu-bar app + voice-agent write).
// Auto:   flip after SUTANDO_VOICE_AUTO_MEETING_AFTER_SEC with no user audio (default 180s).
const VOICE_MODE_FILE = join(WORKSPACE_DIR, 'state', 'voice-mode.txt');
const AUTO_MEETING_TIMEOUT_MS = parseInt(process.env.SUTANDO_VOICE_AUTO_MEETING_AFTER_SEC || '180', 10) * 1000;
// Wake phrases that exit meeting mode when user speaks them (case-insensitive).
// #1427: wake = a DELIBERATE, NAME-QUALIFIED command, never a loose
// substring. Earlier 'active mode' / 'wake up' matched incidental conversation — a bot
// saying "I'm in active mode now" (heard via channel echo) woke this bot from its OWN words.
// Matching lives in name-gate.ts isWakePhrase (unit-tested): punctuation-normalized,
// word-boundary, name-qualified forms — STT renders "Hi, Lucy! Wake up." and the old
// raw `includes('hi lucy')` missed it (the meeting→active "won't wake" bug).
function _isWakePhrase(text: string): boolean {
	return isWakePhrase(text, [STAND_NAME, ...STAND_NAME_ALIASES]);
}
// Enter-meeting cues (#1427): the bot joins active and switches to silent
// meeting/note-taker mode only when the user cues it with one of these.
// DELIBERATE mode-switch commands only. Removed the note-CONTENT and filler
// phrases that conflated "record this" / "wait a sec" with "switch to silent
// mode" (e.g. 'take notes', 'take meeting note(s)', 'hold on'). Asking the
// bot to write something down (add_to_vault) must NOT also flip it into meeting mode.
const ENTER_MEETING_PHRASES = ['meeting mode', 'be silent', 'go silent',
	'stand by', 'standby', 'passive mode',
	'start the meeting', 'start meeting',
	'会议模式', '安静', '静音'];
function _isEnterMeetingPhrase(text: string): boolean {
	// normalizeSpoken: punctuation-robust ("Lucy, stand by." → "lucy stand by").
	const lower = normalizeSpoken(text);
	return ENTER_MEETING_PHRASES.some(p => lower.includes(p));
}
// A mode command is a SHORT imperative ("<bot name>, stand by"), NOT the phrase
// buried in narrative speech ("…I told you to stand by so you wouldn't…"). Gate the
// meeting cue on utterance length so a merely-REFERENCED phrase doesn't false-trigger a
// switch (which would silence the bot mid-reply and drop its last sentence). CJK has
// no spaces → also allow on short char length. Env-tunable.
const _CMD_MAX_WORDS = Number(process.env.SUTANDO_MODECMD_MAX_WORDS) || 8;
function _looksLikeCommand(text: string): boolean {
	const t = text.trim();
	return t.split(/\s+/).filter(Boolean).length <= _CMD_MAX_WORDS || t.length <= 16;
}
// #1427: does this utterance NAME this bot (stand name or an
// ASR alias)? Used to gate meeting-mode ENTRY so a cue addressed to a PEER
// ("<peer name>, take notes") doesn't flip THIS bot into meeting mode. Word-boundary
// mention — looser than isAddressedBy (no punctuation/verb required, so it's
// robust to ASR dropping commas), which is the right bar for "is this for me".
function _namesThisBot(text: string): boolean {
	const lc = text.toLowerCase();
	return [STAND_NAME, ...STAND_NAME_ALIASES]
		.map(n => n.toLowerCase().trim()).filter(Boolean)
		.some(n => new RegExp(`\\b${n.replace(/[.*+?^${}()|[\]\\\\]/g, '\\$&')}\\b`).test(lc));
}
// (peer-naming detection lives in name-gate.ts decideForTurn / isAddressedToOther — no
//  local copy needed; the sticky gate reads s.gate.lastAddressedToMe, fed by decideForTurn
//  on the controller's own per-user stream.)

// --- Screen sharing: REMOVED from discord-voice (#1427) -----
// All screen-push machinery (setScreenPush, the 👁 indicators, vision-push.txt,
// the magic-word screen phrases) is gone. Screen sharing in a Discord voice
// session is now owned entirely by the join_discord_screen inline tool
// (src/vision-tools.ts), which the model calls on "join/share screen". The only
// magic word that remains is "za warudo" (summon). discord-voice attaches its
// Gemini session to vision-tools (attachVisionToSession), so join_discord_screen
// streams frames into THIS session — no discord-voice-side push code needed.

// Susan 2026-06-08: reverted 2.5-pro → 2.5-flash. The pro upgrade did NOT fix the clause-2
// false-negatives (her "submit a work task" command, buried in a discursive utterance, was
// still read as non-action) — confirming a side-classifier can't beat the live model's full
// context and loses the dispatch timing race. Moving intent judgment INTO the live model
// (option B) instead; no point paying for pro on the side classifier.
const VOICE_MODEL = process.env.VOICE_MODEL || 'gemini-2.5-flash';
// #1456: per-speaker STT model for the dedicated recording path. Mirrors the
// describe_screen / describeScreenshot REST pattern (src/browser-tools.ts) —
// raw generateContent with inline base64 media, free-tier voice key preferred.
const STT_MODEL = process.env.DISCORD_VOICE_STT_MODEL || 'gemini-2.5-flash';
// Min/max accumulated PCM (16k mono s16le = 32000 B/s) for an utterance to be
// worth transcribing. Floor ~0.4s (skip clicks/noise); cap ~30s (avoid huge
// payloads — Discord AfterSilence chops longer utterances anyway).
const STT_MIN_BYTES = 12_800;   // ~0.4s
const STT_MAX_BYTES = 960_000;  // ~30s
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
const SUTANDO_PEER_USERNAME_PATTERNS = (process.env.SUTANDO_PEER_USERNAME_PATTERNS ?? 'Sutando-,Sutando_')
	.split(',').map(s => s.trim()).filter(Boolean);

// Disable #1089 single-bot enforcement (testing-only). Set to "1" to allow
// multiple sutando peers in the same voice channel without auto-leave. Defaults
// to enabled. NEVER set in production — bypassing defeats the defense-in-depth
// design where each peer self-declines AND the already-present peer auto-
// leaves if a peer joins anyway.
const SUTANDO_PEER_ENFORCEMENT_DISABLED = process.env.SUTANDO_PEER_ENFORCEMENT_DISABLED === '1';

// Meeting-companion v1 boundary (a peer bot's design, #1389 thread): owner-only
// addressing. Only the OWNER may break the bot's silence by name — a non-owner
// in the room saying the bot's name is ignored. Open-floor consultancy (anyone
// can address the owner's bot) is a bigger consent question, deferred to v2
// behind this opt-in flag.
const SUTANDO_ALLOW_OPEN_FLOOR = process.env.SUTANDO_ALLOW_OPEN_FLOOR === '1';

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
mkdirSync(dirname(DISCORD_VOICE_LOG), { recursive: true });

let _opLogStream: WriteStream | null = null;
try {
	_opLogStream = createWriteStream(DISCORD_VOICE_LOG, { flags: 'a' });
	_opLogStream.on('error', () => { _opLogStream = null; });
} catch {}

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

// --- Screen-share indicator (#1427) -----------------------
// The 👁 visible trace is KEPT (silent screen capture violates "no silent
// action"), but it is now driven by the join_discord_screen TOOL invocation
// (onToolResult hook below) — NOT by the old setScreenPush / magic-phrase path.
// Streaming itself lives entirely in the join_discord_screen inline tool
// (src/vision-tools.ts, unchanged); this only renders the Discord-side trace
// (VC-status + nickname 👁 + a frame-count message) and clears it reliably on
// stop_vision / session exit so it never lingers (the old lingering bug).

// VC-status + nickname 👁 toggle. Idempotent; safe to call for stale-cleanup.
async function _setScreenIndicators(s: DiscordVoiceSession, on: boolean): Promise<void> {
	try {
		await fetch(`https://discord.com/api/v10/channels/${s.channelId}/voice-status`, {
			method: 'PUT',
			headers: { Authorization: `Bot ${DISCORD_BOT_TOKEN}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ status: on ? '👁 seeing my local screen' : '' }),
		});
	} catch (e) { console.error(`${ts()} [ScreenShare] VC-status update failed:`, e); }
	try {
		const g = await s.client.guilds.fetch(s.guildId);
		const me = await g.members.fetchMe();
		const base = (me.nickname || me.user.username).replace(/^👁\s*/, '');
		await me.setNickname(on ? `👁 ${base}` : base);
	} catch (e) { console.error(`${ts()} [ScreenShare] nickname update failed:`, e); }
}

// Mode indicator on the bot's guild NICKNAME (next to Lucy's name): active -> 🗣,
// meeting/旁听 -> 🔇. Susan 2026-06-09: keep it on the nickname, NOT the voice-channel
// status. Driven by the 2s voice-mode poll; the only-on-change guard avoids spamming
// Discord's (rate-limited) nickname endpoint. Fix vs the prior version: on a FAILED
// update, reset `_modeEmoji` so the next 2s tick RETRIES — the old code left the guard
// optimistically set, so a dropped/throttled update never self-corrected (looked stuck).
// (Heavy rapid mode-toggling can still lag — Discord rate-limits nickname changes and
// there's no faster path for this field; normal use updates within ~2s.) Strip regex
// uses codepoints (🔇 U+1F507, 🗣 U+1F5E3, 👁 U+1F441) so a prior prefix never stacks.
async function _setModeIndicator(s: DiscordVoiceSession, active: boolean): Promise<void> {
	const want = active ? '🗣' : '🔇';
	if ((s as any)._modeEmoji === want) return;
	(s as any)._modeEmoji = want;
	try {
		const g = await s.client.guilds.fetch(s.guildId);
		const me = await g.members.fetchMe();
		const base = (me.nickname || me.user.username).replace(/^(?:[\u{1F507}\u{1F5E3}\u{1F441}]\u{FE0F}?\s*)+/u, '');
		await me.setNickname(`${want} ${base}`);
	} catch (e) {
		(s as any)._modeEmoji = undefined;  // reset so the next 2s tick retries — don't leave it stuck
		console.error(`${ts()} [ModeIndicator] nickname update failed:`, e);
	}
}

// Strip the mode emoji on session end so the nickname never lingers. Also clears any VC
// status left by the short-lived 2026-06-09 voice-status experiment (migration cleanup).
async function _clearModeIndicator(s: DiscordVoiceSession): Promise<void> {
	(s as any)._modeEmoji = undefined;
	(s as any)._modeStatus = undefined;
	try {
		await fetch(`https://discord.com/api/v10/channels/${s.channelId}/voice-status`, {
			method: 'PUT',
			headers: { Authorization: `Bot ${DISCORD_BOT_TOKEN}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ status: '' }),
		});
	} catch { /* best-effort: clear interim VC-status experiment */ }
	try {
		const g = await s.client.guilds.fetch(s.guildId);
		const me = await g.members.fetchMe();
		const base = (me.nickname || me.user.username).replace(/^(?:[\u{1F507}\u{1F5E3}\u{1F441}]\u{FE0F}?\s*)+/u, '');
		if ((me.nickname || '') !== base) await me.setNickname(base);
	} catch { /* best-effort cleanup */ }
}

// #1427 dynamic task board (Susan 2026-06-09): a single live-edited message in
// the bound text channel acts as a task list — ⚙️ a line when a task is
// delegated, ✅/⏱ when it finishes. Discord-native equivalent of the web UI's
// #tasks panel. Best-effort: if the post/edit fails, the task still runs.
async function _renderTaskBoard(s: DiscordVoiceSession): Promise<void> {
	const board = s.taskBoard;
	if (!board || board.length === 0 || s.closing) return;
	const icon = (st: string) => (st === 'done' ? '✅' : st === 'timeout' ? '⏱' : '⚙️');
	const lines = board.slice(-10).map(t => `${icon(t.status)} ${t.desc.slice(0, 80)}`);
	const running = board.filter(t => t.status === 'running').length;
	const header = running > 0 ? `📋 **Tasks** · ⚙️ ${running} running` : '📋 **Tasks**';
	const text = `${header}\n${lines.join('\n')}`;
	try {
		const ch = await s.client.channels.fetch(s.channelId);
		if (!ch || !('send' in ch)) return;
		if (s.taskBoardMsgId) {
			try {
				const m = await (ch as { messages: { fetch: (id: string) => Promise<{ edit: (t: string) => Promise<unknown> }> } }).messages.fetch(s.taskBoardMsgId);
				await m.edit(text);
				return;
			} catch { s.taskBoardMsgId = null; /* message gone — repost below */ }
		}
		const m = await (ch as { send: (t: string) => Promise<{ id: string }> }).send(text);
		s.taskBoardMsgId = m.id;
	} catch (e) { console.error(`${ts()} [TaskBoard] update failed:`, e); }
}

// Show / hide the full screen-share indicator: VC-status + nickname + a posted
// message that live-updates the frame count (proof frames are actually flowing).
// Driven by the join_discord_screen tool-call, cleared on stop_vision / exit.
async function setScreenShareIndicator(s: DiscordVoiceSession, on: boolean): Promise<void> {
	if (on === s.screenShareOn) return;
	s.screenShareOn = on;
	await _setScreenIndicators(s, on);
	const editIndicator = async (text: string): Promise<void> => {
		if (!s.pushIndicatorMsgId) return;
		try {
			const ch = await s.client.channels.fetch(s.channelId);
			if (ch && 'messages' in ch) {
				const m = await (ch as { messages: { fetch: (id: string) => Promise<{ edit: (t: string) => Promise<unknown> }> } }).messages.fetch(s.pushIndicatorMsgId);
				await m.edit(text);
			}
		} catch { /* best-effort */ }
	};
	if (on) {
		let vt: typeof import('../../../src/vision-tools.js') | null = null;
		try { vt = await import('../../../src/vision-tools.js'); } catch {}
		try {
			const ch = await s.client.channels.fetch(s.channelId);
			if (ch && 'send' in ch) {
				const m = await (ch as { send: (t: string) => Promise<{ id: string }> }).send(
					'👁 **I can see your local computer screen** (not the Discord stream) · starting…');
				s.pushIndicatorMsgId = m.id;
			}
		} catch (e) { console.error(`${ts()} [ScreenShare] indicator message failed:`, e); }
		s.pushIndicatorTimer = setInterval(() => {
			if (s.closing || !s.screenShareOn) return;
			const frames = vt?.getVisionState?.().frames ?? 0;
			void editIndicator(`👁 **I can see your local computer screen** (not the Discord stream) · ${frames} frames`);
		}, 6_000);
	} else {
		if (s.pushIndicatorTimer) { clearInterval(s.pushIndicatorTimer); s.pushIndicatorTimer = null; }
		await editIndicator('⏹ Stopped seeing your local screen');
		s.pushIndicatorMsgId = null;
	}
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
		_opLogStream?.write(`${new Date().toISOString()} ${level} ${line}\n`);
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
	/** #1427 interrupt: drop queued + playing audio, squelch the rest of the turn. */
	interruptPlayback?: (why: string) => void;
	sessionId: string;
	connection: VoiceConnection;
	player: AudioPlayer;
	voiceSession: VoiceSession;
	guildId: string;
	channelId: string;
	startTime: number;
	transcript: { role: string; text: string }[];
	// Durable session grounding (e.g. the za-warudo join context). Stored so it
	// can be re-injected on reconnect and periodically, surviving the ~10min
	// rolloff and session resets that would otherwise drop the join-time context.
	groundingContext: string | null;
	// #1585 provenance action lease — minted ONLY when a real user STT turn lands; a gated
	// tool may dispatch only while a fresh lease is held. Model-fabricated "user:" turns never
	// run the STT path → never mint one → can't trigger tools.
	actionLease: ActionLease | null;
	resultQueue: { text: string }[];
	pendingTasks: number;
	// #1427 dynamic task board: live-edited message in the bound text channel
	// (➕ on delegate, ✅/⏱ on finish). taskBoardMsgId is the message being edited.
	taskBoard?: { id: string; desc: string; status: 'running' | 'done' | 'timeout' }[];
	taskBoardMsgId?: string | null;
	closing: boolean;
	taskResultCache?: Map<string, string>;
	_toolIdMap?: Map<string, string>;
	subscribedUsers: Set<string>;
	client: Client;
	// Cache of userId → isBot flag from User.bot. Populated lazily on first
	// speaking.start for each speaker. Used to auto-ignore bot accounts so
	// the receiver doesn't pipe peer-bot audio to Gemini.
	botFlagCache: Map<string, boolean>;
	// Cache of userId → display name + human/agent type, for per-speaker
	// attribution in the discord_voice recording (#1427). Populated alongside
	// botFlagCache on speaking.start (same User.fetch).
	speakerNameCache: Map<string, { name: string; type: 'human' | 'agent' }>;
	// #1456: per-user clean-audio accumulation buffer (userId → PCM chunks),
	// keyed by Discord user so each speaker's utterance is transcribed and
	// recorded SEPARATELY — correct attribution by construction, decoupled from
	// the mixed-into-one Gemini live turn. Filled in resampler.on('data'),
	// flushed+transcribed in resampler.on('end').
	sttBuffers: Map<string, Buffer[]>;
	// Every Discord user who contributed audio to the in-progress Gemini turn.
	// Added on speaking.start, cleared on turn.end. The tier gate reads this
	// set (not a live last-speaker pointer) so a tool call is attributed to
	// the turn that produced it, not to whoever spoke most recently.
	turnSpeakers: Set<string>;
	// Last speaker's user-id, set on every audio receive, NEVER cleared. Tier
	// resolution falls back to this when turnSpeakers is empty (it's cleared at
	// turn.end, but tool calls — e.g. `work` — execute AFTER that, so without a
	// fallback the tier resolves to 'other' and owner tools are wrongly denied).
	lastSpeaker: string | null;
	audioPending: Buffer[];
	toolCalls: { name: string; durationMs: number; timestamp: string }[];
	events: { event: string; timestamp: string }[];
	meetingMode: boolean;
	// #1427: sticky "has entered meeting mode" flag. The bot JOINS active
	// (meetingEntered=false → responds normally); the per-turn name-gate
	// silencing applies only once meeting-mode has been entered via a cue
	// ("take notes"/"meeting mode") or the auto-timeout. Wake phrases reset it.
	meetingEntered: boolean;
	// #1427: one-shot "let the next agent turn be HEARD even though meetingMode is
	// on" — set when entering meeting mode so the spoken "Got it, I'll take notes"
	// confirmation is audible BEFORE silence engages (the ack was
	// being suppressed by the same turn that set meetingMode=true). Cleared once the
	// ack turn's audio has actually emitted (_ackEmitted), so only that one turn passes.
	allowAckAudible: boolean;
	lastUserAudioAt: number;
	// Screen-share indicator state (#1427): the 👁 visible trace shown while the
	// join_discord_screen tool is actively streaming. Streaming itself is owned by
	// the inline tool (vision-tools); these only track the Discord-side indicator.
	screenShareOn: boolean;
	pushIndicatorMsgId: string | null;
	pushIndicatorTimer: ReturnType<typeof setInterval> | null;
	// Speak-gate (name-gate). Null when disabled (no stand name / no peers).
	gate: GateState | null;
}

// Effective tier of the in-progress turn — the gate owner/team tools check.
// Resolves across every speaker who contributed audio to this turn, failing
// closed to the least-privileged among them (see effectiveTier). TREAT_AS_OWNER
// (legacy DISCORD_VOICE_OWNER) overrides to owner.
function currentTier(s: DiscordVoiceSession): Tier {
	// Tier of WHO issued the command — the most-recent speaker (lastSpeaker), NOT
	// the most-restrictive of everyone present. What matters is who gave the
	// command. The owner's command must work even with a non-owner in the channel;
	// a non-owner's command is gated to their own tier. Using mostRestrictiveTier
	// over ALL turn speakers let any guest mute/deny the owner's own commands.
	// turnSpeakers (all participants) is only the fallback when no single last
	// speaker is known.
	const commander = s.lastSpeaker
		? new Set([s.lastSpeaker])
		: s.turnSpeakers;
	return effectiveTier(commander, ACCESS, TREAT_AS_OWNER);
}

let active: DiscordVoiceSession | null = null;
// Base port for the per-session bodhi VoiceSession server. Env-overridable so a
// second gated identity can run on the same host without an EADDRINUSE collision.
let nextBodhiPort = Number(process.env.SUTANDO_BODHI_BASE_PORT) || 9930;

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
	// Layered 2-min protocol (Susan 2026-06-09): core MUST write an interim
	// summary here within 2 minutes (then keep working); the poll below relays
	// it the moment it lands. The voice-side check-in prompt is only the
	// FALLBACK for when core stays silent past the deadline.
	const partialPath = join(RESULTS_DIR, `${taskId}.partial.txt`);

	s.pendingTasks++;
	console.log(`${ts()} [Task] delegated: ${taskId} — "${taskDescription}" (pending: ${s.pendingTasks})`);
	s.events.push({ event: `task_delegated:${taskDescription.slice(0, 60)}`, timestamp: new Date().toISOString() });
	if (!s.taskBoard) s.taskBoard = [];
	s.taskBoard.push({ id: taskId, desc: taskDescription, status: 'running' });
	void _renderTaskBoard(s);

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
		`deadline_protocol: MANDATORY — within 2 minutes of reading this task, write an interim summary (1-3 sentences: what you understood, findings so far or current step, rough ETA) to results/${taskId}.partial.txt. Then KEEP WORKING and write the full result to results/${taskId}.txt as usual. If you can fully answer within 2 minutes, skip the partial and write the full result directly.\n` +
		`transcript:\n${fullTranscript}\n`;
	writeFileSync(taskPath, content);

	const startTime = Date.now();
	let _checkinSent = false;
	const poll = setInterval(() => {
		if (s.closing || s !== active) {
			clearInterval(poll);
			s.pendingTasks = Math.max(0, s.pendingTasks - 1);
			return;
		}
		// Layer 1: core's interim summary (deadline_protocol in the task file).
		// Relay it the moment it lands; it satisfies the 2-min feedback contract,
		// so the fallback check-in below is suppressed.
		if (!_checkinSent && existsSync(partialPath)) {
			_checkinSent = true;
			try {
				const partial = readFileSync(partialPath, 'utf-8').trim();
				unlinkSync(partialPath);
				console.log(`${ts()} [Task] interim summary ${taskId} (${Date.now() - startTime}ms): ${partial.slice(0, 120)}`);
				(s.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[Interim update for task "${taskDescription.slice(0, 80)}" — report this to the user now in one or two short sentences, then ask if they want to keep waiting in this call or get the final result as a Discord message later:]\n${partial}` },
				], true);
			} catch {}
		}
		// Layer 2 (fallback): core stayed silent past the deadline — prompt from
		// THIS timer so the user gets feedback even if core ignores the protocol.
		if (!_checkinSent && Date.now() - startTime > TASK_CHECKIN_MS) {
			_checkinSent = true;
			console.log(`${ts()} [Task] check-in ${taskId} — no result after ${Math.round(TASK_CHECKIN_MS / 1000)}s, prompting model to ask the user`);
			try {
				(s.voiceSession as any).transport.sendContent([
					{ role: 'user', text: `[Status check from the task system, speak to the user now: the task "${taskDescription.slice(0, 120)}" is still running after ${Math.round((Date.now() - startTime) / 1000)} seconds. In ONE short sentence: tell the user it's still in progress and ask whether they want to keep waiting in this call, or have it finish offline so the result arrives as a Discord message later. Do not invent a result.]` },
				], true);
			} catch {}
		}
		if (existsSync(resultPath)) {
			clearInterval(poll);
			s.pendingTasks = Math.max(0, s.pendingTasks - 1);
			{ const _bt = s.taskBoard?.find(t => t.id === taskId); if (_bt) _bt.status = 'done'; void _renderTaskBoard(s); }
			const result = readFileSync(resultPath, 'utf-8').trim();
			console.log(`${ts()} [Task] result ${taskId} (${Date.now() - startTime}ms): ${result.slice(0, 200)}`);
			s.events.push({ event: `task_result:${taskId}:${Date.now() - startTime}ms`, timestamp: new Date().toISOString() });
			try { unlinkSync(resultPath); } catch {}
			try { unlinkSync(partialPath); } catch {}  // final result supersedes a lingering interim
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
			{ const _bt = s.taskBoard?.find(t => t.id === taskId); if (_bt) _bt.status = 'timeout'; void _renderTaskBoard(s); }
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
		// Canonical Sutando repo. Was `git remote get-url origin`, but `origin` is
		// the per-instance PRIVATE mirror (liususan091219/sutando-private) → the
		// model was told the wrong repo and kept opening the private fork for
		// "open the sutando repo / PR N" (Susan 2026-06-09). Hardcode the canonical
		// public repo (env-overridable) so the prompt + open_github_url always
		// resolve to sonichi/sutando, never a remote-derived guess.
		const repoUrl = process.env.SUTANDO_GH_REPO_URL || 'https://github.com/sonichi/sutando';
		instructions = [
			`You are Sutando, a personal AI assistant. You are in a Discord voice channel with your owner${OWNER_NAME ? ` ${OWNER_NAME}` : ''}.`,
			'YOU are Sutando — the AI assistant. The person speaking is your OWNER, a human. Do NOT confuse yourself with them.',
			// Per-node Stand identity — mirrors src/voice-agent.ts:606 pattern.
			// `stand-identity.json` carries name + nameOrigin for the bot on
			// this machine (e.g. a distinct Stand name per node). Loading it here lets the discord-voice
			// agent answer "who are you" with the same Stand name the core
			// voice-agent already uses — single per-node identity contract
			// across surfaces, no parallel env var. Silent fall-through if
			// the file is absent (kept the generic "You are Sutando" framing).
			(() => { try { const si = JSON.parse(readFileSync(personalPath('stand-identity.json'), 'utf-8')); return si.name ? `Your Stand name is ${si.name}. Origin: ${si.nameOrigin || 'earned through use'}. When asked your name or who you are, say "I'm Sutando — ${si.name}."` : ''; } catch { return ''; } })(),
			'You have full capabilities — use the work tool for anything: check the screen, send emails, look things up, make calls, browse the web, or check results of previous tasks.',
			// Meeting/silent-mode tool restriction (#1427): when the
			// name-gate / meeting-mode is in play, the bot is a silent note-taker for
			// turns where it is NOT addressed. It must NOT silently take actions then —
			// silent listening is fine, silent tool-execution is not. Prompt-level for
			// now; a hard execution-point gate is the follow-up.
			(STAND_NAME && (PEER_NAMES.length > 0 || SUTANDO_MEETING_MODE))
				? '## Silent / meeting mode — DO NOT act silently\nWhen you are NOT being explicitly addressed by name (i.e. in silent note-taking / meeting mode, producing no audio), do NOT call the work tool or ANY other tool. Just listen and track the discussion silently. ONLY call tools or take actions when you are explicitly addressed by name. NEVER take an action silently — the owner must always be able to hear when you do something.'
				: '',
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
			// "Hi <stand name>" doesn't get the rigid "I'm Sutando, not X"
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
		// open_github_url (#1427 demo, Susan 2026-06-09): resolve + open/answer
		// GitHub items in the CANONICAL repo (sonichi/sutando), never the private
		// mirror. The model kept opening liususan091219/sutando-private because it
		// guessed URLs from the origin remote; this tool removes the guessing —
		// canonical repo baked in, PR/issue numbers map deterministically.
		// Opens via `open`; summaries via read-only `gh` (best-effort — if gh isn't
		// authed in this process, the open still works, summary is omitted).
		// execFileSync (array args) — no shell, `who` sanitized → no injection.
		tools.push(openGithubUrlTool);  // moved to discord-voice-tools.ts (Susan 2026-06-09)
		// Skill-local override of `dismiss` — in a Discord voice context, the
		// generic core dismissTool (which runs Zoom AppleScript) is wrong; here
		// dismiss = SIGTERM self so cleanupSession() handler runs.
		// Pushed BEFORE the inline-tools merge loop so the dedupe-by-name
		// keeps THIS one and drops the core dismissTool.
		// #1456: switch_mode TOOL — Gemini calls it on INTENT, so a mode
		// switch no longer depends on phrase-matching the ASR transcript (which garbled
		// "meeting mode" → "switch to me" and silently failed to flip the flag). Mirrors
		// voice-agent's switchModeTool. The tool-gate below allows it whenever the controller
		// is addressing the bot, in either mode (so it can also EXIT meeting mode).
		tools.push(...makeSwitchModeTools(s, { voiceModeFile: VOICE_MODE_FILE, standName: STAND_NAME, getHumanCount: () => (s as any)._humanCount ?? 1 }));  // #1427: TWO tools — switch_mode (solo) + switch_mode_group
		tools.push(makeDismissTool(s, { voiceController: VOICE_CONTROLLER }));  // moved to discord-voice-tools.ts
		// Upstream sutando does NOT ship a screen-share implementation — it lives
		// in the operator's private repo. Without an explicit `share_screen` tool
		// that always returns unavailable, Gemini may silently route a "share my
		// screen" utterance to a sibling tool (switch_tab / core summon → Zoom.app)
		// — wrong behavior, no signal to the user. This stub guarantees a clean
		// unavailability reply.
		tools.push(shareScreenTool);  // moved to discord-voice-tools.ts
		const seen = new Set(tools.map(t => t.name));
		for (const t of inlineTools) {
			if (!seen.has(t.name)) { tools.push(t); seen.add(t.name); }
		}
		for (const t of [...ownerOnlyTools, ...configurableTools]) {
			if (!seen.has(t.name)) { tools.push(t); seen.add(t.name); }
		}
		// get_task_status REMOVED (Susan 2026-06-09): a read-only status tool, but it
		// drove an unsolicited spoken turn ("still working in the background") when the
		// model fired it spontaneously — and being read-only it was exempt from the
		// dispatch gate, so nothing stopped it. Delegated-`work` results already
		// auto-inject ("Report this result to the user now") when ready, so the model
		// just waits; checking progress goes back through `work` if needed. No bespoke
		// status tool.

		// send_discord_message — voice-session-scoped inline tool (needs s.client), so it
		// lives in discord-voice-tools.ts (Susan 2026-06-09: voice-specific tools belong
		// in their own module, not inlined here). Owner-tier (below) + dispatch-gated.
		tools.push(makeSendDiscordMessageTool(s));
	}

	// Per-speaker tier gate. The Gemini session's tool list is fixed at start,
	// so enforce the tier at execute() time, keyed off the last speaker.
	// toolNeed() classifies each tool (see access-tier.ts):
	//   owner-only — work, screen-share tools, ownerOnlyTools
	//   owner+team — configurableTools + dismiss (a teammate may end the
	//                session — owner can rejoin via DM)
	//   open       — inlineTools + get_task_status (read-only surface)
	const ownerOnlyNames = new Set<string>(ownerOnlyTools.map(t => t.name));
	ownerOnlyNames.add('switch_mode');  // #1456: classify as owner-tier so the controller-gate wrapper applies (only the controller may switch the bot's mode)
	ownerOnlyNames.add('switch_mode_group');  // #1427: the group-regime sibling — same owner-tier classification
	ownerOnlyNames.add('send_discord_message');  // Susan 2026-06-09: only the owner may post to channels via the voice bot
	const teamNames = new Set<string>(configurableTools.map(t => t.name));

	// #1427 clause 2+3 dispatch gate (Susan 2026-06-08): the tier/name gate alone
	// let Gemini fire side-effecting tools during plain chit-chat (logged: open_url
	// ×3, capture_screen, get_current_time, switch_app, add_to_vault — none requested).
	// Gate these tools on BOTH (clause 2) a FRESH explicit action-intent signal from
	// the per-utterance classifier AND (clause 3) — when the call carries a string
	// argument — that some token of that argument was actually SPOKEN recently, so a
	// hallucinated URL/app/note can't slip through. switch_mode is intentionally NOT
	// here (the wake-gate already governs it); only read-only/conversational tools
	// (recent_context, get_task_status, get_current_time) are exempt — destructive and
	// session-ending tools (close_tab/close_window/dismiss) ARE gated (see below).
	// Kill switch: SUTANDO_DISPATCH_GATE=0. Override set via
	// SUTANDO_DISPATCH_GATE_TOOLS (comma-separated names).
	const DISPATCH_GATE_ON = process.env.SUTANDO_DISPATCH_GATE !== '0';
	// Destructive/session-ending tools (close_tab, close_window, dismiss) are gated TOO —
	// Susan 2026-06-08 live test: after a legit "open GitHub", the model spuriously fired
	// close_tab then dismiss ("My owner left — leaving too") ~26s later with NO user ask,
	// closing her tab + ENDING the meeting. dismiss is NOT exempt: a spurious session-end is
	// worse than a missed leave, and an explicit "leave/bye" still sets actionIntent so a real
	// leave passes clause 2. (Legit owner-actually-left auto-leave is a code path, not this LLM tool.)
	// Zoom tools (summon/share_screen/join_zoom) added Susan 2026-06-08 retest: `summon` (join
	// Zoom + share screen) fired spuriously mid-conversation (she: "你怎么调用 salmon?") — same
	// ungated-tool gap as close_tab. NOTE: this allowlist keeps needing additions per tool; the
	// robust follow-up is to flip to an EXEMPT-list (gate every tier tool except read-only
	// recent_context/get_task_status/get_current_time + switch_mode) — proposed to Susan.
	// #1585 — EXEMPT-LIST, not allowlist (Susan/Mini 2026-06-09). The old `_defaultGated`
	// allowlist meant any NEW action tool was UNGATED until someone remembered to add it —
	// that's how `switch_voice_config` slipped through and a fabricated turn fired it. Flip it:
	// gate EVERY tier-classified action tool by default, exempting only read-only / self-governed
	// ones. New tools are gated automatically — no per-tool maintenance, no more whack-a-mole.
	// The only exempts are conversational/status reads + switch_mode (governed by its own
	// controller/name-gate). Env override (SUTANDO_DISPATCH_GATE_TOOLS) still wins for testing.
	const _exemptFromGate = new Set<string>(['recent_context', 'get_current_time', 'switch_mode', 'switch_mode_group']);
	const _gatedEnv = (process.env.SUTANDO_DISPATCH_GATE_TOOLS || '').split(',').map(x => x.trim()).filter(Boolean);
	const _gatedEnvSet: Set<string> | null = _gatedEnv.length ? new Set<string>(_gatedEnv) : null;
	// gate unless exempt (or, if an env override list is set, gate only those named).
	const isActionGated = (name: string): boolean => _gatedEnvSet ? _gatedEnvSet.has(name) : !_exemptFromGate.has(name);
	// Window was 12s; Susan 2026-06-08 live test: she commanded "open the repo" but the
	// model (stuck in the interruption/apology loop) didn't actually fire open_url until
	// 23-45s later — past 12s → her LEGIT call got blocked as "no intent" while the model
	// narrated "已经打开了". Widen to 30s so a genuine request survives the model's firing
	// latency. (Option A interruption fix is the real cure — it makes the model fire promptly;
	// this is insurance.) clause 3 (entity must be spoken) still bounds what can fire in-window.
	const _ACTION_INTENT_FRESH_MS = Number(process.env.SUTANDO_ACTION_INTENT_FRESH_MS) || 30000;
	// Tools whose argument is FREE-FORM CONTENT (a task description, a note body) rather than a
	// spoken TARGET — clause 3's "entity must be in recent speech" check is wrong for these and
	// silently swallows legit requests. They are gated by clause 2 (explicit intent) only.
	// open_url added 2026-06-08: clause 3 matched the URL's tokens against recent speech, but
	// STT garbles proper nouns ("Sutando" heard as "蘇丹娜") so a legit "open the Sutando page"
	// got blocked — brutal for bilingual speech. clause 2 (explicit "open" intent) already
	// gates chit-chat, so open_url relies on that alone now (tradeoff: the model may open an
	// unnamed page when asked to "open something" without a clear target).
	const _clause3Skip = new Set<string>((process.env.SUTANDO_CLAUSE3_SKIP || 'work,add_to_vault,open_url').split(',').map(x => x.trim()).filter(Boolean));
	// clause-3 helper: does any meaningful token of the tool's string args appear in
	// recent speech? Returns true (allow) when the call has no checkable string arg.
	const _entitySpoken = (args: any): boolean => {
		const _recent = (((s as any)._recentUserSpeech || []) as { text: string }[]).map(e => e.text).join(' ').toLowerCase();
		if (!_recent) return false;
		const _vals: string[] = [];
		const _collect = (v: any) => {
			if (typeof v === 'string') _vals.push(v);
			else if (v && typeof v === 'object') Object.values(v).forEach(_collect);
		};
		_collect(args);
		// tokens worth checking: words/domains length >= 3 (drop punctuation, scheme)
		const _tokens = _vals.join(' ').toLowerCase()
			.replace(/https?:\/\//g, ' ').replace(/[^a-z0-9一-鿿]+/g, ' ')
			.split(' ').filter(t => t.length >= 3);
		if (!_tokens.length) return true; // no checkable entity (e.g. capture_screen) — clause 2 governs
		return _tokens.some(t => _recent.includes(t));
	};

	// #1427 option B (Susan 2026-06-08): dispatch-time intent check. The pre-computed
	// per-utterance actionIntent flag (clause 2) missed commands buried in discursive speech
	// and raced the tool call; the entity check (clause 3) broke on STT-garbled proper nouns
	// ("Sutando"→"蘇丹娜"). Instead, the moment a side-effecting tool fires, ask a model whether
	// the FULL recent transcript actually requested THIS tool+args — full context, semantic
	// (tolerates STT garble + bilingual), synchronous (no timing race). Fails OPEN (allow) on
	// error so a transient hiccup never blocks a real command (Susan's pain is false-negatives).
	const _liveIntentCheck = async (toolName: string, args: any): Promise<{ ok: boolean; reason: string }> => {
		const _recent = (((s as any)._recentUserSpeech || []) as { text: string }[]).map(e => e.text).join(' / ').trim();
		if (!_recent) return { ok: false, reason: 'no recent user speech' };
		const apiKey = process.env.GEMINI_API_KEY || process.env.GEMINI_VOICE_API_KEY;
		if (!apiKey) return { ok: true, reason: 'no api key — fail open' };
		const prompt = `You gate an assistant's tool calls. Recent user speech (oldest→newest, may contain speech-to-text errors): "${_recent}". The assistant is about to run tool \`${toolName}\` with arguments ${JSON.stringify(args).slice(0, 400)}. Did the user EXPLICITLY ask for THIS action just now? Account for STT garbling proper nouns and for bilingual / indirect phrasing. Answer ONLY JSON: {"ok": true|false, "reason": "<=10 words"}. ok=true ONLY if the user clearly requested this kind of action on this target; ok=false for chit-chat, thinking aloud, or merely mentioning something without asking to act on it.`;
		try {
			const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${STT_MODEL}:generateContent?key=${apiKey}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { responseMimeType: 'application/json' } }),
			});
			const data = await res.json() as any;
			const raw = (data?.candidates?.[0]?.content?.parts?.[0]?.text ?? '').trim();
			const m = raw.match(/\{[\s\S]*\}/);
			if (m) { const j = JSON.parse(m[0]) as { ok?: boolean; reason?: string }; return { ok: j.ok === true, reason: String(j.reason ?? '').slice(0, 80) }; }
			return { ok: true, reason: 'unparseable — fail open' };
		} catch (e) {
			console.error(`${ts()} [DispatchGate] intent-check error (fail open):`, e);
			return { ok: true, reason: 'check error — fail open' };
		}
	};
	void _entitySpoken; // retained for reference / env fallback; superseded by _liveIntentCheck

	for (let i = 0; i < tools.length; i++) {
		const t = tools[i];
		const need: Tier | null = toolNeed(t.name, ownerOnlyNames, teamNames);
		if (!need) continue;
		const inner = t.execute.bind(t);
		tools[i] = {
			...t,
			execute: async (args: any) => {
				// #1456 (refactor 2026-06-05): in controller mode the bot is INERT unless
				// the controller addressed it — block action tools by the SAME precise gate
				// as audio (controller named it within the window, or an ack is in flight),
				// not just meetingMode. Gemini ignores the "do NOT call tools" prompt and
				// fired `work` while gated (the bot kept acting while it should have been silent); this enforces it deterministically.
				if (VOICE_CONTROLLER) {
					const _win = Number(process.env.SUTANDO_NAMEGATE_WINDOW_MS) || 20000;
					const _addressed = (s.gate?.lastAddressedToMe ?? true) && (Date.now() - ((s as any)._controllerNamedAt || 0) < _win);
					// switch_mode requires the controller to have EXPLICITLY named THIS bot in the
					// immediate command (≤10s) — NOT the sticky state — so a bare "switch to active
					// mode" switches nobody, and only "Hi <bot name>, switch …" switches this bot
					// (the controller must name THIS bot for it to switch). It's exempt from the !meetingMode
					// clause so it can also EXIT meeting mode. Other tools stay inert while silent.
					const _SWITCH_FRESH_MS = Number(process.env.SUTANDO_SWITCHMODE_FRESH_MS) || 10000;
					const _namedFresh = Date.now() - ((s as any)._namedThisBotAt || 0) < _SWITCH_FRESH_MS;
					const _allowed = s.allowAckAudible
						|| ((t.name === 'switch_mode' || t.name === 'switch_mode_group') ? _namedFresh : (!s.meetingMode && _addressed));
					if (!_allowed) {
						console.log(`${ts()} [NameGate] tool '${t.name}' blocked — controller hasn't addressed the bot`);
						return { status: 'silent', message: 'Gated: the bot is silent until the controller addresses it.' };
					}
				}
				const tier = currentTier(s);
				const ok = toolAllowed(need, tier);
				if (!ok) {
					console.log(`${ts()} [Tier] '${t.name}' denied — speaker tier=${tier}, needs ${need}`);
					return { status: 'denied', message: `That needs ${need}-tier access; the current speaker is ${tier}-tier.` };
				}
				// #1427/#1585 dispatch gate (merged): exempt-list (gate all action tools except
				// read-only/self-governed) → gate-wait (don't judge stale speech) → provenance
				// lease → semantic content-check.
				if (DISPATCH_GATE_ON && isActionGated(t.name)) {
					// (a) GATE-WAIT (stt-gate.ts): Gemini fires off LIVE AUDIO faster than the STT
					// path transcribes, so the triggering utterance may not be in the buffer yet —
					// judging now would block a legit request against stale speech. If a burst just
					// ended but its transcript hasn't landed, wait for it before judging.
					const _STT_MAX_LAG = Number(process.env.SUTANDO_STT_MAX_LAG_MS) || 5000;
					let _sttLagged = false;
					for (;;) {
						const _d = sttGateDecision({
							speechEndAt: Number((s as any)._lastSpeechEndAt || 0),
							sttAt: Number((s as any)._lastSttAt || 0),
							now: Date.now(),
							maxLagMs: _STT_MAX_LAG,
						});
						if (_d === 'wait') { await new Promise(r => setTimeout(r, 150)); continue; }
						if (_d === 'failopen') _sttLagged = true;
						break;
					}
					// (b) #1585 PROVENANCE LEASE: a gated tool may fire only while a fresh lease,
					// minted from a REAL user speech burst, is held. A model-fabricated "user:" turn
					// produces no real audio → no lease → dropped, regardless of content. Exception:
					// if a real burst ended but its STT never landed (lagged), we KNOW the user just
					// spoke → fail open rather than block a real request.
					const _LEASE_TTL = Number(process.env.SUTANDO_ACTION_LEASE_TTL_MS) || _ACTION_INTENT_FRESH_MS;
					if (!leaseValid(s.actionLease, Date.now(), _LEASE_TTL)) {
						if (_sttLagged) {
							console.log(`${ts()} [DispatchGate] '${t.name}' STT lag fail-open (real burst, transcript not landed within ${_STT_MAX_LAG}ms)`);
							return inner(args);
						}
						console.log(`${ts()} [ActionLease] '${t.name}' blocked - no fresh real-STT lease (fabricated / unprompted) | args=${JSON.stringify(args).slice(0, 100)}`);
						return { status: 'not_requested', message: 'BLOCKED - no real user request backs this action; it did NOT run. Do NOT claim you did it. Wait for the user to actually ask.' };
					}
					// (c) SEMANTIC content-check: confirm the real recent speech requested THIS tool.
					const _v = await _liveIntentCheck(t.name, args);
					if (!_v.ok) {
						console.log(`${ts()} [DispatchGate] '${t.name}' blocked - intent-check: ${_v.reason} | args=${JSON.stringify(args).slice(0, 100)}`);
						return { status: 'not_requested', message: 'BLOCKED - this tool did NOT run; you did NOT perform this action. Do NOT tell the user you did it or that anything was opened / written / saved. If the user wants it, ask them to state it clearly.' };
					}
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
// Length of the post-utterance silence burst, in 20ms frames. Must stay ABOVE
// Gemini Live's automatic end-of-speech silence window (~1s default) so the
// burst reliably marks turn-end — but no longer than needed, since every extra
// frame is added latency before the reply. Default 50 (~1000ms); env-tunable.
// (Was 75/~1500ms before the 2026-06-09 no-turn-hang fix — see below.)
const SILENCE_BURST_FRAMES = Number(process.env.SUTANDO_DISCORD_SILENCE_BURST_FRAMES) || 50;

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
// HYPOTHESIS: the controller reported a 30s gap between their utterance and the bot's reply.
// The earlier continuous-silence ticker (50fps of
// zero-PCM forever) appears to suppress Gemini Live's automatic VAD —
// Gemini sees a never-ending audio stream and never marks end-of-speech
// until its internal hard timeout (~25-30s).
//
// FIX: only send silence in a short BURST after Discord's
// EndBehaviorType.AfterSilence fires (i.e. user stopped speaking). The burst
// is ~1000ms (SILENCE_BURST_FRAMES = 50 frames × 20ms by default) — kept just
// above Gemini Live's silenceDurationMs (~1s default) so it reliably marks
// turn-end without adding needless latency, and it terminates instead of
// flooding silence forever. Both the burst length and the upstream
// AfterSilence window (see subscribeUser) are env-tunable.
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

// #1456: wrap raw 16k-mono-s16le PCM in a 44-byte WAV header so Gemini's
// generateContent accepts it as audio/wav inline data.
function pcm16ToWav(pcm: Buffer, sampleRate = 16000): Buffer {
	const header = Buffer.alloc(44);
	const dataLen = pcm.length;
	header.write('RIFF', 0);
	header.writeUInt32LE(36 + dataLen, 4);
	header.write('WAVE', 8);
	header.write('fmt ', 12);
	header.writeUInt32LE(16, 16);          // fmt chunk size
	header.writeUInt16LE(1, 20);           // PCM
	header.writeUInt16LE(1, 22);           // mono
	header.writeUInt32LE(sampleRate, 24);
	header.writeUInt32LE(sampleRate * 2, 28); // byte rate (mono * 2B)
	header.writeUInt16LE(2, 32);           // block align
	header.writeUInt16LE(16, 34);          // bits per sample
	header.write('data', 36);
	header.writeUInt32LE(dataLen, 40);
	return Buffer.concat([header, pcm]);
}

// #1456: transcribe ONE user's clean accumulated PCM via Gemini STT and record
// it as a correctly-attributed discord-user row. Fire-and-forget — never blocks
// the audio pipeline, never throws into it. Mirrors describeScreenshot's REST
// generateContent pattern (src/browser-tools.ts).
async function transcribeAndRecordUtterance(s: DiscordVoiceSession, userId: string, pcm: Buffer): Promise<void> {
	// Speech-end time, captured at entry (this fn is invoked from resampler.on('end'),
	// i.e. the moment the utterance ended). STT below takes ~3s, so we stamp the recorded
	// row with THIS time, not the post-STT write time — otherwise the utterance sorts AFTER
	// the tool_call it preceded ("记录反了", Susan 2026-06-09).
	const _utteranceTsUnix = Date.now() / 1000;
	// Prefer the GENERAL (paid) key for this background recording STT so it does
	// NOT compete with — and exhaust — the live voice session's free voice key.
	// One STT call per utterance per speaker blew GEMINI_VOICE_API_KEY's free-tier
	// quota within seconds (429s → missing rows) in testing. Fall
	// back to the voice key only if no general key is configured.
	const apiKey = process.env.GEMINI_API_KEY || process.env.GEMINI_VOICE_API_KEY;
	if (!apiKey) return; // no-op gracefully when no key configured
	try {
		const wav = pcm16ToWav(pcm);
		const audioData = wav.toString('base64');
		const res = await fetch(
			`https://generativelanguage.googleapis.com/v1beta/models/${STT_MODEL}:generateContent?key=${apiKey}`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					contents: [{
						parts: [
							// #1427 semantic addressing: besides the verbatim transcript, the
							// model judges whether the speaker is CALLING/addressing an assistant
							// named STAND_NAME — tolerating speech-to-text garble of the name
							// ("hi Maddy" often comes through as "hi May"), WITHOUT firing on the
							// name appearing as an unrelated word ("may I…", "on Monday") or a
							// "go quiet"/standby command. Replaces the brittle string name-match.
							{ text: addressingClassifierPrompt(STAND_NAME)  },
							{ inlineData: { mimeType: 'audio/wav', data: audioData } },
						],
					}],
					generationConfig: { responseMimeType: 'application/json' },
				}),
			},
		);
		const data = await res.json() as any;
		if (!data?.candidates?.[0]) {
			const reason = data?.promptFeedback?.blockReason || data?.error?.message || JSON.stringify(data).slice(0, 200);
			console.log(`${ts()} [STT] no transcript for ${userId}: ${reason}`);
			return;
		}
		const _raw = (data.candidates[0].content?.parts?.[0]?.text ?? '').trim();
		// Parse the {transcript, addressed} JSON; fall back to treating the whole
		// reply as the transcript (addressed=false) if the model didn't return JSON.
		let transcript = _raw;
		let _aiAddressed = false;
		try {
			const _m = _raw.match(/\{[\s\S]*\}/);
			if (_m) {
				const _j = JSON.parse(_m[0]) as { transcript?: string; addressed?: boolean; wake?: boolean; actionIntent?: boolean };
				transcript = String(_j.transcript ?? '').trim();
				_aiAddressed = _j.addressed === true;
				// Wake intent (Susan's distinction): a greeting / re-establish-contact that
				// should bring the bot ACTIVE — as opposed to a bare name or a quiet command.
				// switch_mode("active") reads this to decide whether to EXIT meeting mode.
				if (_j.wake === true) (s as any)._aiWakeAt = Date.now();
				// Action intent (#1427 clause 2 — Susan 2026-06-08): the speaker EXPLICITLY
				// asked for a concrete action. The tool-dispatch gate reads this freshness
				// stamp so Gemini can't fire a side-effecting tool (open_url, capture_screen,
				// …) during plain chit-chat — the model kept hallucinating actions
				// ("loading files to cache", open_url with a URL no one named). See the
				// dispatch gate in buildAgent().
				if (_j.actionIntent === true) (s as any)._actionIntentAt = Date.now();
			}
		} catch { /* non-JSON reply — keep _raw as transcript, addressed=false */ }
		if (!transcript) return; // skip empty/whitespace
		// #1427 clause 3 — rolling recent-speech buffer (Susan 2026-06-08): the
		// tool-dispatch gate checks that a side-effecting tool's argument entity was
		// actually SPOKEN (verbatim) in the last few seconds, so Gemini can't open a
		// URL / switch to an app / save a note it invented. Keep ~25s, pruned by age.
		{
			const _now = Date.now();
			const _RECENT_MS = Number(process.env.SUTANDO_RECENT_SPEECH_MS) || 25000;
			const _buf = ((s as any)._recentUserSpeech || []) as { text: string; at: number }[];
			_buf.push({ text: transcript, at: _now });
			(s as any)._recentUserSpeech = _buf.filter(e => _now - e.at < _RECENT_MS);
			// STT-calibration (stt-gate.ts): stamp when a transcript landed so the dispatch
			// gate can tell whether the utterance that triggered a tool has been transcribed yet.
			(s as any)._lastSttAt = _now;
		}
		// #1456 PRECISE name-gate: the gate must key off EXACTLY who spoke — not a
		// loose "if anyone named the bot" rule. It keys ONLY off the CONTROLLER's OWN clean per-
		// user utterance — a relay account / peer bot naming the bot can NOT open it, because this is
		// the controller's separate Discord stream. Stamp the time; the audio-output
		// gate reads this window. Also wake immediately if currently silent.
		// #1456 STICKY conversation — use the CANONICAL gate (name-gate.ts decideForTurn +
		// GateState.lastAddressedToMe), NOT a hand-rolled copy (the sticky-conversation
		// behavior is already implemented there). Feed it the controller's OWN per-user STT
		// transcript so only THEIR stream moves the sticky state (a relay account / peer bot can't). Semantics
		// (from decideForTurn): my-name→allow+sticky, peer-name→drop, standby→drop, neither→
		// carries. Confirmed need via the ✂ log: a reply was cut "name-window expired 41190ms"
		// because the controller named the bot once and kept talking 41s without re-naming.
		// #1427 deterministic wake (clean per-user STT stream): OR the fixed-phrase
		// matcher with the classifier's wake=true — the classifier alone missed
		// obvious wakes ("Hi Lucy, wake up") and switch_mode("active") stayed refused.
		// Applies in BOTH gate models — controller mode accepts only the controller's
		// stream; legacy (no controller, the live deployment) accepts an owner-tier
		// speaker. Without the legacy arm both stamps sat in controller-gated blocks
		// and the no-controller config silently kept the classifier as the single
		// point of failure (found verifying the 2026-06-09 live test).
		{
			const _wakeEligible = VOICE_CONTROLLER
				? userId === VOICE_CONTROLLER
				: effectiveTier(new Set([userId]), ACCESS, TREAT_AS_OWNER) === 'owner';
			if (_wakeEligible && _isWakePhrase(transcript) && !_isEnterMeetingPhrase(transcript)) {
				(s as any)._aiWakeAt = Date.now();
			}
		}
		if (VOICE_CONTROLLER && userId === VOICE_CONTROLLER && s.gate) {
			const _wasAddressed = s.gate.lastAddressedToMe;
			// Track when the controller EXPLICITLY named THIS bot (not the sticky state) —
			// switch_mode is gated on this so only "Hi <bot name>, switch …" switches this bot, and a
			// bare "switch to active mode" (no name) switches nobody.
			if (_aiAddressed || _namesThisBot(transcript)) (s as any)._namedThisBotAt = Date.now();
			decideForTurn(s.gate, transcript);  // updates s.gate.lastAddressedToMe in-place
			// AI-semantic wake (#1427): the model judged the controller is addressing
			// THIS bot — tolerating ASR garble of the name ("hi Maddy"→"hi May") that the
			// string matcher in decideForTurn misses. Open the gate, UNLESS this turn is a
			// standby command (decideForTurn already dropped it — don't fight that).
			if (_aiAddressed && !isStandby(transcript, s.gate.standbyVariants)) {
				s.gate.lastAddressedToMe = true;
			}
			if (s.gate.lastAddressedToMe) {
				(s as any)._controllerNamedAt = Date.now();  // keep window fresh while she's with me (sticky)
				// Fresh name (false→true) wakes from meeting mode — unless THIS utterance is a
				// meeting-ENTER command (naming + "switch to meeting mode" in one breath was
				// waking AND entering and cancelling out; let the enter-cue win).
				if (!_wasAddressed && s.meetingMode && !_isEnterMeetingPhrase(transcript)) {
					s.meetingMode = false; s.allowAckAudible = true; (s as any)._ackEmitted = false;
					try { recordConversation('discord-agent', '⇄ MODE → active (controller named the bot)', s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
				}
			} else if (_wasAddressed) {
				console.log(`${ts()} [NameGate] controller yielded the conversation (named a peer / standby): "${transcript.slice(0, 50)}"`);
			}
		}
		const spk = s.speakerNameCache.get(userId);
		// #1585 MINT the action lease — a REAL user STT turn just landed (this path runs only
		// on inbound Discord audio/VAD). This is the ONLY place a lease is minted, so a
		// model-fabricated "user:" turn can never produce one. Gated tool dispatch requires it.
		s.actionLease = mintLease(transcript, Date.now());
		recordConversation('discord-user', transcript, s.sessionId, {
			speakerId: userId,
			speakerName: spk?.name,
			speakerType: 'human',
			spoken: true,
			tsUnix: _utteranceTsUnix,  // speech-end time, not post-STT write time (fixes 记录反了)
		});
		console.log(`${ts()} [STT] recorded ${userId} (${spk?.name ?? '?'}): "${transcript.slice(0, 60)}"`);
	} catch (err) {
		console.error(`${ts()} [STT] transcription failed for ${userId}:`, err instanceof Error ? err.message : err);
	}
}

function subscribeUser(s: DiscordVoiceSession, userId: string): void {
	if (s.subscribedUsers.has(userId)) return;
	s.subscribedUsers.add(userId);

	// AfterSilence ends the opus sub-stream once the user pauses for this long.
	// 200ms (the old value) is SHORTER than the natural gaps between words and
	// phrases, so one spoken sentence fragmented into 5–6 "utterances" — each
	// firing a silence burst, which interleaved real-speech↔injected-silence
	// and starved Gemini's auto-VAD into never marking turn-end (the recurring
	// "[Watchdog] Gemini session hung — no turn" failure). 800ms coalesces
	// intra-sentence gaps into a single utterance. Env-tunable. (Fix 2026-06-09.)
	const endSilenceMs = Number(process.env.SUTANDO_DISCORD_END_SILENCE_MS) || 800;
	const opusStream = s.connection.receiver.subscribe(userId, {
		// AfterSilence duration via endSilenceMs (#1572 — "no turn" hang fix). The
		// earlier inline `SUTANDO_AFTERSILENCE_MS || 800` (#1427 interruption fix) is
		// superseded by #1572's endSilenceMs (defined above, also 800ms default).
		end: { behavior: EndBehaviorType.AfterSilence, duration: endSilenceMs },
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
		// #1465 defense-in-depth (from #1516): never pipe our own TTS output back
		// to Gemini as user input. If the isBot check raced (fetch error →
		// isBot=false), this catches the bot's own audio before handleAudioFromClient.
		if (userId === s.client.user?.id) return;
		chunks++;
		try { (s.voiceSession as any).handleAudioFromClient(pcm16Mono); } catch {}
		// #1456: ALSO accumulate this user's clean PCM into a per-user buffer for
		// the dedicated STT recording path (separate from the mixed live turn).
		// Cap the buffer so a never-ending stream can't grow unbounded.
		try {
			let buf = s.sttBuffers.get(userId);
			if (!buf) { buf = []; s.sttBuffers.set(userId, buf); }
			const cur = buf.reduce((n, b) => n + b.length, 0);
			if (cur < STT_MAX_BYTES) buf.push(pcm16Mono);
		} catch {}
		(s as any)._noteSpoken?.();
		s.lastUserAudioAt = Date.now();
		if (chunks === 1) console.log(`${ts()} [Voice] first chunk: ${pcm16Mono.length}B`);
	});
	resampler.on('end', () => {
		s.subscribedUsers.delete(userId);
		console.log(`${ts()} [Voice] user ${userId} stopped speaking (${chunks} chunks) — silence burst`);
		// STT-calibration (stt-gate.ts): stamp when a speech burst ended so the dispatch gate
		// knows an utterance is pending transcription (Gemini fires tools off live audio before
		// this burst's STT lands; the gate must wait for it rather than judge stale speech).
		(s as any)._lastSpeechEndAt = Date.now();
		// #1456: utterance just ended (AfterSilence 200ms). Flush this user's
		// accumulated clean PCM and transcribe it on the dedicated STT path —
		// fire-and-forget so the audio pipeline is never blocked. Clear the
		// buffer regardless so the next utterance starts fresh.
		const _buf = s.sttBuffers.get(userId);
		s.sttBuffers.delete(userId);
		if (_buf && _buf.length) {
			const pcm = Buffer.concat(_buf);
			if (pcm.length >= STT_MIN_BYTES) {
				void transcribeAndRecordUtterance(s, userId, pcm);
			}
		}
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

	// #1427 stop-word interrupt (Susan 2026-06-10): cut the bot off NOW, in
	// code — drop everything queued, halt the playing resource, and squelch
	// the remainder of this Gemini turn (chunks keep streaming after a stop;
	// without the squelch the reply resumes mid-sentence). The squelch flag is
	// cleared by the existing >1.5s turn-gap detector in handleAudioOutput.
	const _interruptPlayback = (why: string): void => {
		const dropped = audioOutQueue.length;
		audioOutQueue.length = 0;
		try { player.stop(true); } catch {}
		console.log(`${ts()} [Interrupt] playback cut (${why}) — dropped ${dropped} queued buffer(s), squelching rest of turn`);
	};

	const s: DiscordVoiceSession = {
		sessionId,
		connection,
		player,
		guildId: GUILD_ID!,
		channelId: CHANNEL_ID!,
		voiceSession: null as unknown as VoiceSession,
		startTime: Date.now(),
		transcript: [],
		groundingContext: null,
		actionLease: null,
		resultQueue: [],
		pendingTasks: 0,
		closing: false,
		subscribedUsers: new Set(),
		client,
		botFlagCache: new Map(),
		speakerNameCache: new Map(),
		sttBuffers: new Map(),
		turnSpeakers: new Set(),
		lastSpeaker: null,
		audioPending: [],
		toolCalls: [],
		events: [{ event: 'session_started', timestamp: new Date().toISOString() }],
		// #1427: JOIN in ACTIVE mode (respond normally). Only
		// switch to silent meeting-mode on a cue ("take notes"/"meeting mode") or
		// the auto-timeout — not silent-upfront. The name-gate's per-turn silencing
		// (below) applies only once s.meetingEntered is true.
		// The desired toggle is active ⟷ meeting: "standby"/"hold on" → meeting
		// (silent), "hi <bot name>" → back to active. Join ACTIVE; the cues flip state.
		// The bot ALWAYS joins ACTIVE (standing rule — do not regress this).
		// standby/"hold on" → meeting (silent), "hi <bot name>" → active. Do NOT join
		// silent — that made it ignore the controller when addressed (one-turn-lag).
		// The strict controller gate below still keeps it
		// from answering anyone but the controller; the tool-gate keeps it inert
		// while silent.
		meetingMode: false,
		meetingEntered: false,
		allowAckAudible: false,
		screenShareOn: false,
		pushIndicatorMsgId: null,
		pushIndicatorTimer: null,
		// Build the name-gate iff we have a stand name + (at least one peer name OR
		// meeting-buddy mode, which keeps the gate active with no peers present).
		gate: (STAND_NAME && (PEER_NAMES.length > 0 || SUTANDO_MEETING_MODE))
			? createGate({ instanceName: STAND_NAME, nameAliases: STAND_NAME_ALIASES, otherInstances: PEER_NAMES, primary: TREAT_AS_OWNER, meetingMode: SUTANDO_MEETING_MODE, standbyAliases: STANDBY_PHRASES })
			: null,
		lastUserAudioAt: Date.now(),
	};
	s.interruptPlayback = (why: string) => {
		(s as any)._squelchThisTurn = true;
		_interruptPlayback(why);
		try { recordEvent('discord-voice', 'interrupt', JSON.stringify({ why, at: new Date().toISOString() }), s.sessionId); } catch {}
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
		// NOTE: turn-end is governed by Gemini Live's built-in automatic VAD plus
		// the post-utterance silence burst (see SILENCE_BURST_FRAMES). The bundled
		// bodhi-realtime-agent build exposes no server-VAD tuning hook, so an
		// earlier `vadConfig: { silenceDurationMs }` option here was silently
		// ignored — removed 2026-06-09 to stop implying VAD is tuned. Real
		// server-VAD tuning needs the newer Bodhi framework (realtimeInputConfig
		// .automaticActivityDetection); tracked as the Tier-2 upgrade.
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
				// #1427: drive the 👁 screen-share indicator off the tool result.
				// join_discord_screen started the stream → show indicator IFF the
				// stream is actually live (isStreaming, read from vision-tools — not
				// modified); stop_vision → clear it. Streaming stays the tool's job.
				if (toolName === 'join_discord_screen') {
					void (async () => {
						try {
							const vt = await import('../../../src/vision-tools.js');
							if (vt.isStreaming()) await setScreenShareIndicator(s, true);
						} catch (err) { console.error(`${ts()} [ScreenShare] indicator-on failed:`, err); }
					})();
				} else if (toolName === 'stop_vision') {
					void setScreenShareIndicator(s, false);
				}
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

	// The PRECISE sticky/wake state lives in transcribeAndRecordUtterance (per-user STT) —
	// that's the single authority. BUT the per-user STT is async (~1s), which lagged the
	// switch_mode TOOL gate: Gemini calls switch_mode the instant it hears "Hi <bot name>", before
	// the STT has registered the name, so the gate wrongly blocked it ("controller hasn't
	// addressed the bot") and the bot stayed in meeting mode (confirmed via
	// the log). Fix: set the switch_mode freshness signal (_namedThisBotAt) from the LIVE input
	// the moment the CONTROLLER's voice names the bot — fast, no STT wait. Gated to lastSpeaker
	// so a peer/relay naming the bot doesn't open switch_mode. Does NOT touch sticky/meeting
	// state (the per-user STT still owns those precisely).
	try {
		const _nt = (session as any).transport;
		if (_nt) {
			const _origNT = _nt.onInputTranscription?.bind(_nt);
			_nt.onInputTranscription = (text: string) => {
				try {
					// Controller mode: only the controller's stream counts. Legacy
					// (no controller, #1427 population-aware regime): an owner-tier
					// last speaker counts — group switch_mode needs _namedThisBotAt
					// in legacy too, and it was never set there (controller-gated).
					const _eligible = VOICE_CONTROLLER
						? s.lastSpeaker === VOICE_CONTROLLER
						: !!s.lastSpeaker && effectiveTier(new Set([s.lastSpeaker]), ACCESS, TREAT_AS_OWNER) === 'owner';
					if (_eligible) {
						// #1427 stop-word: only meaningful while the bot is audibly
						// talking (that context is what makes a bare "stop" unambiguous).
						const _botTalking = Date.now() - ((s as any)._lastAudioTs || 0) < 2500;
						if (_botTalking && isStopWord(text)) s.interruptPlayback?.('stop-word: ' + text.slice(0, 40));
						if (_namesThisBot(text)) (s as any)._namedThisBotAt = Date.now();
						// #1427 deterministic wake fast path: the switch_mode("active") gate
						// requires a fresh _aiWakeAt, but that was set ONLY by the async per-user
						// STT classifier — which missed/lagged obvious wakes ("Hi Lucy, wake up",
						// "Hi Lucy, can you hear me") and the tool got refused in a loop. A
						// name-qualified fixed wake phrase in the LIVE input is unambiguous —
						// stamp the wake here too, so EITHER signal (classifier wake=true OR
						// deterministic phrase) opens the gate. Enter-cue still wins in-turn.
						if (_isWakePhrase(text) && !_isEnterMeetingPhrase(text)) (s as any)._aiWakeAt = Date.now();
					}
				} catch {}
				_origNT?.(text);
			};
		}
	} catch {}

	// Clear any STALE 👁 screen-share indicator left by a previous session that
	// crashed without cleaning up (the Set-Voice-Channel-Status API needs the bot
	// IN the channel, so a disconnected session can't clear its own status). We
	// ARE connected now — clear it; a fresh share this session re-sets it.
	try { await _setScreenIndicators(s, false); } catch {}
	try { await _clearModeIndicator(s); } catch {}

	// [Outbound] Gemini PCM 24k mono → upsample to 48k stereo → pipe to AudioPlayer.
	const sessionAny = session as any;
	let outChunks = 0;
	sessionAny.handleAudioOutput = (data: string) => {
		sessionAny.notificationQueue?.markAudioReceived?.();
		try {
			const pcm24Mono = Buffer.from(data, 'base64');
			const pcm48Stereo = upsample24MonoTo48Stereo(pcm24Mono);
			// #1456 PRECISE name-gate at the AUDIO output. When a
			// controller is configured, the bot speaks ONLY if the CONTROLLER named it
			// in their own utterance within the window (per-user STT set _controllerNamedAt)
			// — deterministic, at output time, ignores the mixed turn entirely so a relay account / peer bot
			// can't open it. allowAckAudible still lets an entry/wake ack through. Without a
			// controller → legacy meeting-mode suppression.
			// Per-turn LATCH (fixes a follow-up sentence being dropped): once a reply STARTS
			// while addressed (within window), let the WHOLE turn play — the window
			// expiring mid-reply must NOT cut off later sentences. The latch is reset
			// at turn.end, so the window only governs whether a NEW turn opens.
			const _NAMEGATE_WINDOW = Number(process.env.SUTANDO_NAMEGATE_WINDOW_MS) || 20000;
			// Coherent gate (refactor 2026-06-05): allowAckAudible (entry/wake ack) always
			// passes. Else in MEETING mode → fully silent (note-taker). In ACTIVE mode + a
			// controller set → PRECISE per-stream gate: speak only if the controller's OWN
			// utterance named the bot within the window (or the latch keeps an in-progress
			// reply going). No controller → legacy (active = speak freely).
			//
			// LATCH tracks the bot's CONTINUOUS audio, NOT conversation turns (fixes the
			// last sentence being dropped in ACTIVE mode): a peer/relay interrupting mid-reply
			// fired turn.end and reset the latch, so the tail got re-gated by the window and
			// cut. Now the latch resets only on a real GAP in the bot's own output (>1.5s) — i.e. its reply
			// actually finished — so an interruption can't sever an in-progress reply.
			const _nowMs = Date.now();
			if (_nowMs - ((s as any)._lastAudioTs || 0) > 1500) { (s as any)._turnAudioAllowed = false; (s as any)._audioPlayedThisTurn = false; (s as any)._recvThisTurn = 0; (s as any)._squelchThisTurn = false; }
			// #1427 stop-word interrupt: the user cut this reply off — drop the
			// turn's remaining chunks (Gemini keeps streaming after player.stop).
			// Do NOT update _lastAudioTs while squelched, so the >1.5s gap above
			// clears the flag once the burst ends.
			if ((s as any)._squelchThisTurn) return;
			// #1456 observability (record what isn't yet recorded): count chunks RECEIVED from
			// Gemini this turn, NOT just chunks pushed. Without this, an ack suppressed from its
			// FIRST chunk logged nothing — indistinguishable from "Gemini emitted no audio at all".
			(s as any)._recvThisTurn = ((s as any)._recvThisTurn || 0) + 1;
			// Audio OUTPUT gate — one call, one source of truth (shouldEmitAudio, defined
			// near VOICE_CONTROLLER). Active mode answers ANY human; meeting mode stays
			// silent unless summoned by name. Summon/mode name-control is unchanged (it
			// lives in the tool + meeting-enter gates). Post bodhi #20 this same function
			// is passed as config.shouldEmitAudio and this monkey-patch path is deleted.
			const _audioOpen = shouldEmitAudio(s, _nowMs);
			// #1427 audit (Susan: "可以通过 audit sqlite log 里的 mode switch 来判断它做
			// 的对不对"): ONE speak_decision row per bot turn — written at the turn's
			// FIRST received chunk (this gate runs per chunk; logging each would flood).
			// Legacy path only: _lastSpeakDecision is set by decideSpeak in shouldEmitAudio.
			if ((s as any)._recvThisTurn === 1 && (s as any)._lastSpeakDecision) {
				const _d = (s as any)._lastSpeakDecision;
				try { recordEvent('discord-voice', 'speak_decision', JSON.stringify({ ..._d, humans: (s as any)._humanCount ?? 1, meeting: !!s.meetingMode, lastSpeaker: s.lastSpeaker || null }), s.sessionId); } catch {}
			}
			if (_audioOpen) {
				(s as any)._turnAudioAllowed = true;  // latch — held across continuous audio
				(s as any)._lastAudioTs = _nowMs;
				(s as any)._wasPlaying = true;
				(s as any)._audioPlayedThisTurn = true;  // #1456: this turn was HEARD (audio left the gate)
				pushAudio(pcm48Stereo);
				outChunks++;
				if (s.allowAckAudible) (s as any)._ackEmitted = true;
				if (outChunks === 1 || outChunks % 50 === 0) {
					console.log(`${ts()} [Audio] outbound chunks: ${outChunks} (last=${pcm48Stereo.length}B)`);
				}
			} else {
				// #1456 OBSERVABILITY (don't guess — record the inputs): audio is
				// SUPPRESSED. Record the EXACT gate inputs so the cause is CONFIRMED, never guessed.
				// Two cases: (a) mid-reply cut (last sentence dropped) — was playing, now gated; (b) muted
				// from the FIRST chunk (the meeting-ack case) — Gemini DID emit audio but the gate
				// never opened. Distinguishing them needs recv-count + forceAudible in the reason.
				const _sinceNamed = _nowMs - ((s as any)._controllerNamedAt || 0);
				const _forceAudible = _nowMs < ((s as any)._forceAudibleUntil || 0);
				const _reason = `meetingMode=${s.meetingMode} allowAck=${!!s.allowAckAudible} forceAudible=${_forceAudible} humanSpoke=${!!(s as any).lastSpeaker} lastSpeaker=${(s as any).lastSpeaker} addressedToMe=${s.gate?.lastAddressedToMe ?? 'n/a'} recv=${(s as any)._recvThisTurn}`;
				if ((s as any)._wasPlaying) {
					(s as any)._wasPlaying = false;
					console.log(`${ts()} [Audio] ✂ SUPPRESSED mid-reply — ${_reason} (chunks so far=${outChunks})`);
					try { recordConversation('discord-agent', `✂ audio cut mid-reply — ${_reason}`, s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
				} else if ((s as any)._recvThisTurn === 1) {
					// Muted from the very first chunk of this turn — Gemini emitted audio, gate closed.
					console.log(`${ts()} [Audio] ✂ MUTED from start — ${_reason}`);
					try { recordConversation('discord-agent', `✂ audio muted from start — ${_reason}`, s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
				}
			}
		} catch (err) {
			console.error(`${ts()} [Audio] outbound convert failed:`, err);
		}
	};

	// --- Meeting mode: manual poll + auto-idle ---
	// Manual: read state/voice-mode.txt every 2s (same file Sutando.app + voice-agent write).
	// Auto:   flip to meeting mode after AUTO_MEETING_TIMEOUT_MS with no user audio.
	// Both timers are cleared in finalizeSession() via the closing flag check.
	const voiceModePoll = setInterval(() => {
		if (s.closing) { clearInterval(voiceModePoll); return; }
		// When the name-gate is active (a peer bot is configured), the gate is the
		// SOLE owner of meetingMode — it flips per-turn on name-address. The manual
		// voice-mode.txt toggle would otherwise fight the gate every 2s (reverting
		// each gate decision back to the file's value), so skip the manual override
		// in gate mode. Single-bot / no-gate keeps the manual toggle.
		if (!s.gate) {
			try {
				const mode = readFileSync(VOICE_MODE_FILE, 'utf-8').trim();
				const want = mode === 'meeting';
				if (want !== s.meetingMode) {
					s.meetingMode = want;
					console.log(`${ts()} [Meeting] voice-mode.txt → ${mode} (meetingMode=${s.meetingMode})`);
				}
			} catch { /* file absent = active mode */ }
		}
		// Mode indicator (🗣 active / 🔇 旁听) — runs in gate and non-gate mode; the
		// only-on-change guard inside keeps this from spamming the nickname rate limit.
		_setModeIndicator(s, !s.meetingMode).catch(() => {});
	}, 2_000);

	// AUTO_MEETING_TIMEOUT_MS === 0 means auto-meeting is disabled.
	const autoMeetingTimer = AUTO_MEETING_TIMEOUT_MS > 0 ? setInterval(() => {
		if (s.closing) { clearInterval(autoMeetingTimer!); return; }
		// Skip when the gate owns meetingMode — it already defaults to silent and
		// flips per-turn, so auto-idle-to-meeting is redundant and would fight it.
		if (!s.gate && !s.meetingMode && Date.now() - s.lastUserAudioAt > AUTO_MEETING_TIMEOUT_MS) {
			s.meetingMode = true;
			console.log(`${ts()} [Meeting] auto-meeting triggered — no user audio for ${AUTO_MEETING_TIMEOUT_MS / 1000}s`);
			try { writeFileSync(VOICE_MODE_FILE, 'meeting'); } catch {}
		}
	}, 10_000) : null;

	// Transcript mirroring + result-queue drain
	let lastProcessedIdx = 0;
	session.eventBus.subscribe('turn.end', () => {
		// #1456: engage DEFERRED meeting silence now that the spoken ack turn has finished
		// (switch_mode → meeting kept us active so Gemini could SAY the confirmation; real
		// silence starts here). force-audible (set in switch_mode) keeps any ack tail playing.
		if ((s as any)._pendingMeeting && ((s as any)._audioPlayedThisTurn || Date.now() >= ((s as any)._forceAudibleUntil || 0))) {
			(s as any)._pendingMeeting = false;
			s.meetingMode = true; s.meetingEntered = true;
			try { recordConversation('discord-agent', '⇄ MODE → meeting (after ack)', s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
			try { writeFileSync(VOICE_MODE_FILE, 'meeting'); } catch {}
			console.log(`${ts()} [Meeting] deferred meeting engaged after ack`);
		}
		// (Refactor 2026-06-05) The audio latch is NO LONGER reset here — turn.end fires
		// when a peer bot / relay account interrupts, which prematurely cut the bot's reply. The latch now
		// resets on a real gap in the bot's OWN output (see handleAudioOutput), so an
		// interruption can't sever an in-progress reply.
		// Watchdog: a turn completed — clear the hang counters.
		(s as any).lastTurnActivityTs = Date.now();
		(s as any).utterancesSinceTurn = 0;
		// #1427: once the entering-meeting acknowledgement has actually been spoken
		// (audio emitted this turn), engage full silence by clearing the one-shot
		// audible override. Only fires after _ackEmitted, so the ack turn is
		// guaranteed audible regardless of how many user turns intervene.
		if (s.allowAckAudible && (s as any)._ackEmitted) {
			s.allowAckAudible = false;
			(s as any)._ackEmitted = false;
			console.log(`${ts()} [Meeting] entry-ack delivered audibly, silence now engaged`);
		}
		// #1427 single-turn summon (Susan 2026-06-10, group-meeting test): in
		// MEETING mode a name-summon ("Lucy, …") must answer EXACTLY THIS turn,
		// then auto-return to silence — otherwise the sticky lastAddressedToMe
		// bit kept the bot answering every subsequent turn and "meeting mode
		// wouldn't stick" (she had to re-issue stand-by / it talked over a
		// 2-person meeting). Reset the sticky bit at turn.end so the NEXT turn
		// requires a fresh name. MEETING ONLY — active-mode stickiness (keep
		// talking to whoever named me) is intentional and untouched. Skipped
		// while a mode-switch ack is in flight so the ack turn isn't cut.
		if (shouldResilenceAtTurnEnd(!!s.meetingMode, !!s.allowAckAudible, s.gate?.lastAddressedToMe === true)) {
			s.gate.lastAddressedToMe = false;
			console.log(`${ts()} [Meeting] single-turn summon answered — re-silenced (next turn needs a fresh name)`);
		}
		// Tier gate: the turn is over — its speaker attribution no longer
		// applies. The next turn re-accumulates speakers from speaking.start.
		// #1427 attribution: snapshot THIS turn's human speaker BEFORE clearing,
		// so the user-row attribution below uses who actually spoke this turn —
		// not s.lastSpeaker (the last audio-packet sender), which mis-attributes
		// when multiple people are in the channel (e.g. two identities for one person:
		// a main account + a test account). Prefer a single human speaker;
		// if several humans spoke, keep lastSpeaker (best available); fall back
		// to lastSpeaker only when the turn set has no humans.
		const _turnHumans = [...s.turnSpeakers].filter(
			id => s.speakerNameCache.get(id)?.type !== 'agent');
		const _turnSpeakerId: string | null =
			_turnHumans.length === 1 ? _turnHumans[0]
			: (s.lastSpeaker && _turnHumans.includes(s.lastSpeaker)) ? s.lastSpeaker
			: (_turnHumans[0] ?? s.lastSpeaker ?? null);
		s.turnSpeakers.clear();
		const items = session.conversationContext.items;
		if (items.length < lastProcessedIdx) lastProcessedIdx = 0;
		const lastText = s.transcript.length > 0 ? s.transcript[s.transcript.length - 1].text : null;
		for (const item of items.slice(lastProcessedIdx)) {
			if (item.content === lastText) continue;
			if (item.role === 'user') {
				s.transcript.push({ role: 'user', text: item.content });
				// #1427: enter-meeting cue — bot joins ACTIVE; a cue switches it to
				// silent meeting / note-taker mode (engages the per-turn name-gate).
				// #1427: only enter meeting mode when THIS bot is named. A cue
				// addressed to a peer ("<peer name>, take notes") must NOT flip this bot —
				// that false-trigger left this bot silently stuck. No
				// gate (single-bot / no stand name) → any cue still enters (back-compat).
				// Direct standby/silence commands flip THIS bot to meeting regardless of
				// naming — they're control commands, not addressed-to-a-peer cues. Note-
				// taking cues ("take notes"/"meeting mode") still require naming (cross-bot).
				const _directStandby = ['standby', 'stand by', 'be silent', 'go silent'].some(p => item.content.toLowerCase().includes(p));
				// #1456: only the designated controller may flip meeting mode —
				// a relay account (a peer bot / relay user-id), a peer, or the bot's own echo cannot.
				// Robustness: require the controller to be the SOLE
				// human speaker this turn — NOT _turnSpeakerId, whose lastSpeaker
				// fallback mis-attributes a relay/peer's audio (a peer bot via a relay user-id) to the
				// controller and false-triggered meeting mode when a peer bot said "silence".
				// If anyone else (or no human, or several humans) is in this turn, the
				// cue is not trusted as a controller command. The rule: only the controller.
				const _byController = !VOICE_CONTROLLER ||
					_turnHumans.includes(VOICE_CONTROLLER);  // controller participated (not necessarily sole — a relay/peer is usually also audible)
				if (_byController && !s.meetingEntered && _isEnterMeetingPhrase(item.content) && _looksLikeCommand(item.content) && (!s.gate || _namesThisBot(item.content) || _directStandby)) {
					s.meetingEntered = true;
					s.meetingMode = true;
					try { recordConversation('discord-agent', '⇄ MODE → meeting (standby cue)', s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
					// #1427: let the bot's acknowledgement be HEARD before silence
					// engages — without this, the same turn that sets meetingMode=true
					// also suppresses the ack.
					s.allowAckAudible = true;
					(s as any)._ackEmitted = false;
					console.log(`${ts()} [Meeting] enter-meeting cue — switching to meeting mode: "${item.content.slice(0, 60)}"`);
					try { writeFileSync(VOICE_MODE_FILE, 'meeting'); } catch {}
					// #1456 (the bot did not acknowledge meeting mode): in
					// meeting mode Gemini just goes silent and swallows the confirmation,
					// so the user never hears an ack. allowAckAudible only un-suppresses
					// the ack turn's audio — it doesn't make Gemini SPEAK one. Inject a
					// prompt (role:user, natural phrasing to avoid fabrication leak) so it
					// produces one short spoken confirmation, then the gate re-silences it.
					try { injectSystemMessage(s, "You are now switching to silent note-taking mode. Reply with ONE short spoken sentence confirming it (for example: \"Got it — I'll take notes silently from here.\"), then stay silent and only listen."); } catch {}
				}
				// Wake-phrase detection: exit meeting mode back to active. BUT skip if the
				// SAME utterance also carries an enter/standby cue (#1427):
				// "Hi <bot name>, can you stand by?" contains both a wake form ("hi <bot name>") and a
				// standby cue ("stand by") — the intent is STANDBY, so the enter wins. Without
				// this guard, enter then wake fire in the same turn and the bot never stays silent.
				if (_byController && s.meetingEntered && _isWakePhrase(item.content) && !_isEnterMeetingPhrase(item.content)) {
					s.meetingEntered = false;
					s.meetingMode = false;
					try { recordConversation('discord-agent', '⇄ MODE → active (wake phrase)', s.sessionId, { speakerId: s.client.user?.id, speakerName: STAND_NAME || 'bot', speakerType: 'agent' }); } catch {}
					console.log(`${ts()} [Meeting] wake-phrase detected — exiting meeting mode: "${item.content.slice(0, 60)}"`);
					try { writeFileSync(VOICE_MODE_FILE, 'active'); } catch {}
				}
				// Screen-push voice phrases REMOVED (#1427): the only magic word is the
				// configured summon phrase. Screen sharing is now driven solely by the
				// join_discord_screen inline tool (the model calls it on "join/share
				// screen"), so there is no screen-push magic phrase here anymore.
				// Speak-gate (name-gate) — LEGACY (no controller) path only.
				// (Refactor 2026-06-05) When a VOICE_CONTROLLER is set, meetingMode is NOT
				// auto-flipped per mixed turn here — that fought the precise per-stream audio
				// gate and caused the thrash. In controller mode, meetingMode changes ONLY via
				// the explicit standby cue (→meeting) and the precise per-user-STT wake
				// (controller names the bot →active); the per-utterance speak decision is the
				// audio-output gate. So this block runs only for the legacy owner-tier model.
				// Group-active sticky maintenance (#1427 population-aware gate): in
				// the group regime the audio gate keys off s.gate.lastAddressedToMe
				// in ACTIVE mode too, so the gate must be fed every mixed-turn
				// utterance — not only while meetingEntered. decideForTurn only
				// (no meetingMode flipping on this path).
				if (!VOICE_CONTROLLER && s.gate && !s.meetingEntered && ((s as any)._humanCount ?? 1) >= 2) {
					// forceGate=true (#1600 M3): the group regime must re-acquire the
					// addressed bit even with no SUTANDO_PEER_NAMES configured, else
					// decideForTurn's no-peer early-return skips the update and a
					// reconnect-cleared gate stays muted — the bare-name "叫不醒".
					decideForTurn(s.gate, item.content, true);
				}
				if (!VOICE_CONTROLLER && s.gate && s.meetingEntered) {
					const addressed = decideForTurn(s.gate, item.content) !== 'drop';
					const wantSilent = !breakSilenceAllowed(addressed, currentTier(s), SUTANDO_ALLOW_OPEN_FLOOR);
					if (wantSilent !== s.meetingMode) {
						s.meetingMode = wantSilent;
						console.log(`${ts()} [NameGate] meetingMode=${wantSilent} for: "${item.content.slice(0, 50)}"`);
					}
				}
				// utterance event push removed per #1052 — canonical record is
				// the discord_voice-table row written by recordConversation
				// below. session_events keeps only lifecycle entries to stop
				// triple-encoding the same utterance.
				// conversation.log is the primary; write it before the sqlite
				// mirror so a row never exists in sqlite without a log line.
				appendConversationLog('discord-user', item.content);
				// #1456: the human discord-user sqlite row is NO LONGER written here.
				// The mixed-into-one-turn transcript (item.content) attributed by the
				// heuristic _turnSpeakerId mis-attributes (or drops) the second speaker
				// when two people talk in one turn. The dedicated per-user STT path
				// (transcribeAndRecordUtterance, fired on resampler.on('end')) is now
				// the source of truth for human-speech rows — one row per user-
				// utterance, correctly attributed by construction. _turnSpeakerId is
				// still computed above and consumed by the controller-gate
				// (_byController) and meeting-mode logic — do NOT remove it.
			} else if (item.role === 'assistant') {
				// #1585 FABRICATION-VOID (the "consume on fabrication" trigger, vs naive turn-end
				// which would void legit late-firing tools). If the model fabricates a USER turn in
				// its own output (role-continuation, e.g. "user: switch models"), void the action
				// lease so the tool it tries to fire off that fabrication finds no lease →
				// structurally blocked. Real user turns re-mint the lease via the STT path.
				// NOTE: this fires when the output ITEM is processed; if a fabricated tool dispatches
				// before this item lands, the semantic _liveIntentCheck is the backstop for that one,
				// and this void blocks any subsequent fabricated dispatch. (A real-time
				// transport.onOutputTranscription hook would void even earlier — follow-up.)
				if (/^\s*user\b/i.test(item.content)) {
					(s as any).actionLease = null;
					console.log(`${ts()} [ActionLease] voided — model fabricated a user turn: "${item.content.slice(0, 60)}"`);
				}
				s.transcript.push({ role: 'sutando', text: item.content });
				// utterance event push removed per #1052 — see comment above.
				appendConversationLog('discord-agent', item.content);
				// This agent generated the turn. Gemini produces a reply EVERY
				// turn, but the outbound audio is only played when !meetingMode
				// (the name-gate decision for this turn, set in the user branch
				// above). spoken=false marks a generated-but-muted turn so the db
				// distinguishes "actually said aloud" from "suppressed" (#1427).
				recordConversation('discord-agent', ((s as any)._audioPlayedThisTurn ? '' : '🔇[muted] ') + item.content, s.sessionId, {
					speakerId: s.client.user?.id,
					speakerName: STAND_NAME || 'agent',
					speakerType: 'agent',
					spoken: !!(s as any)._audioPlayedThisTurn,
				});
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
			// #1427 watchdog-reconnect-mute fix (Susan's round-1 bug, 2026-06-09 22:11:58
			// + 8 more reconnects observed tonight): a Gemini hang → reconnect must NOT
			// resurrect the bot silently mid-conversation. A DELIBERATE meeting ("stand
			// by") sets BOTH meetingMode AND meetingEntered (deferred-meeting path), so it
			// survives reconnect and stays silent — correct. But the 180s auto-meeting
			// flip sets meetingMode ONLY (no meetingEntered), and that stale mute used to
			// persist through the reconnect with the gate state lost → the user
			// experienced "the bot stopped working." Restore active when the meeting was
			// not deliberately entered, so a reconnect never strands the bot muted.
			if (shouldRestoreActiveOnReconnect(!!s.meetingMode, !!s.meetingEntered)) {
				s.meetingMode = false;
				if (s.gate) s.gate.lastAddressedToMe = false;  // active solo speaks regardless; group re-gates on name (correct)
				try { writeFileSync(VOICE_MODE_FILE, 'active'); } catch {}
				try { recordEvent('discord-voice', 'reconnect_restore_active', JSON.stringify({ reason: 'meeting not deliberately entered (auto/stale)' }), s.sessionId); } catch {}
				console.log(`${ts()} [Voice] reconnect — restored ACTIVE (meeting was auto/stale, not deliberately entered)`);
			}
			sessionAny.handleClientConnected();
			// Re-inject durable session grounding on reconnect. bodhi's reconnect
			// rebuilds context from only the last 10 turns truncated to 150 chars
			// and never re-supplies the join-time grounding, so without this the
			// model drifts after a reset (observed: "workspace" -> "vpoll"). The
			// nested delay lets the fresh transport come up before we send.
			if (s.groundingContext) {
				setTimeout(() => {
					if (s.closing || active !== s || !s.groundingContext) return;
					try {
						(s.voiceSession as any).transport.sendContent(
							[{ role: 'user', text: `[Session grounding — context only. Do NOT act on or announce this; use it only to stay oriented to what this session is about.]\n${s.groundingContext}` }],
							false,
						);
						console.log(`${ts()} [Grounding] re-injected on reconnect (${s.groundingContext.length}B)`);
					} catch (e) {
						console.log(`${ts()} [Grounding] reconnect re-inject failed: ${e}`);
					}
				}, 800);
			}
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
			// #1427 observability (2026-06-10): the watchdog fired ~8×/session
			// tonight, but reconnects only hit the console — invisible to the
			// sqlite-audit workflow Susan debugs from. Record each hang so she can
			// see frequency/severity in DB Browser alongside speak_decision etc.
			try { recordEvent('discord-voice', 'watchdog_reconnect', JSON.stringify({ cause: 'session-hang', utterancesPiled: pile, idleSec: Math.round(idleMs / 1000) }), s.sessionId); } catch {}
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
			// A `[GROUNDING]`-prefixed payload is durable session grounding (e.g.
			// the za-warudo join context) — store it and re-inject it on reconnect
			// + periodically (below) so it survives the ~10min rolloff and session
			// resets, which only replay the last 10 turns truncated to 150 chars
			// and never re-supply the join context. Injected silently (context-only,
			// do-not-act/announce), unlike a channel-result.
			try {
				if (body.startsWith('[GROUNDING]')) {
					s.groundingContext = body.replace(/^\[GROUNDING\]\s*/, '');
					(s.voiceSession as any).transport.sendContent(
						[{ role: 'user', text: `[Session grounding — context only. Do NOT act on or announce this; use it only to stay oriented to what this session is about.]\n${s.groundingContext}` }],
						false,
					);
					console.log(`${ts()} [Grounding] stored + injected durable grounding (${s.groundingContext.length}B)`);
				} else {
					(s.voiceSession as any).transport.sendContent(
						[{ role: 'user', text: `[Channel result]\n${body}\n\nReport this result to the user now.` }],
						true,
					);
				}
			} catch (e) {
				console.log(`${ts()} [ChannelScan] inject failed for ${name}: ${e}`);
			}
			// Read-and-delete so the scan doesn't re-deliver and so other
			// consumers can't pick the file up after we've claimed it.
			try { unlinkSync(full); } catch {}
		}
	}, 3000);
	(s as any)._channelScanHandle = channelScan;

	// Periodic backstop: re-inject durable grounding every 4min so it survives the
	// ~10min context rolloff even within a single connection (no reconnect). The
	// reconnect handler above covers session resets; this covers in-connection
	// rolloff. Without it a flash model drifts (observed: "workspace"->"vpoll").
	// Re-sent in full, silent (context-only).
	const groundingReinject = setInterval(() => {
		if (s.closing || !s.groundingContext) return;
		try {
			(s.voiceSession as any).transport.sendContent(
				[{ role: 'user', text: `[Session grounding -- context only. Do NOT act on or announce this; use it only to stay oriented to what this session is about.]\n${s.groundingContext}` }],
				false,
			);
			console.log(`${ts()} [Grounding] re-injected durable grounding (${s.groundingContext.length}B)`);
		} catch (e) {
			console.log(`${ts()} [Grounding] re-inject failed: ${e}`);
		}
	}, 240000);
	(s as any)._groundingReinjectHandle = groundingReinject;

	// Subscribe to anyone currently speaking, and to anyone who starts.
	connection.receiver.speaking.on('start', async (userId) => {
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
				// Same fetch feeds the speaker-attribution cache (#1427): prefer
				// the guild nickname, fall back to the global username.
				let display = user.username;
				try {
					const member = await s.client.guilds.cache.get(s.guildId)?.members.fetch(userId);
					if (member?.displayName) display = member.displayName;
				} catch { /* nickname unavailable — keep username */ }
				s.speakerNameCache.set(userId, { name: display, type: isBot ? 'agent' : 'human' });
			} catch {
				isBot = false;
			}
			s.botFlagCache.set(userId, isBot);
		}
		if (isBot && !ALLOWED_BOT_USER_IDS.has(userId)) {
			console.log(`${ts()} [Voice] ignoring bot user ${userId} (not in SUTANDO_ALLOWED_BOT_USER_IDS)`);
			return;
		}
		// Attribute this speaker to the in-progress turn — ONLY after passing the
		// bot allow/deny gate, so an ignored peer-bot can't poison tier attribution
		// (turnSpeakers feeds effectiveTier; a stray bot id would drag the turn to
		// 'other' and, via the lastSpeaker fallback, could even deny the owner).
		s.turnSpeakers.add(userId);
		s.lastSpeaker = userId;
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

	// Stop the screen-share indicator's frame-count ticker (the indicator itself
	// is cleared while still connected in shutdownAfterFlush; this just kills the timer).
	if (s.pushIndicatorTimer) { try { clearInterval(s.pushIndicatorTimer); } catch {} s.pushIndicatorTimer = null; }

	detachVisionFromSession();

	try { clearInterval((s as any)._tickHandle); } catch {}
	try { clearInterval((s as any)._outTickHandle); } catch {}
	try { clearInterval((s as any)._watchdogHandle); } catch {}
	try { clearInterval((s as any)._channelScanHandle); } catch {}
	try { clearInterval((s as any)._groundingReinjectHandle); } catch {}
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
			// #1120: if the spawner threaded --reply-channel and --reply-user
			// through, post the refusal in that channel (mentioning the
			// inviter) — "reply where invited" instead of falling back to
			// owner-DM. The previous proactive-*.txt path stays as fallback
			// only when those args are absent (out-of-band spawns, manual
			// testing).
			const channelName = (channel as any).name ?? CHANNEL_ID;
			const refusalText =
				`Skipping voice join in #${channelName} — peer already present: ${presentPeers.join(', ')}. ` +
				`Single-bot enforcement (#1089); reinvite once they leave.`;
			const REPLY_CHANNEL_ID = getArg('reply-channel');
			const REPLY_USER_ID = getArg('reply-user');
			// Track whether the channel-reply was actually delivered. If not — for ANY
			// reason: arg absent, fetch threw, channel isn't text-capable, send threw —
			// fall back to proactive-*.txt so the operator still sees the refusal.
			// (Per @bassilkhilo-ag2's #1132 review: prior shape logged "falling back to
			// proactive-*.txt" on catch but didn't actually write it, silently dropping
			// the #1089 refusal when the channel send failed.)
			let channelReplyDelivered = false;
			if (REPLY_CHANNEL_ID) {
				try {
					const replyCh = await client.channels.fetch(REPLY_CHANNEL_ID);
					if (replyCh && 'send' in replyCh) {
						const mention = REPLY_USER_ID ? `<@${REPLY_USER_ID}> ` : '';
						await (replyCh as any).send(mention + refusalText);
						channelReplyDelivered = true;
					}
				} catch (e) {
					console.error(`${ts()} [Setup] #1120 channel-reply failed:`, e);
				}
			}
			if (!channelReplyDelivered) {
				try {
					const proactivePath = join(WORKSPACE_DIR, 'results', `proactive-${Date.now()}.txt`);
					writeFileSync(proactivePath, refusalText + '\n');
				} catch (e) {
					console.error(`${ts()} [Setup] #1089 couldn't surface refusal to operator:`, e);
				}
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
			// the Gemini Live transport so the bot speaks before disconnecting.
			// We don't wait for the actual TTS to complete — Gemini might
			// reword the request — just give it a short window. Worst case
			// (TTS no-shows) the bot still leaves; the disconnect is the
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

	// #1427 population tracking (Susan's 2026-06-09 spec): count non-bot members
	// in OUR channel; the speak gate selects the solo/group regime from this.
	// A solo↔group flip is an EXPLICIT event: sqlite regime_switch audit row +
	// a brief spoken ack in active mode (silent log-only in meeting mode, so a
	// join during note-taking doesn't interrupt the humans).
	const _recountHumans = (trigger: string) => {
		try {
			const ch = client.channels.cache.get(CHANNEL_ID ?? '');
			const members = (ch as any)?.members as Map<string, { id: string; user?: { bot?: boolean; username?: string } }> | undefined;
			const humans = members ? [...members.values()].filter((m) => !m.user?.bot).length : 1;
			const s2 = active;
			if (!s2) return;
			const prev = (s2 as any)._humanCount ?? 1;
			(s2 as any)._humanCount = humans;
			const from = regimeFor(prev), to = regimeFor(humans);
			const _toolHint = `[System note, do not reply to it: ${humans} human${humans === 1 ? '' : 's'} in the voice channel. For any mode switch use ${to === 'group' ? 'switch_mode_group' : 'switch_mode'} — not the other one.]`;
			if (trigger === 'session-start') {
				// Tell the model which switch tool applies from turn one — round-3
				// audit showed it guessing switch_mode_group in a solo room.
				try { injectSystemMessage(session, _toolHint); } catch {}
			}
			if (from !== to) {
				console.log(`${ts()} [Regime] ${from} → ${to} (humans=${humans}, trigger=${trigger})`);
				try { recordEvent('discord-voice', 'regime_switch', JSON.stringify({ from, to, humans, trigger }), s2.sessionId); } catch {}
				if (!s2.meetingMode) {
					try { injectSystemMessage(session, `[System] The voice channel now has ${humans} human${humans === 1 ? '' : 's'} (${to} regime). Say ONE short sentence acknowledging it (e.g. "${to === 'group' ? 'I see we have company — I\'ll only speak when addressed by name.' : 'Back to just us — I\'m all yours.'}"). For mode switches from now on use ${to === 'group' ? 'switch_mode_group' : 'switch_mode'}.`); } catch {}
				} else {
					try { injectSystemMessage(session, _toolHint); } catch {}
				}
			}
		} catch (e) { console.error(`${ts()} [Regime] recount failed:`, e); }
	};
	_recountHumans('session-start');
	client.on('voiceStateUpdate', (oldState, newState) => {
		const touchedOurChannel = oldState.channelId === CHANNEL_ID || newState.channelId === CHANNEL_ID;
		if (touchedOurChannel) _recountHumans(`${(newState.member ?? oldState.member)?.user?.username ?? 'unknown'} ${newState.channelId === CHANNEL_ID ? 'joined' : 'left'}`);
	});

	// Piece ④ owner-presence (multi-bot meeting mode): this bot stays only while
	// ITS OWNER is in the channel. When the last owner leaves, the bot leaves too
	// — no orphan bot listening to a meeting without its owner present. "Alone" =
	// no owner here, NOT channel-empty: in a 2-person meeting, A leaving drops A's
	// bot while B + B's bot stay. `.on` (not `.once`) — an owner may leave/rejoin
	// many times; no cleanup needed since the process exits on Disconnected.
	// Only armed when we know the owner by id (ACCESS.owner populated) and we're
	// NOT in treat-everyone-as-owner mode (where no single owner can be singled out).
	if (ACCESS.owner.size > 0 && !TREAT_AS_OWNER) {
		client.on('voiceStateUpdate', (oldState, newState) => {
			const leftOurChannel = oldState.channelId === CHANNEL_ID && newState.channelId !== CHANNEL_ID;
			if (!leftOurChannel) return;
			const leaverId = (newState.member ?? oldState.member)?.id;
			// Remaining members in our channel (members is a discord.js Collection).
			const ch = (oldState.guild ?? newState.guild)?.channels?.cache?.get(CHANNEL_ID ?? '');
			const members = (ch as any)?.members as Map<string, { id: string }> | undefined;
			const remainingIds = members ? [...members.values()].map((m) => m.id) : [];
			// Pure decision (unit-tested): leave only if an OWNER left and no owner remains.
			if (!shouldLeaveOnOwnerExit(leaverId, remainingIds, ACCESS.owner)) return;
			console.error(`${ts()} [Setup] owner-presence: last owner left — leaving channel`);
			try {
				injectSystemMessage(
					session,
					`[System] My owner left the voice channel. Say briefly: "My owner left — leaving too." Then stop.`,
				);
			} catch (e) {
				console.error(`${ts()} [Setup] owner-presence announcement injection failed:`, e);
			}
			setTimeout(() => {
				try { connection.destroy(); } catch {}
				process.exit(0);
			}, 2500);
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
async function shutdownAfterFlush(code: number): Promise<void> {
	// Clear the 👁 screen-share indicator WHILE still connected, BEFORE
	// cleanupSession() destroys the connection (the Set-Voice-Channel-Status API
	// 403s once disconnected). Makes a graceful exit self-clean so 👁 never
	// lingers; the session-start clear is the fallback for hard crashes. 1s cap.
	if (active && active.screenShareOn) {
		try {
			await Promise.race([
				setScreenShareIndicator(active, false),
				new Promise(res => setTimeout(res, 1000)),
			]);
		} catch {}
	}
	if (active) { try { cleanupSession(active); } catch {} }
	setTimeout(() => process.exit(code), 1500);
}
process.on('SIGINT', () => { void shutdownAfterFlush(0); });
process.on('SIGTERM', () => { void shutdownAfterFlush(0); });
process.on('uncaughtException', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });
process.on('unhandledRejection', (err) => { console.error(`${ts()} [FATAL]`, err); if (active) cleanupSession(active); process.exit(1); });

start().catch(err => { console.error('Fatal:', err); process.exit(1); });
