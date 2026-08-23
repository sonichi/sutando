/**
 * Vision pipeline — pipe JPEG frames from a source (screen, webcam) into the
 * Gemini Live voice session.
 *
 * Sources are pluggable via the VisionSource interface; tools accept a
 * `source` argument. Modes: one-shot (send_vision_frame) and continuous
 * streaming (start_vision / stop_vision).
 *
 * Wire-up: voice-agent calls setVisionSession(session) once the VoiceSession
 * is constructed (and setVisionSession(null) on close). Tool definitions are
 * exported and registered via inline-tools.ts so they appear in both the
 * voice and phone agents.
 *
 * Frame path: source.capture() → JPEG bytes → base64 →
 * (session as any).transport.sendFile(b64, 'image/jpeg'). Gemini Live's
 * realtime_input.video slot accepts single-frame images.
 */

import { readFileSync, writeFileSync, unlinkSync, mkdtempSync, mkdirSync, openSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { readCaptureToken } from './util_paths.js';
import { findRepoRoot } from './sutando_config.js';
import { readBodyCapped } from './http-body-limit.js';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer, type Server } from 'node:http';
import { connect } from 'node:net';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveWorkspace, statusPath } from './workspace_default.js';

const execFileAsync = promisify(execFile);
// UTC, matching voice-agent's ts() — these logs interleave in the same file,
// and a local-time subsystem next to UTC subsystems made incident timelines
// off-by-timezone (field report 2026-08-14: 09:48:04 [Vision] and 16:48:04
// [VoiceSession] were the same instant).
const ts = () => new Date().toISOString().slice(11, 23);

const DEFAULT_FPS = 1;
// Push-path floor for the documented 1 fps cap: MAX_FPS below bounds only the
// pull ticker (#3089 deferred this gate to #3090, which never landed it).
export const VISION_MIN_SEND_INTERVAL_MS = 1000;
// https://ai.google.dev/gemini-api/docs/live-api — video is sampled at 1 fps.
// Cite it: the repo states this rate nowhere else, so an uncited literal here
// would be unfalsifiable for the next reader.
export const MAX_FPS = 1.0;
// Floor is deliberately below any shipping default: sub-0.5 rates exist so the
// cost/cadence experiments can run, not because they are a good user default.
export const MIN_FPS = 0.1;
const MIN_INTERVAL_MS = 250;
// TODO(roadmap §5 Now: cost posture): A 720p JPEG q=0.6 ≈ 80–150KB. At 1 fps
// continuous that's ~6–9MB/min into Gemini Live's video slot, plus context-
// window growth from frame turns. Default off and tools never auto-start are
// the cheap guard — the open work is a quota-aware throttle (drop fps on
// rate-limit hints) and a brief doc explaining the per-minute cost
// envelope. See vision and roadmap.md §5 Now.

// --- Source abstraction ---------------------------------------------------

export interface VisionFrame {
	data: Buffer;
	mimeType: string;
}

export interface VisionSource {
	readonly name: string;
	capture(): Promise<VisionFrame>;
}

// --- screen-capture-server (:7845) lazy start ----------------------------
// The screen source grabs frames from the screen-capture-server on :7845. That
// server is launched by startup.sh (the OSS menu-bar path) — but NOT by
// start-cli.sh, which is how the bundled desktop app (ag2-space/ag2space-cinny-
// desktop) launches the core. So in the bundled app the Watch toggle hit a dead
// :7845 (connection refused) and silently did nothing, even with Screen Recording
// granted. Rather than depend on every launch path remembering to start :7845,
// the vision pipeline starts it ON DEMAND here: self-healing, works identically
// in the OSS app and the bundled Tauri app. (First-run macOS Screen Recording
// prompting via CGRequestScreenCaptureAccess is a documented follow-up — this
// change unblocks the already-granted case and stops the silent failure.)
const SCREEN_CAPTURE_PORT = 7845;
let _screenCaptureStarting: Promise<void> | null = null;

function _portListening(port: number): Promise<boolean> {
	return new Promise((resolve) => {
		const sock = connect({ host: '127.0.0.1', port });
		const done = (up: boolean) => {
			sock.destroy();
			resolve(up);
		};
		sock.once('connect', () => done(true));
		sock.once('error', () => done(false));
		sock.setTimeout(600, () => done(false));
	});
}

/**
 * Where screen-capture-server.py actually lives, as candidate paths in
 * probe order. The old single answer — "next to this module" — was only true
 * in dev (`tsx src/voice-agent.ts`): the bundled app runs
 * `dist/voice-agent.js`, and build-bundle.mjs ships no .py files, so the
 * module-relative path resolved to a nonexistent dist/screen-capture-server.py
 * and every Watch attempt died as a silent 8s port timeout (field report
 * 2026-08-14). The bundle DOES ship src/*.py as a sibling of dist/, so the
 * canonical answer is `<sutando root>/src/…` via findRepoRoot (the same
 * helper python-binary.ts uses), with the module-sibling kept as the dev/
 * exotic-layout fallback. Exported for tests.
 */
export function _captureServerScriptCandidates(moduleDir: string): string[] {
	const candidates: string[] = [];
	const root = findRepoRoot(moduleDir);
	if (root) candidates.push(join(root, 'src', 'screen-capture-server.py'));
	const sibling = join(moduleDir, 'screen-capture-server.py');
	if (!candidates.includes(sibling)) candidates.push(sibling);
	return candidates;
}

/** Ensure the screen-capture-server is up on :7845, spawning it if absent.
 *  Reuses a running server (startup.sh's, the supervisor's, or a prior lazy
 *  spawn); memoizes the in-flight spawn so concurrent captures don't
 *  double-start it. */
export async function ensureScreenCaptureServer(): Promise<void> {
	if (await _portListening(SCREEN_CAPTURE_PORT)) return; // reuse a running server
	if (_screenCaptureStarting) return _screenCaptureStarting; // join an in-flight spawn
	const start = (async () => {
		const moduleDir = dirname(fileURLToPath(import.meta.url));
		const candidates = _captureServerScriptCandidates(moduleDir);
		const script = candidates.find((c) => existsSync(c));
		if (!script) {
			// Fail FAST and say why — the old path burned 8s per attempt on a
			// port that could never open, with the real cause discarded.
			throw new Error(`screen-capture-server.py not found (tried: ${candidates.join(', ')})`);
		}
		// The bundled .app's voice-agent runs under a MINIMAL launchd PATH (no
		// /opt/homebrew/bin etc. — same class as the claude-on-PATH gotcha, desktop
		// PR #50). A bare `python3` would ENOENT there — Watch would still silently
		// fail, one layer deeper. And screen-capture-server.py itself shells out to
		// `screencapture` (/usr/sbin). Prepend the standard macOS bin dirs so BOTH
		// the interpreter and the server's own subprocess resolve regardless of how
		// the app launched the runtime. (Review: air, bundled-spawn-PATH risk.)
		const augmentedPath = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/usr/sbin', '/bin', process.env.PATH]
			.filter(Boolean)
			.join(':');
		// Observability + process shape (field report 2026-08-14): the old
		// spawn was detached with stdio 'ignore' and no handlers, so a child
		// that died instantly (missing script, python error, bind failure) was
		// indistinguishable from a slow boot. Now: NOT detached — the server
		// stays a non-disclaimed child of the voice agent, the same shape the
		// supervisor deliberately uses for screen-capture so TCC attributes the
		// Screen Recording grant to the app lineage — stderr/stdout go to a log
		// file, the pid is logged, and an early exit fails the wait immediately
		// with the code + log path instead of a ghost timeout. unref() still
		// keeps the child from holding the event loop open.
		let logPath = '/dev/null';
		let logfd: number | undefined;
		try {
			const logDir = join(resolveWorkspace(), 'logs');
			mkdirSync(logDir, { recursive: true });
			logPath = join(logDir, 'screen-capture-server.lazy.log');
			logfd = openSync(logPath, 'a');
		} catch {
			logfd = undefined; // fall back to ignore — logging must not block the spawn
		}
		let exited: { code: number | null; signal: string | null } | null = null;
		let spawnError: Error | null = null;
		const child = spawn('python3', [script], {
			detached: false,
			stdio: logfd !== undefined ? ['ignore', logfd, logfd] : 'ignore',
			env: { ...process.env, PATH: augmentedPath },
		});
		child.unref();
		child.on('error', (err) => {
			spawnError = err;
		});
		child.on('exit', (code, signal) => {
			exited = { code, signal };
		});
		console.log(`${ts()} [Vision] spawned screen-capture-server pid=${child.pid} script=${script} log=${logPath}`);
		for (let i = 0; i < 40; i++) {
			if (await _portListening(SCREEN_CAPTURE_PORT)) return;
			if (spawnError) {
				throw new Error(`screen-capture-server spawn failed: ${(spawnError as Error).message}`);
			}
			if (exited) {
				const e = exited as { code: number | null; signal: string | null };
				throw new Error(
					`screen-capture-server exited before listening (code=${e.code} signal=${e.signal}) — see ${logPath}`,
				);
			}
			await new Promise((r) => setTimeout(r, 200));
		}
		throw new Error(`screen-capture-server did not come up on :7845 within 8s — see ${logPath}`);
	})();
	_screenCaptureStarting = start;
	try {
		await start;
	} finally {
		// Clear on BOTH success and failure: _screenCaptureStarting exists only to
		// dedupe a CONCURRENT spawn, not to cache a completed one. Memoizing the
		// resolved promise would make a later call short-circuit past the spawn even
		// after the detached server has died/crashed — the port check above would
		// see :7845 down but this guard would return the stale "up" promise, leaving
		// Watch connection-refused until the whole voice-agent restarts (review P1 on
		// 0589a18). Clearing here means the next capture re-checks the port and
		// re-spawns if the server is gone — true self-healing.
		_screenCaptureStarting = null;
	}
}

/**
 * Which `screencapture -D<n>` display this session watches. Chosen once and held
 * for the session: a stream that follows the frontmost screen would silently
 * change what the user is being watched on mid-conversation.
 */
let _sessionDisplay: number | null = null;

export function getSessionDisplay(): number | null {
	return _sessionDisplay;
}

/** Exported for tests; the session choice is otherwise set only via start_vision. */
export function setSessionDisplay(display: number | null): void {
	_sessionDisplay = display;
}

export type DisplayInfo = { index: number; width: number; height: number; name?: string; is_main?: boolean };

/** Ask the capture server which displays are attached. Empty list on any failure. */
export async function listDisplays(): Promise<DisplayInfo[]> {
	await ensureScreenCaptureServer();
	const tok = readCaptureToken();
	const res = await fetch('http://localhost:7845/displays', tok ? { headers: { 'X-Sutando-Capture-Token': tok } } : {});
	const data = (await res.json()) as { status?: string; displays?: DisplayInfo[] };
	return data.status === 'ok' && Array.isArray(data.displays) ? data.displays : [];
}

/** "2: U28E510 3840x2160" — what the model reads back to the user. */
export function describeDisplay(d: DisplayInfo): string {
	const label = d.name ? `${d.name}` : `Display ${d.index}`;
	return `${d.index}: ${label}${d.is_main ? ' (main)' : ''} ${d.width}x${d.height}`;
}

export type DisplayGate =
	| { kind: 'use'; display: number | null }
	| { kind: 'ask'; displays: DisplayInfo[] };

/**
 * Decide whether the user still has a display choice to make.
 *
 * Asking is only worth a turn when the answer can differ, so a single display
 * (or an enumeration that failed, giving an empty list) resolves silently
 * rather than blocking the stream on a question with one answer.
 */
export function decideDisplayGate(
	sessionDisplay: number | null,
	requested: number | undefined,
	displays: DisplayInfo[],
): DisplayGate {
	if (requested !== undefined) return { kind: 'use', display: requested };
	if (sessionDisplay !== null) return { kind: 'use', display: sessionDisplay };
	if (displays.length > 1) return { kind: 'ask', displays };
	return { kind: 'use', display: displays.length === 1 ? displays[0].index : null };
}

const screenSource: VisionSource = {
	name: 'screen',
	async capture() {
		// Self-healing: bring up the screen-capture-server if the current launch
		// path (e.g. the bundled desktop app's start-cli.sh) didn't start it.
		await ensureScreenCaptureServer();
		// silent=true skips the menu-bar flash + macOS notification, which would
		// otherwise fire on every frame during a stream. format=jpeg keeps
		// frames small; maxdim/quality make the CAPTURE SERVER resize in its
		// own process (P7 D7.4: compression never competes with this event
		// loop) to the ~720p/q0.6 budget before the file comes back.
		const _capTok = readCaptureToken();
		// Without this the server captures its default display, so on a multi-display
		// Mac the stream shows a screen the user did not pick.
		const _display = _sessionDisplay !== null ? `&display=${_sessionDisplay}` : '';
		const res = await fetch(
			`http://localhost:7845/capture?format=jpeg&silent=true&maxdim=${VISION_FRAME_MAX_DIM}&quality=${VISION_FRAME_JPEG_QUALITY}${_display}`,
			_capTok ? { headers: { 'X-Sutando-Capture-Token': _capTok } } : {},
		);
		const data = (await res.json()) as { status: string; path?: string; error?: string };
		if (data.status !== 'ok' || !data.path) {
			throw new Error(`screen-capture-server: ${data.error || 'no path'}`);
		}
		const buf = readFileSync(data.path);
		// Drop the on-disk copy — we've already uploaded the bytes.
		try {
			unlinkSync(data.path);
		} catch (err) {
			console.warn(`${ts()} [Vision] failed to unlink ${data.path}: ${(err as Error)?.message ?? err}`);
		}
		return { data: buf, mimeType: 'image/jpeg' };
	},
};

const webcamSource: VisionSource = (() => {
	// One scratch dir for the whole process — imagesnap is happiest writing to
	// a real path it can stat, and reusing the path keeps tmpdir clean.
	const dir = mkdtempSync(join(tmpdir(), 'sutando-webcam-'));
	const path = join(dir, 'frame.jpg');
	return {
		name: 'webcam',
		async capture() {
			// `-w 0` skips the default 0.5s warmup. The first frame after a
			// long pause may still look dim while the auto-exposure settles —
			// callers driving a stream should expect frame #1 to be lower
			// quality than steady-state. JPEG, default resolution.
			await execFileAsync('imagesnap', ['-q', '-w', '0', path], { timeout: 8_000 });
			const buf = readFileSync(path);
			return { data: buf, mimeType: 'image/jpeg' };
		},
	};
})();

const sources: Record<string, VisionSource> = {
	screen: screenSource,
	webcam: webcamSource,
};

/** Register an additional vision source at runtime.
 *
 * Lets external integrations (AI glasses webhooks, Telegram photo bridges,
 * external camera daemons, etc.) plug into the same start_vision /
 * send_vision_frame pipeline without modifying this file. Example:
 *
 *   import { registerSource } from './vision-tools.js';
 *   registerSource({
 *     name: 'glasses',
 *     async capture() { return { data: await fetchLatestGlassesFrame(), mimeType: 'image/jpeg' }; },
 *   });
 *
 * Names are case-insensitive. Re-registering a name overwrites the prior source. */
export function registerSource(source: VisionSource): void {
	sources[source.name.toLowerCase()] = source;
}

/** Names of all currently registered sources, for tool descriptions / diagnostics. */
export function listSources(): string[] {
	return Object.keys(sources);
}

function resolveSource(name?: string): VisionSource {
	const key = (name ?? 'screen').toLowerCase();
	const src = sources[key];
	if (!src) throw new Error(`Unknown vision source "${name}". Known: ${Object.keys(sources).join(', ')}.`);
	return src;
}

// --- Session wiring -------------------------------------------------------

interface MinimalSession {
	transport?: {
		sendFile?: (base64: string, mimeType: string) => void;
		// turnComplete is IGNORED by the transport — it always sends realtime
		// input, so the model may answer this injection aloud.
		sendContent?: (turns: Array<{ role: 'user' | 'assistant'; text: string }>, turnComplete?: boolean) => void;
		isConnected?: boolean;
	};
}

let sessionRef: MinimalSession | null = null;

// --- Vision-on contributor registry ---------------------------------------
//
// When push mode starts, we inject a hidden system note that tells the model
// what's happening. The BASE note is core's concern (frames flowing, brief
// acknowledgement, default screen-aware behavior). Anything skill-specific —
// e.g. "screen-companion mode is available with guided-setup" — must NOT
// live in this file (per CLAUDE.md: core services contain no feature-specific
// logic; skills are optional). Instead, skills register a contributor at
// module-load time; the injection concatenates the base note + each
// contributor's text.
//
// If no skills register, the injected note is generic and never mentions
// modes that don't exist on this install.

export type VisionOnContributor = () => string | null | undefined;
const visionOnContributors: VisionOnContributor[] = [];

/** Register a contributor whose output is appended to the screen-share-started
 *  system note. Called by skills at module-load time. Returns an unregister
 *  function (useful for tests). */
export function registerVisionOnContributor(fn: VisionOnContributor): () => void {
	visionOnContributors.push(fn);
	return () => {
		const i = visionOnContributors.indexOf(fn);
		if (i >= 0) visionOnContributors.splice(i, 1);
	};
}

/** Visible for tests. */
export function _getVisionOnContributorCount(): number {
	return visionOnContributors.length;
}

// --- Per-frame post-send hook registry ----------------------
//
// Skills may register a hook that fires after each push-mode frame is sent.
// Core provides no AX or Chrome selection logic — that belongs in skills.
// The hook receives a `sendUserCtx(text)` helper that injects a hidden user
// turn so the model sees context without triggering audio output. Hooks run
// synchronously on the tick path so they must be fast (probe with short
// timeouts or compare against cached state before calling AX).

export type VisionFramePostSendHook = (sendUserCtx: (text: string) => void) => void;
const visionFrameHooks: VisionFramePostSendHook[] = [];

/** Register a hook called after every push-mode frame is sent. Returns an
 *  unregister function. Called by skills at module-load time. */
export function registerVisionFrameHook(fn: VisionFramePostSendHook): () => void {
	visionFrameHooks.push(fn);
	return () => {
		const i = visionFrameHooks.indexOf(fn);
		if (i >= 0) visionFrameHooks.splice(i, 1);
	};
}

/** Visible for tests. */
export function _getVisionFrameHookCount(): number {
	return visionFrameHooks.length;
}

// TODO(roadmap §5 Now: "Define DeviceSession"): Replace this single-session
// global with a DeviceSession map keyed by device ID. Today push-mode senders
// (browser, Mentra glasses, Discord/Telegram photo helper, phone agent) all
// race for one slot — last-set wins, and the phone-agent fix in
// skills/phone-conversation/scripts/conversation-server.ts uses a fragile
// swap-and-restore. Once DeviceSession exists, frames should carry a target
// device ID and fan out only to that session.
export function setVisionSession(session: unknown): void {
	sessionRef = session as MinimalSession | null;
	if (!session) stopStream();
}

// --- Tool-surface updater registry ----------------------------------------
//
// Lets skills call session.updateTools() without importing voice-agent.ts.
// voice-agent registers the updater + full tool list after session creation,
// clears it on shutdown. Skills call callUpdateTools/callRestoreTools to
// enforce or lift a tools_allow constraint.

type ToolUpdateFn = (tools: ToolDefinition[]) => void;
let toolUpdaterFn: ToolUpdateFn | null = null;
let fullToolSurface: ToolDefinition[] = [];

/** Called by voice-agent after VoiceSession is constructed (and with null on shutdown). */
export function setSessionToolUpdater(fn: ToolUpdateFn | null, fullTools: ToolDefinition[]): void {
	toolUpdaterFn = fn;
	fullToolSurface = fn ? fullTools : [];
}

/** Replace the live session's tool surface. Returns true if the updater is registered. */
export function callUpdateTools(tools: ToolDefinition[]): boolean {
	if (!toolUpdaterFn) return false;
	toolUpdaterFn(tools);
	return true;
}

/** Restore the session's tool surface to the full set registered at startup. */
export function callRestoreTools(): boolean {
	if (!toolUpdaterFn || fullToolSurface.length === 0) return false;
	toolUpdaterFn(fullToolSurface);
	return true;
}

/**
 * The full tool surface registered at startup (e.g. voice-agent's
 * `mainAgentTools` = workTool + switchModeTool + … + inlineTools). Empty until
 * setSessionToolUpdater() runs. Skills that need to RESTRICT the surface should
 * filter THIS, not `inlineTools` alone — otherwise the non-inline mainAgentTools
 * (work, switch_mode, …) are silently dropped and become uncallable in-mode.
 */
export function getFullToolSurface(): ToolDefinition[] {
	return fullToolSurface;
}

function getSendFile(): ((b64: string, mime: string) => void) | null {
	const t = sessionRef?.transport;
	if (!t || !t.sendFile) return null;
	// isConnected is optional — if exposed and false, skip; otherwise trust the call.
	if (t.isConnected === false) return null;
	return t.sendFile.bind(t);
}

// --- Streaming controller -------------------------------------------------

let ticker: NodeJS.Timeout | null = null;
/** Bumped on every stream start/stop: an in-flight pull capture from a
 *  stopped stream must not send its stale frame after stopStream() (P7
 *  round-3 #8 — stop semantics beat a slow source.capture()). */
let streamGen = 0;
let activeSource: VisionSource | null = null;
let inFlight = false;
let frameCount = 0;
// Consecutive-failure backoff (2026-06-10): when screencapture keeps failing
// (permission lost, display asleep, source gone) tick() used to log a frame
// error EVERY interval — 1-2/s, hundreds of lines (observed 23:08 tonight) —
// while uselessly re-shelling the broken command. Count consecutive failures:
// log only the 1st + a periodic heartbeat, and auto-stop the stream after the
// threshold so a dead source self-cleans instead of spamming forever.
let consecutiveFrameErrors = 0;
// Note: parse THEN validate — `Number(env || 30)` would coerce the string
// first, so SUTANDO_VISION_FAIL_STOP=0 → threshold 0 (stop on first transient
// failure) and ="abc" → NaN (never stop). Guard with isFinite + >0. (Maddy
// review of 4b7f967c, 2026-06-10.)
const VISION_FAIL_STOP_THRESHOLD = (() => {
	const t = Number(process.env.SUTANDO_VISION_FAIL_STOP);
	return Number.isFinite(t) && t > 0 ? t : 30;
})();
let startedAt = 0;
// Push mode: the web-client owns capture (via getDisplayMedia, so the user
// gets the native "Chrome Tab / Window / Entire Screen" picker) and POSTs
// each JPEG frame to /vision/frame. The controller doesn't tick — it just
// forwards frames to the live session.
let pushMode = false;
let pushSourceName: string | null = null;

export function isStreaming(): boolean {
	return ticker !== null || pushMode;
}

// --- P7 D7.4 vision egress controls ---------------------------------------
// The FE-1 controls: EVERY frame — pull tick, browser push, external
// sources — passes one central gate + token bucket before sendFile. Deferral
// is a latest-frame-only slot, never a backlog: a queued burst draining after
// speech would re-create FE-1 under a different name.

/** Token bucket for vision egress on the shared upstream socket. The burst
 *  ceiling is 2 s of budget; a 720p/q0.6 frame is ~80–150 KB. */
export const VISION_BUCKET_BYTES_PER_SEC = 300 * 1024;
export const VISION_BUCKET_MAX_BYTES = 600 * 1024;
const VISION_DRAIN_INTERVAL_MS = 250;
/** Downscale budget requested from the capture server (which resizes in ITS
 *  process — compression never competes with this event loop). ~720p-class. */
export const VISION_FRAME_MAX_DIM = 1280;
export const VISION_FRAME_JPEG_QUALITY = 60;

let speechEvidenceFn: (() => { active: boolean }) | null = null;
/** Voice-agent injects the engine ledger's getSpeechEvidence — the CANONICAL
 *  speech signal (D7.1) — so vision defers while the user is speaking. */
export function setVisionSpeechEvidence(fn: (() => { active: boolean }) | null): void {
	speechEvidenceFn = fn;
}

/** Why the last stop happened. 'no-client' is TERMINAL: the browser must tear
 *  its push session down rather than treat the stop as a server-side glitch. */
let lastStopReason: string | null = null;

let bucketBytes = VISION_BUCKET_MAX_BYTES;
let bucketRefillAt = Date.now();
let lastFrameSentAt = 0;
let deferredSlot: { data: Buffer; mimeType: string; fireHooks: boolean } | null = null;
let drainTimer: NodeJS.Timeout | null = null;
const egressStats = { sent: 0, deferredGate: 0, deferredBudget: 0, displaced: 0, droppedOversize: 0 };

/** Read-only egress diagnostics (drop counters are cumulative per process). */
export function getVisionEgressStats(): {
	sent: number;
	deferredGate: number;
	deferredBudget: number;
	displaced: number;
	droppedOversize: number;
	slotOccupied: boolean;
} {
	return { ...egressStats, slotOccupied: deferredSlot !== null };
}

/** TEST-ONLY: reset the egress gate/bucket/slot state between test cases
 *  (module state is process-wide; production never calls this). */
export function resetVisionEgressForTests(): void {
	deferredSlot = null;
	stopDrainTimer();
	bucketBytes = VISION_BUCKET_MAX_BYTES;
	bucketRefillAt = Date.now();
	lastFrameSentAt = 0;
	egressStats.sent = 0;
	egressStats.deferredGate = 0;
	egressStats.deferredBudget = 0;
	egressStats.displaced = 0;
	egressStats.droppedOversize = 0;
}

function refillBucket(now: number): void {
	const elapsed = Math.max(0, now - bucketRefillAt);
	bucketRefillAt = now;
	bucketBytes = Math.min(
		VISION_BUCKET_MAX_BYTES,
		bucketBytes + (elapsed / 1000) * VISION_BUCKET_BYTES_PER_SEC,
	);
}

/**
 * Tranche-A gate: ACTIVE ∧ ¬speechActive. Bodhi's buffering/replaying flags
 * are not observable pre-pin — that blind spot is NAMED (D7.4): until the
 * bodhi tranche, vision can still slip into the window where buffered speech
 * awaits reconnect replay.
 */
function visionGate(): { open: boolean; reason: string } {
	// Same trust contract as getSendFile's isConnected: if the session
	// exposes a state and it is not ACTIVE, gate; a session object without a
	// sessionManager (integration fakes/bridges) is trusted.
	const sm = (sessionRef as unknown as { sessionManager?: { state?: string } } | null)
		?.sessionManager;
	if (sm && sm.state !== 'ACTIVE') return { open: false, reason: `session=${sm.state ?? '?'}` };
	if (speechEvidenceFn) {
		try {
			if (speechEvidenceFn().active) return { open: false, reason: 'speech-active' };
		} catch {
			/* evidence source failure never blocks vision */
		}
	}
	return { open: true, reason: '' };
}

function stopDrainTimer(): void {
	if (drainTimer) {
		clearInterval(drainTimer);
		drainTimer = null;
	}
}

function parkFrame(
	data: Buffer,
	mimeType: string,
	fireHooks: boolean,
	why: 'gate' | 'budget',
	fromDrain: boolean,
): void {
	if (!fromDrain) {
		// A drain retry re-parks the SAME frame — that is neither a new
		// deferral nor a displacement.
		if (deferredSlot) egressStats.displaced++; // the old frame is dropped forever
		if (why === 'gate') egressStats.deferredGate++;
		else egressStats.deferredBudget++;
	}
	deferredSlot = { data, mimeType, fireHooks };
	if (!drainTimer) {
		drainTimer = setInterval(drainDeferredFrame, VISION_DRAIN_INTERVAL_MS);
		drainTimer.unref?.();
	}
}

function drainDeferredFrame(): void {
	if (!deferredSlot) {
		stopDrainTimer();
		return;
	}
	const slot = deferredSlot;
	// Take the frame OUT before retrying — a re-defer re-parks it without a
	// self-displacement, and a fresh frame arriving mid-retry wins the slot.
	deferredSlot = null;
	const r = sendFrameGated(slot.data, slot.mimeType, slot.fireHooks, true);
	if (!r.ok) {
		// Session gone — a stale frame must not survive to the next session.
		stopDrainTimer();
		return;
	}
	if (!r.deferred && !deferredSlot) stopDrainTimer();
}

/** The ONE vision egress path (gate → bucket → send). All sources call this. */
function sendFrameGated(
	data: Buffer,
	mimeType: string,
	fireHooks: boolean,
	fromDrain = false,
): { ok: boolean; deferred?: boolean; reason?: string; error?: string } {
	const sendFile = getSendFile();
	if (!sendFile) return { ok: false, error: 'no active voice session' };
	const now = Date.now();
	refillBucket(now);
	// A frame larger than the bucket burst can never drain; reject it instead.
	const wireBytes = Math.ceil((data.byteLength * 4) / 3);
	if (wireBytes > VISION_BUCKET_MAX_BYTES) {
		egressStats.droppedOversize++;
		return {
			ok: false,
			reason: 'frame-too-large',
			error: `frame exceeds the ${VISION_BUCKET_MAX_BYTES}-byte vision egress budget`,
		};
	}
	const gate = visionGate();
	if (!gate.open) {
		parkFrame(data, mimeType, fireHooks, 'gate', fromDrain);
		return { ok: true, deferred: true, reason: gate.reason };
	}
	// The wire carries base64, so the bucket charges encoded bytes rather than raw.
	if (now - lastFrameSentAt < VISION_MIN_SEND_INTERVAL_MS || wireBytes > bucketBytes) {
		parkFrame(data, mimeType, fireHooks, 'budget', fromDrain);
		return { ok: true, deferred: true, reason: 'budget' };
	}
	// The residual voice-loop cost — base64 + the SDK's synchronous
	// JSON.stringify (~1-3 ms per 720p frame at ≤1 fps) — is named in D7.4,
	// bounded by the capture-server downscale, and covered by the budget test.
	try {
		sendFile(data.toString('base64'), mimeType);
	} catch (err) {
		return { ok: false, error: (err as Error)?.message ?? 'sendFile threw' };
	}
	bucketBytes = Math.max(0, bucketBytes - wireBytes);
	lastFrameSentAt = now;
	egressStats.sent++;
	frameCount++;
	if (frameCount === 1 || frameCount % 10 === 0) {
		console.log(`${ts()} [Vision] sent frame #${frameCount} (${Math.round(data.byteLength / 1024)}KB ${mimeType})`);
	}
	if (fireHooks && visionFrameHooks.length > 0) {
		const transport = sessionRef?.transport;
		if (transport && typeof transport.sendContent === 'function') {
			const sendUserCtx = (text: string): void => {
				try {
					transport.sendContent!([{ role: 'user', text }], false);
				} catch {}
			};
			for (const hook of visionFrameHooks) {
				try {
					hook(sendUserCtx);
				} catch (err) {
					console.warn(`${ts()} [Vision] frame hook threw: ${(err as Error)?.message}`);
				}
			}
		}
	}
	return { ok: true };
}

export interface VisionState {
	streaming: boolean;
	source: string | null;
	fps: number;
	frames: number;
	durationMs: number;
	sessionReady: boolean;
	/** Why streaming last stopped, when it was stopped deliberately. 'no-client'
	 *  is terminal — a push driver seeing it must tear down, not re-arm. */
	stoppedReason: string | null;
	/** P7 D7.4 egress diagnostics: real sends vs gate/budget deferrals and
	 *  displaced (dropped) slot frames. */
	egress: {
		sent: number;
		deferredGate: number;
		deferredBudget: number;
		displaced: number;
		slotOccupied: boolean;
	};
}

/** Public read-only view of vision streaming state.
 *
 * Used by the web-client toggle button to reflect "currently streaming /
 * idle" regardless of whether the stream was started by voice or button. */
export function getVisionState(): VisionState {
	const streaming = ticker !== null || pushMode;
	return {
		streaming,
		source: pushMode ? pushSourceName : (activeSource?.name ?? null),
		fps: streaming ? Math.round((frameCount * 1000) / Math.max(1, Date.now() - startedAt)) : 0,
		frames: frameCount,
		durationMs: streaming && startedAt ? Date.now() - startedAt : 0,
		sessionReady: getSendFile() !== null,
		stoppedReason: streaming ? null : lastStopReason,
		egress: getVisionEgressStats(),
	};
}

/** Programmatic start (used by the HTTP control server / button).
 *
 *  Two modes:
 *    - **pull** (default): the controller ticks at `fps` Hz and calls the
 *      registered source's `capture()`. Source must exist in the sources map
 *      (built-in `screen`/`webcam`, or registered via `registerSource`).
 *    - **push**: the caller owns capture and POSTs frames to /vision/frame.
 *      Source is a free-form label ('browser', 'glasses', 'mentra-camera').
 *      Use this for browser getDisplayMedia, Mentra glasses, AI-glasses
 *      webhooks, or anything that produces frames out-of-band.
 *  `browser` is an alias for `mode: 'push'` (back-compat).
 *
 *  Returns the same shape as the start_vision tool. */
export function startStreaming(
	sourceName: string | undefined,
	fps: number | undefined,
	mode?: 'pull' | 'push',
):
	| { status: 'streaming'; source: string; fps: number; intervalMs: number; mode: 'pull' | 'push' }
	| { status: 'failed'; error: string } {
	if (!getSendFile()) {
		return { status: 'failed', error: 'No active voice session — vision streaming requires a connected session.' };
	}
	try {
		const lower = (sourceName ?? 'screen').toLowerCase();
		const effectiveMode: 'pull' | 'push' = mode ?? (lower === 'browser' ? 'push' : 'pull');
		if (effectiveMode === 'push') {
			// Push mode — caller (web-client, Mentra bridge, glasses webhook,
			// etc.) captures frames and POSTs them to /vision/frame. No ticker.
			stopStream();
			lastStopReason = null; // a new push session supersedes any terminal stop
			pushMode = true;
			pushSourceName = lower;
			frameCount = 0;
			startedAt = Date.now();
			console.log(`${ts()} [Vision] started ${lower} (push mode)`);
			// Tell the model push just started so it can briefly acknowledge
			// on its next turn. The BASE note is generic — anything skill-
			// specific (mode catalogs, etc.) comes from contributors that
			// skills register at module-load time via
			// registerVisionOnContributor. If no skills register, the model
			// gets just the base note and operates in default screen-aware
			// mode. Symmetric to the stop-side cache-clear in stopStream().
			const transport = sessionRef?.transport;
			if (transport && typeof transport.sendContent === 'function') {
				try {
					const baseNote =
						`[system note] User just started sharing their screen via the Watch button (source='${lower}'). Frames are now flowing live. On your next turn, briefly acknowledge that you can see their shared screen and ask what they're trying to do. Keep it to one sentence. Do not describe the screen in detail unless the user asks.`;
					const contributions = visionOnContributors
						.map(fn => {
							try { return fn(); } catch (e) {
								console.warn(`${ts()} [Vision] contributor threw: ${(e as Error).message}`);
								return null;
							}
						})
						.filter((s): s is string => typeof s === 'string' && s.length > 0);
					const fullText = contributions.length > 0
						? `${baseNote}\n\n${contributions.join('\n\n')}`
						: baseNote;
					transport.sendContent([{ role: 'user', text: fullText }], false);
					console.log(`${ts()} [Vision] injected screen-share-started context hint (${contributions.length} contributor(s))`);
				} catch (err) {
					console.warn(`${ts()} [Vision] failed to inject screen-share-started hint: ${(err as Error).message}`);
				}
			}
			return { status: 'streaming', source: lower, fps: 0, intervalMs: 0, mode: 'push' };
		}
		const source = resolveSource(sourceName);
		const info = startStream(source, fps ?? DEFAULT_FPS);
		return { status: 'streaming', source: source.name, fps: info.fps, intervalMs: info.intervalMs, mode: 'pull' };
	} catch (err) {
		console.error(`${ts()} [Vision] startStreaming threw: ${(err as Error)?.message ?? err}`);
		return { status: 'failed', error: 'startStreaming failed' };
	}
}

/** Programmatic stop (used by the HTTP control server / button). */
export function stopStreaming(reason?: string): { status: 'stopped' | 'idle'; source: string | null; frames: number; durationMs: number } {
	const r = stopStream();
	if (r.wasRunning) lastStopReason = reason ?? null;
	return { status: r.wasRunning ? 'stopped' : 'idle', source: r.source, frames: r.frames, durationMs: r.durationMs };
}

function stopStream(): { wasRunning: boolean; frames: number; durationMs: number; source: string | null } {
	const wasRunning = ticker !== null || pushMode;
	const sourceName = pushMode ? pushSourceName : (activeSource?.name ?? null);
	const wasPush = pushMode;
	if (ticker) {
		clearInterval(ticker);
		ticker = null;
	}
	pushMode = false;
	pushSourceName = null;
	const frames = frameCount;
	const durationMs = startedAt ? Date.now() - startedAt : 0;
	if (wasRunning) {
		console.log(`${ts()} [Vision] stopped ${sourceName}${wasPush ? ' (push)' : ''} — ${frames} frame(s) in ${(durationMs / 1000).toFixed(1)}s`);
	}
	streamGen++; // fence any in-flight pull capture (see captureAndSend)
	activeSource = null;
	frameCount = 0;
	startedAt = 0;
	// P7 D7.4: a parked frame must not outlive its stream — a stale deferred
	// frame draining into a later session is exactly the backlog rule's target.
	deferredSlot = null;
	stopDrainTimer();
	// Stale frames STAY in context — this only tells the model they are stale.
	// turnComplete is ignored by the transport, so it may answer the hint aloud.
	if (wasPush) {
		const transport = sessionRef?.transport;
		// Call as a method (not via an extracted reference) so `this` binds
		// to the transport — GeminiLiveTransport.sendContent uses `this.session`
		// internally and throws otherwise.
		if (transport && typeof transport.sendContent === 'function') {
			try {
				transport.sendContent([{
					role: 'user',
					text: '[system note] User has stopped sharing their screen. Previously-streamed video frames are stale — do not describe them as the current view. If the user now asks "what do you see", call send_vision_frame to capture a fresh image.',
				}], false);
				console.log(`${ts()} [Vision] injected screen-share-ended context hint`);
			} catch (err) {
				console.warn(`${ts()} [Vision] failed to inject screen-share-ended hint: ${(err as Error).message}`);
			}
		}
	}
	return { wasRunning, frames, durationMs, source: sourceName };
}

/** Inject a frame from an external pusher (the web-client's
 *  getDisplayMedia loop). Push-mode must be active — caller should have
 *  hit /vision/start with source='browser' first. */
export function submitFrame(data: Buffer, mimeType: string = 'image/jpeg'): { ok: boolean; deferred?: boolean; reason?: string; error?: string } {
	if (!getSendFile()) {
		console.warn(`${ts()} [Vision] frame dropped: no active voice session (sessionRef=${!!sessionRef}, transport=${!!sessionRef?.transport})`);
		return { ok: false, error: 'no active voice session' };
	}
	if (!pushMode) {
		console.warn(`${ts()} [Vision] frame dropped: push mode inactive — call /vision/start with source=browser first`);
		return { ok: false, error: 'not in push mode — call /vision/start with source=browser first' };
	}
	// P7 D7.4: browser push was the ungated FE-1 hole — it rides the same
	// central gate + token bucket + latest-frame slot as every other source.
	const r = sendFrameGated(data, mimeType, false);
	if (!r.ok) {
		console.error(`${ts()} [Vision] sendFile failed: ${r.error}`);
		return { ok: false, reason: r.reason, error: r.error ?? 'submitFrame failed' };
	}
	return r;
}

async function captureAndSend(
	source: VisionSource,
	fenceGen?: number,
): Promise<{ ok: boolean; deferred?: boolean; error?: string }> {
	if (!getSendFile()) return { ok: false, error: 'no active voice session' };
	const frame = await source.capture();
	if (fenceGen !== undefined && fenceGen !== streamGen) {
		// The stream this capture belonged to was stopped (or replaced) while
		// the source was capturing — the stale frame is dropped, not sent.
		return { ok: true, deferred: false };
	}
	// P7 D7.4: the central gate + bucket own the egress decision; post-send
	// hooks (screen-companion selection text) fire only when the frame
	// actually sends — including a later drain of the deferred slot.
	return sendFrameGated(frame.data, frame.mimeType, true);
}

/** Capture a single frame from `sourceName` (default 'screen') and send it to
 *  the active Gemini Live session as vision input. Pull-mode one-shot — does
 *  not require push mode to be running. Returns `{ ok: false }` if no session
 *  is connected or the source is unknown. */
export async function captureSendFrame(sourceName?: string): Promise<{ ok: boolean; source?: string; error?: string }> {
	try {
		const source = resolveSource(sourceName);
		const r = await captureAndSend(source);
		return r.ok ? { ok: true, source: source.name } : r;
	} catch (err) {
		return { ok: false, error: (err as Error)?.message ?? String(err) };
	}
}

async function tick(): Promise<void> {
	if (inFlight || !activeSource) return; // skip overlap — slow camera or slow disk
	inFlight = true;
	try {
		const r = await captureAndSend(activeSource, streamGen);
		if (r.ok) {
			// Deferred counts as healthy: the gate/bucket parked the frame
			// deliberately (frameCount advances only on real sends, inside
			// sendFrameGated).
			if (consecutiveFrameErrors > 0) {
				console.log(`${ts()} [Vision] frame capture recovered after ${consecutiveFrameErrors} failure(s)`);
				consecutiveFrameErrors = 0;
			}
		} else {
			noteFrameFailure(r.error ?? 'tick skipped');
		}
	} catch (err) {
		noteFrameFailure((err as Error)?.message ?? String(err));
	} finally {
		inFlight = false;
	}
}

// Rate-limited failure logging + auto-stop. Log the 1st failure and then only
// every 10th (heartbeat), never per-tick; stop the stream entirely once a
// source has been failing continuously past the threshold (the screen-share is
// effectively dead — capturing a broken source is just CPU + log noise).
function noteFrameFailure(msg: string): void {
	consecutiveFrameErrors++;
	if (consecutiveFrameErrors === 1 || consecutiveFrameErrors % 10 === 0) {
		console.error(`${ts()} [Vision] frame error (${consecutiveFrameErrors} consecutive): ${msg}`);
	}
	if (consecutiveFrameErrors >= VISION_FAIL_STOP_THRESHOLD) {
		console.error(`${ts()} [Vision] ${consecutiveFrameErrors} consecutive frame errors — stopping stream (source unavailable: ${msg})`);
		stopStream();
		consecutiveFrameErrors = 0;
		// Tell the live session vision died, IN-BAND (Maddy review note #2,
		// 2026-06-10): without this the agent keeps believing it can see the
		// screen and may answer "what's on screen" from a stale/empty frame.
		// transport.sendContent is the same drain path the work-tool results
		// use, so the notice lands as a normal user turn.
		try {
			const t = sessionRef?.transport;
			if (t?.sendContent) {
				t.sendContent([{ role: 'user', text: '[System: the screen share / camera feed stopped — you can no longer see it. If the user asks what you see, tell them the view dropped and ask them to re-share; do not answer from a previous frame.]' }], true);
				console.log(`${ts()} [Vision] notified session that the feed dropped`);
			}
		} catch (e) {
			console.error(`${ts()} [Vision] failed to notify session of feed drop: ${(e as Error)?.message ?? e}`);
		}
	}
}

function startStream(source: VisionSource, fps: number): { fps: number; intervalMs: number } {
	lastStopReason = null; // a new stream supersedes any terminal stop
	const clamped = Math.max(MIN_FPS, Math.min(MAX_FPS, fps));
	const intervalMs = Math.max(MIN_INTERVAL_MS, Math.round(1000 / clamped));
	if (ticker) clearInterval(ticker);
	streamGen++;
	activeSource = source;
	frameCount = 0;
	startedAt = Date.now();
	console.log(`${ts()} [Vision] started ${source.name} — ${clamped} fps (${intervalMs}ms)`);
	// Send one frame immediately so the model has context before the first interval.
	void tick();
	ticker = setInterval(() => { void tick(); }, intervalMs);
	return { fps: clamped, intervalMs };
}

// --- Tools ----------------------------------------------------------------

export const sendVisionFrameTool: ToolDefinition = {
	name: 'send_vision_frame',
	description:
		"Capture and send a single image to you (Gemini) as vision input. Use for one-off " +
		"\"what am I looking at\", \"can you see this\", \"check what's on my screen now\". " +
		"Source defaults to the user's screen; pass source='webcam' for the front camera, or any other registered source (e.g. 'glasses'). " +
		'For ongoing observation, use start_vision instead. Instant.',
	parameters: z.object({
		source: z.string().optional().describe("Frame source. Default 'screen'. Built-in: 'screen', 'webcam'. External integrations may register more (e.g. 'glasses')."),
	}),
	execution: 'inline',
	async execute(args) {
		const { source: sourceName } = (args ?? {}) as { source?: string };
		// Push mode: frames are already streaming; the latest one is in your
		// context. Don't fight the active stream by shelling out to a
		// (possibly permission-denied) screencapture.
		if (pushMode) {
			return {
				status: 'sent',
				source: `push:${pushSourceName || 'unknown'}`,
				framesSinceStart: frameCount,
				note: 'Push mode active — latest frame is already in your context.',
			};
		}
		try {
			const source = resolveSource(sourceName);
			const r = await captureAndSend(source);
			if (!r.ok) return { status: 'failed', error: r.error };
			return { status: 'sent', source: source.name };
		} catch (err) {
			console.error(`${ts()} [Vision] sendVisionFrameTool threw: ${(err as Error)?.message ?? err}`);
			return { status: 'failed', error: 'captureAndSend failed' };
		}
	},
};

export const startVisionTool: ToolDefinition = {
	name: 'start_vision',
	description:
		"Start streaming live vision frames to you (Gemini) so you can see what the user is doing in real time. " +
		"Use for: \"watch my screen\", \"look at what I'm doing\", \"follow along\", \"see me as I talk\". " +
		'Frames flow at ~1 fps until stop_vision is called or the session ends. ' +
		"Source defaults to the user's screen; pass source='webcam' for the front camera, or any other registered source (e.g. 'glasses'). " +
		'Prefer send_vision_frame for one-off "look at this" questions. Instant.',
	parameters: z.object({
		source: z.string().optional().describe("Frame source. Default 'screen'. Built-in: 'screen', 'webcam'. External integrations may register more (e.g. 'glasses')."),
		fps: z.number().optional().describe('Frames per second, 0.1–1.0 (Gemini Live caps video at 1 fps). Default 1. Webcam may not keep up above 0.5.'),
		display: z.number().optional().describe('Which display to watch, as its index from the needs_display_choice list. Only for source=screen, and only needed on a multi-display Mac. Held for the rest of the session once set.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { source: sourceName, fps, display } = (args ?? {}) as { source?: string; fps?: number; display?: number };
		if (!getSendFile()) {
			return { status: 'failed', error: 'No active voice session — vision streaming requires a connected session.' };
		}
		// Push mode: the user has already chosen a surface (tab/window/screen)
		// via the browser's getDisplayMedia picker (or another pusher), and
		// frames are flowing. Don't switch to pull-mode screencapture — that
		// would replace the user's deliberately-chosen surface with the whole
		// desktop. Just acknowledge that we're already watching.
		if (pushMode) {
			return {
				status: 'streaming',
				source: `push:${pushSourceName || 'unknown'}`,
				fps: 0,
				intervalMs: 0,
				mode: 'push',
				note: 'Push mode already active — frames are flowing from the externally-chosen surface. Latest frame is in your context.',
			};
		}
		try {
			const source = resolveSource(sourceName);
			if (source.name === 'screen') {
				// Enumeration is best-effort: if it fails the gate sees an empty list
				// and streams the default display rather than refusing to start.
				const known = display === undefined && _sessionDisplay === null
					? await listDisplays().catch(() => [] as DisplayInfo[])
					: [];
				const gate = decideDisplayGate(_sessionDisplay, display, known);
				if (gate.kind === 'ask') {
					return {
						status: 'needs_display_choice',
						displays: gate.displays,
						note:
							'This Mac has more than one display and screencapture would default to one of them. ' +
							'Ask the user which to watch, then call start_vision again with display=<index>. ' +
							'The choice is held for the rest of the session. Options — ' +
							gate.displays.map(describeDisplay).join('; '),
					};
				}
				_sessionDisplay = gate.display;
			}
			const info = startStream(source, fps ?? DEFAULT_FPS);
			return {
				status: 'streaming',
				source: source.name,
				fps: info.fps,
				intervalMs: info.intervalMs,
				...(source.name === 'screen' && _sessionDisplay !== null ? { display: _sessionDisplay } : {}),
			};
		} catch (err) {
			console.error(`${ts()} [Vision] startVisionTool threw: ${(err as Error)?.message ?? err}`);
			return { status: 'failed', error: 'startStream failed' };
		}
	},
};

// A plugin-contributed screen-share tool (manifest-loaded) is a thin wrapper
// over the exported startStreaming() primitive below; the host keeps no
// plugin-specific footprint here (#1720).

export const stopVisionTool: ToolDefinition = {
	name: 'stop_vision',
	description:
		'Stop the live vision stream started by start_vision. ' +
		'Use for: "stop watching", "you can stop looking now", "stop the screen share", "stop the camera". Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		const r = stopStream();
		if (!r.wasRunning) return { status: 'idle', note: 'Vision was not streaming.' };
		return { status: 'stopped', source: r.source, frames: r.frames, durationMs: r.durationMs };
	},
};

// --- HTTP control server --------------------------------------------------
// Tiny localhost-only server so the web-client's Watch button (and any other
// out-of-process caller) can drive the same controller the voice tools use.
// web-client.ts proxies /vision/* to this port to keep the browser
// same-origin — don't expose this port externally.

// 7846 is taken by credential-proxy (ANTHROPIC_BASE_URL); 7847 is free.
const DEFAULT_CONTROL_PORT = Number(process.env.VISION_CONTROL_PORT) || 7847;
let controlServer: Server | null = null;

function readJsonBody(req: import('node:http').IncomingMessage): Promise<Record<string, unknown>> {
	return new Promise((resolve) => {
		const chunks: Buffer[] = [];
		req.on('data', (c: Buffer) => chunks.push(c));
		req.on('end', () => {
			if (chunks.length === 0) return resolve({});
			try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf-8')) as Record<string, unknown>); }
			catch { resolve({}); }
		});
		req.on('error', () => resolve({}));
	});
}

export function startVisionControlServer(port: number = DEFAULT_CONTROL_PORT): Server {
	if (controlServer) return controlServer;
	const srv = createServer(async (req, res) => {
		const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
		const respond = (status: number, body: unknown) => {
			res.writeHead(status, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify(body));
		};
		if (url.pathname === '/vision/state' && req.method === 'GET') {
			return respond(200, getVisionState());
		}
		if (url.pathname === '/vision/start' && req.method === 'POST') {
			const body = await readJsonBody(req);
			const source = typeof body.source === 'string' ? body.source : undefined;
			const fps = typeof body.fps === 'number' ? body.fps : undefined;
			const mode = body.mode === 'push' || body.mode === 'pull' ? body.mode : undefined;
			const r = startStreaming(source, fps, mode);
			return respond(r.status === 'failed' ? 409 : 200, r);
		}
		if (url.pathname === '/vision/stop' && req.method === 'POST') {
			return respond(200, stopStreaming());
		}
		if (url.pathname === '/vision/frame' && req.method === 'POST') {
			// Cap the body BEFORE buffering it. The egress gate rejects an
			// oversized frame, but only after the whole request is already in
			// memory — and this listener binds 0.0.0.0 by default.
			void readBodyCapped(req).then((buf) => {
				if (!buf) return respond(413, { status: 'failed', error: 'frame body too large' });
				const mime = (req.headers['content-type'] as string | undefined) || 'image/jpeg';
				const r = submitFrame(buf, mime);
				// The 409 carries stoppedReason so the client can distinguish a
				// terminal stop from a lost flag without awaiting the 2s poll.
				respond(r.ok ? 200 : r.reason === 'frame-too-large' ? 413 : 409,
					r.ok ? { status: 'sent' } : { status: 'failed', error: r.error, stoppedReason: lastStopReason });
			});
			return;
		}
		respond(404, { error: 'not found' });
	});
	srv.on('error', (err: NodeJS.ErrnoException) => {
		// EADDRINUSE means another voice-agent is already running. Don't crash —
		// the existing instance owns the control endpoint.
		if (err.code === 'EADDRINUSE') {
			console.warn(`${ts()} [Vision] control port ${port} in use; skipping (another voice-agent?)`);
			// Intentionally null — another process owns the listener, so our
			// stopVisionControlServer() should be a no-op (don't close
			// someone else's server on shutdown).
			controlServer = null;
			return;
		}
		console.error(`${ts()} [Vision] control server error: ${err.message}`);
	});
	srv.listen(port, '127.0.0.1', () => {
		// Record the port the OS ACTUALLY bound, read from srv.address() — not the
		// `port` parameter, which would echo `0` when the caller requested an
		// ephemeral port instead of the real assigned one (the whole point of the
		// runtime-authored state is correctness under a non-default port).
		const addr = srv.address();
		const boundPort = (addr && typeof addr === 'object') ? addr.port : port;
		console.log(`${ts()} [Vision] control server listening on 127.0.0.1:${boundPort}`);
		// vision-control.json is runtime-authored state recording the ACTUAL bound
		// control port. `sutando-config.sh runtime` reads it (validated by pid
		// liveness) so the AgentRuntime descriptor's `vision_control` reports the
		// port this process really bound — correct for a VISION_CONTROL_PORT
		// override, not a hardcoded default. Same pattern as voice-agent.ts's
		// writeVoiceRuntimeState() for voice_ws (#2115); consumed by the desktop
		// 'Watch' toggle (ag2-space/ag2space-cinny-desktop v0.3.0 Slice-2).
		try {
			writeFileSync(
				statusPath('vision-control.json', resolveWorkspace()),
				JSON.stringify({ vision_control: `http://127.0.0.1:${boundPort}`, port: boundPort, pid: process.pid, ts: Math.floor(Date.now() / 1000) })
			);
		} catch (err) {
			console.error(`${ts()} [Vision] runtime state write failed:`, err);
		}
	});
	controlServer = srv;
	return srv;
}

export function stopVisionControlServer(): void {
	if (!controlServer) return;
	try { controlServer.close(); } catch {}
	controlServer = null;
}
