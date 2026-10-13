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
/** room.events.search: full-text over the room's messages, newest first; the server caps a page at 50. */
export const ROOM_SEARCH_DEFAULT_LIMIT = 10;
export const ROOM_SEARCH_MAX_LIMIT = 50;
/** A spoken answer needs the gist, not a pasted log. */
export const ROOM_READ_BODY_MAX = 400;
/** room.list refuses more than 50 per page (measured); the agent's rooms are a dozen or two, paged just in case. */
export const ROOM_INDEX_PAGE = 50;
export const ROOM_INDEX_MAX_PAGES = 4;
/** How long the room index is trusted before the next question refreshes it in the background. */
export const ROOM_INDEX_TTL_MS = 5 * 60_000;
/** The first fetch mints the token and lists cold (6.7 s measured on 2026-10-13); after that a list is well under a second. */
export const ROOM_INDEX_COLD_TIMEOUT_MS = 15_000;

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

const normName = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();

/**
 * A spoken room name against the rooms the agent is in: an exact name wins; else the rooms whose name
 * contains what was said (or the reverse) — one is it, several are ambiguous; none is not_found.
 * What was misheard ("Sutando Triaging") is not guessed here: the model sees the index and says the name again.
 */
export function pickRoom(query: string, rooms: RoomRef[]): RoomResolution {
	const q = normName(query);
	const exact = rooms.filter(r => normName(r.name) === q);
	if (exact.length === 1) return { ok: true, room: exact[0] };
	const pool = exact.length > 1 ? exact : rooms.filter(r => { const n = normName(r.name); return n.includes(q) || q.includes(n); });
	if (pool.length === 1) return { ok: true, room: pool[0] };
	if (pool.length === 0) return { ok: false, error: 'not_found' };
	return { ok: false, error: 'ambiguous', candidates: pool.slice(0, 6).map(r => r.name) };
}

/** Every room the agent is a member of (room.list, scope self), named rooms only; pages followed. */
export async function listRooms(client: McpClient, timeoutMs = ROOM_READ_CALL_TIMEOUT_MS): Promise<RoomRef[]> {
	const out: RoomRef[] = [];
	let cursor: string | undefined;
	for (let page = 0; page < ROOM_INDEX_MAX_PAGES; page++) {
		const res = await client.callTool('room.list', { scope: 'self', limit: ROOM_INDEX_PAGE, ...(cursor ? { cursor } : {}) }, timeoutMs);
		const payload = mcpPayload(res);
		if (res.isError || !payload) throw new Error(`room.list: ${String((payload as { code?: string } | null)?.code ?? 'failed')}`);
		for (const r of Array.isArray(payload.rooms) ? payload.rooms : []) {
			const room = r as { room_id?: unknown; name?: unknown };
			if (typeof room.room_id === 'string' && typeof room.name === 'string' && room.name.trim()) out.push({ room_id: room.room_id, name: room.name.trim() });
		}
		cursor = typeof payload.next_cursor === 'string' && payload.next_cursor ? payload.next_cursor : undefined;
		if (!cursor) break;
	}
	return out;
}

export interface RoomIndex {
	/** The rooms as last fetched (possibly stale); a stale index is refreshed in the background. */
	rooms(): RoomRef[];
	/** Fetch now; one fetch at a time. Rejects when the MCP cannot answer, keeping the last list. */
	refresh(): Promise<RoomRef[]>;
	/** When the list was last fetched, 0 before the first. */
	fetchedAt(): number;
}

/**
 * The rooms the agent can read, kept so the voice model sees their real names: a room name is often
 * misheard, and a name it can see it can say back exactly (2026-10-13 17:41: "Sutando Triaging").
 */
export function createRoomIndex(deps: { client: () => McpClient | null; log?: (m: string) => void; ttlMs?: number; now?: () => number }): RoomIndex {
	const ttl = deps.ttlMs ?? ROOM_INDEX_TTL_MS;
	const now = deps.now ?? Date.now;
	let rooms: RoomRef[] = [];
	let fetchedAt = 0;
	let inflight: Promise<RoomRef[]> | null = null;
	const refresh = (): Promise<RoomRef[]> => {
		if (inflight) return inflight;
		const client = deps.client();
		if (!client) return Promise.reject(new Error('not_connected'));
		inflight = listRooms(client, fetchedAt === 0 ? ROOM_INDEX_COLD_TIMEOUT_MS : ROOM_READ_CALL_TIMEOUT_MS).then(
			(list) => { rooms = list; fetchedAt = now(); inflight = null; deps.log?.(`[RoomIndex] ${list.length} rooms`); return list; },
			(err) => { inflight = null; deps.log?.(`[RoomIndex] refresh failed: ${err instanceof Error ? err.message : String(err)}`); throw err; },
		);
		return inflight;
	};
	return {
		rooms() {
			if (now() - fetchedAt > ttl) refresh().catch(() => {});
			return rooms;
		},
		refresh,
		fetchedAt: () => fetchedAt,
	};
}

/** The prompt line that carries the index; nothing before the first fetch. */
export function roomIndexContextLines(index: RoomIndex): string[] {
	const names = index.rooms().map(r => r.name);
	if (!names.length) return [];
	return [`ROOMS: AG2 Space rooms you can read with read_room: ${names.join(' | ')}. A room name you hear is often misheard: when the user names a room, pass the one of these it clearly is, spelled exactly like this; ask which only when none clearly is.`];
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
	index: RoomIndex;
	log?: (m: string) => void;
}

export type ReadRoomResult =
	| { ok: true; room: string; messages: RoomMessageLine[]; ms: number }
	/** not_found carries every room the agent can read, so the model can say the right name next. */
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

/** A spoken name against the index; a miss refreshes it once (the room may be newly joined) before giving up. */
async function resolveRoom(query: string, index: RoomIndex): Promise<RoomResolution & { rooms?: string[] }> {
	const first = pickRoom(query, index.rooms());
	if (first.ok || first.error === 'ambiguous') return first;
	const fresh = await index.refresh().catch(() => index.rooms());
	const again = pickRoom(query, fresh);
	return again.ok ? again : { ...again, rooms: fresh.map(r => r.name) };
}

/** The room an argument names: the session's room when none, an id as given, else a name against the index. */
async function targetRoom(name: string | undefined, deps: ReadRoomDeps): Promise<{ ok: true; room: RoomRef } | { ok: false; error: string; candidates?: string[]; rooms?: string[] }> {
	const query = (name ?? '').trim();
	if (!query) {
		const cur = deps.currentRoom();
		if (!cur) return { ok: false, error: 'no_room' };
		return { ok: true, room: { room_id: cur.id, name: cur.name ?? cur.id } };
	}
	if (MATRIX_ROOM_ID.test(query)) return { ok: true, room: { room_id: query, name: query } };
	return resolveRoom(query, deps.index);
}

/** One room action that reads: a thrown error is "timeout" or "unavailable"; an error answer is its code. */
async function roomRead(client: McpClient, room: RoomRef, action: string, args: Record<string, unknown>, deps: ReadRoomDeps): Promise<{ ok: true; result: Record<string, unknown> } | { ok: false; error: string }> {
	try {
		const res = await client.callTool('room.action.read', { room_id: room.room_id, action, arguments: args });
		const payload = mcpPayload(res);
		if (res.isError || !payload) return { ok: false, error: String((payload as { code?: string } | null)?.code ?? 'read_failed') };
		return { ok: true, result: (payload.result ?? payload) as Record<string, unknown> };
	} catch (err) {
		const why = err instanceof Error ? err.message : String(err);
		deps.log?.(`[RoomRead] ${action} failed: ${why}`);
		return { ok: false, error: /timed out/.test(why) ? 'timeout' : 'unavailable' };
	}
}

export async function readRoom(args: { room?: string; limit?: number; around?: string }, deps: ReadRoomDeps): Promise<ReadRoomResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const limit = Math.min(ROOM_READ_MAX_LIMIT, Math.max(1, Math.floor(args.limit ?? ROOM_READ_DEFAULT_LIMIT)));
	const target = await targetRoom(args.room, deps);
	if (!target.ok) return target;
	const around = (args.around ?? '').trim();
	const read = await roomRead(client, target.room, 'room.context.read', { limit, ...(around ? { around_event_id: around } : {}) }, deps);
	if (!read.ok) return read;
	const messages = toMessageLines(Array.isArray(read.result.messages) ? read.result.messages : []);
	return { ok: true, room: target.room.name, messages, ms: Date.now() - t0 };
}

export interface RoomHit { at: string; from: string; text: string; event_id: string; thread?: boolean }

/** Search hits (newest first) → lines the model can retell, each with the id read_room can read around. */
export function toHitLines(hits: unknown[], tz?: string): RoomHit[] {
	const out: RoomHit[] = [];
	for (const h of hits) {
		if (!h || typeof h !== 'object') continue;
		const hit = h as { event_id?: string; sender?: string; snippet?: string; origin_server_ts?: number | null; thread_root?: string | null };
		if (typeof hit.event_id !== 'string' || typeof hit.snippet !== 'string' || !hit.snippet.trim()) continue;
		const from = (hit.sender ?? '?').replace(/^@/, '').replace(/:.*$/, '');
		const at = typeof hit.origin_server_ts === 'number'
			? new Date(hit.origin_server_ts).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
			: '';
		const text = hit.snippet.replace(/\s+/g, ' ').trim();
		out.push({ at, from, text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text, event_id: hit.event_id, ...(hit.thread_root ? { thread: true } : {}) });
	}
	return out;
}

export type SearchRoomResult =
	| { ok: true; room: string; query: string; hits: RoomHit[]; ms: number }
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

/**
 * Full-text search of a room's messages through room.events.search: what was said about a PR, a topic
 * or a person, however far back (2026-10-13 20:19: the #5473 review was 40 messages back, out of read_room's reach).
 */
export async function searchRoom(args: { room?: string; query: string; limit?: number }, deps: ReadRoomDeps): Promise<SearchRoomResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const query = (args.query ?? '').trim().slice(0, 200);
	if (!query) return { ok: false, error: 'no_query' };
	const limit = Math.min(ROOM_SEARCH_MAX_LIMIT, Math.max(1, Math.floor(args.limit ?? ROOM_SEARCH_DEFAULT_LIMIT)));
	const target = await targetRoom(args.room, deps);
	if (!target.ok) return target;
	const found = await roomRead(client, target.room, 'room.events.search', { query, limit }, deps);
	if (!found.ok) return found;
	const hits = toHitLines(Array.isArray(found.result.results) ? found.result.results : []);
	return { ok: true, room: target.room.name, query, hits, ms: Date.now() - t0 };
}

export function createReadRoomTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'read_room',
		description:
			'Read the recent messages of an AG2 Space room, right now (about a second). ' +
			'Call it when the user asks what is new in a room, what was said there lately, or whether someone replied or posted something there. ' +
			'For what was said about a particular PR, topic or person — which may be far back — call search_room instead, then read_room with `around` = a hit\'s event_id to read the discussion around it. ' +
			'A room\'s messages and who can read it change at any time: call read_room every time the user asks about a room, even if you answered about that room earlier or a task result said something about it — never answer from an earlier answer. ' +
			'Pass `room` = the room name as spoken; leave it out for the room this session is in. ' +
			'Returns {ok:true, room, messages:[{at, from, text}]} oldest first — answer from them in a sentence or two; do not read them all out. ' +
			'Errors: "not_found" with rooms (every room you can read) — the name you passed is not one of them; room names are often misheard, so if one of rooms is clearly the room the user meant, call read_room again with that exact name, and ask which only when none clearly is; ' +
			'"ambiguous" with candidates — ask which one; "no_room" — ask which room; "not_connected", "timeout" or "unavailable" — say you cannot read the room right now.',
		parameters: z.object({
			room: z.string().optional().describe('The room name as spoken, e.g. "Sutando Triage". Omit for the current room.'),
			limit: z.number().int().min(1).max(ROOM_READ_MAX_LIMIT).optional().describe(`How many messages (default ${ROOM_READ_DEFAULT_LIMIT}).`),
			around: z.string().optional().describe('A search hit\'s event_id: read the messages around that one instead of the latest.'),
		}),
		execution: 'inline',
		// A miss while the index is still cold refetches it (cold) and then reads.
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + ROOM_READ_CALL_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as { room?: string; limit?: number; around?: string };
			const r = await readRoom(a, deps);
			deps.log?.(`[RoomRead] ${a.room ? `"${a.room}"` : '(current room)'}${a.around ? ' around a hit' : ''} → ${r.ok ? `${r.room}: ${r.messages.length} messages in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export function createSearchRoomTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'search_room',
		description:
			'Search an AG2 Space room\'s messages for words, however far back, right now (a second or two). ' +
			'Call it when the user asks what was said in a room about a particular PR, issue, topic or person ("what was the review of 5473 in Sutando Triage", "what did Chi say about the guest line"), or about something from earlier than the latest messages — never answer such a question from memory or from an earlier answer; search every time. ' +
			'Pass `query` = the words to find (a PR number, a name, a phrase), `room` = the room name as spoken or as listed in ROOMS; leave `room` out for the room this session is in. ' +
			'Returns {ok:true, room, hits:[{at, from, text, event_id}]} newest first, each a snippet around the match — answer from them in a sentence or two; to read the whole discussion around a hit, call read_room with `around` = its event_id. ' +
			'No hits means nothing in that room mentions it; say so. ' +
			'Errors: "not_found" with rooms (every room you can read) — the name you passed is not one of them; room names are often misheard, so if one of rooms is clearly the room the user meant, call search_room again with that exact name, and ask which only when none clearly is; ' +
			'"ambiguous" with candidates — ask which one; "no_room" — ask which room; "not_connected", "timeout" or "unavailable" — say you cannot search the room right now.',
		parameters: z.object({
			query: z.string().min(1).max(200).describe('The words to find, e.g. "5473" or "guest line".'),
			room: z.string().optional().describe('The room name, e.g. "Sutando Triage". Omit for the current room.'),
			limit: z.number().int().min(1).max(ROOM_SEARCH_MAX_LIMIT).optional().describe(`How many hits (default ${ROOM_SEARCH_DEFAULT_LIMIT}).`),
		}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + ROOM_READ_CALL_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as { room?: string; query: string; limit?: number };
			const r = await searchRoom(a, deps);
			deps.log?.(`[RoomRead] search ${a.room ? `"${a.room}"` : '(current room)'} for "${a.query}" → ${r.ok ? `${r.room}: ${r.hits.length} hits in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

/** list_rooms: the rooms the agent can read, fetched now — for "which rooms are you in", and to refresh the index. */
export function createListRoomsTool(deps: { index: RoomIndex; log?: (m: string) => void }): ToolDefinition {
	return {
		name: 'list_rooms',
		description:
			'List the AG2 Space rooms you can read, right now. Call it when the user asks which rooms you are in or can read, ' +
			'or when a room they name is not in the ROOMS line and may be newly joined. Returns {ok:true, rooms:[names]}; "unavailable" when the list cannot be fetched.',
		parameters: z.object({}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + 1000,
		async execute() {
			try {
				const rooms = (await deps.index.refresh()).map(r => r.name);
				deps.log?.(`[RoomRead] list_rooms: ${rooms.length} rooms`);
				return { ok: true, rooms };
			} catch (err) {
				deps.log?.(`[RoomRead] list_rooms failed: ${err instanceof Error ? err.message : String(err)}`);
				return { ok: false, error: 'unavailable', rooms: deps.index.rooms().map(r => r.name) };
			}
		},
	};
}

/** The prompt rule that goes with the tool; present only when the tool is declared. */
export const ROOM_READ_PROMPT_RULE = '- ROOM MESSAGES: a question about an AG2 Space room is answered from the room, every time, never from memory or an earlier answer — what a room holds and who can read it change, so an earlier answer or task result about a room is never the answer now. "What\'s new in <room>", "did <person> reply there" → read_room. "What was said about <PR/topic/person> in <room>", "what was the review of <PR> there", anything that may be earlier than the latest messages → search_room, then read_room with `around` for the discussion. Never submit work just to read or search a room. Pass the room name as it appears in the ROOMS line, not as it was heard; a "not_found" lists the rooms — pick the one meant and call again.';
