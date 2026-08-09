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
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

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
function buildWorkTool(): ToolDefinition {
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
				`task: ${task.replace(/\n/g, ' ')}`,
				'source: wearable-voice',
				'channel_id: voice-host',
				'user_id: wearable',
				'access_tier: owner',
				'priority: urgent',
				'',
			].join('\n');
			writeFileSync(join(resolveWorkspace(), 'tasks', `${id}.txt`), body);
			console.log(`${ts()} [work] delegated ${id}: ${task.slice(0, 80)}`);
			return { status: 'delegated', taskId: id,
				note: 'Task handed to Sutando. Tell the user it is underway; results arrive as a notification.' };
		},
	};
}

function buildWearableAgent(): MainAgent {
	return {
		name: 'wearable',
		instructions:
			'You are Sutando speaking through the user\'s wearable (a watch). '
			+ 'Answers are SPOKEN and shown on a tiny screen: be brief — one or two short sentences. '
			+ 'You are the same persistent Sutando that works on the user\'s computer. '
			+ 'If the user asks for real work (research, code, checking a PR, email), call the work tool '
			+ 'and confirm briefly; do not attempt long tasks in conversation. '
			+ 'For questions about current status, answer from context conversationally.',
		tools: [buildWorkTool()],
	} as MainAgent;
}

async function handleSession(ws: WebSocket): Promise<void> {
	const n = nextSessionN++;
	const bodhiPort = nextBodhiPort++;
	let session: VoiceSession | null = null;

	const teardown = () => {
		const s = session as any;
		session = null;
		try { s?.stop?.(); s?.close?.(); } catch { /* best-effort */ }
	};

	ws.once('message', async (first: Buffer, isBinary: boolean) => {
		try {
			if (isBinary) throw new Error('first frame must be the {"open"} handshake');
			const params = JSON.parse(first.toString()).open ?? {};
			session = new VoiceSession({
				sessionId: `wearable_${Date.now()}_${n}`,
				userId: 'wearable_user',
				apiKey: GEMINI_API_KEY,
				agents: [buildWearableAgent()],
				initialAgent: 'wearable',
				port: bodhiPort,
				host: '127.0.0.1',
				model: google(TEXT_MODEL),
				geminiModel: NATIVE_AUDIO_MODEL,
				inputAudioTranscription: true,
				speechConfig: { voiceName: params.voice || 'Aoede' },
			} as any);
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
			(session as any).handleAudioOutput = (data: string) => {
				if (ws.readyState === WebSocket.OPEN) ws.send(Buffer.from(data, 'base64'));
				if (!spokeThisTurn) { spokeThisTurn = true; sendState('speaking'); }
			};
			const bus = (session as any).eventBus;
			bus?.subscribe?.('turn.start', () => { spokeThisTurn = false; sendState('thinking'); });
			bus?.subscribe?.('turn.end', () => { spokeThisTurn = false; sendState('listening'); });
			bus?.subscribe?.('turn.interrupted', () => { spokeThisTurn = false; sendState('interrupted'); });
			await (session as any).start();
			console.log(`${ts()} [session ${n}] open (bodhi :${bodhiPort})`);
			// The {"ok"} ack MUST be the first text frame — the bridge handshake
			// reads it before anything else; state comes after.
			ws.send(JSON.stringify({ ok: true }));
			sendState('listening');
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
						(session as any).handleInterrupted?.();
						console.log(`${ts()} [session ${n}] interrupted by device`);
					}
				} catch { /* ignore malformed control frames */ }
				return;
			}
			try { (session as any).handleAudioFromClient(data); } catch (e) {
				console.error(`${ts()} [session ${n}] audio-in error:`, e);
			}
		});
	});

	ws.on('close', () => { console.log(`${ts()} [session ${n}] closed`); teardown(); });
	ws.on('error', () => { ws.close(); teardown(); });
}

function main(): void {
	if (!GEMINI_API_KEY) {
		console.error('GEMINI_API_KEY is required');
		process.exit(1);
	}
	const wss = new WebSocketServer({ port: PORT, path: '/session', host: '127.0.0.1' });
	wss.on('connection', (ws) => { void handleSession(ws); });
	console.log(`${ts()} voice-host listening on ws://127.0.0.1:${PORT}/session `
		+ `(audio model: ${NATIVE_AUDIO_MODEL})`);
}

main();
