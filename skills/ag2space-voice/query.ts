// query_messages: a question over every room at once ("my latest message", "how many new messages"),
// answered by counting. No read marker is visible to the agent, so "new" is since the last full check.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { INBOX_HIDDEN_TEXT, fetchInbox, ownerInboxSupported, toInboxItems, type InboxWhose } from './inbox.js';
import { statusPath } from '../../src/workspace_default.js';
import { listRooms, roomRead, targetRoom, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS, ROOM_READ_BODY_MAX, ROOM_READ_CALL_TIMEOUT_MS, type McpClient, type ReadRoomDeps, type RoomRef } from './room-read.js';

export const QUERY_DEFAULT_LATEST = 5;
export const QUERY_MAX_LATEST = 20;
/** room.context.read pages newest first (up to 1000 a page); a room busier than this in the window is "at least". */
export const QUERY_ROOM_PAGE = 200;
export const QUERY_ROOM_MAX_PAGES = 2;
export const QUERY_ROOM_CONCURRENCY = 6;
/** A busy room's page of 200 is 0.2–0.45 MB (measured 2026-10-16); after a timeout it is read again in pages this size. */
export const QUERY_ROOM_SMALL_PAGE = 50;
/** Asked again, a room's newer messages are read in pages this size: they are few, and a page of 200 costs seconds. */
export const QUERY_ROOM_NEWER_PAGE = 20;
/** What a room read already returned is reused this long: a question asked again reads only what is newer. */
export const QUERY_ROOM_CACHE_TTL_MS = 10 * 60_000;
/** How many groups a result names, in rank order; `groups_total` says how many there were. */
export const QUERY_MAX_GROUPS = 10;
/** A word over every room goes to the server's full-text search instead (space.search.messages): 50 a page. */
export const QUERY_SEARCH_PAGE = 50;
export const QUERY_SEARCH_MAX_PAGES = 4;
/** "new" with no full check before in this voice process: the last day. */
export const QUERY_FIRST_WINDOW_MS = 24 * 3_600_000;
/** All reads stop starting after this; what was not read is reported, and the tool's own timeout is longer. */
export const QUERY_BUDGET_MS = 30_000;

export type QuerySince = 'last_check' | 'hour' | 'today' | 'day' | 'week';
export type QueryScope = 'to_me' | 'all';
export type QueryGroupBy = 'room' | 'sender' | 'hour' | 'day';

export interface QueryHit { at: string; room: string; from: string; text: string; event_id: string; why?: string }

export type QueryResult =
	| {
		ok: true; scope: QueryScope; since: string; count: number; complete: boolean; at_least: boolean;
		group_by: QueryGroupBy; rank: 'most' | 'least'; groups: Array<{ key: string; count: number }>; groups_total: number;
		distinct_rooms: number; distinct_senders: number; latest: QueryHit[]; oldest?: QueryHit;
		rooms_read?: number; rooms_unread?: number; unread_rooms?: string[]; partial_rooms?: string[]; rooms_skipped?: number;
		whose?: InboxWhose; owner_error?: string; ms: number;
	}
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

const fmt = (ms: number, tz?: string) => new Date(ms).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' });
const short = (id: string) => id.replace(/^@/, '').replace(/:.*$/, '');
const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();

/** When "new" last meant now, per scope and owner, during this process; never moves back. */
const lastChecked = new Map<string, number>();
/** What earlier calls left behind: written when a call ends (the voice agent's post-processing), read once at first use. */
let heard: Map<string, number> | null = null;
let heardPath: string | null = null;
const heardFile = () => heardPath ?? statusPath('voice-query-heard.json');

/** Tests: nothing checked, and the call-end record kept in `path`. */
export function resetLastChecked(path: string | null = null): void { lastChecked.clear(); heard = null; heardPath = path; }

function readHeard(): Map<string, number> {
	if (heard) return heard;
	heard = new Map();
	try {
		for (const [k, v] of Object.entries(JSON.parse(readFileSync(heardFile(), 'utf-8')) as Record<string, unknown>)) {
			if (typeof v === 'number' && Number.isFinite(v)) heard.set(k, v);
		}
	} catch { /* no call has ended yet, or unreadable: start from the last day */ }
	return heard;
}

/** Post-processing for a call that ended: what this process checked becomes "heard", for the next call and a restart. */
export function recordCallEnded(): void {
	const merged = readHeard();
	let changed = false;
	for (const [k, v] of lastChecked) if (v > (merged.get(k) ?? 0)) { merged.set(k, v); changed = true; }
	if (!changed) return;
	const path = heardFile();
	mkdirSync(dirname(path), { recursive: true });
	const tmp = `${path}.${process.pid}.tmp`;
	writeFileSync(tmp, JSON.stringify(Object.fromEntries(merged)));
	renameSync(tmp, path);
}

/** The start of the window a `since` names; `key` is what "new" is kept for (the scope, and whose messages). */
export function windowStart(since: QuerySince, key: string, now: number): number {
	switch (since) {
		case 'hour': return now - 3_600_000;
		case 'day': return now - 86_400_000;
		case 'week': return now - 7 * 86_400_000;
		case 'today': { const d = new Date(now); d.setHours(0, 0, 0, 0); return d.getTime(); }
		default: return lastChecked.get(key) ?? readHeard().get(key) ?? now - QUERY_FIRST_WINDOW_MS;
	}
}

interface Msg { ts: number; room: string; from: string; fromId: string; body: string; event_id: string; why?: string; mentions?: string[]; replyTo?: string; hidden?: boolean }

/** Why a room message is addressed to `owner`, by the rule the server's inbox uses: named in its mentions, or (with none) its id in the body; or a reply to the owner. */
export function addressedTo(m: Pick<Msg, 'body' | 'mentions' | 'replyTo'>, owner: string): 'mention' | 'reply' | null {
	if (m.mentions?.length ? m.mentions.includes(owner) : m.body.includes(owner)) return 'mention';
	if (m.replyTo === owner) return 'reply';
	return null;
}

type Page = { ok: true; msgs: Msg[]; cursor: string | null; older: boolean; size: number } | { ok: false; error: string };

/** One page of a room, newest first; messages older than `floor` only mark that the page reached past it. */
async function readPage(client: McpClient, room: RoomRef, limit: number, before: string | null, floor: number, deps: ReadRoomDeps): Promise<Page> {
	const read = await roomRead(client, room, 'room.context.read', { limit, ...(before ? { before } : {}) }, deps, ROOM_COMMONS_TIMEOUT_MS);
	if (!read.ok) return read;
	const list = Array.isArray(read.result.messages) ? read.result.messages : [];
	const msgs: Msg[] = [];
	let older = false;
	for (const m of list) {
		const x = m as { sender?: unknown; body?: unknown; ts?: unknown; event_id?: unknown; mentions?: unknown; reply_to_sender?: unknown };
		const ts = Number(x.ts);
		if (!Number.isFinite(ts)) continue;
		if (ts < floor) { older = true; continue; }
		if (typeof x.body !== 'string' || !x.body.trim() || typeof x.event_id !== 'string') continue;
		const sender = typeof x.sender === 'string' ? x.sender : '';
		const mentions = Array.isArray(x.mentions) ? x.mentions.filter((u): u is string => typeof u === 'string') : undefined;
		const replyTo = typeof x.reply_to_sender === 'string' ? x.reply_to_sender : undefined;
		msgs.push({ ts, room: room.name, from: short(sender || '?'), fromId: sender, body: x.body, event_id: x.event_id, ...(mentions ? { mentions } : {}), ...(replyTo ? { replyTo } : {}) });
	}
	const cursor = typeof read.result.cursor === 'string' && read.result.cursor ? read.result.cursor : null;
	return { ok: true, msgs, cursor, older, size: list.length };
}

/** What a room read returned: every message from `from` up to when it was read (`at`), newest first. */
interface RoomCache { msgs: Msg[]; from: number; at: number }
const roomCache = new WeakMap<McpClient, Map<string, RoomCache>>();

/** Pages of a room back to `floor`, or until a message the cache already holds (`known`); a page that times out is read again smaller. */
async function pagesBack(client: McpClient, room: RoomRef, floor: number, known: Set<string> | null, deadline: number, deps: ReadRoomDeps): Promise<{ ok: true; msgs: Msg[]; reached: boolean } | { ok: false }> {
	const msgs: Msg[] = [];
	let before: string | null = null;
	let limit = known ? QUERY_ROOM_NEWER_PAGE : QUERY_ROOM_PAGE;
	// The same number of messages at most, whichever page size the room ends up read in.
	for (let fetched = 0; fetched < QUERY_ROOM_PAGE * QUERY_ROOM_MAX_PAGES;) {
		const first = fetched === 0;
		if (Date.now() > deadline) return first ? { ok: false } : { ok: true, msgs, reached: false };
		let page = await readPage(client, room, limit, before, floor, deps);
		// A read is safe to repeat: once more after a failed call (an upstream 502); after a timeout, in smaller pages, if there is time.
		if (!page.ok && page.error === 'unavailable' && Date.now() <= deadline) page = await readPage(client, room, limit, before, floor, deps);
		if (!page.ok && page.error === 'timeout' && limit > QUERY_ROOM_SMALL_PAGE && Date.now() + ROOM_COMMONS_TIMEOUT_MS <= deadline) {
			limit = QUERY_ROOM_SMALL_PAGE;
			page = await readPage(client, room, limit, before, floor, deps);
		}
		if (!page.ok) {
			deps.log?.(`[RoomQuery] ${room.name}: ${page.error}`);
			// What the earlier pages returned still counts; only the rest of the window is missing.
			return first ? { ok: false } : { ok: true, msgs, reached: false };
		}
		let overlap = false;
		for (const m of page.msgs) { if (known?.has(m.event_id)) overlap = true; else msgs.push(m); }
		if (overlap || page.older || !page.cursor || page.size < limit) return { ok: true, msgs, reached: true };
		fetched += page.size;
		before = page.cursor;
	}
	return { ok: true, msgs, reached: false };
}

/** One room's messages since `start`, newest first; `full` when it could not be read back to `start` (pages, budget or a failure). */
async function roomSince(client: McpClient, room: RoomRef, start: number, deadline: number, deps: ReadRoomDeps): Promise<{ ok: true; msgs: Msg[]; full: boolean } | { ok: false }> {
	const byRoom = roomCache.get(client) ?? new Map<string, RoomCache>();
	roomCache.set(client, byRoom);
	const cached = byRoom.get(room.room_id);
	const at = Date.now();
	if (cached && cached.from <= start && at - cached.at < QUERY_ROOM_CACHE_TTL_MS) {
		// Asked again: only what came after the cached read; the rest is what that read returned.
		const got = await pagesBack(client, room, start, new Set(cached.msgs.map(m => m.event_id)), deadline, deps);
		if (got.ok && got.reached) {
			const msgs = [...got.msgs, ...cached.msgs];
			byRoom.set(room.room_id, { msgs, from: cached.from, at });
			return { ok: true, msgs: msgs.filter(m => m.ts >= start), full: false };
		}
		// Too much new to reach the cached part: what was read now, cut short.
		byRoom.delete(room.room_id);
		return got.ok ? { ok: true, msgs: got.msgs, full: true } : got;
	}
	const got = await pagesBack(client, room, start, null, deadline, deps);
	if (!got.ok) return got;
	// A read cut short still holds everything from its oldest message on.
	const from = got.reached ? start : got.msgs.length ? got.msgs[got.msgs.length - 1].ts : at;
	byRoom.set(room.room_id, { msgs: got.msgs, from, at });
	return { ok: true, msgs: got.msgs, full: !got.reached };
}

/** space.search.messages since `start`, newest first, paged; `full` when the pages or the budget ran out with more to come. */
async function searchSince(client: McpClient, query: string, start: number, deadline: number, room: RoomRef | null, names: Map<string, string>, deps: ReadRoomDeps): Promise<{ ok: true; msgs: Msg[]; full: boolean } | { ok: false; error: string }> {
	const msgs: Msg[] = [];
	let cursor: string | null = null;
	for (let page = 0; page < QUERY_SEARCH_MAX_PAGES; page++) {
		if (page > 0 && Date.now() > deadline) return { ok: true, msgs, full: true };
		const read = await roomRead(client, null, 'space.search.messages', { query, limit: QUERY_SEARCH_PAGE, kind: 'messages', after: new Date(start).toISOString(), ...(room ? { room_ids: [room.room_id] } : {}), ...(cursor ? { cursor } : {}) }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		for (const h of Array.isArray(read.result.results) ? read.result.results : []) {
			const x = h as { room_id?: unknown; event_id?: unknown; sender?: unknown; snippet?: unknown; origin_server_ts?: unknown };
			const ts = Number(x.origin_server_ts);
			if (typeof x.event_id !== 'string' || typeof x.snippet !== 'string' || !Number.isFinite(ts) || ts < start) continue;
			const roomId = typeof x.room_id === 'string' ? x.room_id : '?';
			const sender = typeof x.sender === 'string' ? x.sender : '';
			msgs.push({ ts, room: names.get(roomId) ?? roomId, from: short(sender || '?'), fromId: sender, body: x.snippet, event_id: x.event_id });
		}
		cursor = typeof read.result.cursor === 'string' && read.result.cursor ? read.result.cursor : null;
		if (!cursor) return { ok: true, msgs, full: false };
	}
	return { ok: true, msgs, full: true };
}

/** Run `fn` over `items`, `n` at a time. */
async function pool<T, R>(items: T[], n: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let next = 0;
	await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
		while (next < items.length) { const i = next++; out[i] = await fn(items[i]); }
	}));
	return out;
}

/** This agent's id and its owner's, from ag2.whoami; null where it cannot say. */
export async function whoami(client: McpClient): Promise<{ self: string | null; owner: string | null }> {
	try {
		const res = await client.callTool('ag2.whoami', {}, ROOM_READ_CALL_TIMEOUT_MS);
		const p = (res.structuredContent ?? null) as { actor?: { id?: unknown }; runtime?: { owner_id?: unknown } } | null;
		const str = (v: unknown) => (typeof v === 'string' && v ? v : null);
		return { self: str(p?.actor?.id), owner: str(p?.runtime?.owner_id) };
	} catch { return { self: null, owner: null }; }
}

/** Whose agent each sender is (space.user.profile.read's owner), cached; null for a person or an unknown answer. */
const senderOwners = new Map<string, string | null>();
export function resetSenderOwners(): void { senderOwners.clear(); }

/** "To me" leaves out what the owner's own agents sent: a Maddy or a Sutando reporting to its owner is not someone looking for them.
 *  A sender whose profile cannot be read is kept. */
export async function withoutOwnersAgents(client: McpClient, msgs: Msg[], owner: string, deadline: number, deps: Pick<ReadRoomDeps, 'log'>): Promise<Msg[]> {
	const ask = [...new Set(msgs.map(m => m.fromId))].filter(id => id && id !== owner && !senderOwners.has(id));
	await pool(ask, QUERY_ROOM_CONCURRENCY, async (id) => {
		if (Date.now() > deadline) return;
		const r = await roomRead(client, null, 'space.user.profile.read', { user_id: id }, deps, ROOM_READ_CALL_TIMEOUT_MS);
		if (!r.ok) return;
		senderOwners.set(id, typeof r.result.owner === 'string' && r.result.owner ? r.result.owner : null);
	});
	return msgs.filter(m => senderOwners.get(m.fromId) !== owner);
}

/** Read rooms back to the window, n at a time; the rooms that could not be read, or only in part, are named. */
async function readRooms(client: McpClient, rooms: RoomRef[], start: number, deadline: number, deps: ReadRoomDeps): Promise<{ msgs: Msg[]; read: string[]; unread: string[]; partial: string[] }> {
	const per = await pool(rooms, QUERY_ROOM_CONCURRENCY, r => roomSince(client, r, start, deadline, deps));
	const msgs: Msg[] = [];
	const read: string[] = [], unread: string[] = [], partial: string[] = [];
	per.forEach((p, i) => {
		if (!p.ok) { unread.push(rooms[i].name); return; }
		read.push(rooms[i].name);
		if (p.full) partial.push(rooms[i].name);
		msgs.push(...p.msgs);
	});
	return { msgs, read, unread, partial };
}

export interface QueryArgs { scope?: QueryScope; since?: QuerySince; room?: string; from?: string; contains?: string; unanswered_only?: boolean; latest?: number; group_by?: QueryGroupBy; rank?: 'most' | 'least' }

/** The group a message falls in: its room, its sender, its hour of the day ("14:00") or its day ("Thu, Oct 16"), in local time. */
function groupKey(m: Msg, by: QueryGroupBy): string {
	if (by === 'room') return m.room;
	if (by === 'sender') return m.from;
	const d = new Date(m.ts);
	if (by === 'hour') return `${String(d.getHours()).padStart(2, '0')}:00`;
	return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}

export async function queryMessages(args: QueryArgs, deps: ReadRoomDeps, now: () => number = Date.now, budgetMs = QUERY_BUDGET_MS): Promise<QueryResult> {
	const t0 = Date.now();
	const deadline = t0 + budgetMs;
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const scope: QueryScope = args.scope === 'all' ? 'all' : 'to_me';
	const since: QuerySince = (['hour', 'today', 'day', 'week'] as const).find(s => s === args.since) ?? 'last_check';
	const ids = await whoami(client);
	// "To me" is the owner's: their inbox where the server offers it, else what mentions or answers them in this agent's rooms.
	const ownerInbox = scope === 'to_me' && !!ids.owner && await ownerInboxSupported(client, now);
	const key = scope === 'all' ? 'all' : ids.owner ? `to_me:${ids.owner}` : 'to_me:this agent';
	const asked = now();
	const start = windowStart(since, key, asked);
	const latestN = Math.min(QUERY_MAX_LATEST, Math.max(1, Math.floor(args.latest ?? QUERY_DEFAULT_LATEST)));
	let room: RoomRef | null = null;
	if ((args.room ?? '').trim()) {
		const t = await targetRoom(args.room, deps);
		if (!t.ok) return t;
		room = t.room;
	}
	const names = new Map(deps.index.rooms().map(r => [r.room_id, r.name] as const));
	const word = norm(args.contains ?? '');
	const from = norm(args.from ?? '');

	let msgs: Msg[] = [];
	let atLeast = false;
	const extra: { rooms_read?: number; rooms_unread?: number; unread_rooms?: string[]; partial_rooms?: string[]; rooms_skipped?: number; whose?: InboxWhose; source?: 'inbox' | 'rooms' | 'rooms+inbox'; owner_error?: string } = {};
	/** Every room a scan read, so a room with nothing in the window still counts as a group of 0. */
	let readRoomNames: string[] = [];
	const scanned = (got: { read: string[]; unread: string[]; partial: string[] }) => {
		atLeast = got.partial.length > 0;
		readRoomNames = got.read;
		extra.rooms_read = got.read.length;
		if (got.unread.length) { extra.rooms_unread = got.unread.length; extra.unread_rooms = got.unread; }
		if (got.partial.length) extra.partial_rooms = got.partial;
	};
	/** The inbox as messages, leaving out the rooms in `scannedRooms` (read whole already); `replied` is every item it marks answered. */
	const fromInbox = async (owner: boolean, scannedRooms: ReadonlySet<string> = new Set()): Promise<{ ok: true; replied: Set<string> } | { ok: false; error: string; refused: boolean }> => {
		// With rooms scanned, answered ones are read too: the scan cannot tell them, and the inbox's mark is what leaves them out.
		const inboxArgs = { since: new Date(start).toISOString(), include_replied: !args.unanswered_only || scannedRooms.size > 0, owner, deadline, ...(room ? { room_ids: [room.room_id] } : {}) };
		const got = await fetchInbox(client, inboxArgs, deps);
		// The owner's inbox refused here (not enabled, revoked) is not an answer: the room scan is.
		if (!got.ok) return { ...got, refused: owner && !['timeout', 'unavailable', 'bad_response'].includes(got.error) };
		if (got.more) atLeast = true;
		if (got.rooms_skipped) extra.rooms_skipped = got.rooms_skipped;
		const replied = new Set<string>();
		for (const raw of got.items) {
			if (!raw || typeof raw !== 'object') continue;
			const it = raw as { room_id?: unknown; sender?: { user_id?: unknown } | null; sent_at?: unknown; body?: unknown; replied?: unknown };
			const ts = typeof it.sent_at === 'string' ? Date.parse(it.sent_at) : NaN;
			const [line] = toInboxItems([raw]);
			if (!line || !Number.isFinite(ts)) continue;
			if (it.replied === true) replied.add(line.event_id);
			if (typeof it.room_id === 'string' && scannedRooms.has(it.room_id)) continue;
			if (args.unanswered_only && it.replied === true) continue;
			const name = line.room.startsWith('!') ? names.get(line.room) ?? (line.why === 'dm' ? 'a direct message' : line.room) : line.room;
			const sender = it.sender && typeof it.sender === 'object' && typeof it.sender.user_id === 'string' ? it.sender.user_id : '';
			// A withheld message counts, and matches no word: its body is not known.
			msgs.push({ ts, room: name, from: line.from, fromId: sender, body: typeof it.body === 'string' ? it.body : '', event_id: line.event_id, why: line.why, ...(line.hidden ? { hidden: true } : {}) });
		}
		return { ok: true, replied };
	};
	if (scope === 'to_me' && !ids.owner) {
		// No owner known: this agent's own inbox, said as such.
		const got = await fromInbox(false);
		if (!got.ok) return { ok: false, error: got.error };
		extra.whose = 'this agent';
		extra.source = 'inbox';
	} else if (scope === 'to_me' && ids.owner) {
		// The owner's: what mentions or answers them in every room this agent is in, read whole (the server's inbox reads only
		// each room's latest 50, and a busy room's mentions from this morning were missing, 2026-10-16); the inbox adds their
		// direct messages and the rooms this agent is not in, where the server offers it.
		const owner = ids.owner;
		const rooms = room ? [room] : await listRooms(client, ROOM_INDEX_COLD_TIMEOUT_MS, { unnamed: true }).catch(() => deps.index.rooms());
		const got = rooms.length ? await readRooms(client, rooms, start, deadline, deps) : { msgs: [], read: [], unread: [], partial: [] };
		const readIds = new Set(rooms.filter(r => got.read.includes(r.name)).map(r => r.room_id));
		const inbox = ownerInbox ? await fromInbox(true, readIds) : null;
		if (inbox && !inbox.ok && inbox.refused) extra.owner_error = inbox.error;
		const inboxRead = !!inbox?.ok;
		if (!got.read.length && !inboxRead) return { ok: false, error: inbox && !inbox.ok && !inbox.refused ? inbox.error : 'unavailable' };
		const partial = atLeast;
		scanned(got);
		atLeast = atLeast || partial;
		const replied = inbox?.ok ? inbox.replied : new Set<string>();
		for (const m of got.msgs) {
			if (m.fromId === owner || m.fromId === ids.self) continue;
			if (args.unanswered_only && replied.has(m.event_id)) continue;
			const why = addressedTo(m, owner);
			if (why) msgs.push({ ...m, why });
		}
		extra.whose = 'owner';
		extra.source = inboxRead ? 'rooms+inbox' : 'rooms';
	} else if (scope === 'all' && word) {
		const got = await searchSince(client, word, start, deadline, room, names, deps);
		if (!got.ok) return got;
		atLeast = got.full;
		msgs = got.msgs.filter(m => !ids.self || m.fromId !== ids.self);
	} else if (scope === 'all') {
		let rooms = room ? [room] : deps.index.rooms();
		if (!rooms.length) rooms = await deps.index.refresh().catch(() => []);
		if (!rooms.length) return { ok: false, error: 'unavailable' };
		const got = await readRooms(client, rooms, start, deadline, deps);
		if (!got.read.length) return { ok: false, error: 'unavailable' };
		scanned(got);
		msgs = got.msgs.filter(m => !ids.self || m.fromId !== ids.self);
	}

	if (scope === 'to_me' && ids.owner) msgs = await withoutOwnersAgents(client, msgs, ids.owner, deadline, deps);
	if (from) msgs = msgs.filter(m => norm(m.from).includes(from) || norm(short(m.fromId)).includes(from));
	if (word) msgs = msgs.filter(m => norm(m.body).includes(word));
	msgs.sort((a, b) => b.ts - a.ts);

	const groupBy: QueryGroupBy = (['sender', 'hour', 'day'] as const).find(g => g === args.group_by) ?? 'room';
	const rank = args.rank === 'least' ? 'least' : 'most';
	const counts = new Map<string, number>();
	if (groupBy === 'room') for (const r of readRoomNames) counts.set(r, 0);
	for (const m of msgs) { const k = groupKey(m, groupBy); counts.set(k, (counts.get(k) ?? 0) + 1); }
	const groups = [...counts.entries()].sort((a, b) => (rank === 'most' ? b[1] - a[1] : a[1] - b[1])).slice(0, QUERY_MAX_GROUPS).map(([key, count]) => ({ key, count }));
	const hit = (m: Msg): QueryHit => {
		const text = m.hidden ? INBOX_HIDDEN_TEXT : m.body.replace(/\s+/g, ' ').trim();
		return { at: fmt(m.ts), room: m.room, from: m.from, text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text, event_id: m.event_id, ...(m.why ? { why: m.why } : {}) };
	};
	const latest = msgs.slice(0, latestN).map(hit);
	const stats = {
		group_by: groupBy, rank, groups, groups_total: counts.size,
		distinct_rooms: new Set(msgs.map(m => m.room)).size, distinct_senders: new Set(msgs.map(m => m.fromId || m.from)).size,
		latest, ...(msgs.length ? { oldest: hit(msgs[msgs.length - 1]) } : {}),
	};
	const complete = !atLeast && !extra.rooms_unread && !extra.rooms_skipped;
	// "New" moves only past what was fully read and not filtered: a "new from Chi" leaves everyone else's new.
	const unfiltered = !room && !from && !word && !args.unanswered_only;
	if (since === 'last_check' && complete && unfiltered && Date.now() <= deadline) lastChecked.set(key, Math.max(lastChecked.get(key) ?? 0, asked));
	return { ok: true, scope, since: fmt(start), count: msgs.length, complete, at_least: atLeast, ...stats, ...extra, ms: Date.now() - t0 };
}

export function createQueryMessagesTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'query_messages',
		description:
			'Count and list AG2 Space messages across all your rooms at once, right now (a few seconds) — for questions that name no single room: "what is my latest message", "how many new messages / notifications do I have", "anything new", "anything from Chi today", "how many messages mention the launch this week". ' +
			'Call it every time such a question is asked, even right after a similar one: counts change, and an earlier answer or earlier search_room results are never the count now. ' +
			'`scope`: "to_me" (default) — messages for the user (`whose` "owner"): those that @-mention them or reply to them, read whole in every room you are in, plus their direct messages and the rooms you are not in from the user\'s own inbox where the server offers it (`source` "rooms+inbox"; the inbox part covers only each room\'s latest 50 messages). Without an owner it reads your own inbox (`whose` "this agent" — say so). "all" — every message in your named rooms, your own left out. ' +
			'`source` "rooms" covers only rooms you are in: a mention in a room you are not in, or a direct message to the user, is not seen — say so when the user asks whether anyone is looking for them. ' +
			'`since`: "last_check" (default — new since the last complete check, in this call or the last call where the user asked; the first time, the last day), "hour", "today" (since midnight: "today", "今天"), "day" (the last 24 hours), "week". The server keeps no read/unread marker you can see, so "new" means since the last check — say so if the user asks about unread. ' +
			'Optional filters: `room` (as listed in ROOMS), `from` (a person\'s name), `contains` (a word), `unanswered_only` (to_me: leaves out what the inbox marks answered; a message the inbox does not list is kept). ' +
			'Most / least / how many: `group_by` "room" (default), "sender", "hour" (of the day) or "day", and `rank` "most" (default) or "least" — "who messaged me most" is group_by "sender"; "the quietest room" is group_by "room", rank "least" (rooms with no messages count as 0 there); "the busiest hour" is group_by "hour". ' +
			'Returns {ok:true, scope, since, count, complete, at_least, group_by, rank, groups:[{key, count}] (at most ' + QUERY_MAX_GROUPS + ', ranked), groups_total, distinct_rooms, distinct_senders, latest:[{at, room, from, text, event_id, why}] newest first, oldest (the earliest message in the window), rooms_read, rooms_unread, unread_rooms, partial_rooms, rooms_skipped, whose, source} — answer in a sentence or two. ' +
			'Answer only from these fields: the earliest message is `oldest`, how many people or rooms is distinct_senders / distinct_rooms, most or least is `groups` with the matching group_by and rank — call again with them rather than working it out from `latest` or from groups ranked the other way. Something none of these fields gives cannot be answered: say so, never estimate it. ' +
			'complete:false means the result is partial — say so first, before any number: at_least:true is "at least <count>"; name the rooms in unread_rooms (not read) and partial_rooms (read only in part); rooms_skipped rooms were skipped by the server. Never say there is nothing new, or name an earliest, least or most, as if complete when complete is false. From the inbox, the server reads only each room\'s latest 50 messages, so over a long window it can come up short. To read around a message, call read_room with its room and `around` = its event_id. ' +
			'Errors: ' + ROOM_NAME_ERRORS + '"not_connected", "timeout" or "unavailable" — say the messages cannot be counted right now.',
		parameters: z.object({
			scope: z.enum(['to_me', 'all']).optional().describe('"to_me" (default): addressed to the user; "all": every message in your rooms.'),
			since: z.enum(['last_check', 'hour', 'today', 'day', 'week']).optional().describe('The window; default "last_check" = new since last asked.'),
			room: z.string().optional().describe('Only this room, e.g. "Sutando Triage". Omit for all rooms.'),
			from: z.string().max(80).optional().describe('Only messages from this person, by name, e.g. "Chi".'),
			contains: z.string().max(200).optional().describe('Only messages containing this word or phrase.'),
			unanswered_only: z.boolean().optional().describe('to_me only: leave out what the inbox marks answered.'),
			latest: z.number().int().min(1).max(QUERY_MAX_LATEST).optional().describe(`How many of the newest to return (default ${QUERY_DEFAULT_LATEST}).`),
			group_by: z.enum(['room', 'sender', 'hour', 'day']).optional().describe('What `groups` counts by (default "room").'),
			rank: z.enum(['most', 'least']).optional().describe('"most" (default) or "least" first in `groups`.'),
		}),
		execution: 'inline',
		// The budget stops new reads; the calls already started finish within one call timeout; before it, a cold index and the
		// first call's check of the owner's inbox (room.actions.describe).
		timeout: QUERY_BUDGET_MS + 2 * ROOM_COMMONS_TIMEOUT_MS + ROOM_INDEX_COLD_TIMEOUT_MS + 2 * ROOM_READ_CALL_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as QueryArgs;
			const r = await queryMessages(a, deps);
			deps.log?.(`[RoomQuery] ${a.scope ?? 'to_me'} since ${a.since ?? 'last_check'}${a.room ? ` in "${a.room}"` : ''}${a.from ? ` from "${a.from}"` : ''}${a.contains ? ` with "${a.contains}"` : ''}${a.group_by ? ` by ${a.group_by}` : ''}${a.rank ? ` ${a.rank}` : ''} → ${r.ok ? `${r.at_least ? '≥' : ''}${r.count}${r.complete ? '' : ` (partial${r.unread_rooms ? `; not read: ${r.unread_rooms.join(', ')}` : ''})`} in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

/** room.members.list per room is fast (0.2 s measured); these stop a slow server from holding the answer. */
export const ROOM_STATS_BUDGET_MS = 15_000;

export type RoomStatsResult =
	| { ok: true; rooms_total: number; rank: 'most' | 'least'; rooms: Array<{ room: string; members: number }>; rooms_unread?: number; unread_rooms?: string[]; complete: boolean; ms: number }
	| { ok: false; error: string };

/** How many rooms this agent is in, and how many members each has, ranked. */
export async function roomStats(args: { rank?: 'most' | 'least' }, deps: ReadRoomDeps, budgetMs = ROOM_STATS_BUDGET_MS): Promise<RoomStatsResult> {
	const t0 = Date.now();
	const deadline = t0 + budgetMs;
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	let rooms: RoomRef[];
	try { rooms = await listRooms(client, ROOM_INDEX_COLD_TIMEOUT_MS, { unnamed: true }); } catch { return { ok: false, error: 'unavailable' }; }
	const per = await pool(rooms, QUERY_ROOM_CONCURRENCY, async (r): Promise<number | null> => {
		if (Date.now() > deadline) return null;
		const got = await roomRead(client, r, 'room.members.list', {}, deps, ROOM_READ_CALL_TIMEOUT_MS);
		return got.ok && Array.isArray(got.result.members) ? got.result.members.length : null;
	});
	const rank = args.rank === 'least' ? 'least' : 'most';
	const counted = rooms.map((r, i) => ({ room: r.name, members: per[i] })).filter((x): x is { room: string; members: number } => x.members !== null);
	counted.sort((a, b) => (rank === 'most' ? b.members - a.members : a.members - b.members));
	const unread = rooms.filter((_, i) => per[i] === null).map(r => r.name);
	return {
		ok: true, rooms_total: rooms.length, rank, rooms: counted.slice(0, QUERY_MAX_GROUPS),
		...(unread.length ? { rooms_unread: unread.length, unread_rooms: unread } : {}), complete: !unread.length, ms: Date.now() - t0,
	};
}

export function createRoomStatsTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'room_stats',
		description:
			'How many AG2 Space rooms you are in, and how many members each has — "how many rooms do I have", "which room has the most people", "the smallest room". These are the rooms you, the agent, are in: say so if the user asks about their own rooms. ' +
			'`rank`: "most" (default) or "least" members first. Returns {ok:true, rooms_total, rank, rooms:[{room, members}] (at most ' + QUERY_MAX_GROUPS + ', ranked), complete, rooms_unread, unread_rooms}; members counts people and agents alike. ' +
			'complete:false: the rooms in unread_rooms could not be counted — say so, and do not call a room the largest or smallest over rooms that were not counted. Errors: "not_connected" or "unavailable" — say the rooms cannot be counted right now.',
		parameters: z.object({
			rank: z.enum(['most', 'least']).optional().describe('"most" (default) or "least" members first.'),
		}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + ROOM_STATS_BUDGET_MS + ROOM_READ_CALL_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as { rank?: 'most' | 'least' };
			const r = await roomStats(a, deps);
			deps.log?.(`[RoomStats] ${a.rank ?? 'most'} → ${r.ok ? `${r.rooms_total} rooms${r.complete ? '' : ` (not counted: ${r.unread_rooms?.join(', ')})`} in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export const QUERY_PROMPT_RULE = '- AG2 SPACE ACROSS ROOMS: whether anyone mentioned, tagged, @-ed, replied to or is looking for the user, which rooms have new notifications for them, and any count or list of messages that names no single room — "my latest message", "how many new messages / notifications", "anything new", "anything from <person> today", in any wording → query_messages, every time, even if you counted a moment ago: never answer from an earlier answer or earlier search_room results, and never search room after room for the user\'s name instead. Scope "to_me" (the default) for what is for the user, "all" for everything in your rooms. "Today" / "今天" is since "today", not "day". Most, least, earliest and how many people or rooms come from its group_by, rank, oldest and distinct fields — call it with them; never work them out from a few listed messages, and what it does not return is said as not available. "New" means since the last complete check; there is no unread marker. A partial result (complete:false) is said as partial first, naming the rooms not read, and with source "rooms" say that rooms you are not in and direct messages are not covered. How many rooms, or members in a room → room_stats. Never submit work for either.';

export const QUERY_TURN_CHECK_RULE = '- fetch query_messages: a count or list of AG2 Space messages or notifications across rooms, or the most, least or earliest of them ("my latest message", "how many new messages", "how many rooms have new messages", "any new notifications", "anything from <person> today", "who sent the most", "the quietest room", "the earliest message"). fetch room_stats: how many rooms there are in all, or members in a room ("how many rooms do I have", "which room has the most people"); rooms that have new messages are query_messages. Never work. A follow-up about the result just given ("when was that sent", "how many people is that") is none.';
