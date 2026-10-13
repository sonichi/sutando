// read_room: the voice model reads a room's recent messages through the AG2 Space MCP,
// inline, instead of submitting work for it. The voice agent is the MCP client: it starts
// the same `ag2-space` stdio server the core has registered (the credential proxy) and
// calls room.list / room.action.read on it. Gemini only ever sees an ordinary function.

import { spawn, type ChildProcess } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { expandHome } from '../../src/util_paths.js';
import type { VoiceSessionRoom } from './session-context.js';

/** The registration name both the desktop app and skills/ag2-space-mcp write. */
export const AG2_MCP_SERVER_NAME = 'ag2-space';
/** First call mints a token (~2 s measured); warm reads ~0.2 s. */
export const ROOM_READ_CALL_TIMEOUT_MS = 6_000;
export const ROOM_READ_DEFAULT_LIMIT = 15;
export const ROOM_READ_MAX_LIMIT = 40;
/** A spoken answer needs the gist, not a pasted log. */
export const ROOM_READ_BODY_MAX = 400;

export interface McpStdioEntry { command: string; args?: string[]; env?: Record<string, string> }

/** The core's `ag2-space` MCP entry from its Claude config, or null when this install has none. */
export function findAg2SpaceMcpEntry(env: NodeJS.ProcessEnv = process.env): McpStdioEntry | null {
	const ccd = env.CLAUDE_CONFIG_DIR;
	const file = ccd ? join(expandHome(ccd), '.claude.json') : join(homedir(), '.claude.json');
	try {
		const cfg = JSON.parse(readFileSync(file, 'utf-8')) as { mcpServers?: Record<string, unknown> };
		const e = cfg.mcpServers?.[AG2_MCP_SERVER_NAME] as { type?: string; command?: unknown; args?: unknown; env?: unknown } | undefined;
		if (!e || (e.type && e.type !== 'stdio') || typeof e.command !== 'string' || !e.command) return null;
		const args = Array.isArray(e.args) ? e.args.filter((a): a is string => typeof a === 'string') : [];
		const envOut: Record<string, string> = {};
		if (e.env && typeof e.env === 'object') for (const [k, v] of Object.entries(e.env)) if (typeof v === 'string') envOut[k] = v;
		return { command: e.command, args, env: envOut };
	} catch {
		return null;
	}
}

export interface McpToolResult { isError?: boolean; structuredContent?: unknown; content?: Array<{ type?: string; text?: string }> }

export interface McpClient {
	callTool(name: string, args: Record<string, unknown>, timeoutMs?: number): Promise<McpToolResult>;
	close(): void;
}

/** A newline-delimited JSON-RPC MCP client over a child's stdio. Started on first use,
 *  started again after the child exits. */
export function createMcpStdioClient(entry: McpStdioEntry, opts: { log?: (m: string) => void; spawnFn?: typeof spawn } = {}): McpClient {
	const log = opts.log ?? (() => {});
	const spawnFn = opts.spawnFn ?? spawn;
	let child: ChildProcess | null = null;
	let ready: Promise<void> | null = null;
	let nextId = 0;
	const pending = new Map<number, { resolve: (m: Record<string, unknown>) => void; reject: (e: Error) => void }>();

	const failAll = (why: string) => {
		for (const p of pending.values()) p.reject(new Error(why));
		pending.clear();
	};

	const send = (method: string, params: unknown, timeoutMs: number): Promise<Record<string, unknown>> => {
		const c = child;
		if (!c?.stdin?.writable) return Promise.reject(new Error('ag2-space MCP is not running'));
		const id = ++nextId;
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out after ${timeoutMs}ms`)); }, timeoutMs);
			pending.set(id, {
				resolve: (m) => { clearTimeout(timer); resolve(m); },
				reject: (e) => { clearTimeout(timer); reject(e); },
			});
			c.stdin!.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
		});
	};

	const start = (timeoutMs: number): Promise<void> => {
		if (ready) return ready;
		const c = spawnFn(entry.command, entry.args ?? [], {
			env: { HOME: process.env.HOME ?? '', PATH: process.env.PATH ?? '', TMPDIR: process.env.TMPDIR ?? '', ...entry.env },
			stdio: ['pipe', 'pipe', 'ignore'],
		});
		child = c;
		createInterface({ input: c.stdout! }).on('line', (line) => {
			let msg: Record<string, unknown>;
			try { msg = JSON.parse(line); } catch { return; }
			const p = typeof msg.id === 'number' ? pending.get(msg.id) : undefined;
			if (!p) return;
			pending.delete(msg.id as number);
			p.resolve(msg);
		});
		const gone = (why: string) => {
			if (child !== c) return;
			child = null;
			ready = null;
			failAll(why);
		};
		c.on('exit', (code) => gone(`ag2-space MCP exited (${code})`));
		c.on('error', (err) => { log(`[RoomRead] MCP start failed: ${err.message}`); gone(`ag2-space MCP failed to start: ${err.message}`); });
		ready = (async () => {
			await send('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'sutando-voice', version: '1' } }, timeoutMs);
			c.stdin!.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
		})();
		ready.catch(() => { if (child === c) { c.kill(); child = null; ready = null; } });
		return ready;
	};

	return {
		async callTool(name, args, timeoutMs = ROOM_READ_CALL_TIMEOUT_MS) {
			await start(timeoutMs);
			const msg = await send('tools/call', { name, arguments: args }, timeoutMs);
			if (msg.error) throw new Error(`${name}: ${(msg.error as { message?: string }).message ?? 'error'}`);
			return (msg.result ?? {}) as McpToolResult;
		},
		close() {
			const c = child;
			child = null;
			ready = null;
			failAll('closed');
			c?.kill();
		},
	};
}

/** The JSON payload of an MCP result: structuredContent, else the last text part that parses. */
export function mcpPayload(r: McpToolResult): Record<string, unknown> | null {
	if (r.structuredContent && typeof r.structuredContent === 'object') return r.structuredContent as Record<string, unknown>;
	for (const part of [...(r.content ?? [])].reverse()) {
		if (typeof part.text !== 'string') continue;
		try { const v = JSON.parse(part.text); if (v && typeof v === 'object') return v as Record<string, unknown>; } catch { /* prose part */ }
	}
	return null;
}

const MATRIX_ROOM_ID = /^![A-Za-z0-9._=\-/+]+:[A-Za-z0-9.\-]+(:\d+)?$/;

export interface RoomRef { room_id: string; name: string }

export type RoomResolution = { ok: true; room: RoomRef } | { ok: false; error: 'not_found' | 'ambiguous'; candidates?: string[] };

/** A spoken room name to one room: an exact name match wins, else a single candidate; more is ambiguous. */
export function pickRoom(query: string, rooms: RoomRef[]): RoomResolution {
	const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();
	const q = norm(query);
	const exact = rooms.filter(r => norm(r.name) === q);
	if (exact.length === 1) return { ok: true, room: exact[0] };
	const pool = exact.length > 1 ? exact : rooms;
	if (pool.length === 1) return { ok: true, room: pool[0] };
	if (pool.length === 0) return { ok: false, error: 'not_found' };
	return { ok: false, error: 'ambiguous', candidates: pool.slice(0, 6).map(r => r.name) };
}

export interface RoomMessageLine { at: string; from: string; text: string; reply?: boolean; thread?: boolean }

/** Server messages (newest first) → oldest-first lines a voice model can retell. */
export function toMessageLines(messages: unknown[], tz?: string): RoomMessageLine[] {
	const out: RoomMessageLine[] = [];
	for (const m of messages) {
		if (!m || typeof m !== 'object') continue;
		const msg = m as { sender?: string; body?: string; ts?: number | null; reply_to_event?: string | null; thread_root?: string | null };
		if (typeof msg.body !== 'string' || !msg.body.trim()) continue;
		const from = (msg.sender ?? '?').replace(/^@/, '').replace(/:.*$/, '');
		const at = typeof msg.ts === 'number'
			? new Date(msg.ts).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
			: '';
		const body = msg.body.replace(/\s+/g, ' ').trim();
		out.push({
			at, from,
			text: body.length > ROOM_READ_BODY_MAX ? `${body.slice(0, ROOM_READ_BODY_MAX)}…` : body,
			...(msg.reply_to_event ? { reply: true } : {}),
			...(msg.thread_root ? { thread: true } : {}),
		});
	}
	return out.reverse();
}

export interface ReadRoomDeps {
	client: () => McpClient | null;
	currentRoom: () => VoiceSessionRoom | null;
	log?: (m: string) => void;
}

export type ReadRoomResult =
	| { ok: true; room: string; messages: RoomMessageLine[]; ms: number }
	| { ok: false; error: string; candidates?: string[] };

export async function readRoom(args: { room?: string; limit?: number }, deps: ReadRoomDeps): Promise<ReadRoomResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const limit = Math.min(ROOM_READ_MAX_LIMIT, Math.max(1, Math.floor(args.limit ?? ROOM_READ_DEFAULT_LIMIT)));
	const query = (args.room ?? '').trim();
	let room: RoomRef;
	try {
		if (!query) {
			const cur = deps.currentRoom();
			if (!cur) return { ok: false, error: 'no_room' };
			room = { room_id: cur.id, name: cur.name ?? cur.id };
		} else if (MATRIX_ROOM_ID.test(query)) {
			room = { room_id: query, name: query };
		} else {
			const listed = mcpPayload(await client.callTool('room.list', { query, limit: 10 }));
			const rooms = (Array.isArray(listed?.rooms) ? listed!.rooms : [])
				.filter((r): r is RoomRef => !!r && typeof (r as RoomRef).room_id === 'string')
				.map(r => ({ room_id: r.room_id, name: typeof r.name === 'string' && r.name ? r.name : r.room_id }));
			const picked = pickRoom(query, rooms);
			if (!picked.ok) return picked;
			room = picked.room;
		}
		const res = await client.callTool('room.action.read', { room_id: room.room_id, action: 'room.context.read', arguments: { limit } });
		const payload = mcpPayload(res);
		if (res.isError || !payload) return { ok: false, error: String((payload as { code?: string } | null)?.code ?? 'read_failed') };
		const result = (payload.result ?? payload) as { messages?: unknown[] };
		const messages = toMessageLines(Array.isArray(result.messages) ? result.messages : []);
		return { ok: true, room: room.name, messages, ms: Date.now() - t0 };
	} catch (err) {
		const why = err instanceof Error ? err.message : String(err);
		deps.log?.(`[RoomRead] failed: ${why}`);
		return { ok: false, error: /timed out/.test(why) ? 'timeout' : 'unavailable' };
	}
}

export function createReadRoomTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'read_room',
		description:
			'Read the recent messages of an AG2 Space room, right now (about a second). ' +
			'Call it when the user asks what was said in a room, what is new there, or whether someone replied or posted something there. ' +
			'A room\'s messages and who can read it change at any time: call read_room every time the user asks about a room, even if you answered about that room earlier or a task result said something about it — never answer from an earlier answer. ' +
			'Pass `room` = the room name as spoken; leave it out for the room this session is in. ' +
			'Returns {ok:true, room, messages:[{at, from, text}]} oldest first — answer from them in a sentence or two; do not read them all out. ' +
			'Errors: "ambiguous" with candidates — ask which one; "not_found" — say there is no room by that name; ' +
			'"no_room" — ask which room; "not_connected", "timeout" or "unavailable" — say you cannot read the room right now.',
		parameters: z.object({
			room: z.string().optional().describe('The room name as spoken, e.g. "Sutando Triage". Omit for the current room.'),
			limit: z.number().int().min(1).max(ROOM_READ_MAX_LIMIT).optional().describe(`How many recent messages (default ${ROOM_READ_DEFAULT_LIMIT}).`),
		}),
		execution: 'inline',
		timeout: ROOM_READ_CALL_TIMEOUT_MS * 2 + 1000,
		async execute(args) {
			const a = (args ?? {}) as { room?: string; limit?: number };
			const r = await readRoom(a, deps);
			deps.log?.(`[RoomRead] ${a.room ? `"${a.room}"` : '(current room)'} → ${r.ok ? `${r.room}: ${r.messages.length} messages in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

/** The prompt rule that goes with the tool; present only when the tool is declared. */
export const ROOM_READ_PROMPT_RULE = '- ROOM MESSAGES: a question about what was said in an AG2 Space room ("what\'s new in <room>", "did <person> reply in <room>", "what are they discussing there") → call read_room and answer from what it returns, every time — what a room holds and who can read it change, so an earlier answer or task result about a room is never the answer now; never submit work just to read a room.';
