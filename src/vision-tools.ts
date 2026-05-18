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

import { readFileSync, unlinkSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer, type Server } from 'node:http';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const execFileAsync = promisify(execFile);
const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

const DEFAULT_FPS = 1;
const MAX_FPS = 2;
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

const screenSource: VisionSource = {
	name: 'screen',
	async capture() {
		// silent=true skips the menu-bar flash + macOS notification, which would
		// otherwise fire on every frame during a stream. format=jpeg keeps
		// frames small (~50–150KB).
		const res = await fetch('http://localhost:7845/capture?format=jpeg&silent=true');
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
		// Inject a hidden conversation turn (no generation trigger when
		// turnComplete=false) so the model sees a context update without
		// the user hearing audio. Used to evict stale push frames from
		// Gemini Live's context when screen sharing ends.
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

function getSendFile(): ((b64: string, mime: string) => void) | null {
	const t = sessionRef?.transport;
	if (!t || !t.sendFile) return null;
	// isConnected is optional — if exposed and false, skip; otherwise trust the call.
	if (t.isConnected === false) return null;
	return t.sendFile.bind(t);
}

// --- Streaming controller -------------------------------------------------

let ticker: NodeJS.Timeout | null = null;
let activeSource: VisionSource | null = null;
let inFlight = false;
let frameCount = 0;
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

export interface VisionState {
	streaming: boolean;
	source: string | null;
	fps: number;
	frames: number;
	durationMs: number;
	sessionReady: boolean;
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
		return { status: 'failed', error: (err as Error)?.message ?? String(err) };
	}
}

/** Programmatic stop (used by the HTTP control server / button). */
export function stopStreaming(): { status: 'stopped' | 'idle'; source: string | null; frames: number; durationMs: number } {
	const r = stopStream();
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
	activeSource = null;
	frameCount = 0;
	startedAt = 0;
	// Push-mode frames accumulate in Gemini Live's conversation context.
	// Without this hint, "what do you see?" after the user stops sharing
	// gets answered from the last frame still in context (model recalls
	// from memory instead of calling send_vision_frame to grab a fresh
	// view). Inject a silent user-role turn (turnComplete=false → no
	// generation triggered) so the next user turn carries the context
	// shift: visual frames are stale.
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
export function submitFrame(data: Buffer, mimeType: string = 'image/jpeg'): { ok: boolean; error?: string } {
	const sendFile = getSendFile();
	if (!sendFile) {
		console.warn(`${ts()} [Vision] frame dropped: no active voice session (sessionRef=${!!sessionRef}, transport=${!!sessionRef?.transport})`);
		return { ok: false, error: 'no active voice session' };
	}
	if (!pushMode) {
		console.warn(`${ts()} [Vision] frame dropped: push mode inactive — call /vision/start with source=browser first`);
		return { ok: false, error: 'not in push mode — call /vision/start with source=browser first' };
	}
	try {
		sendFile(data.toString('base64'), mimeType);
		frameCount++;
		// Log first frame so the user can confirm vision is wired end-to-end,
		// and every 10th to keep tail noise low.
		if (frameCount === 1 || frameCount % 10 === 0) {
			console.log(`${ts()} [Vision] sent frame #${frameCount} (${Math.round(data.byteLength / 1024)}KB ${mimeType})`);
		}
		return { ok: true };
	} catch (err) {
		console.error(`${ts()} [Vision] sendFile threw: ${(err as Error)?.message ?? err}`);
		return { ok: false, error: (err as Error)?.message ?? String(err) };
	}
}

async function captureAndSend(source: VisionSource): Promise<{ ok: boolean; error?: string }> {
	const sendFile = getSendFile();
	if (!sendFile) return { ok: false, error: 'no active voice session' };
	const frame = await source.capture();
	sendFile(frame.data.toString('base64'), frame.mimeType);
	return { ok: true };
}

async function tick(): Promise<void> {
	if (inFlight || !activeSource) return; // skip overlap — slow camera or slow disk
	inFlight = true;
	try {
		const r = await captureAndSend(activeSource);
		if (r.ok) frameCount++;
		else console.error(`${ts()} [Vision] tick skipped: ${r.error}`);
	} catch (err) {
		console.error(`${ts()} [Vision] frame error: ${(err as Error)?.message ?? err}`);
	} finally {
		inFlight = false;
	}
}

function startStream(source: VisionSource, fps: number): { fps: number; intervalMs: number } {
	const clamped = Math.max(0.5, Math.min(MAX_FPS, fps));
	const intervalMs = Math.max(MIN_INTERVAL_MS, Math.round(1000 / clamped));
	if (ticker) clearInterval(ticker);
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
			return { status: 'failed', error: (err as Error)?.message ?? String(err) };
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
		fps: z.number().optional().describe('Frames per second, 0.5–2. Default 1. Webcam may not keep up above 0.5.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { source: sourceName, fps } = (args ?? {}) as { source?: string; fps?: number };
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
			const info = startStream(source, fps ?? DEFAULT_FPS);
			return { status: 'streaming', source: source.name, fps: info.fps, intervalMs: info.intervalMs };
		} catch (err) {
			return { status: 'failed', error: (err as Error)?.message ?? String(err) };
		}
	},
};

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
			const chunks: Buffer[] = [];
			req.on('data', (c: Buffer) => chunks.push(c));
			req.on('end', () => {
				const buf = Buffer.concat(chunks);
				const mime = (req.headers['content-type'] as string | undefined) || 'image/jpeg';
				const r = submitFrame(buf, mime);
				respond(r.ok ? 200 : 409, r.ok ? { status: 'sent' } : { status: 'failed', error: r.error });
			});
			req.on('error', () => respond(500, { status: 'failed', error: 'request error' }));
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
		console.log(`${ts()} [Vision] control server listening on 127.0.0.1:${port}`);
	});
	controlServer = srv;
	return srv;
}

export function stopVisionControlServer(): void {
	if (!controlServer) return;
	try { controlServer.close(); } catch {}
	controlServer = null;
}
