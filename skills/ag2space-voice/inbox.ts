// The AG2 Space inbox (mentions.inbox, as the account): what is addressed to the owner where the server
// reads the owner's own inbox, else to this agent. The reading only; query_messages decides what it answers.

import { roomRead, ROOM_COMMONS_TIMEOUT_MS, ROOM_READ_BODY_MAX, type McpClient, type ReadRoomDeps } from './room-read.js';

/** mentions.inbox lists oldest first, at most 100 a page; the newest are on the last page. */
export const INBOX_PAGE = 100;
export const INBOX_MAX_PAGES = 5;
/** How long the server's answer to "can a linked agent read its owner's inbox" is trusted. */
export const OWNER_SCOPE_TTL_MS = 10 * 60_000;

export interface InboxItem { at: string; room: string; from: string; text: string; why: string; event_id: string; answered?: boolean; hidden?: boolean }

/** Said in place of a message the server withholds: it reads no body from a room this agent is not in (a direct message to the owner). */
export const INBOX_HIDDEN_TEXT = '(not shown: you are not in that conversation)';

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

/** mentions.inbox items (oldest first) → newest-first lines the model can retell; malformed items are skipped, a withheld body is said as hidden. */
export function toInboxItems(items: unknown[], tz?: string): InboxItem[] {
	const out: InboxItem[] = [];
	for (const m of items) {
		if (!m || typeof m !== 'object') continue;
		const it = m as { room_id?: unknown; room_name?: unknown; event_id?: unknown; sender?: unknown; sent_at?: unknown; body?: unknown; reason?: unknown; replied?: unknown };
		const eventId = str(it.event_id);
		// body null is the server withholding it (2026-10-16: a direct message from Yixuan was dropped as malformed); still a message.
		const hidden = it.body === null;
		if (!eventId || (!hidden && (typeof it.body !== 'string' || !it.body.trim()))) continue;
		const sender = (it.sender && typeof it.sender === 'object' ? it.sender : {}) as { user_id?: unknown; display_name?: unknown };
		const from = str(sender.display_name) ?? (str(sender.user_id) ?? '?').replace(/^@/, '').replace(/:.*$/, '');
		const t = str(it.sent_at) ? Date.parse(it.sent_at as string) : NaN;
		const at = Number.isFinite(t) ? new Date(t).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }) : '';
		const text = hidden ? INBOX_HIDDEN_TEXT : (it.body as string).replace(/\s+/g, ' ').trim();
		out.push({
			at, room: str(it.room_name) ?? str(it.room_id) ?? '?', from,
			text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text,
			why: str(it.reason) ?? 'mention', event_id: eventId,
			...(it.replied === true ? { answered: true } : {}),
			...(hidden ? { hidden: true } : {}),
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
