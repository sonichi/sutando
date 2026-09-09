/**
 * voice-host — the Node process that owns VoiceSession for SCP media streams.
 *
 * The runtime-api daemon (Python) routes each SCP audio stream here over a
 * minimal local wire (one WS per stream), because the voice stack (bodhi
 * VoiceSession) is TypeScript. This host is a thin session shell — the same
 * "dumb audio pipe + session lifecycle + injected tools" shape as
 * conversation-server, minus Twilio:
 *
 *   wire (spoken by src/runtime-api/voice_host_bridge.py):
 *     WS /session → first text frame {"open": {...}} → ack {"ok": true}
 *     binary in   = upstream audio  (device mic, PCM 16kHz s16le mono)
 *     binary out  = downstream audio (agent voice, PCM 24kHz s16le mono)
 *     WS close    = session over
 *
 * Intelligence boundaries (V1 spec): the DEVICE does no STT/TTS/intent; this
 * host does no task scheduling — the model decides chat-vs-task by calling the
 * injected `work` tool, whose implementation writes a canonical task file the
 * core picks up (the verified conversation-server pattern).
 *
 * Run:  npx tsx src/voice-host.ts        (env: GEMINI_API_KEY required;
 *       SUTANDO_VOICE_HOST_PORT default 8788)
 */
import { VoiceSession, type MainAgent, type ToolDefinition } from 'bodhi-realtime-agent';
import { WebSocketServer, WebSocket } from 'ws';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';
import { tryStampText } from './task_envelope.js';
import { confineUserContent } from './task_body_guard.js';

const PORT = Number(process.env.SUTANDO_VOICE_HOST_PORT || 8788);
const GEMINI_API_KEY = process.env.GEMINI_API_KEY || '';
// Same native-audio model family the voice agent runs (src/voice-config.ts).
const NATIVE_AUDIO_MODEL = process.env.SUTANDO_VOICE_NATIVE_MODEL
	|| 'gemini-2.5-flash-native-audio-preview-12-2025';
const TEXT_MODEL = process.env.SUTANDO_VOICE_TEXT_MODEL || 'gemini-2.5-flash';

const google = createGoogleGenerativeAI({ apiKey: GEMINI_API_KEY });
const ts = () => new Date().toISOString().slice(11, 19);

let nextSessionN = 1;
let nextBodhiPort = Number(process.env.SUTANDO_VOICE_HOST_BODHI_BASE || 8850);

/** The `work` tool — durable-task handoff, the conversation-server pattern:
 * the MODEL decides an utterance is work and calls this; the implementation
 * writes a canonical task file the core's watcher picks up. */
/** Where a session's delegated work goes — the multi-tenant seam.
 * 'dir' = this machine's core (today's local mode). 'gateway' = the TENANT'S
 * own core over the relay (cloud mode: the session runs cloudside but work
 * and results stay with the user's core). The transport layer injects this
 * per session after credential verification — NEVER client-supplied. */
interface TenantRoute {
	kind: 'dir' | 'gateway';
	agentId?: string;
	url?: string;      // gateway: relay task-submit endpoint for the tenant
	token?: string;    // gateway: the tenant's relay bearer
}

async function submitViaGateway(route: TenantRoute, id: string, body: string): Promise<void> {
	const res = await fetch(route.url!, {
		method: 'POST',
		headers: { 'content-type': 'application/json',
			authorization: `Bearer ${route.token}` },
		body: JSON.stringify({ id, task: body }),
	});
	if (!res.ok) throw new Error(`gateway submit ${res.status}`);
}

function buildWorkTool(device: { deviceId?: string; label?: string },
		route: TenantRoute = { kind: 'dir' }): ToolDefinition {
	return {
		name: 'work',
		description:
			'Delegate a task to Sutando to work on (research, code, email, any real work). '
			+ 'Use for requests that need doing rather than a conversational answer.',
		parameters: z.object({
			task: z.string().describe('What to do, self-contained and specific'),
		}),
		execute: async ({ task }: { task: string }) => {
			const id = `task-wearable-${Date.now()}`;
			const body = [
				`id: ${id}`,
				`timestamp: ${new Date().toISOString()}`,
				`task: ${confineUserContent(task).replace(/\n/g, ' ')}`,
				'source: wearable-voice',
				'channel_id: voice-host',
				`user_id: ${device.label || 'wearable'}`,
				...(device.deviceId ? [`device_id: ${device.deviceId}`] : []),
				...(device.label ? [`device_label: ${device.label}`] : []),
				...(route.agentId ? [`agent_id: ${route.agentId}`] : []),
				'access_tier: owner',
				'priority: urgent',
				'',
			].join('\n');
			if (route.kind === 'gateway') {
				await submitViaGateway(route, id, body);
			} else {
				writeFileSync(join(resolveWorkspace(), 'tasks', `${id}.txt`), tryStampText(body));
			}
			console.log(`${ts()} [work] delegated ${id} via ${route.kind}`
				+ `${route.agentId ? ' for ' + route.agentId : ''}: ${task.slice(0, 80)}`);
			return { status: 'delegated', taskId: id,
				note: 'Task handed to Sutando. Tell the user it is underway; results arrive as a notification.' };
		},
	};
}

// Cross-session context (owner ask 2026-08-12): the wearable agent used to fly
// blind — every session started from zero. Inject at session-open: the shared
// voice context file (maintained by the core) + the tail of this device's own
// rolling conversation log, so "that PR thing from earlier" just resolves.
function wearableLogPath(device: { deviceId?: string }): string {
	const dir = join(resolveWorkspace(), 'state', 'wearable-conversations');
	if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
	return join(dir, `${device.deviceId || 'unknown'}.log`);
}

export function wearableContext(device: { deviceId?: string }): string {
	let out = '';
	try {
		const shared = JSON.parse(readFileSync(
			join(resolveWorkspace(), 'state', 'voice-session-context.json'), 'utf8'));
		out += `\nShared context (from the core, updated ${shared.updated_at}): `
			+ JSON.stringify({ pending: shared.pending_action, recent: shared.last_results }).slice(0, 900);
	} catch { /* absent/corrupt → skip */ }
	try {
		const lines = readFileSync(wearableLogPath(device), 'utf8').trim().split('\n');
		if (lines.length && lines[0] !== '') {
			out += '\nRecent watch conversation (oldest first):\n' + lines.slice(-14).join('\n');
		}
	} catch { /* first session ever → skip */ }
	try {
		// Cross-surface memory: the desktop voice log, when present, rides along
		// with a smaller share so the watch can follow up on desktop threads.
		const desk = readFileSync(join(resolveWorkspace(), 'state',
			'wearable-conversations', 'local-voice.log'), 'utf8').trim().split('\n');
		if (desk.length && desk[0] !== '') {
			out += '\nRecent desktop voice conversation:\n' + desk.slice(-6).join('\n');
		}
	} catch { /* desktop log absent → skip */ }
	return out ? '\n\nCONTEXT FROM EARLIER (use naturally when the user refers back):' + out : '';
}

function buildWearableAgent(device: { deviceId?: string; label?: string },
		route: TenantRoute = { kind: 'dir' }): MainAgent {
	return {
		name: 'wearable',
		instructions:
			'You are Sutando speaking through the user\'s wearable (a watch). '
			+ 'Answers are SPOKEN and shown on a tiny screen: be brief — one or two short sentences. '
			+ 'You are the same persistent Sutando that works on the user\'s computer. '
			+ 'If the user asks for real work (research, code, checking a PR, email), call the work tool '
			+ 'and confirm briefly; do not attempt long tasks in conversation. '
			+ 'For questions about current status, answer from context conversationally.'
			+ wearableContext(device),
		// Spoken the moment a session opens: tells the wearer the session is
		// live, and exercises the downstream path with no mic dependency.
		greeting: 'Say only: "Hi, I\'m listening."',
		tools: [buildWorkTool(device, route)],
	} as MainAgent;
}

/** The bodhi VoiceSession members this host drives. bodhi exports none of
 *  them, so the surface is named once rather than at 13 `as any` call sites. */
interface SessionInternals {
	start(): Promise<void>;
	stop?(): void;
	close?(): void;
	sendGreeting?(): void;
	handleInterrupted?(): void;
	handleAudioFromClient(data: unknown): void;
	handleAudioOutput?: (data: string) => void;
	handleTransportClose?: (code?: number, reason?: string) => void;
	notifyBackground(text: string, opts?: { priority?: string }): void;
	sessionManager?: { state?: string };
	eventBus?: { subscribe?: (event: string, fn: () => void) => void };
	transcriptManager?: {
		sink?: { sendToClient?: (msg: Record<string, unknown>) => void };
	};
}

/** Only the generateContent fields this tool loop reads or sends. */
interface GeminiPart {
	text?: string;
	functionCall?: { name: string; args?: Record<string, unknown> };
	functionResponse?: { name: string; response: unknown };
}
interface GeminiContent { role: string; parts: GeminiPart[] }

const inner = (s: VoiceSession): SessionInternals =>
	s as unknown as SessionInternals;

async function handleSession(ws: WebSocket): Promise<void> {
	const n = nextSessionN++;
	const bodhiPort = nextBodhiPort++;
	let session: VoiceSession | null = null;
	let bytesIn = 0, bytesOut = 0;
	let resultsPoll: ReturnType<typeof setInterval> | null = null;

	const teardown = () => {
		const s = session === null ? null : inner(session);
		session = null;
		if (resultsPoll) { clearInterval(resultsPoll); resultsPoll = null; }
		try { s?.stop?.(); s?.close?.(); } catch { /* best-effort */ }
	};

	// Spoken results: while the session is live, results for wearable-delegated
	// tasks are read INTO the conversation so ask-by-voice completes by voice.
	// Files are not consumed — the daemon's task.result push (screen flash)
	// works on the same files independently; a seen-set prevents re-speaking.
	const startResultsPoll = (since: number) => {
		const dir = join(resolveWorkspace(), 'results');
		const spoken = new Set<string>();
		resultsPoll = setInterval(() => {
			if (!session) return;
			let names: string[];
			try { names = readdirSync(dir); } catch { return; }
			for (const f of names) {
				if (!/^task-wearable-.*\.txt$/.test(f) || spoken.has(f)) continue;
				try {
					if (statSync(join(dir, f)).mtimeMs < since) { spoken.add(f); continue; }
					const body = readFileSync(join(dir, f), 'utf8').trim();
					spoken.add(f);
					if (!body) continue;
					console.log(`${ts()} [session ${n}] speaking result ${f}`);
					inner(session).notifyBackground(
						`A delegated task just finished. Relay the result to the user in one or two spoken sentences: ${body}`,
						{ priority: 'high' });
				} catch { /* unreadable file — retry next pass is pointless; skip */ }
			}
		}, 2000);
	};

	ws.once('message', async (first: Buffer, isBinary: boolean) => {
		try {
			if (isBinary) throw new Error('first frame must be the {"open"} handshake');
			const params = JSON.parse(first.toString()).open ?? {};
			const device = params.device ?? {};   // transport-stamped, not client-supplied
			// Tenant route: also transport-injected. Absent → local 'dir' mode.
			// The ISOLATION INVARIANT lives here: everything tenant-specific in
			// this session (task destination, attribution, later context reads)
			// derives from THIS object and nothing else.
			const route: TenantRoute = params.tenant ?? { kind: 'dir' };
			session = new VoiceSession({
				sessionId: `wearable_${Date.now()}_${n}`,
				userId: 'wearable_user',
				apiKey: GEMINI_API_KEY,
				agents: [buildWearableAgent(device, route)],
				initialAgent: 'wearable',
				port: bodhiPort,
				host: '127.0.0.1',
				model: google(TEXT_MODEL),
				geminiModel: NATIVE_AUDIO_MODEL,
				inputAudioTranscription: true,
				speechConfig: { voiceName: params.voice || 'Aoede' },
			} as unknown as ConstructorParameters<typeof VoiceSession>[0]);
			// voice.state — UI metadata, never transport sync: the device plays
			// audio the moment frames arrive; these only drive its tiny screen.
			// Mapping: turn.start → thinking, first audio of the turn → speaking,
			// turn.interrupted → interrupted, turn.end → listening.
			let spokeThisTurn = false;
			const sendState = (state: string) => {
				if (ws.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ method: 'voice.state', params: { state } }));
				}
			};
			// Downstream: Gemini audio (base64 PCM 24k) → binary frame to the
			// bridge — the same handleAudioOutput override conversation-server
			// uses, minus the mu-law hop.
			// Gemini hands audio in large buffers; the ESP32 websocket client
			// drops the connection on frames over its RX buffer. 1600B ≈ 33ms
			// @24kHz — small enough for any client, smooth for the device ring.
			const CHUNK = 1600;
			inner(session).handleAudioOutput = (data: string) => {
				const buf = Buffer.from(data, 'base64');
				bytesOut += buf.length;
				if (ws.readyState === WebSocket.OPEN) {
					for (let o = 0; o < buf.length; o += CHUNK) {
						ws.send(buf.subarray(o, Math.min(o + CHUNK, buf.length)));
					}
				}
				if (!spokeThisTurn) { spokeThisTurn = true; sendState('speaking'); }
			};
			// LLM-transport death while the device session is live: bodhi parks the
			// session CLOSED and waits for a client-connect that will never re-fire
			// (the client is already attached). Without this, the watch shows
			// "Listening" while streaming into a dead link (2026-08-12 hotel test).
			// Tell the device the truth, then drop the stream so its own
			// connection-lost cleanup runs and the next YELLOW opens fresh.
			const origTransportClose = inner(session).handleTransportClose?.bind(session);
			if (origTransportClose) {
				inner(session).handleTransportClose = (code?: number, reason?: string) => {
					origTransportClose(code, reason);
					if (session && inner(session).sessionManager?.state === 'CLOSED'
						&& ws.readyState === WebSocket.OPEN) {
						console.log(`${ts()} [session ${n}] LLM died mid-session (code=${code}) — closing device stream`);
						sendState('offline');
						setTimeout(() => { try { ws.close(); } catch { /* gone */ } }, 300);
					}
				};
			}
			const bus = inner(session).eventBus;
			bus?.subscribe?.('turn.start', () => { spokeThisTurn = false; sendState('thinking'); });
			bus?.subscribe?.('turn.end', () => { spokeThisTurn = false; sendState('listening'); });
			bus?.subscribe?.('turn.interrupted', () => { spokeThisTurn = false; sendState('interrupted'); });
			// voice.transcript — both sides of the conversation as text (input
			// transcription + Gemini's always-on output transcription), tapped at
			// the TranscriptManager sink. UI metadata like voice.state: partials
			// replace the current line per role, finals append.
			const tm = inner(session).transcriptManager;
			if (tm?.sink?.sendToClient) {
				const orig = tm.sink.sendToClient.bind(tm.sink);
				tm.sink.sendToClient = (msg: Record<string, unknown>) => {
					orig(msg);
					if (msg?.type === 'transcript' && ws.readyState === WebSocket.OPEN) {
						ws.send(JSON.stringify({ method: 'voice.transcript', params: {
							role: msg.role, text: msg.text, partial: !!msg.partial } }));
					}
					// Finals feed the per-device rolling log → next session's context.
					if (msg?.type === 'transcript' && !msg.partial && msg.text) {
						try {
							appendFileSync(wearableLogPath(device),
								`${msg.role === 'user' ? 'user' : 'sutando'}: ${String(msg.text).slice(0, 300)}\n`);
						} catch { /* log is best-effort */ }
					}
				};
			}
			await inner(session).start();
			console.log(`${ts()} [session ${n}] open (bodhi :${bodhiPort}, `
				+ `device ${device.label || '?'}${device.deviceId ? '/' + device.deviceId : ''})`);
			// The {"ok"} ack MUST be the first text frame — the bridge handshake
			// reads it before anything else; state comes after.
			ws.send(JSON.stringify({ ok: true }));
			sendState('listening');
			try { inner(session).sendGreeting?.(); } catch { /* greeting is best-effort */ }
			// ISOLATION: the local results dir belongs to THIS machine's core.
			// Cloud (gateway-routed) sessions never touch it — their results
			// arrive via the relay (follow-up); polling here would read another
			// tenant's files.
			if (route.kind === 'dir') startResultsPoll(Date.now());
		} catch (e) {
			console.error(`${ts()} [session ${n}] open failed:`, e);
			try { ws.send(JSON.stringify({ ok: false, error: String(e) })); } catch { /* */ }
			ws.close();
			teardown();
			return;
		}
		// Upstream: binary = device mic PCM 16k → straight into the session.
		// Text after open = control: {"interrupt": true} marks the turn
		// interrupted (device already cut its speaker; keeps queued
		// notifications from flushing into a dead turn).
		ws.on('message', (data: Buffer, isBin: boolean) => {
			if (!session) return;
			if (!isBin) {
				try {
					if (JSON.parse(data.toString()).interrupt) {
						inner(session).handleInterrupted?.();
						console.log(`${ts()} [session ${n}] interrupted by device`);
					}
				} catch { /* ignore malformed control frames */ }
				return;
			}
			if (bytesIn === 0) console.log(`${ts()} [session ${n}] first mic frame (${data.length}B)`);
			bytesIn += data.length;
			try { inner(session).handleAudioFromClient(data); } catch (e) {
				console.error(`${ts()} [session ${n}] audio-in error:`, e);
			}
		});
	});

	ws.on('close', () => {
		console.log(`${ts()} [session ${n}] closed (audio in=${bytesIn}B out=${bytesOut}B)`);
		teardown();
	});
	ws.on('error', () => { ws.close(); teardown(); });
}

/** Text-plane sessions (/text): for endpoints whose platform owns STT/TTS
 * (Mentra glasses — transcriptions in, their TTS out; no raw PCM access).
 * SAME agent definition as the audio plane — instructions + work tool come
 * from buildWearableAgent, so there is no per-device brain. Wire: text frames
 * {"user": "..."} in → {"reply": "..."} out; {"open": {...}} handshake first.
 * Results for delegated tasks stream back as {"result": "..."} frames via the
 * same results-poll the audio plane speaks through. */
function handleTextSession(ws: WebSocket): void {
	const n = nextSessionN++;
	let device: { deviceId?: string; label?: string } = {};
	const history: { role: 'user' | 'assistant'; content: string }[] = [];
	let poll: ReturnType<typeof setInterval> | null = null;

	ws.on('message', (data: Buffer, isBin: boolean) => {
		if (isBin) return; // text plane only
		void (async () => {
			try {
				const msg = JSON.parse(data.toString());
				if (msg.open) {
					device = msg.open.device ?? {};
					ws.send(JSON.stringify({ ok: true }));
					console.log(`${ts()} [text ${n}] open (device ${device.label || '?'})`);
					// spoken-results parity: relay finished wearable tasks as text
					const dir = join(resolveWorkspace(), 'results');
					const seen = new Set<string>();
					const since = Date.now();
					poll = setInterval(() => {
						let names: string[];
						try { names = readdirSync(dir); } catch { return; }
						for (const f of names) {
							if (!/^task-wearable-.*\.txt$/.test(f) || seen.has(f)) continue;
							try {
								if (statSync(join(dir, f)).mtimeMs < since) { seen.add(f); continue; }
								const body = readFileSync(join(dir, f), 'utf8').trim();
								seen.add(f);
								if (body && ws.readyState === WebSocket.OPEN) {
									ws.send(JSON.stringify({ result: body }));
								}
							} catch { /* skip unreadable */ }
						}
					}, 2000);
					return;
				}
				if (typeof msg.user === 'string' && msg.user.trim()) {
					const agent = buildWearableAgent(device);
					const workTool = (agent.tools as ToolDefinition[])[0];
					history.push({ role: 'user', content: msg.user });
					// Direct Gemini REST (generateContent + function calling): the
					// repo's ai/@ai-sdk versions disagree on model spec, and this
					// plane needs only a plain tool loop.
					const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
						+ `${TEXT_MODEL}:generateContent?key=${GEMINI_API_KEY}`;
					const contents: GeminiContent[] = history.slice(-20).map((m) => (
						{ role: m.role === 'assistant' ? 'model' : 'user', parts: [{ text: m.content }] }));
					const body = {
						systemInstruction: { parts: [{ text: agent.instructions }] },
						contents,
						tools: [{ functionDeclarations: [{
							name: workTool.name, description: workTool.description,
							parameters: { type: 'object',
								properties: { task: { type: 'string' } }, required: ['task'] },
						}] }],
					};
					let reply = '';
					for (let step = 0; step < 3 && !reply; step++) {
						const res = await fetch(url, { method: 'POST',
							headers: { 'content-type': 'application/json' },
							body: JSON.stringify(body) });
						if (!res.ok) throw new Error(`gemini ${res.status}: ${(await res.text()).slice(0, 200)}`);
						const json = await res.json() as
							{ candidates?: { content?: { parts?: GeminiPart[] } }[] };
						const parts: GeminiPart[] = json?.candidates?.[0]?.content?.parts ?? [];
						const call = parts.find((p) => p.functionCall);
						if (call?.functionCall) {
							const fc = call.functionCall;
							// This hand-rolled loop has no ToolContext to pass, and the
							// local tool's implementation takes args only. Call the shape.
							const run = workTool.execute as
								(a: Record<string, unknown>) => Promise<unknown>;
							const result = await run(fc.args ?? {});
							body.contents.push({ role: 'model', parts: [call] });
							body.contents.push({ role: 'user', parts: [{ functionResponse: {
								name: fc.name, response: result } }] });
							continue;
						}
						reply = parts.map((p) => p.text ?? '').join('').trim();
					}
					if (!reply) reply = 'On it.';
					history.push({ role: 'assistant', content: reply });
					if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ reply }));
				}
			} catch (e) {
				console.error(`${ts()} [text ${n}] error:`, e);
				try { ws.send(JSON.stringify({ error: String(e) })); } catch { /* */ }
			}
		})();
	});
	ws.on('close', () => {
		if (poll) clearInterval(poll);
		console.log(`${ts()} [text ${n}] closed`);
	});
}

function main(): void {
	if (!GEMINI_API_KEY) {
		console.error('GEMINI_API_KEY is required');
		process.exit(1);
	}
	const wss = new WebSocketServer({ port: PORT, host: '127.0.0.1' });
	wss.on('connection', (ws, req) => {
		if (req.url?.startsWith('/text')) void handleTextSession(ws);
		else void handleSession(ws);
	});
	console.log(`${ts()} voice-host listening on ws://127.0.0.1:${PORT}/session + /text `
		+ `(audio model: ${NATIVE_AUDIO_MODEL}, text model: ${TEXT_MODEL})`);
	startContextSync();
}

// Mechanical shared-context updates (owner ask 2026-08-12): last_results must
// track EVERY task result without anyone remembering to write it. The voice
// host is the context CONSUMER, so it owns the sync — no new service. 15s
// scan of results/; protocol-marker bodies and question files are skipped.
function startContextSync(): void {
	const resultsDir = join(resolveWorkspace(), 'results');
	const ctxPath = join(resolveWorkspace(), 'state', 'voice-session-context.json');
	let lastScan = Date.now();
	setInterval(() => {
		const fresh: { task_id: string; subject: string; ts: string }[] = [];
		try {
			for (const f of readdirSync(resultsDir)) {
				if (!f.startsWith('task-') || !f.endsWith('.txt')) continue;
				const full = join(resultsDir, f);
				const st = statSync(full);
				if (st.mtimeMs <= lastScan) continue;
				const body = readFileSync(full, 'utf8').trim();
				if (!body || body.startsWith('[deduped:') || body.startsWith('[no-send]')
					|| body.startsWith('[REPLIED]')) continue;
				fresh.push({ task_id: f.replace(/\.txt$/, ''),
					subject: body.split('\n')[0].slice(0, 140),
					ts: new Date(st.mtimeMs).toISOString() });
			}
		} catch { return; }
		if (!fresh.length) { lastScan = Date.now(); return; }
		lastScan = Date.now();
		try {
			let ctx: { last_results?: unknown[]; [k: string]: unknown } = {};
			try { ctx = JSON.parse(readFileSync(ctxPath, 'utf8')); } catch { /* fresh */ }
			ctx.updated_at = new Date().toISOString();
			ctx.last_results = [...fresh.reverse(), ...(ctx.last_results ?? [])].slice(0, 3);
			const tmp = ctxPath + '.tmp';
			writeFileSync(tmp, JSON.stringify(ctx, null, 1));
			renameSync(tmp, ctxPath);
		} catch { /* context write is best-effort */ }
	}, 15000).unref();
}

main();
