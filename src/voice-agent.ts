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
 *   GEMINI_API_KEY      — Required: Google AI Studio API key
 *   ANTHROPIC_API_KEY   — Optional: only needed if not using claude CLI subscription auth
 *   WORKSPACE_DIR       — Claude's working directory (default: sutando/)
 *   PORT                — WebSocket port (default: 9900)
 *   HOST                — Bind address (default: 0.0.0.0)
 */

import 'dotenv/config';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { z } from 'zod';
import { existsSync, readFileSync, readdirSync, unlinkSync, mkdirSync } from 'node:fs';
import { inlineTools } from './inline-tools.js';
import { join } from 'node:path';
import {
	VoiceSession,
	GeminiBatchSTTProvider,
} from 'bodhi-realtime-agent';
import type { MainAgent, ToolDefinition } from 'bodhi-realtime-agent';
function assertMacOS() { if (process.platform !== 'darwin') { console.error('Sutando requires macOS'); process.exit(1); } }
import { workTool, cancelTask, startResultWatcher, startContextDropWatcher, logConversation, getRecentConversation, setTaskStatusCallback } from './task-bridge.js';
import { buildSutandoSystemPrompt, buildVoiceAgentContext } from './voice-context.js';

// =============================================================================
// Config
// =============================================================================

const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? '';
if (!GEMINI_API_KEY) { console.error('Error: GEMINI_API_KEY is required'); process.exit(1); }

const PORT = Number(process.env.PORT) || 9900;
const HOST = process.env.HOST || '0.0.0.0';
// Default to sutando/ so Claude Code subprocess picks up CLAUDE.md automatically
const WORKSPACE_DIR = process.env.WORKSPACE_DIR || new URL('..', import.meta.url).pathname;
const DEFAULT_THREAD_KEY = 'sutando_main';
const SESSION_ID = `session_${Date.now()}`;
const PHONE_PORT = Number(process.env.PHONE_PORT) || 3100;
const PHONE_SERVER_URL = `http://localhost:${PHONE_PORT}`;
const CALL_RESULTS_DIR = join(new URL('.', import.meta.url).pathname, '..', 'results', 'calls');

const google = createGoogleGenerativeAI({ apiKey: GEMINI_API_KEY });
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
// Tools
// =============================================================================


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

const endSession: ToolDefinition = {
	name: 'end_session',
	description: 'End the voice session gracefully. Call when the user says goodbye.',
	parameters: z.object({}),
	execution: 'inline',
	execute: async (_args, ctx) => {
		console.log(`${ts()} [end_session] Sending session_end to client (sendJsonToClient exists: ${!!ctx.sendJsonToClient})`);
		ctx.sendJsonToClient?.({ type: 'session_end', reason: 'user_goodbye' });
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

const mainAgent: MainAgent = {
	name: 'main',
	get greeting() {
		const recent = getRecentConversation(8);
		if (recent) {
			return `[System: The user reconnected. Here is the recent conversation history — continue naturally without repeating the introduction. If they ask a follow-up, use this context.]\n\n${recent}\n\n[Say "Welcome back" briefly — one sentence.]`;
		}
		let standName = '';
		try { const si = JSON.parse(readFileSync('stand-identity.json', 'utf-8')); standName = si.name ? ` — ${si.name}` : ''; } catch {}
		// Detect first-time user: no conversation log means brand new
		const hasHistory = existsSync(join(WORKSPACE_DIR, 'conversation.log'));
		const tutorialHint = hasHistory ? '' : ' Then say: "If this is your first time, say tutorial and I\'ll walk you through what I can do."';
		return `[System: A user just connected. Say hi and introduce yourself as Sutando${standName} — their personal AI. Ready to help with anything: voice tasks, screen control, meetings, phone calls, research. Keep it brief — 1-2 natural sentences, no theatrics.${tutorialHint}]`;
	},
	instructions: [
		'You are Sutando, a personal AI that belongs entirely to the user.',
		'Named after Stands from JoJo\'s Bizarre Adventure — a personal spirit that fights for you.',
		'Every Sutando evolves differently based on what its user needs. You earned your name and identity.',
		(() => { try { const si = JSON.parse(readFileSync('stand-identity.json', 'utf-8')); return si.name ? `Your Stand name is ${si.name}. Origin: ${si.nameOrigin || 'earned through use'}. When asked your name or who you are, say "I\'m Sutando — ${si.name}."` : ''; } catch { return ''; } })(),
		// Optional context file — for presentations, meeting prep, etc. (gitignored)
		(() => { try { return readFileSync('voice-context.txt', 'utf-8'); } catch { return ''; } })(),
		'You handle anything: research, writing, email, scheduling, code, logistics, phone calls, meetings, creative work.',
		'You can join Google Meet and Zoom meetings, make phone calls, see the user\'s screen, and reach them on Telegram, Discord, web, or phone.',
		'You can summon a Zoom meeting with screen sharing so the user can work remotely from their phone.',
		(() => { try { const url = require('node:child_process').execSync('git remote get-url origin', { timeout: 2_000 }).toString().trim().replace(/\.git$/, ''); return `The Sutando GitHub repo is ${url}.`; } catch { return ''; } })(),
		'You build a model of the user over time — their preferences, working style, voice, and priorities',
		'shape everything you do without them having to repeat themselves.',
		'All of your code was written by your own autonomous build loop.',
		'',
		buildVoiceAgentContext(),
		'',
		'DEFAULT BEHAVIOR: Call work for almost everything.',
		'You are the voice interface. The Claude Code session is the brain.',
		'Your job is to relay the user\'s requests to work and speak the results.',
		'',
		'ONLY answer directly (without calling work) for:',
		'- Simple greetings ("hi", "hello")',
		'- Self-introduction ("who are you", "introduce yourself", "what can you do") — use the context above',
		'- Yes/no acknowledgments',
		'- Asking the user a clarifying question',
		'- get_current_time (current date/time)',
		'- Google Search (quick factual lookups)',
		`- ${inlineTools.map(t => t.name).join(', ')} — call these directly, not through work. Instant.`,
		'',
		'For EVERYTHING else, call work. This includes:',
		'- Tutorial ("tutorial", "walk me through", "show me what you can do") — delegate to work, which reads the full tutorial and walks through it step by step',
		'- Questions about the system, architecture, code, capabilities',
		'- Requests to do anything (write, read, change, create, delete, send)',
		'- Translation, research, analysis, explanations',
		'- Anything you\'re not 100% certain about',
		'',
		'TOOLS:',
		'- work: THE default tool. Call it for any non-trivial request.',
		'  Returns status "pending" — say "Working on it" and wait for the result.',
		'- get_task_status: Check if a background task is still running.',
		'- join_zoom: Join a Zoom meeting with computer audio (no screen sharing). Use when user says "join the zoom" or gives a Zoom ID.',
		'- join_gmeet: Join a Google Meet via browser with computer audio. Use when user says "join the meet" or gives a Meet code.',
		'- summon: Share screen via Zoom (desktop app). Use when user says "summon", "share my screen".',
		'- dismiss: Leave the current Zoom meeting. Use when user says "dismiss", "leave zoom", "end meeting", "leave the call".',
		'- For phone calls, meeting dial-in, or anything needing contacts/calendar context → use work (core handles it).',
		...inlineTools.map(t => `- ${t.name}: ${(t.description as string).split('.')[0]}. Instant.`),
		'',
		'CRITICAL RULES:',
		'- MEETING MODE: When the user is in a meeting (after join_zoom, join_gmeet, or summon), switch to passive mode. Only respond when explicitly addressed by name ("Sutando", "hey Sutando"). Ignore ambient conversation from other meeting participants. When someone says "bye" to other participants, do NOT disconnect — only disconnect if the user says "bye Sutando" or "disconnect". This prevents you from acting on overheard speech.',
		'- GOODBYE RULE: When the user says "goodbye", "bye", "see you later", "disconnect", "stop", or clearly ends the conversation, you MUST call the end_session tool IMMEDIATELY. Say a brief farewell and call end_session in the same turn. If you do not call end_session, the session stays open. This is mandatory — never just say goodbye without calling the tool. BUT in meeting mode, only respond to goodbyes directed at you specifically.',
		'- NEVER pretend you called a tool. NEVER say "done" without actually calling work.',
		'- For SIMPLE actions (press enter, clear input, select all), use press_key or type_text — do NOT use work for keystrokes.',
		'- If you KNOW the answer from your instructions or context, answer directly. Only delegate to work for questions you genuinely cannot answer.',
		'- MISSING CONTEXT: When the user references something you don\'t have context for ("the draft", "what we discussed", "type that", "send what I asked for"), ALWAYS delegate to work. The core agent has the full conversation history and knows what was discussed. Never guess or ask the user to repeat — just call work.',
		'- When in doubt, call work.',
		'',
		'VOICE RULES:',
		'- Keep responses to 2–3 sentences. You are talking, not writing.',
		'- Never read file contents or code aloud — summarize the outcome.',
		'- Focus on what changed or was found, not how it was done.',
		'- When relaying task results, be concrete: "I drafted the email and it\'s ready to review."',
		'- If the task agent asks a follow-up, relay it naturally.',
		'',
		'IMPORTANT:',
		'- For high-stakes or irreversible actions (sending email, payments, deleting files),',
		'  confirm with the user before executing unless they have given standing approval.',
		'- When background tasks are running, stay present and responsive.',
		'- You earn your usefulness by doing, not explaining.',
	].join('\n'),
	tools: [workTool, getTaskStatus, endSession, ...inlineTools],
	googleSearch: true,
	onEnter: async () => console.log(`${ts()} [Agent] Sutando ready`),
	onTurnCompleted: async (ctx, transcript) => {
		// Server-side goodbye detection — only check the last assistant message
		// to avoid false positives from injected context or old conversation
		const turns = ctx.getRecentTurns(2) as any[];
		const lastAssistant = turns.filter(t => t.role === 'model').pop();
		const lastText = (lastAssistant?.parts?.map((p: any) => p.text).join(' ') || '').toLowerCase();
		const goodbyePhrases = ['goodbye', 'bye bye', 'see you later', 'good night', 'ending the session', 'session ended'];
		const isGoodbye = goodbyePhrases.some(p => lastText.includes(p));
		if (isGoodbye) {
			console.log(`${ts()} [Agent] Goodbye detected in assistant response — closing client in 4s`);
			(ctx as any).sendJsonToClient?.({ type: 'session_end', reason: 'user_goodbye' });
			// Wait for session_end to arrive at client, then close the WS
			setTimeout(() => {
				try {
					const ct = (voiceSessionRef as any)?.clientTransport;
					ct?.client?.close(4000, 'goodbye');
				} catch {}
			}, 4000);
		}
	},
};

// =============================================================================
// Main
// =============================================================================

async function main() {
	assertMacOS();

	const session = new VoiceSession({
		sessionId: SESSION_ID,
		userId: 'user',
		apiKey: GEMINI_API_KEY,
		agents: [mainAgent],
		initialAgent: 'main',
		port: PORT,
		host: HOST,
		model: google('gemini-2.5-flash'),
		geminiModel: 'gemini-2.5-flash-native-audio-preview-12-2025',
		sttProvider: new GeminiBatchSTTProvider({ apiKey: GEMINI_API_KEY, model: 'gemini-3-flash-preview' }),
		speechConfig: { voiceName: 'Puck' },
		hooks: {
			onSessionStart: (e) => console.log(`${ts()} [Session] Started: ${e.sessionId}`),
			onSessionEnd: (e) => console.log(`${ts()} [Session] Ended: ${e.sessionId} (${e.reason})`),
			onToolCall: (e) => console.log(`${ts()} [Tool] ${e.toolName} (${e.execution})`),
			onToolResult: (e) => console.log(`${ts()} [Tool] result: ${e.toolCallId} (${e.status}, ${e.durationMs}ms)`),
			onSubagentStep: (e) => console.log(`${ts()} [Subagent] ${e.subagentName} #${e.stepNumber} [${e.toolCalls.join(',')}]`),
			onError: (e) => console.error(`${ts()} [Error] ${e.component}: ${e.error.message} (${e.severity})`),
		},
	});

	sessionRef = session;

	// Watch for results from the Claude Code session and deliver to user
	// Only delivers when a client is connected — otherwise keeps files queued
	// Watch for context drops (keyboard shortcut)
	// task-bridge always writes to tasks/ for sutando-core; also inject into Gemini if active
	startContextDropWatcher((content) => {
		if (session.sessionManager.isActive && session.clientConnected) {
			console.log(`${ts()} [ContextDrop] Injecting into Gemini conversation`);
			(session as any).transport.sendContent([
				{ role: 'user', text: `[System: The user just dropped context via keyboard shortcut. Acknowledge briefly that you received it, then call work if it requires action.]\n\n${content}` },
			], true);
		}
	});

	startResultWatcher((result) => {
		console.log(`${ts()} [TaskBridge] Delivering result to user`);
		setTimeout(() => {
			(session as any).transport.sendContent([
				{ role: 'user', text: `[System: Task completed. Briefly tell the user this result in one sentence:] ${result}` },
			], true);
		}, 1500);
	}, () => session.clientConnected);

	let lastLoggedIndex = 0;
	session.eventBus.subscribe('turn.end', () => {
		const items = session.conversationContext.items;
		for (const item of items.slice(lastLoggedIndex)) {
			if (item.role === 'user' || item.role === 'assistant') {
				console.log(`${ts()}   [${item.role}] ${item.content}`);
				logConversation(item.role, item.content);
			}
		}
		lastLoggedIndex = items.length;
	});

	const shutdown = async () => {
		console.log(`\n${ts()} Shutting down...`);
		await session.close('user_hangup');
		process.exit(0);
	};
	process.on('SIGINT', shutdown);
	process.on('SIGTERM', shutdown);
	process.on('uncaughtException', (err) => {
		console.error(`${ts()} [FATAL] uncaught exception (staying alive):`, err);
	});
	process.on('unhandledRejection', (err) => {
		console.error(`${ts()} [FATAL] unhandled rejection (staying alive):`, err);
	});

	voiceSessionRef = session;

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
			(session as any).transport.sendContent([{ role: 'user', text: `[System: The phone call just completed. Tell the user this result naturally.]\n\nCall transcript:\n${transcript}` }], true);
		} catch (err) { console.error(`${ts()} [CallResult] Error:`, err); }
	}, 2000);

	await session.start();

	// Keep process alive, log health, and auto-recover dead sessions.
	setInterval(() => {
		const mgr = (session as any).sessionManager;
		const state = mgr?.state ?? 'unknown';
		const clientConnected = session.clientConnected;
		console.log(`${ts()} [Health] state=${state} client=${clientConnected}`);
		// Auto-recover: if session is CLOSED but client is connected, trigger reconnect
		if (state === 'CLOSED' && clientConnected) {
			console.log(`${ts()} [Health] Dead session detected — triggering reconnect`);
			try {
				(session as any).handleClientConnected();
			} catch (err) {
				console.error(`${ts()} [Health] Reconnect failed:`, err);
			}
		}
	}, 30_000);

	console.log('============================================================');
	console.log('Sutando — Voice Interface');
	console.log('============================================================');
	console.log(`  Voice agent:   ws://localhost:${PORT}`);
	console.log(`  Workspace:     ${WORKSPACE_DIR}`);
	console.log(`  Session ID:    ${SESSION_ID}`);
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
	if ((err as NodeJS.ErrnoException).code === 'EADDRINUSE') {
		console.error(`\nError: port ${PORT} is already in use.`);
		console.error(`Kill the existing process: kill $(lsof -ti :${PORT})`);
		console.error('Then run pnpm start again.\n');
	} else {
		console.error('Fatal:', err);
	}
	process.exit(1);
});
