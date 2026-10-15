// read_inbox: what is addressed to the owner — or, where the server cannot read the owner's inbox
// yet, to this agent — across rooms (mentions.inbox, as the account), newest first.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { roomRead, ROOM_COMMONS_TIMEOUT_MS, ROOM_READ_BODY_MAX, type McpClient, type ReadRoomDeps } from './room-read.js';

export const INBOX_DEFAULT_LIMIT = 10;
export const INBOX_MAX_LIMIT = 30;
export const INBOX_DEFAULT_DAYS = 7;
export const INBOX_MAX_DAYS = 30;
/** mentions.inbox lists oldest first, at most 100 a page; the newest are on the last page. */
export const INBOX_PAGE = 100;
export const INBOX_MAX_PAGES = 5;
/** How long the server's answer to "can a linked agent read its owner's inbox" is trusted. */
export const OWNER_SCOPE_TTL_MS = 10 * 60_000;
/** When a window outruns the pages, the newest are reached by asking again over these shorter ones. */
export const INBOX_FALLBACK_WINDOWS_MS = [86_400_000, 3_600_000];

export interface InboxItem { at: string; room: string; from: string; text: string; why: string; event_id: string; answered?: boolean }

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

/** mentions.inbox items (oldest first) → newest-first lines the model can retell; malformed items are skipped. */
export function toInboxItems(items: unknown[], tz?: string): InboxItem[] {
	const out: InboxItem[] = [];
	for (const m of items) {
		if (!m || typeof m !== 'object') continue;
		const it = m as { room_id?: unknown; room_name?: unknown; event_id?: unknown; sender?: unknown; sent_at?: unknown; body?: unknown; reason?: unknown; replied?: unknown };
		const eventId = str(it.event_id);
		if (!eventId || typeof it.body !== 'string' || !it.body.trim()) continue;
		const sender = (it.sender && typeof it.sender === 'object' ? it.sender : {}) as { user_id?: unknown; display_name?: unknown };
		const from = str(sender.display_name) ?? (str(sender.user_id) ?? '?').replace(/^@/, '').replace(/:.*$/, '');
		const t = str(it.sent_at) ? Date.parse(it.sent_at as string) : NaN;
		const at = Number.isFinite(t) ? new Date(t).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }) : '';
		const text = it.body.replace(/\s+/g, ' ').trim();
		out.push({
			at, room: str(it.room_name) ?? str(it.room_id) ?? '?', from,
			text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text,
			why: str(it.reason) ?? 'mention', event_id: eventId,
			...(it.replied === true ? { answered: true } : {}),
		});
	}
	return out.reverse();
}

/** Whose inbox a read returns: the owner's own account, or this agent's. */
export type InboxWhose = 'owner' | 'this agent';

let ownerScope: { client: McpClient; ok: boolean; at: number } | null = null;
export function resetOwnerScope(): void { ownerScope = null; }

/** Whether mentions.inbox takes scope "owner" here, from the server's own description of it; false when it cannot say. */
export async function ownerInboxSupported(client: McpClient, now: () => number = Date.now): Promise<boolean> {
	if (ownerScope && ownerScope.client === client && now() - ownerScope.at < OWNER_SCOPE_TTL_MS) return ownerScope.ok;
	let ok = false;
	try {
		const res = await client.callTool('room.actions.describe', { target_kind: 'account', action: 'mentions.inbox' }, ROOM_COMMONS_TIMEOUT_MS);
		const scope = (((res.structuredContent ?? null) as { input_schema?: { properties?: Record<string, { enum?: unknown }> } } | null)?.input_schema?.properties ?? {}).scope;
		ok = !res.isError && !!scope && Array.isArray(scope.enum) && scope.enum.includes('owner');
	} catch { /* unknown: read the agent's own */ }
	ownerScope = { client, ok, at: now() };
	return ok;
}

export interface InboxPage { items: unknown[]; more: boolean; rooms_skipped: number }

/** mentions.inbox since a time, every page up to INBOX_MAX_PAGES, as the server gave them (oldest first). */
export async function fetchInbox(client: McpClient, args: { since: string; include_replied: boolean; room_ids?: string[]; owner?: boolean; deadline?: number }, deps: Pick<ReadRoomDeps, 'log'>): Promise<({ ok: true } & InboxPage) | { ok: false; error: string }> {
	const items: unknown[] = [];
	let cursor: string | null = null;
	let more = false;
	let skipped = 0;
	for (let page = 0; page < INBOX_MAX_PAGES; page++) {
		// Past the caller's deadline no new page starts; what was read is returned as having more.
		if (page > 0 && args.deadline !== undefined && Date.now() > args.deadline) return { ok: true, items, more: true, rooms_skipped: skipped };
		const read = await roomRead(client, null, 'mentions.inbox', { limit: INBOX_PAGE, since: args.since, include_replied: args.include_replied, ...(args.owner ? { scope: 'owner' } : {}), ...(args.room_ids?.length ? { room_ids: args.room_ids } : {}), ...(cursor ? { cursor } : {}) }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		if (!Array.isArray(read.result.mentions)) return { ok: false, error: 'bad_response' };
		items.push(...read.result.mentions);
		skipped = Math.max(skipped, Number(read.result.rooms_skipped) || 0);
		cursor = str(read.result.next_cursor);
		more = read.result.has_more === true;
		if (!more || !cursor) break;
	}
	return { ok: true, items, more, rooms_skipped: skipped };
}

/** One inbox read as the owner where the server offers it, else (or when it refuses the owner here) as this agent. */
export async function fetchInboxAs(client: McpClient, args: { since: string; include_replied: boolean; room_ids?: string[]; owner: boolean; deadline?: number }, deps: Pick<ReadRoomDeps, 'log'>): Promise<({ ok: true; whose: InboxWhose; owner_error?: string } & InboxPage) | { ok: false; error: string }> {
	const r = await fetchInbox(client, args, deps);
	if (r.ok) return { ...r, whose: args.owner ? 'owner' : 'this agent' };
	if (!args.owner || ['timeout', 'unavailable', 'bad_response'].includes(r.error)) return r;
	const mine = await fetchInbox(client, { ...args, owner: false }, deps);
	return mine.ok ? { ...mine, whose: 'this agent', owner_error: r.error } : mine;
}

export type ReadInboxResult =
	| { ok: true; whose: InboxWhose; items: InboxItem[]; more: boolean; complete: boolean; rooms_skipped?: number; since?: string; owner_error?: string; ms: number }
	| { ok: false; error: string };

/**
 * What is addressed to this agent across its rooms, newest first. The server pages oldest first, so a
 * window with more than the pages hold is asked again over a day, then an hour, to reach the newest.
 */
export async function readInbox(args: { limit?: number; days?: number; include_answered?: boolean }, deps: ReadRoomDeps, now: () => number = Date.now): Promise<ReadInboxResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const limit = Math.min(INBOX_MAX_LIMIT, Math.max(1, Math.floor(args.limit ?? INBOX_DEFAULT_LIMIT)));
	const days = Math.min(INBOX_MAX_DAYS, Math.max(1, Math.floor(args.days ?? INBOX_DEFAULT_DAYS)));
	const asked = now();
	let owner = await ownerInboxSupported(client, now);
	let ownerError: string | undefined;
	const windows = [days * 86_400_000, ...INBOX_FALLBACK_WINDOWS_MS.filter(w => w < days * 86_400_000)];
	let got: InboxPage | null = null;
	let window = windows[0];
	for (const w of windows) {
		const r = await fetchInboxAs(client, { since: new Date(asked - w).toISOString(), include_replied: !!args.include_answered, owner }, deps);
		if (!r.ok) return r;
		if (r.owner_error) { ownerError = r.owner_error; owner = false; }
		got = r;
		window = w;
		if (!r.more) break;
	}
	const items = toInboxItems(got!.items);
	const narrowed = window !== windows[0];
	const complete = !got!.more && got!.rooms_skipped === 0 && !narrowed;
	return {
		ok: true, whose: owner ? 'owner' : 'this agent', items: items.slice(0, limit), more: got!.more || narrowed || items.length > limit, complete,
		...(got!.rooms_skipped ? { rooms_skipped: got!.rooms_skipped } : {}),
		...(ownerError ? { owner_error: ownerError } : {}),
		...(narrowed ? { since: new Date(asked - window).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }) } : {}),
		ms: Date.now() - t0,
	};
}

export function createReadInboxTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'read_inbox',
		description:
			'Read the AG2 Space inbox, right now (a few seconds): messages across rooms that @-mention, reply to, or are sent directly to the user — or to you, this agent, where the server cannot read the user\'s own yet. ' +
			'Call it whenever the user asks whether anyone is looking for, mentioned, tagged, @-ed, replied to or messaged them or you, or what notifications or messages are waiting — in any wording, with or without a room. ' +
			'This is how such a question is answered: never search room after room for the user\'s name, handle or id instead, and never answer it from an earlier answer. ' +
			'`whose` says which it read: "owner" — the user\'s own inbox; "this agent" — yours, not the user\'s personal account, so a mention of the user is not in it — say so when it matters; with owner_error the server refused the user\'s own inbox (it may not be enabled for you) — say that too. ' +
			'Unanswered items only, unless `include_answered`. Returns {ok:true, whose, items:[{at, room, from, text, why, event_id}], more, complete, rooms_skipped, since} newest first — answer in a sentence or two: who, where, about what; to read the discussion around one, call read_room with that room and `around` = its event_id. ' +
			'complete:false means the list is partial: rooms_skipped rooms could not be read, or (with `since`) there were too many to list and only those since then are here — say so, and never say nothing is waiting unless complete is true and items is empty. ' +
			'Errors: "not_connected", "timeout", "unavailable" or "bad_response" — say the inbox cannot be read right now.',
		parameters: z.object({
			limit: z.number().int().min(1).max(INBOX_MAX_LIMIT).optional().describe(`How many items (default ${INBOX_DEFAULT_LIMIT}).`),
			days: z.number().int().min(1).max(INBOX_MAX_DAYS).optional().describe(`How far back, in days (default ${INBOX_DEFAULT_DAYS}).`),
			include_answered: z.boolean().optional().describe('Also list items already answered.'),
		}),
		execution: 'inline',
		timeout: (1 + INBOX_FALLBACK_WINDOWS_MS.length) * INBOX_MAX_PAGES * ROOM_COMMONS_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as { limit?: number; days?: number; include_answered?: boolean };
			const r = await readInbox(a, deps);
			deps.log?.(`[RoomActions] read_inbox → ${r.ok ? `${r.whose}: ${r.items.length} items${r.more ? ' (more)' : ''}${r.complete ? '' : ' (partial)'} in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export const INBOX_PROMPT_RULE = '- AG2 SPACE INBOX: whether anyone is looking for, mentioned, tagged, @-ed, replied to or messaged the user (or you), or what is waiting for an answer — in any wording, with or without a room → read_inbox, every time, never from memory. It comes before search_room: never search room after room for the user\'s name, handle or id to find mentions. Its `whose` says whether it read the user\'s own inbox or only yours — when only yours, say so. Never submit work for it.';

export const INBOX_TURN_CHECK_RULE = '- none: the AG2 Space inbox — whether anyone is looking for, mentioned, tagged or messaged the user or the assistant, or what is waiting for an answer — is the assistant\'s own to read, never work.';
