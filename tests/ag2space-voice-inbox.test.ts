/**
 * read_inbox — what is addressed to this agent across its rooms (mentions.inbox).
 *
 * Pins: read as the account (target_kind account), never a room; over the last days, unanswered
 * unless asked; paged to the newest, newest first, cut to the limit; the tool says it is this
 * agent's inbox, not the owner's; every failure is an answer, never a throw.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'inbox.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const { readInbox, toInboxItems, ownerInboxSupported, createReadInboxTool, INBOX_PROMPT_RULE, INBOX_TURN_CHECK_RULE, INBOX_DEFAULT_LIMIT, OWNER_SCOPE_TTL_MS } = await import('../skills/ag2space-voice/inbox.ts');

type Call = { name: string; args: Record<string, unknown> };
const ok = (v: unknown) => ({ structuredContent: v });
const err = (code: string) => ({ isError: true, structuredContent: { code } });

/** `owner`: what room.actions.describe says of mentions.inbox — a scope with "owner", none, or a failure. */
function fake(handler: (name: string, args: Record<string, unknown>) => unknown, owner: 'yes' | 'no' | 'fails' = 'no') {
	const calls: Call[] = [];
	const described: Call[] = [];
	const client = {
		async callTool(name: string, args: Record<string, unknown>) {
			if (name === 'room.actions.describe') {
				described.push({ name, args });
				if (owner === 'fails') throw new Error('room.actions.describe timed out after 15000ms');
				const props: Record<string, unknown> = { limit: {}, since: {} };
				if (owner === 'yes') props.scope = { enum: ['self', 'owner'] };
				return ok({ action: 'mentions.inbox', input_schema: { properties: props } }) as never;
			}
			calls.push({ name, args }); const r = handler(name, args); if (r instanceof Error) throw r; return r as never;
		},
		close() {},
	};
	const index = { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 };
	const deps = { client: () => client, currentRoom: () => ({ id: '!cur:ag2.space', name: 'Current' }), index, log: () => {} };
	return { calls, described, client, deps };
}

const mention = (i: number, extra: Record<string, unknown> = {}) => ({
	room_id: '!r:ag2.space', room_name: 'Bodhi-migration', event_id: `$e${i}`, sender: { user_id: '@yixuan:ag2.space', display_name: 'Yixuan' },
	sent_at: new Date(Date.UTC(2026, 9, 14, 20, i)).toISOString(), body: `message ${i}`, reason: 'mention', in_reply_to: null, thread_root: null, replied: false, link: null, ...extra,
});

describe('read_inbox: what is addressed to this agent, newest first', () => {
	it('reads mentions.inbox as the account, over the last days, unanswered only', async () => {
		const f = fake(() => ok({ status: 'completed', result: { mentions: [mention(1), mention(2)], next_cursor: null, has_more: false } }));
		const now = Date.UTC(2026, 9, 14, 21, 0);
		const r = await readInbox({}, f.deps, () => now);
		assert.equal(f.calls.length, 1);
		const c = f.calls[0];
		assert.equal(c.name, 'room.action.read');
		assert.equal(c.args.target_kind, 'account');
		assert.equal(c.args.room_id, undefined);
		assert.equal(c.args.action, 'mentions.inbox');
		const a = c.args.arguments as Record<string, unknown>;
		assert.equal(a.include_replied, false);
		assert.equal(a.since, new Date(now - 7 * 86_400_000).toISOString());
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.whose, 'this agent');
			assert.deepEqual(r.items.map(i => i.event_id), ['$e2', '$e1']);
			assert.equal(r.items[0].room, 'Bodhi-migration');
			assert.equal(r.items[0].from, 'Yixuan');
			assert.equal(r.more, false);
			assert.equal(r.complete, true);
		}
	});

	it('follows the cursor to the newest page and keeps only the newest `limit`', async () => {
		let page = 0;
		const f = fake((_n, args) => {
			page++;
			const cur = (args.arguments as { cursor?: string }).cursor;
			if (page === 1) { assert.equal(cur, undefined); return ok({ result: { mentions: Array.from({ length: 12 }, (_, i) => mention(i)), next_cursor: 'c1', has_more: true } }); }
			assert.equal(cur, 'c1');
			return ok({ result: { mentions: [mention(30), mention(31)], next_cursor: 'c2', has_more: false } });
		});
		const r = await readInbox({ include_answered: true, days: 2 }, f.deps);
		assert.equal(f.calls.length, 2);
		assert.equal((f.calls[0].args.arguments as Record<string, unknown>).include_replied, true);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.items.length, INBOX_DEFAULT_LIMIT);
			assert.equal(r.items[0].event_id, '$e31');
			assert.equal(r.more, true);
		}
	});

	it('a window past the page cap is asked again over a day, then an hour, so the newest are the newest', async () => {
		// The server pages oldest first: 7 pages of 100 for the week, 2 for the day, 1 for the hour.
		const now = Date.UTC(2026, 9, 14, 21, 0);
		const sinces: string[] = [];
		const f = fake((_n, a) => {
			const x = a.arguments as { since: string; cursor?: string };
			if (!x.cursor) sinces.push(x.since);
			const window = now - Date.parse(x.since);
			const pages = window > 86_400_000 ? 7 : window > 3_600_000 ? 7 : 1;
			const page = x.cursor ? Number(x.cursor) : 0;
			const base = 700 - pages * 100 + page * 100;
			return ok({ result: { mentions: Array.from({ length: 100 }, (_, i) => mention(base + i)), next_cursor: String(page + 1), has_more: page + 1 < pages, rooms_skipped: 0 } });
		});
		const r = await readInbox({ limit: 3 }, f.deps, () => now);
		assert.deepEqual(sinces, [7 * 86_400_000, 86_400_000, 3_600_000].map(w => new Date(now - w).toISOString()));
		assert.ok(r.ok);
		if (r.ok) {
			assert.deepEqual(r.items.map(i => i.event_id), ['$e699', '$e698', '$e697']);
			assert.equal(r.complete, false);
			assert.equal(r.more, true);
			assert.ok(r.since);
		}
	});

	it('rooms the server skipped make the list partial, never "nothing waiting"', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false, rooms_skipped: 2 } }));
		const r = await readInbox({}, f.deps);
		assert.ok(r.ok);
		if (r.ok) { assert.equal(r.items.length, 0); assert.equal(r.complete, false); assert.equal(r.rooms_skipped, 2); }
	});

	it('past the caller\'s deadline no new page starts, and the read says there is more', async () => {
		const { fetchInbox } = await import('../skills/ag2space-voice/inbox.ts');
		const f = fake(() => ok({ result: { mentions: [mention(1)], next_cursor: 'c', has_more: true, rooms_skipped: 0 } }));
		const r = await fetchInbox(f.client as never, { since: 'x', include_replied: false, deadline: Date.now() - 1 }, f.deps);
		assert.equal(f.calls.length, 1);
		assert.ok(r.ok && r.more && r.items.length === 1);
	});

	it('a response without a mentions list is an error, not an empty inbox', async () => {
		assert.deepEqual(await readInbox({}, fake(() => ok({ result: { next_cursor: null } })).deps), { ok: false, error: 'bad_response' });
	});

	it('every failure is an answer', async () => {
		assert.deepEqual(await readInbox({}, { ...fake(() => null).deps, client: () => null }), { ok: false, error: 'not_connected' });
		assert.deepEqual(await readInbox({}, fake(() => err('FORBIDDEN')).deps), { ok: false, error: 'FORBIDDEN' });
		assert.deepEqual(await readInbox({}, fake(() => new Error('tools/call timed out after 15000ms')).deps), { ok: false, error: 'timeout' });
	});

	it('items: a sender without a display name, an empty body skipped, a long body cut, answered marked', () => {
		const items = toInboxItems([
			mention(1, { sender: { user_id: '@chi:ag2.space', display_name: null }, replied: true }),
			mention(2, { body: '   ' }),
			mention(3, { body: 'x'.repeat(1000), room_name: null }),
			null,
		]);
		assert.equal(items.length, 2);
		assert.equal(items[1].from, 'chi');
		assert.equal(items[1].answered, true);
		assert.equal(items[0].room, '!r:ag2.space');
		assert.ok(items[0].text.endsWith('…') && items[0].text.length < 1000);
	});

	it('items: wrong field types are skipped or defaulted, never thrown', () => {
		const items = toInboxItems([
			mention(1, { sender: { user_id: 42, display_name: 7 } }),
			mention(2, { sender: 'x', sent_at: 5, reason: 3, room_name: 9, room_id: 9 }),
			mention(3, { event_id: 12 }),
		]);
		assert.deepEqual(items.map(i => [i.event_id, i.from, i.room, i.why, i.at]), [['$e2', '?', '?', 'mention', ''], ['$e1', '?', 'Bodhi-migration', 'mention', items[1].at]]);
	});
});

describe('the owner\'s own inbox, where the server offers it', () => {
	const page = (items: unknown[]) => ok({ result: { mentions: items, next_cursor: null, has_more: false, rooms_skipped: 0 } });

	it('the server lists scope "owner": read with it, and say it is the owner\'s', async () => {
		const f = fake(() => page([mention(1)]), 'yes');
		const r = await readInbox({}, f.deps);
		assert.equal(f.described.length, 1);
		assert.equal(f.described[0].args.target_kind, 'account');
		assert.equal(f.described[0].args.action, 'mentions.inbox');
		assert.equal((f.calls[0].args.arguments as Record<string, unknown>).scope, 'owner');
		assert.ok(r.ok && r.whose === 'owner' && !('owner_error' in r));
	});

	it('the server does not list it, or cannot say: read this agent\'s, without scope', async () => {
		for (const owner of ['no', 'fails'] as const) {
			const f = fake(() => page([mention(1)]), owner);
			const r = await readInbox({}, f.deps);
			assert.equal((f.calls[0].args.arguments as Record<string, unknown>).scope, undefined, owner);
			assert.ok(r.ok && r.whose === 'this agent', owner);
		}
	});

	it('offered but refused here (not enabled): this agent\'s inbox, with the refusal', async () => {
		const f = fake((_n, a) => (a.arguments as { scope?: string }).scope === 'owner' ? err('OWNER_SCOPE_NOT_GRANTED') : page([mention(2)]), 'yes');
		const r = await readInbox({}, f.deps);
		assert.equal(f.calls.length, 2);
		assert.equal((f.calls[1].args.arguments as Record<string, unknown>).scope, undefined);
		assert.ok(r.ok && r.whose === 'this agent' && r.owner_error === 'OWNER_SCOPE_NOT_GRANTED' && r.items[0].event_id === '$e2');
	});

	it('a transport failure on the owner read is that failure, not a silent switch', async () => {
		const f = fake(() => new Error('tools/call timed out after 15000ms'), 'yes');
		assert.deepEqual(await readInbox({}, f.deps), { ok: false, error: 'timeout' });
		assert.equal(f.calls.length, 1);
	});

	it('the answer is kept for a while, per client, then asked again', async () => {
		const f = fake(() => page([]), 'yes');
		let t = 1_000;
		assert.equal(await ownerInboxSupported(f.client as never, () => t), true);
		t += OWNER_SCOPE_TTL_MS - 1;
		assert.equal(await ownerInboxSupported(f.client as never, () => t), true);
		assert.equal(f.described.length, 1);
		t += 2;
		await ownerInboxSupported(f.client as never, () => t);
		assert.equal(f.described.length, 2);
		const g = fake(() => page([]), 'no');
		assert.equal(await ownerInboxSupported(g.client as never, () => t), false);
	});
});

describe('read_inbox as the voice model sees it', () => {
	it('answers through readInbox, and an outage is an answer', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false } }));
		const t = createReadInboxTool(f.deps);
		assert.equal(t.name, 'read_inbox');
		assert.equal(t.execution, 'inline');
		assert.equal(((await t.execute({}, {} as never)) as { ok: boolean }).ok, true);
		assert.equal(((await createReadInboxTool({ ...f.deps, client: () => null }).execute({}, {} as never)) as { ok: boolean }).ok, false);
	});

	it('says whose inbox it is, and the rules name it', () => {
		assert.match(createReadInboxTool(fake(() => null).deps).description, /`whose` says which it read/);
		assert.match(INBOX_PROMPT_RULE, /read_inbox/);
		assert.match(INBOX_PROMPT_RULE, /`whose`/);
		assert.match(INBOX_TURN_CHECK_RULE, /^- none:/);
	});
});
}
