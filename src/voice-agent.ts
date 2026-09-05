/**
 * Sutando — Voice Interface
 *
 * A voice-first personal AI backed by Claude Code for task execution.
 * Handles anything: research, writing, email, scheduling, code, logistics.
 *
 * Usage:
 *   1. Copy .env.example to .env and fill in keys
 *   2. pnpm start
 *   3. In another terminal: pnpm tsx ../bodhi_realtime_agent/examples/web-client.ts
 *   4. Open http://localhost:8080 in Chrome and click Connect
 *
 * Environment:
 *   GEMINI_API_KEY       — Google AI Studio API key used as the default voice key.
 *   GEMINI_VOICE_API_KEY — Optional dedicated key for the Gemini Live voice session.
 *                          Takes precedence over GEMINI_API_KEY. Useful for isolating voice
 *                          (free-tier eligible) from paid-tier spend on a single key.
 *   ANTHROPIC_API_KEY   — Optional: only needed if not using claude CLI subscription auth
 *   (workspace)         — Per-user workspace dir resolved via `resolveWorkspace()`
 *                          from src/workspace_default.ts. Post-v0.8 (#1440) default is
 *                          `<repo>/workspace/`; configurable via `sutando.config.local.json`.
 *                          $SUTANDO_WORKSPACE is no longer honored for resolution.
 *                          Stores tasks/, results/, state/, logs/, conversation.log.
 *   PORT                — WebSocket port (default: 9900)
 *   HOST                — Bind address (default: 127.0.0.1 loopback; the voice WS
 *                          has no auth. Set 0.0.0.0 only for a trusted deployment;
 *                          LAN reach normally goes through the opt-in /ws proxy.)
 */

import 'dotenv/config';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import { existsSync, readFileSync, readdirSync, unlinkSync, mkdirSync, copyFileSync, appendFileSync, writeFileSync, realpathSync, renameSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { inlineTools, personalSkillSetups } from './inline-tools.js';
import { runSkillSetups } from './skill-setup-runner.js';
import { setVisionSession, startVisionControlServer, stopVisionControlServer, setSessionToolUpdater, setVisionSpeechEvidence, getVisionEgressStats, isStreaming, stopStreaming as stopVisionStreaming } from './vision-tools.js';
import { clearActiveArtifact } from './artifact-cache-tools.js';
import { injectText } from './browser-tools.js';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GeminiBatchSTTProvider, VoiceSession } from 'bodhi-realtime-agent';
import type { MainAgent, ToolDefinition } from 'bodhi-realtime-agent';
function assertMacOS() {
	if (process.platform !== 'darwin' && process.env.SUTANDO_TEST_MODE !== '1') {
		console.error('Sutando requires macOS');
		process.exit(1);
	}
}
import { workTool, resetNoteViewingDebounce, logConversation, logSessionBoundary, getRecentConversation, getSecondsSinceLastTurn, setTaskStatusCallback } from './task-bridge.js';
import { createAudioHealthLedger } from './voice-audio-health.js';
import { createHealthPersistence } from './voice-audio-health-persist.js';
import { evaluateMatrix, type MatrixBaseline } from './voice-health-matrix.js';
import { initialGoodbyeGuard, shouldFireGoodbye, createConversationClearHelper, clearStaleResumptionHandle } from './voice-continuity.js';
import { classifyFatalExitCode, isFatalExit, markFatalExit, writeCrashRecordAndExit, EXIT_CODE_DUPLICATE_INSTANCE } from './crash-only.js';
import { acquireVoiceLock, releaseOnExitUnlessFatal, resolveLockPython, voiceLockGuardPath } from './voice-lock.js';
import { recordToolCall } from './conversation-store.js';
import { buildGreeting, buildInstructions, type VoiceConfigContext } from './voice-agent-config.js';
import { wireDurableChannels, createSessionRecorder } from './live-agent-runtime.js';
import {
	classifyTransportClose,
	recordTerminalClassification,
	lastTerminalClassification,
	clearTerminalClassification,
	formatVoiceOfflineNotification,
	formatVoiceRecoveryNotification,
	type ClassifiedClose,
} from './voice-error-classifier.js';
import {
	createAgentStateProvider,
	createIsolatedIdleRestore,
	publishCapabilitiesMarker,
	publishLifecycleSnapshot,
	type AgentStateV1,
} from './voice-agent-state.js';

import { sharedPersonalPath, claudeHomePath, claudeProjectSlug } from './util_paths.js';
import { nextConnectingTick } from './voice-connect-watchdog.js';
import { VoiceWatchdogShadow, DETECTOR_VERSION, CAPABILITY_SET } from './voice-watchdog-shadow.js';
import { WatchdogLedger } from './voice-watchdog-ledger.js';
import { parseActiveSilenceMode, parseActiveSilenceTicks } from './voice-active-silence-watchdog.js';
import {
	VoiceSilenceRecoveryCoordinator,
	recoverySurfaceSupported,
	type RecoverySessionSurface,
} from './voice-silence-recovery-coordinator.js';
import {
	initialRedialState, noteLifecycle, noteDialed, shouldEventDial, tickMayDial,
} from './voice-redial-scheduler.js';

// Cartesia is loaded dynamically at the bottom of the config section so
// the `@cartesia/cartesia-js` package is only required when the user has
// set CARTESIA_API_KEY. Gemini-only setups (the default) skip the import
// entirely — no install cost, no type-check cost (see tsconfig `exclude`).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let generateSpeech: ((text: string, opts: { category: string; label: string }) => Promise<string>) | null = null;

// =============================================================================
// Config
// =============================================================================

// Shape check: catch common misconfigurations (truncated paste, wrong
// variable, stale template value) at startup instead of letting the voice
// session fail silently on connect. Do not pin this to a fixed prefix:
// Google has issued multiple AI Studio API-key formats over time.
function assertGeminiKey(name: string, value: string): void {
	if (!value) { console.error(`Error: ${name} is required`); process.exit(1); }
	const looksValid =
		value === value.trim()
		&& value.length >= 20
		&& value.length <= 200
		&& !/\s/.test(value)
		&& value !== 'your-gemini-key';
	if (!looksValid) {
		// Do NOT interpolate anything derived from `value` into the log —
		// CodeQL's js/clear-text-logging treats env vars matching the KEY
		// heuristic as taint sources, and any PropRead of that source
		// (e.g. `value.length`) flows into the console.error sink. Keep the
		// log static: name + expected format + remediation URL.
		console.error(
			`Error: ${name} does not look like a Google AI Studio key. ` +
			`Rotate at https://ai.google.dev → "Get API key" and update .env.`
		);
		process.exit(1);
	}
}

import { resolveCredential } from './credential-resolver.js';
// Voice credential resolves via the G8 capability resolver: managed tier
// (state/auth/managed-credentials.json) → GEMINI_VOICE_API_KEY → GEMINI_API_KEY.
// The VOICE-key fallback path isolates voice billing onto a paid-tier key when
// set; unset still works. `source` names the winning tier in startup errors.
const voiceCredential = resolveCredential('gemini-voice');
const GEMINI_VOICE_API_KEY = voiceCredential.key;
assertGeminiKey(
	voiceCredential.source === 'managed'
		? 'managed credentials (state/auth)'
		: process.env.GEMINI_VOICE_API_KEY ? 'GEMINI_VOICE_API_KEY' : 'GEMINI_API_KEY',
	GEMINI_VOICE_API_KEY,
);

const PORT = Number(process.env.PORT) || 9900;
// Loopback by default: the voice WS has no auth, so it must NOT be reachable
// from the LAN out of the box. LAN reach is an explicit opt-in via the
// web-client /ws proxy (SUTANDO_LAN_SHARE), never a direct bind to this port.
// Set HOST=0.0.0.0 explicitly only for a trusted deployment that needs it.
const HOST = process.env.HOST || '127.0.0.1';

// Per-user runtime state lives under the resolved workspace (post-v0.8
// / #1440 default: <repo>/workspace/), not the repo checkout. Pre-#762
// voice-agent resolved its tasks/results/state against the repo path via
// the legacy `WORKSPACE_DIR` env name + `import.meta.url`-relative
// fallback; post-#762 the canonical workspace lives elsewhere.
// resolveWorkspace() is the TS twin of resolve_workspace() introduced
// in #821. Also remove the prior
// "default to sutando/ so Claude Code subprocess picks up CLAUDE.md" comment
// — voice-agent no longer spawns Claude Code (task-bridge handles that via
// the file pipeline); the dual-use rationale is obsolete.
import { resolveWorkspace, statusPath } from './workspace_default.js';
const WORKSPACE_DIR = resolveWorkspace();
// Canonical since #2722; the root `.voice-agent.pid` is the pre-move legacy
// location, read by the shell side only via `sutando-config.sh voice-pidfile`.
const PIDFILE = join(WORKSPACE_DIR, 'state', 'locks', 'voice-agent.pid');
const LEGACY_PIDFILE = join(WORKSPACE_DIR, '.voice-agent.pid');

/** Bounded primitive-only crash record — shared by BOTH fatal paths (the
 * uncaught handler and `main().catch`), which obey identical crash-only
 * rules (design 1d; amendments R1/R2). */
const CRASH_RECORD_PATH = join(WORKSPACE_DIR, 'logs', 'voice-agent.crash.json');
const SESSION_ID = `session_${Date.now()}`;
// ACTIVE-silence watchdog, Phase 0a shadow observer (never touches the live
// session; see docs/design-voice-active-silence-recovery.md in the desktop
// repo). Timestamps deliberately share the audio-health snapshot's Date.now
// domain for this diagnostic phase; the armed implementation migrates to the
// monotonic domain with the bodhi surface.
const voiceWatchdogShadow = new VoiceWatchdogShadow({
	voiceSessionId: SESSION_ID,
	ledger: new WatchdogLedger({
		path: join(WORKSPACE_DIR, 'logs', 'voice-watchdog.jsonl'),
		meta: {
			detectorVersion: DETECTOR_VERSION,
			capabilitySet: CAPABILITY_SET,
			capabilitySetId: JSON.stringify(CAPABILITY_SET),
			pid: process.pid,
		},
		onError: (err) => console.error(`${new Date().toISOString().slice(11, 23)} [SilenceShadow] ledger write failed: ${err.message}`),
	}),
});

// ACTIVE-silence recovery, Phase 1 (armed): explicit opt-in via
// VOICE_ACTIVE_SILENCE_MODE=armed; everything else stays Phase 0a shadow.
const ACTIVE_SILENCE_MODE = parseActiveSilenceMode(process.env.VOICE_ACTIVE_SILENCE_MODE);
const ACTIVE_SILENCE_TICKS = parseActiveSilenceTicks(process.env.VOICE_ACTIVE_SILENCE_TICKS);
let voiceRecoveryCoordinator: VoiceSilenceRecoveryCoordinator | null = null;
let voiceRecoveryLedger: WatchdogLedger | null = null;
/** True while the legacy CLOSED guard is inside its cast-call reconnect —
 *  bodhi fires onClientConnected synchronously in there, and that fake
 *  attach must not mint a coordinator client epoch. */
let legacyReconnectInFlight = false;

const CALL_RESULTS_DIR = join(WORKSPACE_DIR, 'results', 'calls');

/** Single-instance lock for this workspace.
 *
 * Voice-agent owns two ports (`:9900` WS server, `:7847` vision control) plus
 * a fan-out of file watchers (tasks/, results/, context-drop, voice-state).
 * A second copy that races for those ports — typically a terminal-launched
 * `npm exec tsx src/voice-agent.ts` next to a healthy launchd one — used to
 * survive an EADDRINUSE on `:9900` AND keep `:7847` bound with a dead Gemini
 * session, so push-mode `/vision/start` from the web-client returned
 * `No active voice session — vision streaming requires a connected session.`
 *
 * The pidfile prevents the duplicate from reaching ANY side effect (no port
 * binds, no watchers wired, no `setVisionSession`) — it exits before the
 * `VoiceSession` constructor runs.
 *
 * The lock is a STRUCTURED JSON record `{v:1, lockId, pid, startTimeMs,
 * entry, workspace}` created/validated/replaced exclusively by the guarded
 * bundled-Python helper `scripts/voice-lock.py` under an advisory
 * `fcntl.flock` on `.voice-agent.lock.guard` — the single implementation
 * shared with the Node supervisor and `restart-voice-agent.sh` (design 1b).
 * Stale locks (dead pid, or PID reuse detected via a `ps -o lstart=`
 * mismatch) are replaced under the guard; a live lock is never removed. If
 * the helper's interpreter is unavailable, lock operations FAIL CLOSED with
 * an actionable error (amendment R3) — there is no unguarded legacy writer.
 *
 * EXIT CODE 7 = duplicate lock/port: "another instance owns the singleton
 * resource; my exit is the expected outcome of a race". The supervisor's
 * exit-7 grace window keys on it to distinguish a lost race from a crash
 * (impl plan WS1 Steps 2/8). The EADDRINUSE fatal path exits 7 for the same
 * reason (see `classifyFatalExitCode`).
 */
let voiceLockId: string | undefined;
function acquirePidLock(): void {
	const myPid = process.pid;
	const guard = voiceLockGuardPath(WORKSPACE_DIR);
	let entry = process.argv[1] ?? fileURLToPath(import.meta.url);
	try { entry = realpathSync(entry); } catch { /* keep the unresolved path */ }
	const py = resolveLockPython();
	if (!py.ok) {
		// Fail closed (amendment R3): never fall back to an unguarded bare-pid
		// writer — a second writer implementation would reopen the lock races
		// the guarded helper exists to close.
		console.error(`${ts()} [Startup] FATAL: cannot resolve a usable python3 for the voice lock helper — ${py.detail}`);
		console.error(`${ts()} [Startup] Lock operations fail closed. Fix: install python3 (brew install python), set SUTANDO_PY to a working interpreter, or run xcode-select --install. Exiting.`);
		process.exit(1);
	}
	try { mkdirSync(join(WORKSPACE_DIR, 'state', 'locks'), { recursive: true }); } catch { /* acquire fails closed below */ }
	const res = acquireVoiceLock({
		pidfile: PIDFILE,
		legacyPidfile: LEGACY_PIDFILE,
		guard,
		pid: myPid,
		entry,
		workspace: WORKSPACE_DIR,
		pythonBin: py.bin,
	});
	if (res.status === 'held') {
		console.error(`${ts()} [Startup] FATAL: voice-agent already running (pid ${res.holderPid ?? 'unknown'}) for ${WORKSPACE_DIR}`);
		console.error(`${ts()} [Startup] Kill it first or remove ${PIDFILE}. Exiting.`);
		process.exit(EXIT_CODE_DUPLICATE_INSTANCE);
	}
	if (res.status !== 'acquired') {
		console.error(`${ts()} [Startup] FATAL: voice lock acquisition failed (fail closed): ${res.detail}`);
		console.error(`${ts()} [Startup] Fix the lock helper (scripts/voice-lock.py + its python3), then restart. Exiting.`);
		process.exit(1);
	}
	// Capability-marker binding token: a stale marker can never match a later
	// acquisition, even one that reuses this pid.
	voiceLockId = res.lockId;
	// Guarded release on clean exit — NON-BLOCKING fire-and-forget (amendment
	// S4: a blocking release can deadlock against the helper that just TERM'd
	// us). Skipped entirely on the fatal path (amendment R1): a stale
	// structured lock is left for the supervisor to replace safely under the
	// guard.
	process.on('exit', () => {
		releaseOnExitUnlessFatal(
			{ pidfile: PIDFILE, guard, pid: myPid, pythonBin: py.bin },
			isFatalExit,
		);
	});
}

// Model configuration — override via .env for cost/quality tuning
const VOICE_MODEL = process.env.VOICE_MODEL || 'gemini-2.5-flash';
// Per-user voice config (native-audio model + googleSearch grounding) is
// data, not code: it lives in the workspace, NOT in the git repo.
//   live config: $SUTANDO_WORKSPACE/config/voice-agent.json
//   template:    src/voice-agent.config.json.example (committed)
// On first run, if the workspace config is missing, the committed .example
// template is copied into place so the operator (and the switch_voice_config
// tool) have a file to edit. If the copy fails (or the template is gone),
// loadVoiceConfig falls back to its built-in defaults. Schema + defaults: see
// src/voice-config.ts. voice-agent ships with model=3.1 + googleSearch=false
// because the web client's code-heavy workload prefers 3.1 and the (key,
// 3.1, googleSearch) combo trips a 1011 close on the VOICE key when search
// is true. Phone inherits the package default (2.5+search).
import { loadVoiceConfig, resolveSessionTuning } from './voice-config.js';
const _voiceAgentDir = dirname(fileURLToPath(import.meta.url));
const VOICE_AGENT_CONFIG_PATH = join(WORKSPACE_DIR, 'config', 'voice-agent.json');
if (!existsSync(VOICE_AGENT_CONFIG_PATH)) {
	const _exampleConfigPath = join(_voiceAgentDir, 'voice-agent.config.json.example');
	try {
		mkdirSync(dirname(VOICE_AGENT_CONFIG_PATH), { recursive: true });
		if (existsSync(_exampleConfigPath)) {
			copyFileSync(_exampleConfigPath, VOICE_AGENT_CONFIG_PATH);
			console.log(`${new Date().toISOString().slice(11, 23)} [voice-agent] seeded config from template → ${VOICE_AGENT_CONFIG_PATH}`);
		}
	} catch (e) {
		console.warn(`${new Date().toISOString().slice(11, 23)} [voice-agent] could not seed config at ${VOICE_AGENT_CONFIG_PATH}: ${(e as Error).message} — using built-in defaults`);
	}
}
const VOICE_AGENT_CONFIG = loadVoiceConfig(VOICE_AGENT_CONFIG_PATH);
const VOICE_NATIVE_AUDIO_MODEL = VOICE_AGENT_CONFIG.model;
const VOICE_GOOGLE_SEARCH = VOICE_AGENT_CONFIG.googleSearch;
// Shadow STT (config "shadowStt": true — default OFF): re-runs the same
// audio through a batch model and logs disagreement — observation-only.
const VOICE_SHADOW_STT = VOICE_AGENT_CONFIG.shadowStt === true;
// "divergenceCorrection": true additionally speaks a self-correction when
// the shadow pass disagrees. Requires shadowStt.
const VOICE_DIVERGENCE_CORRECTION = VOICE_AGENT_CONFIG.divergenceCorrection === true;
// Phase 0.5 seams (design §2.1/§2.2): OFF unless configured — with nothing
// set the VoiceSession config carries neither key and the wire behaviour is
// byte-identical to the previous build (the Phase 0.5 gate). A half-set or
// inverted threshold pair throws HERE, failing startup loudly.
const VOICE_SESSION_TUNING = resolveSessionTuning(VOICE_AGENT_CONFIG);
console.log(
	`${new Date().toISOString().slice(11, 23)} [voice-agent] session tuning: compression=${
		VOICE_SESSION_TUNING.compressionConfig === undefined
			? 'off'
			: VOICE_SESSION_TUNING.compressionConfig.triggerTokens === undefined
				? 'server-defaults'
				: `trigger=${VOICE_SESSION_TUNING.compressionConfig.triggerTokens},target=${VOICE_SESSION_TUNING.compressionConfig.targetTokens}`
	} mediaResolution=${VOICE_SESSION_TUNING.mediaResolution ?? 'unset'}`,
);
const VOICE_NAME = process.env.VOICE_NAME || 'Puck';
const CARTESIA_API_KEY = process.env.CARTESIA_API_KEY || '';

// Lazy-load Cartesia TTS only when a key is set. This means Gemini-only
// users don't need `@cartesia/cartesia-js` installed at all — the
// cartesia-*.ts files are excluded from tsc via tsconfig and never loaded
// by tsx at runtime unless this branch runs.
if (CARTESIA_API_KEY) {
	try {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const ttsMod: any = await import('./cartesia-tts.js');
		generateSpeech = ttsMod.generateSpeech;
	} catch (err) {
		console.error(
			`[Cartesia] failed to load TTS module — is @cartesia/cartesia-js installed?`,
			err instanceof Error ? err.message : err
		);
		// generateSpeech stays null; the Cartesia TTS branch below will be skipped.
	}
}

// Uses GEMINI_VOICE_API_KEY because the only consumer of `google()` below is
// the VoiceSession `model:` field — voice-session subagent text LLM calls.
// Routes with the voice key so free-tier voice setups don't leak subagent
// traffic onto the paid GEMINI_API_KEY. Deliberate tradeoff: subagents lose
// access to any paid-tier quota on GEMINI_API_KEY (rate-limited on free).
// If subagent throughput becomes a concern, revisit by giving subagents
// their own key or routing to `createGoogleGenerativeAI({apiKey:GEMINI_API_KEY})`.
const google = createGoogleGenerativeAI({ apiKey: GEMINI_VOICE_API_KEY });
let sessionRef: VoiceSession | null = null;

function ts(): string { return new Date().toISOString().slice(11, 23); }

// =============================================================================
// Pending tool call tracker
// =============================================================================

function getPendingToolCalls(toolName?: string) {
	const items = sessionRef?.conversationContext.items ?? [];
	const calls = new Map<string, { toolCallId: string; toolName: string; startedAt: number; args: Record<string, unknown> }>();
	const completed = new Set<string>();

	for (const item of items) {
		if (item.role === 'tool_call') {
			try {
				const p = JSON.parse(item.content) as Partial<{ toolCallId: string; toolName: string; args: Record<string, unknown> }>;
				if (typeof p.toolCallId === 'string' && typeof p.toolName === 'string') {
					calls.set(p.toolCallId, { toolCallId: p.toolCallId, toolName: p.toolName, startedAt: item.timestamp, args: p.args ?? {} });
				}
			} catch { /* ignore */ }
		}
		if (item.role === 'tool_result') {
			try {
				const p = JSON.parse(item.content) as Partial<{ toolCallId: string }>;
				if (typeof p.toolCallId === 'string') completed.add(p.toolCallId);
			} catch { /* ignore */ }
		}
	}

	const pending = [...calls.values()].filter((c) => !completed.has(c.toolCallId));
	return toolName ? pending.filter((c) => c.toolName === toolName) : pending;
}

// =============================================================================
// Meeting mode state — persists across Gemini reconnects
// =============================================================================
let meetingActive = false;
// Third base mode (mirrors discord-voice PR #39: active ⊕ meeting ⊕ presenter,
// mutually exclusive). Toggled via switch_mode("presenter"); previously the
// prompt referenced a presenter_mode tool that only exists on installs with
// the talk-highlight manifest skill — on installs without it the phrase went
// to a nonexistent tool and presenter mode could never engage by voice.
let presenterActive = false;
// PR #1879 sentinel (notification mute): bridges + check-pending-questions
// read <workspace>/state/presenter-mode.sentinel (ISO expiry inside). Voice
// toggle syncs it so "presenter mode on" also mutes notifications.
const PRESENTER_SENTINEL_MINUTES = 120;
function syncPresenterSentinel() {
	const sentinel = join(WORKSPACE_DIR, 'state', 'presenter-mode.sentinel');
	try {
		if (presenterActive) {
			mkdirSync(join(WORKSPACE_DIR, 'state'), { recursive: true });
			const expire = new Date(Date.now() + PRESENTER_SENTINEL_MINUTES * 60_000);
			writeFileSync(sentinel, expire.toISOString().replace(/\.\d{3}Z$/, 'Z') + '\n');
		} else {
			unlinkSync(sentinel);
		}
	} catch {}
}
// Sentinel for the 3-mode indicator (menu-bar + web-badge read this).
function writeVoiceModeSentinel() {
	try {
		mkdirSync(join(WORKSPACE_DIR, 'state'), { recursive: true });
		writeFileSync(join(WORKSPACE_DIR, 'state', 'voice-mode.txt'), presenterActive ? 'presenter' : meetingActive ? 'meeting' : 'active');
	} catch {}
}

// Poll state/voice-mode.request every 1s — external controllers (Swift
// menu-bar clickable items) write "active" or "meeting" to ask voice-agent
// to switch. Same code path as the switch_mode tool. File is consumed on
// apply so requests don't re-fire.
function applyModeRequest() {
	try {
		const reqPath = join(WORKSPACE_DIR, 'state', 'voice-mode.request');
		const req = readFileSync(reqPath, 'utf-8').trim().toLowerCase();
		unlinkSync(reqPath);
		const wantPresenter = req === 'presenter';
		const want = req === 'meeting';
		if (meetingActive === want && presenterActive === wantPresenter) return; // no-op if already in that mode
		meetingActive = want;
		voiceWatchdogShadow.noteMeetingMode(want);
		voiceRecoveryCoordinator?.noteMeetingMode(want);
		presenterActive = wantPresenter;
		writeVoiceModeSentinel();
		syncPresenterSentinel();
		console.log(`${ts()} [Meeting] External request applied: mode=${wantPresenter ? 'presenter' : want ? 'meeting' : 'active'}`);
	} catch {
		// no request file or delete failed — both are fine (silent poll)
	}
}
setInterval(applyModeRequest, 1_000);

// Detect active meeting on startup — sync so it runs before first greeting
try {
	const zoomRunning = execFileSync('/usr/bin/pgrep', ['-f', 'zoom.us'], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
	if (zoomRunning) {
		const inMeeting = execFileSync('osascript', ['-e', 'tell application "System Events" to tell process "zoom.us" to count of windows'], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
		if (parseInt(inMeeting) >= 2) {
			meetingActive = true;
			voiceWatchdogShadow.noteMeetingMode(true);
			console.log(`${new Date().toLocaleTimeString()} [Meeting] Detected active Zoom meeting on startup`);
		}
	}
} catch { /* no zoom */ }

// Write the initial voice-mode sentinel AFTER the Zoom auto-detect — so
// the on-disk state matches the in-memory `meetingActive` decision (was
// previously written before the auto-detect, leaving voice-mode.txt
// stuck on "active" even when Zoom was detected as active).
writeVoiceModeSentinel();

// =============================================================================
// Tools
// =============================================================================

const switchModeTool: ToolDefinition = {
	name: 'switch_mode',
	description:
		'Switch between active, meeting, and presenter mode (mutually exclusive). ' +
		'Call switch_mode("meeting") when user says "take notes", "be silent", "meeting mode", "passive mode", or joins a meeting. ' +
		'Call switch_mode("presenter") when user says "presenter mode on", "going live", "starting the talk", "the talk starts", or "I am on stage". ' +
		'Call switch_mode("active") when user says "I need you", "come back", "active mode", "presenter mode off", "talk is done", or the meeting ends. ' +
		'In meeting mode: listen to everything and track discussion internally, but produce ZERO audio output and do NOT call any other tools — unless explicitly addressed by name ("Sutando" or "hey Sutando").',
	parameters: z.object({
		mode: z.enum(['active', 'meeting', 'presenter']).describe('"meeting" = silent note-taker, "presenter" = on-stage co-presenter (mutes notifications), "active" = normal assistant'),
	}),
	execution: 'inline',
	async execute(args) {
		const { mode } = args as { mode: 'active' | 'meeting' | 'presenter' };
		meetingActive = mode === 'meeting';
		voiceWatchdogShadow.noteMeetingMode(meetingActive);
		voiceRecoveryCoordinator?.noteMeetingMode(meetingActive);
		presenterActive = mode === 'presenter';
		syncPresenterSentinel();
		// Sync the on-disk sentinel so menu-bar consumers (Sutando.app
		// pollVoiceMode + web-client /voice-mode endpoint) reflect the
		// switch immediately. Without this, voice-triggered switch_mode
		// flips meetingActive in-memory but voice-mode.txt stays stale,
		// causing the menu radio to lag + the next applyModeRequest from
		// Sutando.app to early-return as a no-op (`meetingActive === want`).
		writeVoiceModeSentinel();
		console.log(`${ts()} [Meeting] Mode switched to: ${mode}`);
		if (mode === 'meeting') {
			return { status: 'meeting_mode', instruction: 'You are now in meeting mode. Listen and track the discussion internally. Produce ZERO audio output unless someone says "Sutando." The ONLY tool you may call unprompted is save_meeting_note — call it every 5-10 minutes to capture key decisions, action items, and discussion points. When you exit meeting mode, call save_meeting_note with type "summary" for a final recap. Do not call work or any other tools unless explicitly addressed.' };
		}
		if (mode === 'presenter') {
			return { status: 'presenter_mode', say: 'Presenter mode on — notifications muted. Break a leg.', instruction: 'You are now in presenter mode (on-stage co-presenter). Notifications are muted for the audience. Follow the CO-PRESENTER protocol from your context for slide cues. Exit ONLY when the user says "presenter mode off", "talk is done", or "active mode" — then call switch_mode("active").' };
		}
		return { status: 'active_mode', instruction: 'Back to active mode. You can speak and use all tools normally.' };
	},
};

const saveMeetingNoteTool: ToolDefinition = {
	name: 'save_meeting_note',
	description:
		'Save a meeting observation, decision, or action item to notes. ' +
		'Use this ONLY in meeting mode to periodically capture key points. ' +
		'Call every 5-10 minutes during a meeting, or when a significant decision/action item is discussed. ' +
		'Also call when exiting meeting mode to save a final summary.',
	parameters: z.object({
		content: z.string().describe('The meeting note: decisions, action items, key discussion points, or a summary. Include speaker names when known.'),
		type: z.enum(['point', 'summary']).optional().describe('"point" for individual observations (default), "summary" for end-of-meeting summary'),
	}),
	execution: 'inline',
	async execute(args) {
		const { content, type } = args as { content: string; type?: 'point' | 'summary' };
		const today = new Date().toISOString().slice(0, 10);
		const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
		const notePath = sharedPersonalPath(`notes/meeting-${today}.md`, WORKSPACE_DIR);
		const isSummary = type === 'summary';

		if (!existsSync(notePath)) {
			// Create new meeting note file with frontmatter
			const header = `---\ntitle: Meeting notes — ${today}\ndate: ${today}\ntags: [meeting, notes]\n---\n\n`;
			writeFileSync(notePath, header);
		}

		const entry = isSummary
			? `\n## Summary (${time})\n${content}\n`
			: `\n- **[${time}]** ${content}`;
		appendFileSync(notePath, entry);
		console.log(`${ts()} [MeetingNote] ${isSummary ? 'Summary' : 'Point'} saved to ${notePath}`);
		return { status: 'saved', path: notePath, type: isSummary ? 'summary' : 'point' };
	},
};

const getTaskStatus: ToolDefinition = {
	name: 'get_task_status',
	description:
		'Check whether Sutando has in-progress or queued tasks. ' +
		'Use for status/progress questions like "any pending tasks?", "are you working on something?". ' +
		'Do NOT call work just to check progress.',
	parameters: z.object({}),
	execution: 'inline',
	execute: async () => {
		const pending = getPendingToolCalls('work');
		const oldest = pending.length > 0 ? Math.min(...pending.map((c) => c.startedAt)) : null;
		// Also check tasks/ directory for queued files waiting for core agent
		let queuedFiles: string[] = [];
		try {
			const tasksDir = join(WORKSPACE_DIR, 'tasks');
			queuedFiles = readdirSync(tasksDir).filter(f => f.endsWith('.txt'));
		} catch {}
		return {
			inProgress: pending.length > 0 || queuedFiles.length > 0,
			pendingToolCalls: pending.length,
			queuedTaskFiles: queuedFiles.length,
			elapsedSeconds: oldest ? Math.floor((Date.now() - oldest) / 1000) : 0,
			pendingTasks: pending.map((c) => typeof c.args.task === 'string' ? c.args.task : '').filter(Boolean).slice(0, 3),
			queuedTasks: queuedFiles.map(f => f.replace('.txt', '')),
		};
	},
};

// end_session has no runtime gate. Both previous gate strategies
// (items-based and event-based) failed under the native-audio model,
// which doesn't populate conversationContext.items with user turns
// and doesn't fire turn.interrupted during silent assistant periods.
// The contamination-loop protection instead comes from upstream
// fixes: the greeting-replay filter in mainAgent.get greeting(), the
// NoteView injection guard markers + debounce, and the result
// injection guard markers. If contamination still triggers an
// end_session call through all those layers, the user can just
// click Connect again — a worse UX than the race-free path, but
// vastly better than being unable to end the session at all.
let userTurnCount = 0;
let userHasInterrupted = false;
// Set to true when end_session fires, cleared on fresh greeting.
// While true, the turn.end handler clears conversationContext.items
// after every turn so bodhi's handleClientConnected replay path has
// nothing to inject on the next reconnect. Without this, Gemini's
// post-goodbye farewell turn ("Farewell. Talk to you next time.")
// accumulates in items AFTER the end_session clear and contaminates
// the next reconnect.
let sessionEnding = false;

// Intentionally unused: kept out of the tool list on purpose — see the
// "endSession intentionally NOT in the tool list" note at the tools: field below.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const endSession: ToolDefinition = {
	name: 'end_session',
	description: 'End the voice session gracefully. Call when the user explicitly says goodbye or bye.',
	parameters: z.object({}),
	execution: 'inline',
	execute: async (_args, ctx) => {
		console.log(`${ts()} [end_session] firing (userTurnCount=${userTurnCount}, userHasInterrupted=${userHasInterrupted})`);
		sessionEnding = true;
		// Write a session-boundary marker to conversation.log so the next
		// getRecentConversation(N) call trims at this point and doesn't
		// replay goodbye text from this session into the reconnect
		// greeting. Structural fix for the 2026-04-09 replay-contamination
		// class of bug.
		logSessionBoundary('user_goodbye');
		console.log(`${ts()} [end_session] Sending session_end to client (sendJsonToClient exists: ${!!ctx.sendJsonToClient})`);
		ctx.sendJsonToClient?.({ type: 'session_end', reason: 'user_goodbye' });
		// CRITICAL: clear bodhi's in-memory conversationContext so the next
		// reconnect doesn't replay the goodbye and trigger another end_session.
		// Bodhi's handleClientConnected (CLOSED branch) builds a contextSummary
		// from conversationContext.items.slice(-10), injects it into the
		// reconnect prompt, and the GOODBYE RULE in our system instructions
		// makes Gemini re-fire end_session on the replayed "goodbye" text.
		// Death spiral observed live 2026-04-09 at 22:57 — 3 self-initiated
		// end_session calls in 36 seconds. sessionManager.reset() only
		// clears the state machine; conversationContext persists separately.
		// (`items` is a GETTER returning bodhi's underlying _items array by
		// reference — mutate in place via length = 0, never reassign. The
		// helper also rebases the turn.end transcript cursor: P7 D7.3.)
		itemsClear.clear('end_session');
		// Also force-close client WS after 4s as fallback
		setTimeout(() => {
			console.log(`${ts()} [end_session] Force-closing client WS`);
			try {
				const ct = (voiceSessionRef as any)?.clientTransport;
				console.log(`${ts()} [end_session] clientTransport exists: ${!!ct}, client exists: ${!!ct?.client}, readyState: ${ct?.client?.readyState}`);
				ct?.client?.close(4000, 'goodbye');
			} catch (e) { console.log(`${ts()} [end_session] Close error: ${e}`); }
		}, 4000);
		return { status: 'ending' };
	},
};







// =============================================================================
// Main agent
// =============================================================================

let voiceSessionRef: VoiceSession | null = null;

// P7 D7.3: the ONE clear path for bodhi's conversationContext — items and the
// turn.end transcript cursor move together (G-P7-8: a clear that leaves the
// cursor pointing past the emptied array makes the logger skip everything
// that accumulates after it). Used by end_session, the goodbye detector, and
// the sessionEnding turn.end sweep.
const itemsClear = createConversationClearHelper(
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	() => (voiceSessionRef as any)?.conversationContext?.items,
	(m) => console.log(`${ts()} ${m}`),
);
// P7 D7.3 stale-repeat goodbye guard (Tranche A engine-side). The guard
// compares against userTurnCount, which resets per logical session — every
// reset MUST rebase the guard too, or a legitimate goodbye in the next
// session (count restarted below the old watermark) would be suppressed.
let goodbyeGuard = initialGoodbyeGuard();
function resetSessionGateState(): void {
	userTurnCount = 0;
	userHasInterrupted = false;
	sessionEnding = false;
	goodbyeGuard = initialGoodbyeGuard();
}

// Unified base-mode resolver: see src/voice-mode-resolver.ts for the
// rationale + canonical mode descriptors. Local wrapper threads the in-memory
// `meetingActive` boolean (this module owns that state) into the pure
// resolver function.
import { resolveCurrentMode as resolveCurrentModeImpl, type ModeState } from './voice-mode-resolver.js';

import { wireSanitizerToTransport } from './output_sanitizer.js';
function resolveCurrentMode(): ModeState {
	return resolveCurrentModeImpl({ meetingActive, presenterActive });
}

const mainAgentTools: ToolDefinition[] = [workTool, getTaskStatus, switchModeTool, saveMeetingNoteTool, ...inlineTools];

// Injection seam for the tuned factories in voice-agent-config.ts: this
// module owns the session-gate + mode state; the config module owns the
// prompt strings (CLAUDE.md: prompts preserved exactly).
const _configCtx: VoiceConfigContext = {
	resolveCurrentMode,
	isMeetingActive: () => meetingActive,
	googleSearch: VOICE_GOOGLE_SEARCH,
	resetSessionGates: () => { resetSessionGateState(); },
	resetNoteViewingDebounce,
	getRecentConversation,
	getSecondsSinceLastTurn,
};

const mainAgent: MainAgent = {
	name: 'main',
	get greeting() {
		// Tuned greeting factory moved verbatim to voice-agent-config.ts
		// (step 5a-1) so it is importable/testable; this module keeps the
		// session-gate state and threads it in via _configCtx.
		return buildGreeting(_configCtx);
	},
	// Tuned system-instruction factory moved verbatim to
	// voice-agent-config.ts (step 5a-1). Per-session evaluation preserved:
	// buildInstructions re-checks mode/meeting state on every call.
	instructions: () => buildInstructions(_configCtx),
	// endSession intentionally NOT in the tool list. After 14 commits
	// trying to gate it against contamination false positives, the
	// conclusion is: don't give Gemini a way to close the session
	// autonomously. The user ends the session by clicking the "End
	// Voice" button in the web UI. Gemini acknowledges the goodbye
	// verbally; the actual disconnect is driven by the client, not
	// the model. Removes the entire class of "Gemini spontaneously
	// calls end_session because of something in the injected context"
	// bug. The endSession definition is retained above so we can re-
	// enable it once we find a reliable gate signal (probably after
	// bodhi exposes a proper "user has actually spoken" signal under
	// native audio).
	tools: mainAgentTools,
	googleSearch: VOICE_GOOGLE_SEARCH,
	onEnter: async () => console.log(`${ts()} [Agent] Sutando ready`),
	// Voice-driven close — strict version. User wants to be able to
	// say "bye" and have the session close, but the previous
	// assistant-turn detector was too loose (matched "goodbye" as a
	// substring anywhere, triggered on mid-sentence uses like
	// "don't say goodbye yet"). Strict version:
	//
	//   1. Last assistant turn must be SHORT (< 80 chars, about one
	//      sentence). Long turns are task responses, not farewells.
	//   2. Turn must START with a farewell word (goodbye, bye, farewell,
	//      good bye, see you). Matches "Goodbye!" or "Bye, see you
	//      tomorrow." but not "I'm back. How can I help?".
	//
	// This is strict enough that contamination-induced goodbye
	// phrasing (which tends to be embedded in longer introductions
	// or apology loops) doesn't match. Real farewell responses to
	// a user "bye" are almost always a short standalone line.
	onTurnCompleted: async (ctx, _transcript) => {
		// Clear narration speaking flag + capture what Gemini actually said
		try {
			const { narrationSpeakingRef, lastSpokenRef } = await import('./recording-state.js');
			if (narrationSpeakingRef.value) {
				narrationSpeakingRef.value = false;
				// Capture what Gemini said so next description has real speech context
				const turns = ctx.getRecentTurns(1) as Array<{ role?: string; content?: string }>;
				const last = turns.find(t => t?.role === 'assistant');
				if (last?.content) lastSpokenRef.value = last.content.trim();
				console.log(`${ts()} [Recording] speech done — ready for next description`);
				// If pre-captured desc is waiting, inject immediately
				const { nextDescRef } = await import('./recording-state.js');
				if (nextDescRef.value) {
					const { _tryInjectNow } = await import('./recording-tools.js');
					if (_tryInjectNow) _tryInjectNow();
				}
			}
		} catch {}
		try {
			// getRecentTurns returns conversationContext.items directly —
			// items have shape {role: 'assistant'|'user'|..., content: string}.
			// The earlier version mistakenly used role==='model' and
			// parts[].text which is Gemini API raw Content format, not
			// bodhi's conversationContext item format. Filter never matched,
			// detector never fired — observed live 00:08:04 when Gemini
			// said "Goodbye! Talk to you later." and the session stayed open.
			const turns = ctx.getRecentTurns(2) as Array<{ role?: string; content?: string }>;
			const lastAssistant = turns.filter(t => t?.role === 'assistant').pop();
			const lastText = (lastAssistant?.content || '').trim();
			console.log(`${ts()} [Agent] onTurnCompleted: lastAssistant.length=${lastText.length} "${lastText.slice(0, 50)}"`);
			if (lastText.length === 0 || lastText.length >= 80) return;
			const FAREWELL_START = /^(goodbye|bye\b|farewell|good\s*bye|see you)/i;
			if (!FAREWELL_START.test(lastText)) return;
			// P7 D7.3 stale-repeat guard: a reconnect replay can make the model
			// repeat the SAME short farewell with no new real user turn — that
			// repeat must not re-fire session_end.
			const verdict = shouldFireGoodbye(goodbyeGuard, lastText, userTurnCount);
			if (!verdict.fire) {
				console.log(`${ts()} [Agent] Stale-repeat goodbye suppressed (same farewell, no new user turn)`);
				return;
			}
			goodbyeGuard = verdict.next;
			console.log(`${ts()} [Agent] Strict goodbye detected — closing client in 3s`);
			logSessionBoundary('voice_goodbye');
			(ctx as any).sendJsonToClient?.({ type: 'session_end', reason: 'user_goodbye' });
			setTimeout(() => {
				try {
					itemsClear.clear('voice_goodbye');
					const ct = (voiceSessionRef as any)?.clientTransport;
					ct?.client?.close(4000, 'goodbye');
				} catch {}
			}, 3000);
		} catch (e) {
			console.error(`${ts()} [Agent] goodbye-detector error:`, e);
		}
	},
};

// =============================================================================
// Main
// =============================================================================

// Ensure the long-term memory directory exists at startup so the agent can
// proactively write user_profile / feedback / project / reference files
// without first having to remember to mkdir. Honours $SUTANDO_MEMORY_DIR
// when set; otherwise uses the Claude Code default
// ($CLAUDE_CONFIG_DIR/projects/-{slug}/memory). Failure-silent: a missing memory
// dir should never block voice startup.
function bootstrapMemoryDir(): void {
	// Claude Code keys its project dir on the REPO it was launched in, not on the
	// workspace. Passing WORKSPACE_DIR derives a slug no project dir ever has, so
	// this silently created an empty memory dir beside the real one.
	const slug = claudeProjectSlug(dirname(_voiceAgentDir).replace(/\/$/, ''));
	const memDir = process.env.SUTANDO_MEMORY_DIR || claudeHomePath('projects', slug, 'memory');
	try {
		mkdirSync(memDir, { recursive: true });
		const indexPath = join(memDir, 'MEMORY.md');
		if (!existsSync(indexPath)) {
			writeFileSync(indexPath, '# Sutando memory index\n\nDurable facts about the user, project, and references. One line per entry: `- [Title](file.md) — one-line hook`. See CLAUDE.md `## Memory` for the schema.\n');
			console.log(`${ts()} [Memory] Initialized ${memDir}`);
		}
	} catch (err) {
		console.log(`${ts()} [Memory] bootstrap failed (non-fatal): ${err instanceof Error ? err.message : err}`);
	}
}

async function main() {
	assertMacOS();
	bootstrapMemoryDir();
	// Refuse to start when another voice-agent already owns this workspace.
	// Runs BEFORE any side effects (port binds, watchers, session construction)
	// so a duplicate exits without stranding `:7847` with a dead session.
	acquirePidLock();

	// Test-only fault injection (SUTANDO_TEST_MODE only): throw AFTER the lock
	// is acquired so the crash-only contract of `main().catch` — bounded crash
	// record, static-string logging, R1 release suppression (the stale lock
	// stays for the supervisor) — is provable end-to-end.
	if (process.env.SUTANDO_TEST_MODE === '1' && process.env.SUTANDO_TEST_FAIL_MAIN) {
		throw new Error(process.env.SUTANDO_TEST_FAIL_MAIN);
	}

	// --- Voice agent observability ---
	// Same format as phone agent's call-metrics.jsonl so diagnose.py can
	// analyze both. State + flush + usage-ticker management moved to
	// live-agent-runtime's SessionRecorder (step 5a-3); the callbacks below
	// push into recorder.events/toolCalls/transcript exactly as they pushed
	// into the old module-level arrays.
	const recorder = createSessionRecorder('voice', SESSION_ID);
	const voiceToolIdMap = new Map<string, string>();

	// Authoritative voice-connection state. web-client reads this file
	// instead of caching the browser's one-shot POST, so a web-client
	// restart during an active session re-syncs on next file read (no
	// manual user toggle needed). Chi's 2026-04-19 regression surfaced
	// this after ~5 PR-restart cycles desyncing voiceConnected.
	function writeVoiceState(connected: boolean) {
		try {
			// voice-state.json is per-user runtime state — lives under
			// $SUTANDO_WORKSPACE/state/. Pre-fix this was a cwd-relative write
			// (effectively REPO_ROOT when launched from there), so the
			// web-client's REPO_ROOT-relative reader happened to find it —
			// but on hosts where SUTANDO_WORKSPACE is set or cwd drifts,
			// voice-agent wrote one place and the consumer read another.
			// Same workspace-contract fix as #849 for core-status.json.
			writeFileSync(statusPath('voice-state.json', WORKSPACE_DIR), JSON.stringify({ connected, ts: Math.floor(Date.now() / 1000) }));
		} catch (err) {
			console.error(`${ts()} [VoiceState] write failed:`, err);
		}
	}

	// Initialize voice-state.json at startup so dm-fallback's voiceConnected
	// query has a fresh, authoritative file to read even before any client
	// has ever connected. Without this, the file doesn't exist on instances
	// that have never seen a client (e.g. Mac Mini, where voice routes to
	// MacBook), and dm-result.py falls back to web-client.ts's `_voiceState`
	// module variable — a sticky value set by browser SSE reports with no
	// TTL. That caused the 2026-05-05 9h friction-delivery delay (see
	// notes/friction-9h-delay-investigation-2026-05-05.md). With this write,
	// the file is always present + always reflects the latest known state.
	writeVoiceState(false);

	// voice-agent.json is runtime-authored state recording the ACTUAL bound WS
	// endpoint. `sutando-config.sh runtime` reads it (validated by pid liveness)
	// so the AgentRuntime descriptor's `voice_ws` reports the port this process
	// really bound — correct for installs on a non-default PORT, not a hardcoded
	// default. Same "the running process is the authority on its own resource"
	// principle by which the tmux socket is sourced from the core's heartbeat.
	function writeVoiceRuntimeState() {
		try {
			writeFileSync(
				statusPath('voice-agent.json', WORKSPACE_DIR),
				JSON.stringify({ voice_ws: `ws://127.0.0.1:${PORT}`, port: PORT, pid: process.pid, ts: Math.floor(Date.now() / 1000) })
			);
		} catch (err) {
			console.error(`${ts()} [VoiceRuntime] state write failed:`, err);
		}
	}


	// Bumped 5min into the future on every non-retryable transport close
	// (set inside the classifier IIFE below). Read by the 30s health
	// monitor — when the deadline is in the future, the monitor skips its
	// reconnect-trigger so a permanent upstream failure (credits depleted,
	// key invalid, quota exceeded) doesn't produce a tight 60s retry loop
	// that spams logs + Gemini API requests until the user fixes things.
	// Auto-recovery resumes ~5min after the last fatal close. Reset to 0
	// when the session reaches ACTIVE so a transient close after recovery
	// doesn't inherit a stale backoff window. (Declared BEFORE the
	// VoiceSession construction so the agent.state provider below can read
	// it — Step 12's `backoff` upstream mapping.)
	let voiceFatalBackoffUntil = 0;

	// F5: event-driven redial with exponential backoff (voice-redial-scheduler.ts).
	// The 30s tick below remains the safety net; these fire on bodhi's
	// connection-lifecycle events instead of waiting up to 60s of dead air.
	// Declared before the VoiceSession constructor because the constructor's
	// onConnectionLifecycle option feeds them; session access is late-bound
	// via sessionRef (assigned right after construction, before any event).
	let redialState = initialRedialState();
	let redialTimer: ReturnType<typeof setTimeout> | null = null;
	// Shared with the 30s tick's throttle + the CONNECTING watchdog below.
	let lastReconnectAt = 0;
	const fireEventRedial = (): void => {
		redialTimer = null;
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const s = sessionRef as any;
		if (!s) return;
		const now = Date.now();
		const state = String(s.sessionManager?.state ?? 'unknown');
		const clientConnected = Boolean(s.clientConnected);
		if (!shouldEventDial({ state, clientConnected, now, nextDialAt: redialState.nextDialAt, fatalBackoffUntil: voiceFatalBackoffUntil })) {
			// Blocked by the fatal gate alone → re-arm for when it lifts.
			// Any other veto drops the dial: the next lifecycle event or the
			// 30s tick takes over.
			if (redialState.nextDialAt > 0 && now <= voiceFatalBackoffUntil && state === 'CLOSED' && clientConnected) {
				armRedialTimer(voiceFatalBackoffUntil - now + 100);
			}
			return;
		}
		// While the armed coordinator owns the episode, F5 stands down: its
		// dial would be an uncounted attempt, and the fake attach below must
		// not mint a coordinator client epoch. Lifecycle events resume F5
		// naturally once the episode resolves.
		if (voiceRecoveryCoordinator?.ownsRecovery ?? false) return;
		redialState = noteDialed(redialState);
		lastReconnectAt = now;
		console.log(`${ts()} [Redial] event-driven reconnect (failures=${redialState.failures})`);
		try {
			legacyReconnectInFlight = true;
			s.handleClientConnected();
		} catch (err) {
			console.error(`${ts()} [Redial] reconnect trigger failed:`, (err as Error)?.message ?? err);
		} finally {
			legacyReconnectInFlight = false;
		}
	};
	const armRedialTimer = (delayMs: number): void => {
		if (redialTimer) clearTimeout(redialTimer);
		redialTimer = setTimeout(fireEventRedial, delayMs);
	};

	// Declared outside the classifier IIFE below so the recovery hook can read it
	// too; a banner already shown is what makes a recovery notice owed.
	const voiceNotifiedCategories = new Set<string>();

	// Announce recovery and re-arm the alert. Clearing the set is what lets a later
	// failure of the same category notify at all; the throttle is once-per-process.
	const notifyVoiceRecovered = (): void => {
		if (voiceNotifiedCategories.size === 0) return;
		const had = [...voiceNotifiedCategories].join(', ');
		voiceNotifiedCategories.clear();
		console.log(`${ts()} [VoiceRecovered] ACTIVE after ${had} — notifying owner + re-arming alerts`);
		try {
			execFileSync('osascript', ['-e',
				`display notification "${formatVoiceRecoveryNotification(new Date())}" with title "Sutando — voice online"`,
			], { stdio: 'ignore' });
		} catch {}
	};

	// =========================================================================
	// `agent.state` v1 provider (design 1a′; impl plan WS1 Step 12,
	// amendments R8/A9/A10/S3). All getters are late-bound: `sessionRef` is
	// assigned right after the constructor, and no client (or probe) can
	// reach the WS server before session.start() runs below.
	// =========================================================================
	let agentInitialized = false;
	const agentStateProvider = createAgentStateProvider({
		initialized: () => agentInitialized,
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		sessionState: () => String((sessionRef as any)?.sessionManager?.state ?? 'CREATED'),
		// Real clients only — probe sockets never attach (Step 11's bodhi
		// interception keeps them off `clientConnected`).
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		clientAttached: () => Boolean((sessionRef as any)?.clientConnected),
		backoffUntil: () => voiceFatalBackoffUntil,
		// R8: the persisted last terminal classification lives in the
		// classifier module — one classifier, one store.
		lastTerminalFailure: () => lastTerminalClassification(),
		// A10: the EXISTING resolver result the agent loaded its key from —
		// `voiceApiKey()`'s string signature is untouched; label mapping
		// (env→byok) happens inside the provider. `credentialGeneration` is
		// only ever REPORTED (Rust mints it; S3 plumbs it via the managed
		// file / SUTANDO_VOICE_CREDENTIAL_GENERATION).
		credential: () => voiceCredential,
		// R17: echo launchdContract:1 only when the launchd contract env
		// marker is present.
		launchdContract: () => process.env.SUTANDO_VOICE_LAUNCHD_CONTRACT === '1',
	});
	const buildAgentState = (): AgentStateV1 => agentStateProvider.build();

	// Feature-detect Step-11 probe support in the pinned bodhi. The
	// `probeState` constructor option ships with the bodhi PR + pin bump
	// (impl plan PR group E); until that pin lands the option would be
	// silently ignored, so the detect keeps the wiring intent explicit and
	// lets the pin bump activate it without touching this file. Detection:
	// the bundled VoiceSession source must mention the option.
	// Test seam (SUTANDO_TEST_MODE only): forces the detect false so the suite
	// can prove the marker gate's dormant branch against a spawned agent.
	const bodhiSupportsProbeState = (() => {
		if (process.env.SUTANDO_TEST_MODE === '1' && process.env.SUTANDO_TEST_FORCE_NO_PROBE_STATE === '1') {
			return false;
		}
		try { return String(VoiceSession).includes('probeState'); } catch { return false; }
	})();

	// P7 D7.1: engine-side audio-progress ledger (Tranche A interim, coverage
	// session-only) + worker-thread persistence. Created before the session so
	// the hooks below can reference it; the wraps install after construction.
	const healthPersistence = createHealthPersistence();
	const audioHealth = createAudioHealthLedger({
		sessionId: SESSION_ID,
		persist: (row) => healthPersistence.tryEnqueue(row),
		log: (m) => console.log(`${ts()} ${m}`),
		// Samples bodhi's getDiagnostics on ledger ticks. `session` is assigned
		// below; the try/catch — not the typeof — is what covers a tick racing
		// construction, since typeof on a const in its TDZ still throws.
		getSessionDiagnostics: () => {
			try {
				return typeof session !== 'undefined' ? (session.getDiagnostics?.() ?? null) : null;
			} catch {
				return null;
			}
		},
	});

	const session = new VoiceSession({
		sessionId: SESSION_ID,
		userId: 'user',
		apiKey: GEMINI_VOICE_API_KEY,
		agents: [mainAgent],
		initialAgent: 'main',
		port: PORT,
		host: HOST,
		model: google(VOICE_MODEL),
		geminiModel: VOICE_NATIVE_AUDIO_MODEL,
		speechConfig: { voiceName: VOICE_NAME },
		inputAudioTranscription: true,
		// ACTIVE-silence recovery wire — a null coordinator (shadow/off mode)
		// makes every forward a no-op.
		onClientCommand: (message) => voiceRecoveryCoordinator?.handleClientCommand(message),
		onClientConnected: () => {
			if (legacyReconnectInFlight) return; // not a real attach edge
			voiceRecoveryCoordinator?.handleClientConnected();
		},
		onClientDisconnected: () => voiceRecoveryCoordinator?.handleClientDisconnected(),
		// Whenever the coordinator owns the episode (restarting, waiting-retry,
		// terminal, or a recovered origin), bodhi's attach auto-actions —
		// greeting, context replay and especially the CLOSED auto-reconnect —
		// would be uncounted bypasses of the attempt budget. The synthetic hold
		// already gates the injection paths post-recovery; this gates the dial.
		suppressClientAutoActions: () => voiceRecoveryCoordinator?.ownsRecovery ?? false,
		// P7 Tranche B: feed the ledger the two provider facts it cannot infer —
		// context occupancy and connection lineage (design §1.1/§1.4).
		// The per-modality breakdown rides the same message (design §1.4) but is
		// provider-specific, so bodhi types only the shared fields — cast to read.
		onUsageMetadata: (u) =>
			audioHealth.noteUsageMetadata(
				u.promptTokenCount,
				(u as { promptTokensDetails?: Array<{ modality?: string; tokenCount?: number }> })
					.promptTokensDetails,
			),
		// One lifecycle stream, THREE consumers: the ledger derives lineage and
		// context facts; F5's redial scheduler reacts to terminal losses; the
		// armed coordinator correlates activations and closes. While the
		// coordinator owns the episode, F5 keeps tracking state but must not
		// arm a dial — that would be an uncounted attempt outside the budget.
		onConnectionLifecycle: (ev) => {
			audioHealth.noteLifecycleEvent(ev);
			voiceRecoveryCoordinator?.handleLifecycleEvent(ev);
			const r = noteLifecycle(redialState, ev, { now: Date.now(), fatalBackoffUntil: voiceFatalBackoffUntil });
			redialState = r.state;
			if (r.scheduleDelayMs !== null && !(voiceRecoveryCoordinator?.ownsRecovery ?? false)) {
				console.log(`${ts()} [Redial] ${ev.kind}${'code' in ev && ev.code !== undefined ? ` code=${ev.code}` : ''} — dial in ${r.scheduleDelayMs}ms (failures=${redialState.failures})`);
				armRedialTimer(r.scheduleDelayMs);
			}
		},
		// Phase 0.5 seams — spread for REAL key absence (design §2.1: an absent
		// key lets the server default apply; `undefined` is not absent).
		...(VOICE_SESSION_TUNING.compressionConfig !== undefined
			? { compressionConfig: VOICE_SESSION_TUNING.compressionConfig }
			: {}),
		...(VOICE_SESSION_TUNING.mediaResolution !== undefined
			? { mediaResolution: VOICE_SESSION_TUNING.mediaResolution }
			: {}),
		...(VOICE_SHADOW_STT
			? {
					shadowSttProvider: new GeminiBatchSTTProvider({
						apiKey: GEMINI_VOICE_API_KEY,
						model: 'gemini-2.5-flash',
					}),
					divergenceCorrection: VOICE_DIVERGENCE_CORRECTION,
					onTranscriptionDivergence: (live: string, shadow: string, turnId?: number) => {
						console.log(`${ts()} [ShadowSTT] model heard ≠ said (turn ${turnId ?? '?'}): live="${live}" shadow="${shadow}"`);
					},
				}
			: {}),
		// Step 11/12: when the pinned bodhi supports `?probe=1` probe
		// interception, hand it the agent.state builder — probes get one
		// frame + close 1000 without ever touching `this.client`. The Z3
		// isolated idle-restore arms here too: a probe is the only
		// probe-shaped hook this repo controls until bodhi exposes a
		// dedicated probe/verifier-close callback (seam documented on
		// `createIsolatedIdleRestore().arm`).
		...(bodhiSupportsProbeState
			? {
				probeState: (): AgentStateV1 => {
					const frame = buildAgentState();
					probeIdleRestore.arm();
					return frame;
				},
			}
			: {}),
		hooks: {
			onSessionStart: (e) => {
				resetSessionGateState();
				recorder.reset();
				recorder.events.push({ event: 'session_started', timestamp: new Date().toISOString() });
				recorder.startTicker(VOICE_NATIVE_AUDIO_MODEL);
				console.log(`${ts()} [Session] Started: ${e.sessionId}`);
			},
			onSessionEnd: (e) => {
				recorder.events.push({ event: `session_ended:${e.reason}`, timestamp: new Date().toISOString() });
				console.log(`${ts()} [Session] Ended: ${e.sessionId} (${e.reason})`);
				clearActiveArtifact();
				recorder.flush();
			},
			// P7 D7.3: bodhi's turn-latency hook was half-blind AND unconsumed
			// (G-P7-12) — the ledger keeps the last value for [Health] and the
			// persisted snapshot.
			onTurnLatency: (e: { turnId?: string; segments?: { totalE2EMs?: number } }) => {
				audioHealth.noteTurnLatency(e?.segments?.totalE2EMs);
				console.log(`${ts()} [Latency] ${e?.turnId ?? '?'} e2e=${e?.segments?.totalE2EMs ?? '?'}ms`);
			},
			onToolCall: (e) => {
				audioHealth.noteModelEvent(); // P7 D7.1: a tool call is model activity
				voiceWatchdogShadow.noteToolCall(e.toolCallId, e.execution);
				voiceRecoveryCoordinator?.noteToolCall(e.toolCallId, e.execution);
				voiceToolIdMap.set(e.toolCallId, e.toolName);
				// tool_call event push removed per #1052 — canonical record
				// is the surface-table row written in onToolResult via
				// recordToolCall(). Pushing here would duplicate in
				// session_events.
				console.log(`${ts()} [Tool] ${e.toolName} (${e.execution})`);
				// Flag the web-client that a tool is in flight so the avatar
				// can show the blue `.working` pulse and the menu bar can
				// switch to the slow-deep-swing signature. `source=tool` pins
				// this to the tool track so the browser's 1s poll can't
				// overwrite it back to listening.
				fetch(`http://localhost:8080/mute-state?state=working&source=tool&label=${encodeURIComponent(e.toolName)}`).catch(() => {});
				// Auto-switch meeting mode on join/dismiss
				if (['summon', 'join_zoom', 'join_gmeet'].includes(e.toolName)) {
					meetingActive = true;
					voiceWatchdogShadow.noteMeetingMode(true);
					voiceRecoveryCoordinator?.noteMeetingMode(true);
					console.log(`${ts()} [Meeting] Auto-activated by ${e.toolName}`);
				} else if (e.toolName === 'dismiss') {
					meetingActive = false;
					voiceWatchdogShadow.noteMeetingMode(false);
					voiceRecoveryCoordinator?.noteMeetingMode(false);
					console.log(`${ts()} [Meeting] Ended by dismiss`);
				}
			},
			onToolResult: (e) => {
				voiceWatchdogShadow.noteToolSettled(e.toolCallId);
				voiceRecoveryCoordinator?.noteToolSettled(e.toolCallId);
				const toolName = voiceToolIdMap.get(e.toolCallId) || 'unknown';
				recorder.toolCalls.push({ name: toolName, durationMs: e.durationMs, timestamp: new Date().toISOString() });
				// tool_result event push removed per #1052 — recordToolCall
				// below is the canonical write (surface table, kind='tool_call',
				// duration_ms column). Pushing here would duplicate in
				// session_events.
				recordToolCall('voice', toolName, e.durationMs, SESSION_ID);
				console.log(`${ts()} [Tool] result: ${toolName} (${e.status}, ${e.durationMs}ms)`);
				// Clear the tool track; browser track takes over immediately.
				fetch('http://localhost:8080/mute-state?state=idle&source=tool').catch(() => {});
			},
			onSubagentStep: (e) => console.log(`${ts()} [Subagent] ${e.subagentName} #${e.stepNumber} [${e.toolCalls.join(',')}]`),
			onError: (e) => {
				recorder.events.push({ event: `error:${e.component}:${e.error.message}`, timestamp: new Date().toISOString() });
				console.error(`${ts()} [Error] ${e.component}: ${e.error.message} (${e.severity})`);
			},
		},
	});

	sessionRef = session;

	// Armed only with the full bodhi recovery surface; anything less falls
	// back to shadow with a loud line (the design's capability-validation rule).
	if (ACTIVE_SILENCE_MODE === 'armed' && ACTIVE_SILENCE_TICKS > 0) {
		if (recoverySurfaceSupported(session)) {
			voiceRecoveryLedger = new WatchdogLedger({
				path: join(WORKSPACE_DIR, 'logs', 'voice-recovery.jsonl'),
				meta: { detectorVersion: DETECTOR_VERSION, mode: 'armed', pid: process.pid },
				onError: (err) => console.error(`${ts()} [SilenceRecovery] ledger write failed: ${err.message}`),
			});
			voiceRecoveryCoordinator = new VoiceSilenceRecoveryCoordinator({
				voiceSessionId: SESSION_ID,
				session: session as unknown as RecoverySessionSurface,
				requiredTicks: ACTIVE_SILENCE_TICKS,
				// Reducer clock domain is MONOTONIC; only wire frames and the
				// ledger use wall time (wallNowFn default).
				nowFn: () => performance.now(),
				log: (m) => console.log(`${ts()} [SilenceRecovery] ${m}`),
				record: (row) => voiceRecoveryLedger?.append(row),
			});
			// Seed startup-detected meeting state — the coordinator was not
			// alive when the boot-time Zoom probe ran.
			voiceRecoveryCoordinator.noteMeetingMode(meetingActive);
			console.log(`${ts()} [SilenceRecovery] ARMED (ticks=${ACTIVE_SILENCE_TICKS})`);
		} else {
			console.warn(`${ts()} [SilenceRecovery] armed requested but the bodhi surface lacks the recovery capabilities — staying in shadow`);
		}
	} else if (ACTIVE_SILENCE_MODE === 'armed') {
		console.warn(`${ts()} [SilenceRecovery] armed requested but VOICE_ACTIVE_SILENCE_TICKS=0 disables the watchdog — staying in shadow`);
	}

	// P7 D7.1: install the session-layer ledger wraps (audio ingress count +
	// ingress-RMS speech tracker, audio_health heartbeat intercept, egress
	// count). Coverage is honestly session-only until the bodhi pin.
	audioHealth.wrapSession(session);
	// P7 D7.4: vision defers frames while the canonical speech evidence is
	// active (pause-during-speech rides the same tracker as the matrix).
	setVisionSpeechEvidence(() => audioHealth.getSpeechEvidence());

	// =========================================================================
	// `agent.state` emission + lifecycle snapshot (Step 12 + amendment A9).
	// One emitter, three triggers: an immediate frame on every accepted real
	// connection, a repeat frame on every upstream transition, and an atomic
	// `state/voice-lifecycle.json` publish on every relevant transition
	// (client attach/detach, initialized flip, upstream change). This module
	// is the ONLY writer of the lifecycle file (A9) — WS2's control consumer
	// reads it cross-process.
	// =========================================================================
	let lastEmittedUpstream: string | null = null;
	let lastLifecycleKey = '';
	// The marker must never advertise a capability the resolved bodhi lacks, and
	// never publish unbound (no token → no marker): a marker on disk is always real and bound.
	if (bodhiSupportsProbeState && typeof voiceLockId === 'string' && voiceLockId) {
		publishCapabilitiesMarker(WORKSPACE_DIR, {
			lockId: voiceLockId,
			onError: (err) => console.error(`${ts()} [AgentState] capabilities marker write failed: ${(err as Error)?.message ?? err}`),
		});
	} else if (!bodhiSupportsProbeState) {
		console.error(`${ts()} [AgentState] bodhi lacks probeState — capability marker NOT published (probes stay dormant)`);
	} else {
		console.error(`${ts()} [AgentState] no acquisition token from the lock helper — capability marker NOT published (probes stay dormant)`);
	}
	const sendAgentStateFrame = (frame: AgentStateV1): void => {
		try {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			(session as any).clientTransport?.sendJsonToClient?.(frame);
		} catch (err) {
			console.error(`${ts()} [AgentState] frame send failed: ${(err as Error)?.message ?? err}`);
		}
	};
	const emitAgentState = (opts: { immediate?: boolean } = {}): void => {
		const frame = buildAgentState();
		// A transition includes reason/category changes within 'failed'
		// (e.g. auth-invalid → quota-exceeded after a key rotation) — those
		// are meaningful upstream transitions even when the state name
		// doesn't change.
		const upstreamKey = `${frame.upstream}|${frame.reason ?? ''}|${frame.category ?? ''}`;
		const upstreamChanged = upstreamKey !== lastEmittedUpstream;
		lastEmittedUpstream = upstreamKey;
		if (opts.immediate || upstreamChanged) sendAgentStateFrame(frame);
		// A9: publish the lifecycle snapshot when any relevant field flipped
		// (attach/detach, initialized, upstream, and — P7 D7.1 — the additive
		// inputHealth verdict P4's evidence ladder consumes) — atomic
		// temp+rename inside publishLifecycleSnapshot.
		const ledgerHealth = audioHealth.getInputHealth(session.clientConnected);
		// The lifecycle schema's four values: no-client folds into 'unknown'
		// (a detached client is absence of evidence for THIS field).
		const inputHealth = ledgerHealth === 'no-client' ? 'unknown' : ledgerHealth;
		const lifecycleKey = `${frame.clientAttached}|${frame.initialized}|${upstreamKey}|${inputHealth}`;
		if (lifecycleKey !== lastLifecycleKey) {
			lastLifecycleKey = lifecycleKey;
			publishLifecycleSnapshot(WORKSPACE_DIR, frame, {
				inputHealth,
				onError: (err) => console.error(`${ts()} [AgentState] lifecycle snapshot write failed: ${(err as Error)?.message ?? err}`),
			});
		}
	};
	// Upstream transitions: bodhi's sessionManager publishes
	// `session.stateChange` on every transitionTo() — the same seam the
	// health monitor's state reads observe. ACTIVE proves the credential
	// works again, so it clears the persisted terminal classification (R8)
	// before the frame is built.
	session.eventBus.subscribe('session.stateChange', (e) => {
		if ((e as { toState?: string })?.toState === 'ACTIVE') {
			clearTerminalClassification();
			// One recovery site, event-driven: the same seam the classification clear
			// uses, so a polled second copy cannot drift from it.
			notifyVoiceRecovered();
		}
		emitAgentState();
	});

	// Wire vision streaming — the start_vision tool needs the live session
	// to call session.transport.sendFile for each frame. Also boot the local
	// HTTP control endpoint so the web-client Watch button can drive the
	// same controller (proxied through web-client to stay same-origin).
	setVisionSession(session);
	// updateTools is on the private transport (GeminiLiveTransport), not VoiceSession.
	// Applied on next reconnect — restricts what Gemini sees after the next transport cycle.
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	setSessionToolUpdater((tools) => (session as any).transport?.updateTools?.(tools), mainAgentTools);
	startVisionControlServer();

	// Wire voice-failure classifier: when the Gemini Live transport closes
	// with a non-retryable reason (credits depleted, quota exceeded, key
	// invalid, model not found), surface an actionable message via the
	// proactive-result channel + an OS notification. Throttled per category
	// so the 30s reconnect loop doesn't spam.
	(() => {
		const transport = (session as any).transport;
		if (!transport || typeof transport !== 'object') {
			console.error(`${ts()} [VoiceFailure] no transport on session — classifier not wired`);
			return;
		}
		const origOnClose = typeof transport.onClose === 'function'
			? transport.onClose.bind(transport)
			: null;
		// Shared with the recovery hook, which needs to know a banner was shown.
		const notifiedCategories = voiceNotifiedCategories;
		const handleClose = (c: ClassifiedClose): void => {
			if (c.retryable) return;
			// R8: persist the terminal classification (one classifier, one
			// store — voice-error-classifier.ts) so buildAgentState() reports
			// `upstream:'failed'` with the stable reason/category between
			// closes, then emit the upstream transition to any attached
			// client + the lifecycle snapshot (Step 12).
			recordTerminalClassification(c);
			// Push the health-monitor reconnect window out by 5min on every
			// non-retryable close — including repeats of an already-notified
			// category — so the 60s retry loop doesn't keep firing while the
			// upstream issue persists. Without this, a 1011 credit-depleted
			// loop produces ~6 log lines / 60s indefinitely.
			voiceFatalBackoffUntil = Date.now() + 5 * 60 * 1000;
			// The reducer clock domain is MONOTONIC — the wall-time deadline
			// above (kept for agent-state/UI) must not cross into it, or the
			// backoff never expires in reducer time.
			voiceRecoveryCoordinator?.handleFatalBackoff(performance.now() + 5 * 60 * 1000);
			emitAgentState();
			if (notifiedCategories.has(c.category)) return;
			notifiedCategories.add(c.category);
			console.error(`${ts()} [VoiceFailure] ${c.category}: ${c.userMessage} (raw="${c.rawReason}")`);
			// Surface via proactive-result channel — picked up by web-client
			// task feed and the Discord/Telegram bridges if configured.
			try {
				const tsMs = Date.now();
				const path = join(WORKSPACE_DIR, 'results', `proactive-voice-${c.category}-${tsMs}.txt`);
				const body = c.userActionUrl
					? `${c.userMessage} ${c.userActionUrl}`
					: c.userMessage;
				// Publish atomically: a consumer must never observe a partial body.
				const tmp = `${path}.tmp-${process.pid}`;
				writeFileSync(tmp, body);
				renameSync(tmp, path);
			} catch (e) {
				console.error(`${ts()} [VoiceFailure] proactive write failed: ${(e as Error)?.message ?? e}`);
			}
			// OS notification — visible even if no browser tab is open.
			// execFileSync avoids the shell entirely, so no sanitization of
			// single-quotes or other shell metacharacters is needed. The
			// double-quote stripping below protects the AppleScript string
			// literal itself (not the shell).
			try {
				const safe = formatVoiceOfflineNotification(c.userMessage, new Date());
				execFileSync('osascript', ['-e', `display notification "${safe}" with title "Sutando — voice offline"`], { stdio: 'ignore' });
			} catch {}
		};
		transport.onClose = (code?: number, reason?: string) => {
			if (origOnClose) {
				try { origOnClose(code, reason); } catch (e) {
					console.error(`${ts()} [VoiceFailure] origOnClose threw: ${(e as Error)?.message ?? e}`);
				}
			}
			try {
				const c = classifyTransportClose(code, reason);
				handleClose(c);
			} catch (e) {
				console.error(`${ts()} [VoiceFailure] classifier threw: ${(e as Error)?.message ?? e}`);
			}
		};
		console.log(`${ts()} [VoiceFailure] classifier wired into transport.onClose`);
	})();

	// Test-only fault injection (SUTANDO_TEST_MODE only): synthesize an
	// upstream transport close through the REAL wrapped `transport.onClose`
	// seam above, so integration tests can drive classifier →
	// recordTerminalClassification → agent.state 'failed' emission
	// deterministically offline (no dependency on live Gemini responses).
	// File-triggered so the test controls WHEN the close fires relative to
	// its own client connection (boot timing on CI is unbounded).
	// Format: SUTANDO_TEST_UPSTREAM_CLOSE="<triggerFile>|<code>|<reason>".
	if (process.env.SUTANDO_TEST_MODE === '1' && process.env.SUTANDO_TEST_UPSTREAM_CLOSE) {
		const [trigger, codeRaw, ...reasonParts] = process.env.SUTANDO_TEST_UPSTREAM_CLOSE.split('|');
		const poll = setInterval(() => {
			if (!existsSync(trigger)) return;
			clearInterval(poll);
			try {
				// eslint-disable-next-line @typescript-eslint/no-explicit-any
				(session as any).transport?.onClose?.(Number(codeRaw) || 1011, reasonParts.join('|'));
			} catch { /* test-only */ }
		}, 250);
	}
	// Native audio streams transcript and audio concurrently, so suppression reaches only
	// the REMAINING chunks of a turn — anything already sent cannot be recalled.
	(() => {
		// Wiring lives in output_sanitizer.ts so it has a test seam: this adapter
		// path had none, which is the standing review blocker on this PR.
		wireSanitizerToTransport({
			transport: (session as any).transport,
			subscribe: (ev, fn) => session.eventBus.subscribe(ev as any, fn),
			beforeTranscriptFlush: (reset) => {
				// bodhi flushes the transcript 2-4 lines BEFORE publishing turn.end
				// (dist/index.js:3019/3106/3177), so a subscriber runs too late.
				const tm = (session as any).transcriptManager;
				if (!tm || typeof tm.flush !== 'function') return;
				const origFlush = tm.flush.bind(tm);
				tm.flush = () => { try { reset(); } catch {} origFlush(); };
			},
			onBlocked: (buffered) =>
				console.error(`${ts()} [OutputSanitizer] BLOCKED fabricated directive spoken aloud: ${buffered.slice(0, 120)}`),
			log: (m) => console.log(`${ts()} ${m}`),
		});
	})();

	// Wire narration-tee: capture Gemini's outbound audio for screen recordings
	try {
		const { teeAudio } = await import('../skills/screen-record/scripts/narration-tee.js');
		const origHandleAudioOutput = (session as any).handleAudioOutput.bind(session);
		(session as any).handleAudioOutput = (data: string) => {
			origHandleAudioOutput(data);
			try { teeAudio(Buffer.from(data, 'base64')); } catch {}
		};
		console.log(`${ts()} [NarrationTee] wired into voice agent audio output`);
	} catch (e) {
		console.log(`${ts()} [NarrationTee] not available: ${e instanceof Error ? e.message : e}`);
	}

	// Wire recording hooks — enables description push during record_screen_with_narration
	try {
		const { setupRecordingHooks } = await import('./recording-tools.js');
		setupRecordingHooks(session);
		console.log(`${ts()} [RecordingHooks] wired into voice agent`);
	} catch (e) {
		console.log(`${ts()} [RecordingHooks] not available: ${e instanceof Error ? e.message : e}`);
	}
	// Durable-channel wiring (context drops, note viewing, task results →
	// session injection) moved verbatim to live-agent-runtime.ts (step 5a-2).
	// The Cartesia stuck-session fallback is adapter-provided via opts.
	wireDurableChannels(session, { cartesiaApiKey: CARTESIA_API_KEY, generateSpeech });

	// P7 D7.3: the transcript cursor lives in the clear helper so every clear
	// path rebases it with the items array (G-P7-8).
	const liveTranscriptPath = '/tmp/sutando-live-transcript-voice.txt';
	try { writeFileSync(liveTranscriptPath, `--- Live Transcript: ${new Date().toISOString()} ---\n\n`); } catch {}
	session.eventBus.subscribe('turn.end', () => {
		const items = session.conversationContext.items;
		// If end_session fired this session, keep clearing items so
		// bodhi's reconnect replay path has nothing goodbye-flavored
		// to inject on the next reconnect. Items re-accumulate during
		// the post-goodbye "Farewell. Talk to you next time." turns.
		if (sessionEnding && Array.isArray(items) && items.length > 0) {
			itemsClear.clear('session-ending-turn-end');
			return;
		}
		for (const item of items.slice(itemsClear.cursor.index)) {
			if (item.role === 'user' || item.role === 'assistant') {
				console.log(`${ts()}   [${item.role}] ${item.content}`);
				logConversation(item.role, item.content, SESSION_ID);
				const evtRole = item.role === 'user' ? 'user' : 'sutando';
				// utterance event push removed per #1052 — canonical record is
				// the voice-table row written by logConversation() above
				// (kind='user'/'agent', ts_unix). session_events keeps only
				// lifecycle entries to stop triple-encoding the same atom.
				recorder.transcript.push({ role: evtRole, text: item.content || '' });
				const label = item.role === 'user' ? 'User' : 'Sutando';
				try { appendFileSync(liveTranscriptPath, `[${new Date().toLocaleTimeString('en-US', {hour12:false})}] ${label}: ${item.content}\n`); } catch {}
				// Track real user turns for the end_session gate.
				// Skip items that are injected system prompts: they get
				// role='user' from bodhi's sendContent/transport but their
				// content starts with '[System:' — those are not real
				// speech and shouldn't unlock end_session.
				if (item.role === 'user' && item.content && !item.content.startsWith('[System:')) {
					userTurnCount++;
				}
			}
		}
		itemsClear.cursor.index = items.length;
	});

	// Track user interruption events as a secondary signal for the
	// end_session gate. bodhi fires turn.interrupted whenever the user's
	// audio interrupts the assistant, regardless of whether transcription
	// succeeds — so it works under native-audio models where items may
	// not get populated with user turns.
	session.eventBus.subscribe('turn.interrupted', () => {
		userHasInterrupted = true;
		console.log(`${ts()} [VoiceSession] user interrupt detected — userHasInterrupted=true`);
	});

	// Give each skill's setup() the live session so it registers handlers without
	// importing core. Guarded: a buggy setup must not break session bootstrap.
	runSkillSetups(personalSkillSetups, { session, injectText },
		(msg, detail) => console.error(`${ts()} ${msg}`, detail));

	// Audio-duck relay: flag the slide server (localhost:7877) when Sutando is
	// producing audio, so the deck ducks the active slide video under the
	// narration. turn.start → speaking on; turn.end / turn.interrupted → off.
	// Fire-and-forget; failures are harmless (deck just won't duck). Decouples
	// ducking from Gemini tool-call timing entirely. (Observe-talk feature.)
	const _duck = (mode: 'on' | 'off') => {
		try { fetch(`http://localhost:7877/speaking/${mode}`, { method: 'POST' }).catch(() => {}); } catch {}
	};
	session.eventBus.subscribe('turn.start', () => _duck('on'));
	// P7 D7.1: the model hop is EVENTS — a text/tool-first turn must count
	// as model activity even before any audio frame lands.
	session.eventBus.subscribe('turn.start', () => audioHealth.noteModelEvent());
	// Armed coordinator: model progress advances the reducer's silence anchor,
	// generation-fenced by the event's own transport generation (bodhi #35).
	session.eventBus.subscribe('turn.start', (e: { transportGeneration?: number }) =>
		voiceRecoveryCoordinator?.handleModelEvent(e?.transportGeneration),
	);
	session.eventBus.subscribe('turn.end', () => _duck('off'));
	session.eventBus.subscribe('turn.interrupted', () => _duck('off'));

	const shutdown = async () => {
		console.log(`\n${ts()} Shutting down...`);
		recorder.flush();
		await voiceWatchdogShadow.flush().catch(() => {});
		voiceRecoveryCoordinator?.stop();
		await voiceRecoveryLedger?.flush().catch(() => {});
		setVisionSession(null);
		setSessionToolUpdater(null, []);
		stopVisionControlServer();
		await session.close('user_hangup');
		process.exit(0);
	};
	process.on('SIGINT', shutdown);
	process.on('SIGTERM', shutdown);
	// Crash-only fatal handlers (design 1d; impl plan WS1 Step 1). The
	// 2026-08-04 incident (pid 14059, ~5 h outage) spun INSIDE the old
	// `console.error(…, err)` reporting path (`TriggerUncaughtException` →
	// `InspectorConsoleCall` / `ErrorStackGetter`) while staying alive and
	// holding :9900. New invariant: a fatal TERMINATES, promptly, via a
	// bounded primitive-only crash record — no logger, no recorder, no
	// SQLite, no `err.stack`, no object inspection. `recorder.flush()` stays
	// ONLY in the graceful shutdown path above; richer crash metadata is the
	// supervisor's job when it reaps the exit.
	const crashRecordPath = CRASH_RECORD_PATH;
	const onFatal = (err: unknown) => {
		// One-shot guard: a second fatal while handling the first goes
		// straight to process.exit(1). markFatalExit() also suppresses the
		// exit-time Python lock release (amendment R1) — the helper can block
		// on the guard; the stale lock is replaced safely by the supervisor.
		if (markFatalExit()) {
			process.exit(1);
			return;
		}
		if (classifyFatalExitCode(err) === EXIT_CODE_DUPLICATE_INSTANCE) {
			// EADDRINUSE on the WS port means another voice-agent (typically
			// the launchd-managed one) already owns it — duplicate-instance
			// semantics, exit 7 (impl plan WS1 Step 2 / amendment R2), same as
			// the duplicate-lock exit. Release the vision control port so the
			// live agent (or the next restart) can claim 7847.
			console.error(`${ts()} [FATAL] EADDRINUSE on :${PORT} — another voice-agent is listening; exiting so the live one keeps the vision control port.`);
			try { stopVisionControlServer(); } catch {}
			process.exit(EXIT_CODE_DUPLICATE_INSTANCE);
			return;
		}
		// Static string only — never interpolate or inspect `err` here.
		console.error(`${ts()} [FATAL] uncaught fatal — crash-only exit (record: ${crashRecordPath})`);
		writeCrashRecordAndExit(err, crashRecordPath, { exit: (code) => process.exit(code) });
	};
	process.on('uncaughtException', onFatal);
	process.on('unhandledRejection', onFatal);

	// Test-only fault injection (SUTANDO_TEST_MODE only): raise an uncaught
	// exception AFTER the fatal handlers are installed so tests can pin the
	// uncaughtException path directly — e.g. EADDRINUSE → exit 7 through
	// `onFatal`, not only through `main().catch` (amendment R2).
	if (process.env.SUTANDO_TEST_MODE === '1' && process.env.SUTANDO_TEST_RAISE_UNCAUGHT) {
		const raiseCode = process.env.SUTANDO_TEST_RAISE_UNCAUGHT;
		setTimeout(() => {
			throw Object.assign(
				new Error(`test uncaught ${raiseCode}`),
				raiseCode === 'EADDRINUSE' ? { code: raiseCode } : {},
			);
		}, 250);
	}

	voiceSessionRef = session;

	// Idle teardown — close the upstream Gemini transport when no client has
	// been connected for IDLE_TEARDOWN_MS. Without this, voice-agent keeps the
	// Gemini Live session alive 24/7; every ~9-min Gemini reconnect ("GoAway")
	// produces a phantom assistant turn (sometimes a tool call) with no user
	// input. Symptoms observed: phantom save_meeting_note polluting markdown
	// notes, phantom open_url opening browser tabs, phantom work tool calls
	// writing fake task files. CLOSED state is a fixed point when
	// clientConnected=false (the existing health monitor only reconnects
	// CLOSED→CONNECTING when a client is present), so once we transition there
	// no phantoms can fire until the next legitimate client reconnect.
	// Tunable via env var per Mini's #602 review note. Defaults to 60s — sane
	// for the voice / phone reconnect cadence we've observed; raise if a host
	// has frequent ~70s connect/disconnect churn that re-opens too aggressively.
	const IDLE_TEARDOWN_MS = Number(process.env.SUTANDO_VOICE_IDLE_TEARDOWN_MS) || 60_000;
	let idleTeardownTimer: ReturnType<typeof setTimeout> | null = null;

	// Shared teardown body — used by the one-shot idle timer below AND by the
	// Z3 isolated idle-restore (probe/verifier fence). Re-checks real-client
	// attachment at fire time so a client that connected while the timer was
	// pending is never torn down under.
	const teardownIdleUpstream = async (via: string) => {
		if ((session as any).clientConnected) return;
		const transport = (session as any).transport;
		if (!transport?.disconnect) return;
		console.log(`${ts()} [VoiceSession] Idle (${via}) — closing Gemini transport (no phantoms while CLOSED)`);
		try {
			await transport.disconnect();
		} catch (err) {
			console.error(`${ts()} [VoiceSession] Idle teardown failed: ${(err as Error)?.message ?? err}`);
		}
	};

	const cancelIdleTeardown = () => {
		if (idleTeardownTimer) {
			clearTimeout(idleTeardownTimer);
			idleTeardownTimer = null;
		}
	};
	const scheduleIdleTeardown = () => {
		cancelIdleTeardown();
		idleTeardownTimer = setTimeout(async () => {
			idleTeardownTimer = null;
			await teardownIdleUpstream(`${IDLE_TEARDOWN_MS / 1000}s idle`);
		}, IDLE_TEARDOWN_MS);
	};

	// Amendment Z3 — verifier/probe idle restoration. The initial idle timer
	// above is one-shot and rearmed only by the REAL-client disconnect
	// wrapper; a probe/verifier that closes after that timer already fired
	// would otherwise leave a woken upstream connected forever (no real
	// client will ever rearm it). The isolated restore timer arms on
	// probe-role close with no real client attached and restores the prior
	// idle state (upstream → CLOSED); a later real connection fences it
	// (handleClientConnected wrapper below). SEAM: until the Step-11 bodhi
	// pin exposes a probe/verifier-close hook, the only in-repo arm point is
	// the `probeState` callback passed to the VoiceSession constructor —
	// when bodhi's role close hook lands, wire it to `probeIdleRestore.arm()`
	// directly.
	const probeIdleRestore = createIsolatedIdleRestore({
		delayMs: IDLE_TEARDOWN_MS,
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		clientAttached: () => Boolean((session as any).clientConnected),
		teardown: () => teardownIdleUpstream('probe-idle-restore'),
	});

	// Flush metrics on client disconnect — bodhi's handleClientDisconnected()
	// doesn't trigger onSessionEnd, so metrics would never be written. Also
	// arms the idle-teardown timer (see above).
	const origDisconnect = (session as any).handleClientDisconnected?.bind(session);
	if (origDisconnect) {
		(session as any).handleClientDisconnected = () => {
			origDisconnect();
			// No listener left, so frames only burn capture + quota. Reason is
			// terminal: a browser push driver must tear down, not re-arm.
			const stopped = stopVisionStreaming('no-client');
			if (stopped.status === 'stopped') {
				console.log(`${ts()} [Vision] stopped on client disconnect (${stopped.frames} frame(s))`);
			}
			recorder.flush();
			writeVoiceState(false);
			// P7 D7.1: final ledger row for the departing epoch — written NOW
			// (the crash-evidence rule: end-of-session-only flushes lose it).
			audioHealth.onClientDisconnected();
			audioHealth.persistTick('final', false);
			scheduleIdleTeardown();
			// Step 12/A9: client detach is a lifecycle transition (and may
			// flip upstream backoff→idle now that no client is attached).
			emitAgentState();
		};
	}

	// Reset per-session state on client connect when a stale flush is sitting
	// in the buffer. Bodhi's onSessionStart only fires on the first ACTIVE
	// transition (index.js:1219 — `!this.startedAt` guard, never reset). So:
	//   (a) 2nd+ user-connects within one process miss the onSessionStart reset
	//   (b) a phantom server-idle session_end can flush `metricsWritten=true`
	//       BEFORE the first real client ever connects (observed 2026-05-22:
	//       server starts → 60s idle → bodhi auto-ends a 0/0 phantom session →
	//       metricsWritten=true → real user connects 30min later → next
	//       onSessionEnd's writeVoiceMetrics returns early → record lost)
	// Both reduce to: whenever a client connects while metricsWritten=true,
	// the previous logical session has already been flushed, so reset for
	// the new one. (The very first connect on a fresh process with no idle
	// phantom has metricsWritten=false and skips the reset — onSessionStart
	// already did it.) Also cancels any pending idle teardown.
	const origConnect = (session as any).handleClientConnected?.bind(session);
	if (origConnect) {
		(session as any).handleClientConnected = () => {
			cancelIdleTeardown();
			// A dead session's resumption handle must not poison the fresh
			// connect (1008 "Requested entity was not found" staircase — see
			// clearStaleResumptionHandle). Only the CLOSED path reconnects.
			if (session.sessionManager.state === 'CLOSED' && clearStaleResumptionHandle(session)) {
				console.log(`${ts()} [Resume] cleared stale resumption handle from dead session before fresh connect`);
			}
			// P7 D7.1: new client connection = new (pending) ledger epoch; the
			// epoch value is minted on first heartbeat sight (nonce mapping).
			audioHealth.onClientConnected();
			// Z3 fence: a REAL connection invalidates any pending
			// probe/verifier idle-restore — the restore must never tear the
			// upstream down under this client.
			probeIdleRestore.fence();
			if (recorder.wasFlushed) {
				resetSessionGateState();
				recorder.reset();
				recorder.events.push({ event: 'session_started:client_connect', timestamp: new Date().toISOString() });
				// bodhi's onSessionStart won't re-fire (#1372 above), so start the
				// usage ticker here too — otherwise this reconnect session emits no usage.
				recorder.startTicker(VOICE_NATIVE_AUDIO_MODEL);
				console.log(`${ts()} [Session] Client connected after prior flush — reset metrics buffer`);
			}
			writeVoiceState(true);
			origConnect();
			// Step 12: immediate `agent.state` frame on every accepted real
			// connection (after origConnect so `clientAttached` reads true),
			// then repeats ride the upstream-transition subscription. Also
			// publishes the A9 lifecycle attach transition.
			emitAgentState({ immediate: true });
		};
	}

	// Arm the initial teardown — voice-agent boots with no client; if none
	// connects within IDLE_TEARDOWN_MS, close the upstream transport.
	scheduleIdleTeardown();

	// Wire task status → web client
	setTaskStatusCallback((taskId, status, text, result) => {
		try {
			(session as any).clientTransport?.sendJsonToClient?.({
				type: 'task.status', taskId, status, text, result: result || '',
			});
		} catch {}
	});

	// Phone server runs independently (launchd daemon or started by Claude Code session).
	// Voice agent only watches for results and injects them into the conversation.
	mkdirSync(CALL_RESULTS_DIR, { recursive: true });

	// Watch for phone call results and inject into voice conversation
	const callResultFile = join(CALL_RESULTS_DIR, 'latest-result.json');
	setInterval(() => {
		if (!session.clientConnected || !existsSync(callResultFile)) return;
		try {
			const data = JSON.parse(readFileSync(callResultFile, 'utf-8'));
			unlinkSync(callResultFile);
			const transcript = data.transcript ?? 'No transcript available.';
			console.log(`${ts()} [CallResult] Injecting call result into conversation`);
			injectText(session, `[System: The phone call just completed. Tell the user this result naturally.]\n\nCall transcript:\n${transcript}`);
		} catch (err) { console.error(`${ts()} [CallResult] Error:`, err); }
	}, 2000);

	// L2 initialized (Step 12): tools are loaded, the VoiceSession is
	// constructed, and the WS server is about to listen (session.start()
	// binds the WS listener before the LLM transport, per bodhi internals) —
	// set immediately before the start() try, per the readiness model, and
	// publish the lifecycle flip (A9).
	agentInitialized = true;
	emitAgentState();

	// Start session — don't let a transient Gemini failure kill the process.
	// WS server starts *before* the LLM transport (per bodhi internals), so the
	// listener on :PORT is already healthy; only the upstream Gemini connection is broken.
	try {
		await session.start();
		console.log(`${ts()} [Startup] session.start() succeeded`);
	} catch (err) {
		const msg = (err as Error)?.message || String(err);
		console.error(`${ts()} [Startup] session.start() failed: ${msg}`);
		console.error(`${ts()} [Startup] Staying alive — WS server on :${PORT}, will retry LLM transport on next client connect`);
		// The regex below is a LOGGING HINT only (amendment R8) — the
		// protocol classification seam is voice-error-classifier.ts: run the
		// startup failure message through the same classifier as transport
		// closes so a terminal cause (bad key, quota) is persisted for
		// buildAgentState() and reported as upstream:'failed'.
		recordTerminalClassification(classifyTransportClose(undefined, msg));
		if (/credit|quota|billing|auth|401|403/i.test(msg)) {
			console.error(`${ts()} [Startup] Likely cause: Gemini API key invalid or prepayment credits depleted`);
			console.error(`${ts()} [Startup] Fix: top up at https://ai.studio/projects or rotate GEMINI_API_KEY in .env`);
		}
		// Force CLOSED so the health monitor's handleClientConnected path recovers.
		// VoiceSession leaves state at CONNECTING after a failed start() and exposes
		// no public reset API (reconnect()/disconnect() are on the internal transport,
		// not on VoiceSession). CONNECTING→CLOSED is valid per bodhi's state table
		// (index.js:1164). CREATED→CLOSED is also valid. If the state is already
		// CLOSED or ACTIVE for some reason, transitionTo throws — log it so the
		// mismatch is visible (the health monitor only recovers from CLOSED).
		// TODO: drop the hack once bodhi exposes a public session.reset().
		try {
			session.sessionManager.transitionTo('CLOSED');
		} catch (e) {
			console.error(`${ts()} [Startup] Could not transition to CLOSED (state=${session.sessionManager.state}): ${(e as Error)?.message ?? e}`);
		}
		// Belt-and-braces: the transitionTo above already emits via the
		// stateChange subscription; when it throws (state already CLOSED)
		// this still publishes the terminal classification (dedup makes a
		// double call a no-op).
		emitAgentState();
	}

	// Health monitor — runs regardless of whether initial start() succeeded.
	// Serialization: bodhi's handleClientConnected() is synchronous and transitions
	// CLOSED→CONNECTING inline before kicking off the async connect. So the next
	// 30s tick sees state=CONNECTING (not CLOSED) and skips the guard. If the
	// connect fails fast and bodhi flips back to CLOSED, the 60s lastReconnectAt
	// throttle prevents a tight retry loop. (lastReconnectAt is declared with
	// the F5 redial machinery above — the event-driven path shares it.)
	let connectingSince = 0;
	let lastLoggedStatus = '';
	let matrixBaseline: MatrixBaseline | null = null;
	let lastMatrixVerdict = '';
	// Phase-1 shadow: the 'session+egress' ruleset runs in parallel and is
	// telemetry-only — canary 3a later compares live vs shadow on identical
	// input, which is impossible if the shadow never ran.
	let shadowBaseline: MatrixBaseline | null = null;
	let lastShadowVerdict = '';
	let lastFailedWrites = 0;
	setInterval(() => {
		const state = session.sessionManager.state ?? 'unknown';
		const clientConnected = session.clientConnected;
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const serverBufferedAmount = (session as any).clientTransport?.client?.bufferedAmount ?? null;
		// Log only on state changes or non-ACTIVE states — avoid 2,880 lines/day of
		// "state=ACTIVE client=true" during healthy operation. P7 D7.1 inverts
		// the suppression for anomalies: any tick where the ledger latched an
		// anomaly logs the FULL line even mid-ACTIVE-streak (the suppression
		// rule was hiding exactly the dead zones we need to see).
		const status = `state=${state} client=${clientConnected}`;
		// PEEK anomalies; the latches clear only AFTER the anomaly row has
		// been persisted (clearing first would serialize a snapshot with the
		// evidence already gone).
		const anomaly = audioHealth.anomalies(clientConnected);
		const persistFailed = healthPersistence.failedWrites > lastFailedWrites;
		lastFailedWrites = healthPersistence.failedWrites;
		// P7 D7.2: evaluate the localization matrix per tick. lastEgressAt is
		// the Tranche-A model-event proxy (model audio out = the model spoke);
		// the native model hop lands with the bodhi pin.
		const snapshot = audioHealth.getSnapshot(clientConnected);
		const matrix = evaluateMatrix({
			sessionState: state,
			clientConnected,
			snapshot,
			prev: matrixBaseline,
			serverBufferedAmount,
			lastModelEventAt: snapshot.lastModelEventAt,
			now: Date.now(),
		});
		matrixBaseline = matrix.baseline;
		const shadow = evaluateMatrix({
			sessionState: state,
			clientConnected,
			snapshot,
			prev: shadowBaseline,
			serverBufferedAmount,
			lastModelEventAt: snapshot.lastModelEventAt,
			effectiveCoverage: 'session+egress',
			now: Date.now(),
		});
		shadowBaseline = shadow.baseline;
		if (
			shadow.verdict !== lastShadowVerdict &&
			(shadow.verdict !== 'healthy-idle' || lastShadowVerdict)
		) {
			console.log(
				`${ts()} [Matrix-shadow] ${shadow.verdict}${shadow.reasons.length ? ' (' + shadow.reasons.join('; ') + ')' : ''}`,
			);
		}
		lastShadowVerdict = shadow.verdict;
		audioHealth.noteMatrixVerdict(matrix.verdict, matrix.facts, matrix.reasons);
		if (matrix.verdict !== lastMatrixVerdict && (matrix.verdict !== 'healthy-idle' || lastMatrixVerdict)) {
			console.log(`${ts()} [Matrix] ${matrix.verdict}${matrix.reasons.length ? ' (' + matrix.reasons.join('; ') + ')' : ''}`);
		}
		const verdictChanged = matrix.verdict !== lastMatrixVerdict;
		lastMatrixVerdict = matrix.verdict;
		// A verdict TRANSITION is itself evidence worth a row — without this a
		// transient verdict can vanish before the 1-min baseline write.
		if (anomaly.anomalous || (verdictChanged && matrix.verdict !== 'healthy-idle')) {
			audioHealth.persistTick('anomaly', clientConnected);
		}
		// Rate windows advance EVERY tick (a suppressed streak must not turn
		// the next logged line into a long-window average).
		const seg = audioHealth.healthSegments(clientConnected, serverBufferedAmount);
		// Whether the vision throttle engaged was previously unknowable from the
		// log — deferredBudget existed but was never surfaced (investigation F2).
		const eg = getVisionEgressStats();
		const visionSeg = isStreaming()
			? ` vision={sent:${eg.sent},defB:${eg.deferredBudget},defG:${eg.deferredGate},disp:${eg.displaced}}`
			: '';
		if (state !== 'ACTIVE' || status !== lastLoggedStatus || anomaly.anomalous || persistFailed) {
			const why = anomaly.anomalous ? ` anomaly=${anomaly.reasons.join(',')}` : '';
			const pf = persistFailed ? ` persistFail=${healthPersistence.failedWrites}` : '';
			console.log(`${ts()} [Health] ${status}${clientConnected ? ' ' + seg : ''}${visionSeg}${why}${pf}`);
			lastLoggedStatus = status;
		}
		audioHealth.clearTickLatches();
		// P7 D7.1: an inputHealth flip republishes the lifecycle snapshot
		// (emitAgentState publishes only when its key changed — cheap).
		emitAgentState();
		// Clear any stale fatal-backoff once we observe a healthy session —
		// otherwise a brief outage that triggered a backoff would suppress
		// recovery from a later transient close even after the upstream
		// issue was fixed.
		if (state === 'ACTIVE' && voiceFatalBackoffUntil > 0) {
			voiceFatalBackoffUntil = 0;
			voiceRecoveryCoordinator?.handleFatalBackoffCleared();
		}
		// A connect that HANGS never returns to CLOSED, so the recovery guard below
		// — which only fires from CLOSED — can never see it. Observed live: 23min
		// in CONNECTING with a client attached, mic captured, nothing reaching the
		// model. Force CLOSED so the next tick recovers; same transition the
		// startup path already uses, and valid per bodhi's state table.
		// The hang clock keys on STATE, not client attachment: a panel reload
		// mid-hang must not restart the countdown (policy + tests live in
		// voice-connect-watchdog.ts).
		const tick = nextConnectingTick({
			connectingSince, state, clientConnected, now: Date.now(),
			lastReconnectAt, fatalBackoffUntil: voiceFatalBackoffUntil,
		});
		connectingSince = tick.connectingSince;
		if (tick.forceClose) {
			console.error(`${ts()} [Health] Stuck in CONNECTING for `
				+ `${Math.round((Date.now() - connectingSince) / 1000)}s — forcing CLOSED to recover`);
			try {
				session.sessionManager.transitionTo('CLOSED');
				connectingSince = 0;
			} catch (err) {
				// Clock stays armed: the throttles in shouldForceClosed bound retries.
				console.error(`${ts()} [Health] Could not force CLOSED (state=${session.sessionManager.state}):`,
					(err as Error)?.message ?? err);
			}
		}
		// Recover when session is CLOSED and a client is waiting. handleClientConnected
		// is bodhi's internal entry point for this exact scenario (CLOSED + client
		// present → transition to CONNECTING, reconnect fire-and-forget).
		// F5: this is now the SAFETY NET behind the event-driven redial —
		// tickMayDial defers to a pending scheduled dial so the tick cannot
		// preempt the backoff.
		// TODO: drop the (session as any) cast once bodhi exposes a public API.
		if (state === 'CLOSED' && clientConnected && Date.now() - lastReconnectAt > 60_000 && Date.now() > voiceFatalBackoffUntil
			&& tickMayDial({ now: Date.now(), nextDialAt: redialState.nextDialAt })
			&& !(voiceRecoveryCoordinator?.ownsRecovery ?? false)) {
			lastReconnectAt = Date.now();
			redialState = noteDialed(redialState);
			console.log(`${ts()} [Health] Dead session — triggering reconnect`);
			try {
				legacyReconnectInFlight = true;
				(session as any).handleClientConnected();
			} catch (err) {
				console.error(`${ts()} [Health] Reconnect trigger failed:`, (err as Error)?.message ?? err);
			} finally {
				legacyReconnectInFlight = false;
			}
		}
		// ACTIVE-silence shadow observation (Phase 0a): diagnostic only — no
		// effect on the guards above, ever, in this mode.
		voiceWatchdogShadow.observeTick({
			at: Date.now(),
			sessionState: state,
			clientConnected,
			meetingMode: meetingActive,
			snapshot,
			facts: matrix.facts,
		});
		// Armed coordinator (Phase 1): live feed on the MONOTONIC clock — a
		// wall-clock jump must not strand waiting-retry or bypass cooldowns.
		// Snapshot speech timestamps are wall-domain; translate by age.
		if (voiceRecoveryCoordinator) {
			const mono = performance.now();
			const wallAboveFloor = snapshot.speech.lastAboveFloorAt;
			const monoAboveFloor =
				wallAboveFloor === null ? null : mono - Math.max(0, Date.now() - wallAboveFloor);
			voiceRecoveryCoordinator.observeTick({
				at: mono,
				sessionState: state,
				facts: matrix.facts,
				lastAboveFloorAt: monoAboveFloor,
				pendingToolCount: 0,
				delivered: {
					epoch: snapshot.epoch,
					chunksEnded: snapshot.clientTotals.chunksEnded,
					egressFrames: snapshot.egressFrames,
					heartbeatSeen: snapshot.lastHeartbeat !== null,
				},
			});
		}
	}, 30_000);

	// P7 D7.1: periodic ledger persistence — a try-enqueue into the worker's
	// one-slot mailbox (a busy slot skips the sample; §D7.0b). Anomaly rows
	// ride the 30 s tick above; this is the once-a-minute baseline. Gated on
	// an attached client: idle no-client rows would slowly evict the real
	// evidence through the per-session row cap.
	setInterval(() => {
		if (session.clientConnected) audioHealth.persistTick('timer', true);
	}, 60_000);

	// The server bound successfully (EADDRINUSE would have exited via main().catch
	// before here) — record the actual bound endpoint for the runtime descriptor.
	writeVoiceRuntimeState();

	console.log('============================================================');
	console.log('Sutando — Voice Interface');
	console.log('============================================================');
	console.log(`  Voice agent:   ws://localhost:${PORT}`);
	console.log(`  Workspace:     ${WORKSPACE_DIR}`);
	console.log(`  Session ID:    ${SESSION_ID}`);
	console.log(`  Models:`);
	console.log(`    Voice LLM:       ${VOICE_MODEL}`);
	console.log(`    Native audio:    ${VOICE_NATIVE_AUDIO_MODEL} (googleSearch=${VOICE_GOOGLE_SEARCH})`);
	console.log(`    Voice name:      ${VOICE_NAME}`);
	console.log(`    STT:             native Gemini Live inputAudioTranscription`);
	console.log(`    Cartesia TTS:    ${CARTESIA_API_KEY ? 'sonic-3' : 'disabled'}`);
	console.log();
	console.log('Start the web client:');
	console.log('  pnpm tsx ../bodhi_realtime_agent/examples/web-client.ts');
	console.log('Then open http://localhost:8080 and click Connect.');
	console.log();
	console.log('Try saying:');
	console.log("  - 'What's on my schedule today?'");
	console.log("  - 'Research X and summarize it'");
	console.log("  - 'Draft an email to ...'");
	console.log("  - 'Generate an image of ...'");
	console.log("  - 'Goodbye'");
	console.log('============================================================');
}

main().catch((err) => {
	// This is a FATAL path and obeys the SAME crash-only rules as the
	// uncaught handler above (design 1d; amendments R1/R2): markFatalExit()
	// FIRST, so the exit-time lock release skips the (potentially
	// guard-blocked) Python helper and a second fatal while handling this
	// one goes straight to process.exit(1); never `console.error(err)` —
	// object inspection inside error reporting was the 2026-08-04 incident's
	// spin — only static strings + the bounded primitive-only crash record.
	if (markFatalExit()) {
		process.exit(1);
		return;
	}
	// Centralized exit classification (amendment R2): EADDRINUSE reaches
	// exit 7 on BOTH fatal paths — the uncaught handler above AND this one —
	// so the supervisor's exit-7 grace window sees every duplicate-instance
	// race, whichever path the bind error takes.
	const code = classifyFatalExitCode(err);
	if (code === EXIT_CODE_DUPLICATE_INSTANCE) {
		console.error(`\nError: port ${PORT} is already in use.`);
		console.error(`Kill the existing process: kill $(lsof -ti :${PORT})`);
		console.error('Then run pnpm start again.\n');
		process.exit(code);
		return;
	}
	// Static string only — the crash record replaces `'Fatal:', err`.
	console.error(`[FATAL] main() failed — crash-only exit (record: ${CRASH_RECORD_PATH})`);
	writeCrashRecordAndExit(err, CRASH_RECORD_PATH, { exit: (c) => process.exit(c) });
});
