// query_messages: "to_me" is the owner's (inbox where offered, else mentions and replies in every room); "all" merges rooms;
// "new" moves only after a complete, unfiltered read, kept per scope and owner; partial reads say so; never a throw.
import { describe, it, test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'query.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const {
	queryMessages, windowStart, resetLastChecked, resetSenderOwners, recordCallEnded, createQueryMessagesTool, addressedTo, QUERY_PROMPT_RULE, QUERY_TURN_CHECK_RULE,
	QUERY_FIRST_WINDOW_MS, QUERY_ROOM_PAGE, QUERY_ROOM_MAX_PAGES, QUERY_SEARCH_MAX_PAGES, QUERY_BUDGET_MS, QUERY_ROOM_SMALL_PAGE, QUERY_ROOM_NEWER_PAGE, QUERY_MAX_GROUPS,
	roomStats, createRoomStatsTool,
} = await import('../skills/ag2space-voice/query.ts');

type Call = { name: string; args: Record<string, unknown> };
const ok = (v: unknown) => ({ structuredContent: v });
const err = (code: string) => ({ isError: true, structuredContent: { code } });
const ME = '@me.agent:ag2.space';
const OWNER = '@susan:ag2.space';
const NOW = Date.UTC(2026, 9, 15, 5, 0); // Oct 14 22:00 PDT
const ROOMS = [{ room_id: '!a:ag2.space', name: 'Alpha' }, { room_id: '!b:ag2.space', name: 'Beta' }];

/** `ownerId`: what whoami says of the owner (none by default); `ownerScope`: whether mentions.inbox offers scope "owner". */
function fake(handler: (name: string, args: Record<string, unknown>) => unknown, rooms: Array<{ room_id: string; name?: string }> = ROOMS, opts: { ownerId?: string; ownerScope?: boolean } = {}) {
	const calls: Call[] = [];
	const client = {
		async callTool(name: string, args: Record<string, unknown>) {
			if (name === 'room.actions.describe') return ok({ input_schema: { properties: opts.ownerScope ? { scope: { enum: ['self', 'owner'] } } : {} } }) as never;
			if (name === 'ag2.whoami') return ok({ actor: { id: ME, kind: 'agent' }, runtime: opts.ownerId ? { owner_id: opts.ownerId } : {} }) as never;
			calls.push({ name, args });
			if (name === 'room.list') return ok({ rooms, next_cursor: null }) as never;
			const r = handler(name, args); if (r instanceof Error) throw r; return r as never;
		},
		close() {},
	};
	const named = rooms.filter(r => r.name) as Array<{ room_id: string; name: string }>;
	const index = { rooms: () => named, refresh: async () => named, fetchedAt: () => 1 };
	const deps = { client: () => client, currentRoom: () => null, index, log: () => {} };
	return { calls, deps, client };
}

const msg = (i: number, sender: string, body: string, minutesAgo: number) => ({ sender, body, event_id: `$m${i}`, ts: String(NOW - minutesAgo * 60_000) });
const item = (i: number, extra: Record<string, unknown> = {}) => ({
	room_id: '!a:ag2.space', room_name: 'Alpha', event_id: `$i${i}`, sender: { user_id: '@chi:ag2.space', display_name: 'Chi Wang' },
	sent_at: new Date(NOW - i * 60_000).toISOString(), body: `ping ${i}`, reason: 'mention', in_reply_to: null, thread_root: null, replied: false, link: null, ...extra,
});
const args = (c: Call) => c.args.arguments as Record<string, unknown>;

const TMP = mkdtempSync(join(tmpdir(), 'voice-query-'));
let heardFile = '';
let n = 0;
beforeEach(() => { heardFile = join(TMP, `heard-${++n}.json`); resetLastChecked(heardFile); resetSenderOwners(); });
after(() => rmSync(TMP, { recursive: true, force: true }));

describe('the window', () => {
	it('hour, day, week, today from local midnight; "new" is the last check, else the last day', () => {
		assert.equal(windowStart('hour', 'all', NOW), NOW - 3_600_000);
		assert.equal(windowStart('day', 'all', NOW), NOW - 86_400_000);
		assert.equal(windowStart('week', 'all', NOW), NOW - 7 * 86_400_000);
		const mid = new Date(NOW); mid.setHours(0, 0, 0, 0);
		assert.equal(windowStart('today', 'all', NOW), mid.getTime());
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW), NOW - QUERY_FIRST_WINDOW_MS);
	});

	it('"new" moves to the time of the last complete, unfiltered check, per scope, and only when asked as "new"', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false } }));
		await queryMessages({}, f.deps, () => NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		await queryMessages({ since: 'hour' }, f.deps, () => NOW + 1000);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW);
	});
});

describe('to_me: the agent\'s inbox', () => {
	it('reads mentions.inbox as the account since the window, counts and lists newest first', async () => {
		const f = fake(() => ok({ result: { mentions: [item(3), item(2), item(1, { room_id: '!dm:ag2.space', room_name: null, reason: 'dm' })], next_cursor: null, has_more: false } }));
		const r = await queryMessages({ latest: 2, since: 'day' }, f.deps, () => NOW);
		const c = f.calls.find(x => x.name === 'room.action.read')!;
		assert.equal(c.args.target_kind, 'account');
		assert.equal(c.args.action, 'mentions.inbox');
		assert.equal(args(c).since, new Date(NOW - 86_400_000).toISOString());
		assert.equal(args(c).include_replied, true);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.scope, 'to_me');
			assert.equal(r.count, 3);
			assert.deepEqual(r.latest.map(m => m.event_id), ['$i1', '$i2']);
			assert.equal(r.latest[0].room, 'a direct message');
			assert.equal(r.latest[0].why, 'dm');
			assert.deepEqual(r.groups, [{ key: 'Alpha', count: 2 }, { key: 'a direct message', count: 1 }]);
			assert.equal(r.oldest?.event_id, '$i3');
		}
	});

	it('a room narrows the inbox; unanswered_only leaves the answered out; more pages mean "at least"', async () => {
		const f = fake(() => ok({ result: { mentions: [item(1, { room_id: '!b:ag2.space', room_name: null })], next_cursor: 'c', has_more: true } }));
		const r = await queryMessages({ room: 'Beta', unanswered_only: true }, f.deps, () => NOW);
		const reads = f.calls.filter(x => x.name === 'room.action.read');
		assert.deepEqual(args(reads[0]).room_ids, ['!b:ag2.space']);
		assert.equal(args(reads[0]).include_replied, false);
		assert.ok(r.ok && r.at_least && r.latest[0].room === 'Beta');
	});
});

describe('all: every room, merged', () => {
	it('reads each room back to the window, leaves its own messages out, merges newest first', async () => {
		const f = fake((_n, a) => {
			const room = a.room_id as string;
			if (room === '!a:ag2.space') return ok({ result: { messages: [msg(1, '@chi:ag2.space', 'hi', 5), msg(2, ME, 'mine', 6), msg(3, '@old:ag2.space', 'old', 90)], cursor: 'x' } });
			return ok({ result: { messages: [msg(4, '@yixuan:ag2.space', 'yo', 1)], cursor: null } });
		});
		const r = await queryMessages({ scope: 'all', since: 'hour' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.count, 2);
			assert.deepEqual(r.latest.map(m => [m.room, m.from]), [['Beta', 'yixuan'], ['Alpha', 'chi']]);
			assert.equal(r.at_least, false);
			assert.equal(r.rooms_unread, undefined);
		}
		assert.equal(f.calls.filter(c => c.name === 'room.action.read').length, 2);
	});

	it('pages a busy room back with the cursor, and says "at least" when the pages run out inside the window', async () => {
		let n = 0;
		const f = fake((_n, a) => {
			if (a.room_id !== '!a:ag2.space') return ok({ result: { messages: [], cursor: null } });
			n++;
			if (n > 1) assert.equal(args({ name: '', args: a }).before, `cur${n - 1}`);
			return ok({ result: { messages: Array.from({ length: QUERY_ROOM_PAGE }, (_, i) => msg(n * 1000 + i, '@chi:ag2.space', 'x', 1)), cursor: `cur${n}` } });
		});
		const r = await queryMessages({ scope: 'all', since: 'day' }, f.deps, () => NOW);
		assert.equal(n, QUERY_ROOM_MAX_PAGES);
		assert.ok(r.ok && r.at_least && r.count === QUERY_ROOM_PAGE * QUERY_ROOM_MAX_PAGES);
	});

	it('filters by sender name and by word; a room that cannot be read is counted', async () => {
		const f = fake((_n, a) => a.room_id === '!b:ag2.space' ? err('FORBIDDEN')
			: ok({ result: { messages: [msg(1, '@chi:ag2.space', 'The Launch plan', 5), msg(2, '@yixuan:ag2.space', 'launch', 6), msg(3, '@chi:ag2.space', 'lunch', 7)], cursor: null } }));
		const r = await queryMessages({ scope: 'all', since: 'hour', from: 'CHI' }, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 2 && r.rooms_unread === 1);
	});

	it('every room unreadable, or no rooms at all, is "unavailable"', async () => {
		assert.deepEqual(await queryMessages({ scope: 'all' }, fake(() => err('FORBIDDEN')).deps, () => NOW), { ok: false, error: 'unavailable' });
		assert.deepEqual(await queryMessages({ scope: 'all' }, fake(() => null, []).deps, () => NOW), { ok: false, error: 'unavailable' });
	});

	it('a word over all rooms is the server\'s full-text search, room ids named from the index', async () => {
		const f = fake((name, a) => {
			assert.equal(name, 'room.action.read');
			assert.equal(a.action, 'space.search.messages');
			assert.equal(a.target_kind, 'account');
			const x = args({ name, args: a });
			assert.equal(x.query, 'launch');
			assert.equal(x.after, new Date(NOW - 7 * 86_400_000).toISOString());
			return ok({ result: { results: [
				{ room_id: '!a:ag2.space', event_id: '$s1', sender: '@chi:ag2.space', snippet: 'launch video', origin_server_ts: NOW - 60_000 },
				{ room_id: '!z:ag2.space', event_id: '$s2', sender: ME, snippet: 'my launch', origin_server_ts: NOW - 30_000 },
				{ room_id: '!z:ag2.space', event_id: '$s3', sender: '@x:ag2.space', snippet: 'launch', origin_server_ts: NOW - 30 * 86_400_000 },
			], cursor: '' } });
		});
		const r = await queryMessages({ scope: 'all', since: 'week', contains: 'Launch' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) { assert.equal(r.count, 1); assert.equal(r.latest[0].room, 'Alpha'); }
	});

	it('search pages follow the cursor and stop at the cap with "at least"; a refusal is its code', async () => {
		let n = 0;
		const f = fake(() => { n++; return ok({ result: { results: [{ room_id: '!a:ag2.space', event_id: `$s${n}`, sender: '@c:ag2.space', snippet: 'w', origin_server_ts: NOW }], cursor: `c${n}` } }); });
		const r = await queryMessages({ scope: 'all', since: 'day', contains: 'w', room: 'Alpha' }, f.deps, () => NOW);
		assert.equal(n, QUERY_SEARCH_MAX_PAGES);
		assert.deepEqual(args(f.calls.find(c => c.name === 'room.action.read')!).room_ids, ['!a:ag2.space']);
		assert.ok(r.ok && r.at_least);
		assert.deepEqual(await queryMessages({ scope: 'all', contains: 'w' }, fake(() => err('RATE_LIMITED')).deps, () => NOW), { ok: false, error: 'RATE_LIMITED' });
	});
});

describe('"new" is never consumed by a filtered or partial read', () => {
	const inbox = (items: unknown[], extra: Record<string, unknown> = {}) => () => ok({ result: { mentions: items, next_cursor: null, has_more: false, rooms_skipped: 0, ...extra } });
	const people = [item(1, { sender: { user_id: '@chi:ag2.space', display_name: 'Chi' } }), item(2, { sender: { user_id: '@bob:ag2.space', display_name: 'Bob' } }), item(3, { sender: { user_id: '@ann:ag2.space', display_name: 'Ann' } })];

	it('"new from Chi" leaves Bob\'s and Ann\'s new for the next "new"', async () => {
		const f = fake(inbox(people));
		const r1 = await queryMessages({ from: 'Chi' }, f.deps, () => NOW);
		assert.ok(r1.ok && r1.count === 1);
		const r2 = await queryMessages({}, f.deps, () => NOW + 1000);
		assert.ok(r2.ok && r2.count === 3);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 3000), NOW + 1000);
	});

	it('a room, a word or unanswered_only is a filter too', async () => {
		for (const a of [{ room: 'Alpha' }, { contains: 'ping' }, { unanswered_only: true }]) {
			resetLastChecked();
			await queryMessages(a, fake(inbox(people)).deps, () => NOW);
			assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS, JSON.stringify(a));
		}
	});

	it('rooms the server skipped, more pages, or an unread room make it partial, and "new" stays', async () => {
		const skipped = await queryMessages({}, fake(inbox(people, { rooms_skipped: 1 })).deps, () => NOW);
		assert.ok(skipped.ok && !skipped.complete && skipped.rooms_skipped === 1);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		const half = fake((_n, a) => a.room_id === '!b:ag2.space' ? err('FORBIDDEN') : ok({ result: { messages: [], cursor: null } }));
		const r = await queryMessages({ scope: 'all' }, half.deps, () => NOW);
		assert.ok(r.ok && !r.complete && r.rooms_unread === 1 && r.rooms_read === 1);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
	});

	it('a later check never moves "new" back', async () => {
		const f = fake(inbox([]));
		await queryMessages({}, f.deps, () => NOW + 10_000);
		await queryMessages({}, f.deps, () => NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 20_000), NOW + 10_000);
	});

	it('a read past the budget is partial and does not move "new"', async () => {
		const f = fake(() => ok({ result: { messages: Array.from({ length: QUERY_ROOM_PAGE }, (_, i) => msg(i, '@c:ag2.space', 'x', 1)), cursor: 'more' } }));
		const r = await queryMessages({ scope: 'all' }, f.deps, () => NOW, 0);
		assert.ok(r.ok === false || !r.complete);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		assert.ok(QUERY_BUDGET_MS < createQueryMessagesTool(f.deps).timeout!);
	});
});

describe('to_me is the owner\'s where the server offers it', () => {
	it('reads with scope "owner" and says whose', async () => {
		const f = fake(() => ok({ result: { mentions: [item(1, { room_id: '!dm:ag2.space', room_name: null, reason: 'dm' })], messages: [], next_cursor: null, has_more: false, rooms_skipped: 0 } }), ROOMS, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.equal(args(f.calls.find(c => (c.args as { action?: string }).action === 'mentions.inbox')!).scope, 'owner');
		assert.ok(r.ok && r.whose === 'owner' && r.count === 1);
	});

	it('a word is matched in the whole message, not the cut shown', async () => {
		const long = 'x'.repeat(600) + ' needle';
		const f = fake(() => ok({ result: { mentions: [item(1, { body: long })], next_cursor: null, has_more: false, rooms_skipped: 0 } }));
		const r = await queryMessages({ contains: 'needle' }, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 1 && r.latest[0].text.endsWith('…'));
	});
});

describe('to_me without the owner\'s inbox: what mentions or answers the owner in every room this agent is in', () => {
	const ROOMS3 = [{ room_id: '!a:ag2.space', name: 'Alpha' }, { room_id: '!b:ag2.space', name: 'Beta' }, { room_id: '!u:ag2.space' }];
	const m = (i: number, sender: string, extra: Record<string, unknown> = {}, minutesAgo = 5) => ({ ...msg(i, sender, `msg ${i}`, minutesAgo), ...extra });
	const rooms = (byRoom: Record<string, unknown[]>) => (_n: string, a: Record<string, unknown>) => ok({ result: { messages: byRoom[a.room_id as string] ?? [], cursor: null } });

	it('the rule: named in mentions; with no mentions, its id in the body; or a reply to the owner', () => {
		assert.equal(addressedTo({ body: 'hi', mentions: [OWNER] }, OWNER), 'mention');
		assert.equal(addressedTo({ body: `hi ${OWNER}`, mentions: ['@x:ag2.space'] }, OWNER), null);
		assert.equal(addressedTo({ body: `hi ${OWNER}` }, OWNER), 'mention');
		assert.equal(addressedTo({ body: `hi ${OWNER}`, mentions: [] }, OWNER), 'mention');
		assert.equal(addressedTo({ body: 'ok', replyTo: OWNER }, OWNER), 'reply');
		assert.equal(addressedTo({ body: 'Susan, look', mentions: [] }, OWNER), null);
	});

	it('reads every room it is in, named or not, and keeps only what is for the owner', async () => {
		const f = fake(rooms({
			'!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] }), m(2, '@chi:ag2.space', { mentions: ['@bob:ag2.space'] }), m(3, OWNER, { mentions: [OWNER] }), m(4, ME, { mentions: [OWNER] })],
			'!b:ag2.space': [m(5, '@yx:ag2.space', { reply_to_sender: OWNER }, 2)],
			'!u:ag2.space': [{ ...msg(6, '@ann:ag2.space', `ping ${OWNER}`, 1) }],
		}), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(f.calls.some(c => c.name === 'room.list'));
		assert.equal(f.calls.filter(c => c.name === 'room.action.read' && (c.args as { action?: string }).action !== 'space.user.profile.read').length, 3);
		assert.ok(!f.calls.some(c => (c.args as { action?: string }).action === 'mentions.inbox'));
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.whose, 'owner');
			assert.equal(r.source, 'rooms');
			assert.equal(r.rooms_read, 3);
			assert.deepEqual(r.latest.map(x => [x.event_id, x.room, x.why]), [['$m6', 'an unnamed room', 'mention'], ['$m5', 'Beta', 'reply'], ['$m1', 'Alpha', 'mention']]);
			assert.equal(r.complete, true);
		}
	});

	it('with the owner\'s inbox too: rooms it is in are read whole, the inbox adds direct messages and other rooms, nothing twice', async () => {
		// 2026-10-16: the server's inbox reads each room's latest 50 only, and four mentions in Sutando Triage from that morning were missing.
		const inbox = [
			item(1, { room_id: '!a:ag2.space', room_name: 'Alpha', event_id: '$m1' }),        // also in the scan: counted once
			item(2, { room_id: '!dm:ag2.space', room_name: null, reason: 'dm', event_id: '$dm' }),
			item(3, { room_id: '!x:ag2.space', room_name: 'Elsewhere', event_id: '$x' }),    // a room this agent is not in
		];
		const scan = rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] }), m(7, '@qy:ag2.space', { mentions: [OWNER] }, 600)] });
		const f = fake((n, a) => (a.action === 'mentions.inbox' ? ok({ result: { mentions: inbox, next_cursor: null, has_more: false, rooms_skipped: 0 } }) : scan(n, a)), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.source, 'rooms+inbox');
			assert.equal(r.whose, 'owner');
			assert.deepEqual(r.latest.map(x => x.event_id).sort(), ['$dm', '$m1', '$m7', '$x']);
			assert.equal(r.count, 4);
		}
	});

	it('unanswered_only leaves out what the inbox marks answered, in scanned rooms too; one it does not list stays', async () => {
		const inbox = [item(1, { room_id: '!a:ag2.space', event_id: '$m1', replied: true })];
		const scan = rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] }), m(2, '@bob:ag2.space', { mentions: [OWNER] })] });
		const f = fake((n, a) => (a.action === 'mentions.inbox' ? ok({ result: { mentions: inbox, next_cursor: null, has_more: false, rooms_skipped: 0 } }) : scan(n, a)), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day', unanswered_only: true }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) assert.deepEqual(r.latest.map(x => x.event_id), ['$m2']);
		assert.equal(args(f.calls.find(c => (c.args as { action?: string }).action === 'mentions.inbox')!).include_replied, true, 'the answered ones are read, to know them');
	});

	it('an inbox that does not answer leaves the scan, said as rooms only; rooms that cannot be read leave the inbox', async () => {
		const scan = rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] })] });
		const f = fake((n, a) => (a.action === 'mentions.inbox' ? new Error('tools/call timed out after 15000ms') : scan(n, a)), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.source === 'rooms' && r.count === 1);
		const g = fake((_n, a) => (a.action === 'mentions.inbox' ? ok({ result: { mentions: [item(2, { room_id: '!a:ag2.space', event_id: '$i' })], next_cursor: null, has_more: false, rooms_skipped: 0 } }) : err('FORBIDDEN')), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const t = await queryMessages({ since: 'day' }, g.deps, () => NOW);
		assert.ok(t.ok && t.count === 1 && !t.complete && t.rooms_unread === 3, 'the inbox fills in for rooms not read, and it is partial');
	});

	it('what the owner\'s own agents sent is left out of "to me"; people and unknown senders stay', async () => {
		const MADDY = '@susan-maddy.agent:ag2.space';
		const elsewhere = { room_id: '!x:ag2.space', room_name: 'Elsewhere' };
		const items = [item(1, elsewhere), item(2, { ...elsewhere, sender: { user_id: MADDY, display_name: 'Maddy' } }), item(3, { ...elsewhere, sender: { user_id: '@dot.agent:ag2.space', display_name: 'Dot' } }), item(4, { ...elsewhere, sender: { user_id: '@lost:ag2.space', display_name: 'Lost' } })];
		const profiles: Record<string, unknown> = { '@chi:ag2.space': ok({ result: { owner: null, kind: 'human' } }), [MADDY]: ok({ result: { owner: OWNER, kind: 'agent' } }), '@dot.agent:ag2.space': ok({ result: { owner: '@someone-else:ag2.space', kind: 'agent' } }), '@lost:ag2.space': err('UPSTREAM_UNAVAILABLE') };
		const handler = (_n: string, a: Record<string, unknown>) => a.action === 'space.user.profile.read'
			? profiles[(a.arguments as { user_id: string }).user_id]
			: ok({ result: { mentions: items, messages: [], next_cursor: null, has_more: false, rooms_skipped: 0 } });
		const f = fake(handler, ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.count, 3);
			assert.deepEqual(r.latest.map(l => l.from).sort(), ['Chi Wang', 'Dot', 'Lost']);
		}
		// Each sender is looked up once; a second query asks only for the one that failed.
		const lookups = () => f.calls.filter(c => (c.args as { action?: string }).action === 'space.user.profile.read').length;
		assert.equal(lookups(), 4);
		await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.equal(lookups(), 5);
	});

	it('the room scan leaves out the owner\'s agents too', async () => {
		const MADDY = '@susan-maddy.agent:ag2.space';
		const scan = rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] }), m(2, MADDY, { mentions: [OWNER] })] });
		const f = fake((n, a) => a.action === 'space.user.profile.read'
			? ok({ result: { owner: (a.arguments as { user_id: string }).user_id === MADDY ? OWNER : null } })
			: scan(n, a), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.source === 'rooms' && r.count === 1);
	});

	it('offered but refused: the scan, with the refusal', async () => {
		const f = fake((n, a) => (a.action === 'mentions.inbox' ? err('OWNER_SCOPE_NOT_GRANTED') : rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] })] })(n, a)), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.source === 'rooms' && r.whose === 'owner' && r.count === 1);
	});

	it('a room not read makes it partial; "new" is kept per owner and stays', async () => {
		const f = fake((n, a) => (a.room_id === '!b:ag2.space' ? err('FORBIDDEN') : ok({ result: { messages: [], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.ok(r.ok && !r.complete && r.rooms_unread === 1);
		assert.equal(windowStart('last_check', `to_me:${OWNER}`, NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		const g = fake(rooms({}), ROOMS3, { ownerId: OWNER });
		await queryMessages({}, g.deps, () => NOW);
		assert.equal(windowStart('last_check', `to_me:${OWNER}`, NOW + 5), NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
	});

	it('a room read that fails once (an upstream 502) is tried again; a timeout in smaller pages, only with time left', async () => {
		let n = 0;
		const f = fake((_n, a) => (a.room_id === '!a:ag2.space' && ++n === 1 ? new Error('room.action.read: AG2 MCP upstream failure (not retried): upstream http 502') : ok({ result: { messages: [m(1, '@chi:ag2.space', { mentions: [OWNER] })], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.complete && !r.rooms_unread);
		assert.equal(f.calls.filter(c => c.args.room_id === '!a:ag2.space').length, 2);
		const limits: unknown[] = [];
		const g = fake((_n, a) => (a.room_id === '!a:ag2.space' ? (limits.push(args({ name: '', args: a }).limit), new Error('tools/call timed out after 15000ms')) : ok({ result: { messages: [], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const t = await queryMessages({ since: 'day' }, g.deps, () => NOW);
		assert.ok(t.ok && t.rooms_unread === 1);
		assert.deepEqual(t.ok && t.unread_rooms, ['Alpha']);
		assert.deepEqual(limits, [QUERY_ROOM_PAGE, QUERY_ROOM_SMALL_PAGE]);
		// With less than one more call's time left in the budget, no second try.
		limits.length = 0;
		const h = fake((_n, a) => (a.room_id === '!a:ag2.space' ? (limits.push(1), new Error('tools/call timed out after 15000ms')) : ok({ result: { messages: [], cursor: null } })), ROOMS3, { ownerId: OWNER });
		await queryMessages({ since: 'day' }, h.deps, () => NOW, 1000);
		assert.equal(limits.length, 1);
	});

	it('a message the server withholds (body null, a conversation this agent is not in) is counted, said as not shown, and matches no word', async () => {
		// 2026-10-16: Yixuan's direct message to the owner came with body null and was dropped as malformed.
		const dm = item(1, { room_id: '!dm:ag2.space', room_name: null, reason: 'dm', body: null, sender: { user_id: '@yixuan:ag2.space', display_name: 'Yixuan Zhai' } });
		const f = fake((_n, a) => (a.action === 'mentions.inbox' ? ok({ result: { mentions: [dm], next_cursor: null, has_more: false, rooms_skipped: 0 } }) : ok({ result: { messages: [], cursor: null, owner: null } })), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 1);
		if (r.ok) assert.deepEqual([r.latest[0].from, r.latest[0].why, r.latest[0].text], ['Yixuan Zhai', 'dm', '(not shown: you are not in that conversation)']);
		const w = await queryMessages({ since: 'day', contains: 'conversation' }, f.deps, () => NOW);
		assert.ok(w.ok && w.count === 0);
	});

	it('one malformed inbox item is skipped, never a crash', async () => {
		const f = fake(() => ok({ result: { mentions: [null, 7, { sender: null }, item(1)], next_cursor: null, has_more: false, rooms_skipped: 0 } }));
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 1);
	});
});

describe('"new" across calls: kept when a call ends (post-processing), read after a restart', () => {
	const inbox = () => ok({ result: { mentions: [], next_cursor: null, has_more: false, rooms_skipped: 0 } });

	it('a call that checked "new" leaves its point behind; a restarted process starts there', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
		resetLastChecked(heardFile);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 60_000), NOW);
	});

	it('a call that never asked leaves the earlier point as it was', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		resetLastChecked(heardFile);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 60_000), NOW);
	});

	it('only a complete, unfiltered "new" moves it; the point never goes back', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		resetLastChecked(heardFile);
		await queryMessages({ from: 'Chi' }, fake(inbox).deps, () => NOW + 5_000);
		await queryMessages({}, fake(inbox).deps, () => NOW - 5_000);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
	});

	it('no file yet, or a broken one: the last day', () => {
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW), NOW - QUERY_FIRST_WINDOW_MS);
		resetLastChecked(join(TMP, 'missing', 'x.json'));
		assert.equal(windowStart('last_check', 'all', NOW), NOW - QUERY_FIRST_WINDOW_MS);
	});

	it('the voice agent runs skills\' call-ended steps from its hang-up report, before telling the core', () => {
		const src = readFileSync(join(process.cwd(), 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /onCallEnded: \(handler\) => \{ callEndedHandlers\.push\(handler\); \}/);
		const onEnded = src.slice(src.indexOf('const callEnd = createCallEndReporter('), src.indexOf('beforeConversationClear = () => callEnd.collect()'));
		assert.ok(onEnded.indexOf('for (const h of callEndedHandlers)') >= 0 && onEnded.indexOf('for (const h of callEndedHandlers)') < onEnded.indexOf('submitVoiceSessionEndTask'));
	});

	it('the plugin registers it as the voice agent\'s call-ended step', async () => {
		const { setup } = await import('../skills/ag2space-voice/tools.ts');
		let handler: (() => unknown) | null = null;
		setup({ onCallEnded: (h: () => unknown) => { handler = h; } } as never);
		assert.ok(handler);
		await queryMessages({}, fake(inbox).deps, () => NOW);
		await handler!();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
	});
});

describe('a busy room: what was read counts, and asking again reads only what is newer', () => {
	const page = (from: number, count: number, minutesAgo: number) => Array.from({ length: count }, (_, i) => msg(from + i, '@chi:ag2.space', `m${from + i}`, minutesAgo));

	it('a later page that fails keeps the earlier pages, and the room is named as read in part', async () => {
		const f = fake((_n, a) => {
			if (a.room_id !== '!a:ag2.space') return ok({ result: { messages: [], cursor: null } });
			return args({ name: '', args: a }).before ? err('UPSTREAM_UNAVAILABLE') : ok({ result: { messages: page(0, QUERY_ROOM_PAGE, 5), cursor: 'p2' } });
		});
		const r = await queryMessages({ scope: 'all', since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.count, QUERY_ROOM_PAGE);
			assert.equal(r.at_least, true);
			assert.equal(r.complete, false);
			assert.deepEqual(r.partial_rooms, ['Alpha']);
			assert.equal(r.rooms_unread, undefined);
		}
	});

	it('asked again, a room is read only back to what the last read returned, and nothing is lost', async () => {
		let pages = 0;
		const limits: unknown[] = [];
		let first: unknown[] = page(0, QUERY_ROOM_PAGE, 5);
		const f = fake((_n, a) => {
			if (a.room_id !== '!a:ag2.space') return ok({ result: { messages: [], cursor: null } });
			pages++;
			limits.push(args({ name: '', args: a }).limit);
			return args({ name: '', args: a }).before
				? ok({ result: { messages: [...page(500, 3, 10), msg(999, '@old:ag2.space', 'old', 120)], cursor: null } })
				: ok({ result: { messages: first, cursor: 'p2' } });
		});
		const r1 = await queryMessages({ scope: 'all', since: 'hour' }, f.deps, () => NOW);
		assert.equal(pages, 2);
		first = [msg(900, '@ann:ag2.space', 'new one', 1), ...page(0, QUERY_ROOM_PAGE - 1, 5)];
		const r2 = await queryMessages({ scope: 'all', since: 'hour' }, f.deps, () => NOW);
		// One small page: it reached a message the first read returned, and the older page came from that read.
		assert.equal(pages, 3);
		assert.deepEqual(limits, [QUERY_ROOM_PAGE, QUERY_ROOM_PAGE, QUERY_ROOM_NEWER_PAGE]);
		assert.ok(r1.ok && r2.ok);
		if (r1.ok && r2.ok) {
			assert.equal(r1.count, QUERY_ROOM_PAGE + 3);
			assert.equal(r2.count, QUERY_ROOM_PAGE + 4);
			assert.equal(r2.latest[0].event_id, '$m900');
			assert.equal(r2.complete, true);
		}
	});

	it('a wider window than the last read reads the room afresh', async () => {
		let pages = 0;
		const f = fake((_n, a) => {
			if (a.room_id !== '!a:ag2.space') return ok({ result: { messages: [], cursor: null } });
			pages++;
			return ok({ result: { messages: [msg(1, '@chi:ag2.space', 'new', 5), msg(2, '@chi:ag2.space', 'earlier', 180)], cursor: null } });
		});
		const hour = await queryMessages({ scope: 'all', since: 'hour' }, f.deps, () => NOW);
		const week = await queryMessages({ scope: 'all', since: 'week' }, f.deps, () => NOW);
		assert.ok(hour.ok && hour.count === 1);
		assert.ok(week.ok && week.count === 2 && week.complete);
		assert.equal(pages, 2);
	});
});

describe('most, least, how many: grouped in the result, never worked out by the model', () => {
	const at = (h: number) => Date.UTC(2026, 9, 15, h, 0);
	const rows = {
		'!a:ag2.space': [msg(1, '@chi:ag2.space', 'a', 10), msg(2, '@chi:ag2.space', 'b', 20), msg(3, '@ann:ag2.space', 'c', 30)],
		'!b:ag2.space': [msg(4, '@chi:ag2.space', 'd', 40)],
		'!c:ag2.space': [],
	};
	const ROOMS_ABC = [...ROOMS, { room_id: '!c:ag2.space', name: 'Gamma' }];
	const scan = () => fake((_n, a) => ok({ result: { messages: rows[a.room_id as keyof typeof rows] ?? [], cursor: null } }), ROOMS_ABC);

	it('by room, a room with nothing is a group of 0, and "least" puts it first', async () => {
		const most = await queryMessages({ scope: 'all', since: 'day' }, scan().deps, () => NOW);
		const least = await queryMessages({ scope: 'all', since: 'day', rank: 'least' }, scan().deps, () => NOW);
		assert.ok(most.ok && least.ok);
		if (most.ok && least.ok) {
			assert.deepEqual(most.groups, [{ key: 'Alpha', count: 3 }, { key: 'Beta', count: 1 }, { key: 'Gamma', count: 0 }]);
			assert.deepEqual(least.groups[0], { key: 'Gamma', count: 0 });
			assert.equal(most.groups_total, 3);
			assert.equal(most.distinct_rooms, 2);
			assert.equal(most.distinct_senders, 2);
			assert.equal(most.oldest?.event_id, '$m4');
			assert.equal(most.latest[0].event_id, '$m1');
		}
	});

	it('by sender, by hour and by day', async () => {
		const s = await queryMessages({ scope: 'all', since: 'day', group_by: 'sender' }, scan().deps, () => NOW);
		assert.ok(s.ok && s.group_by === 'sender');
		if (s.ok) assert.deepEqual(s.groups, [{ key: 'chi', count: 3 }, { key: 'ann', count: 1 }]);
		const h = await queryMessages({ scope: 'all', since: 'day', group_by: 'hour' }, scan().deps, () => NOW);
		assert.ok(h.ok);
		if (h.ok) {
			assert.equal(h.groups.reduce((n, g) => n + g.count, 0), 4);
			assert.ok(h.groups.every(g => /^\d\d:00$/.test(g.key)));
		}
		const d = await queryMessages({ scope: 'all', since: 'day', group_by: 'day' }, scan().deps, () => NOW);
		assert.ok(d.ok && d.groups.length >= 1 && /\w{3}, \w{3} \d+/.test(d.groups[0].key));
		void at;
	});

	it('at most a fixed number of groups, with how many there were', async () => {
		const many = Array.from({ length: QUERY_MAX_GROUPS + 3 }, (_, i) => msg(i, `@p${i}:ag2.space`, 'x', 5));
		const f = fake((_n, a) => ok({ result: { messages: a.room_id === '!a:ag2.space' ? many : [], cursor: null } }));
		const r = await queryMessages({ scope: 'all', since: 'day', group_by: 'sender' }, f.deps, () => NOW);
		assert.ok(r.ok && r.groups.length === QUERY_MAX_GROUPS && r.groups_total === QUERY_MAX_GROUPS + 3 && r.distinct_senders === QUERY_MAX_GROUPS + 3);
	});

	it('the description and the rule say "today" is since midnight and that nothing is estimated', async () => {
		const t = createQueryMessagesTool(scan().deps);
		assert.match(t.description, /"today" \(since midnight: "today", "今天"\), "day" \(the last 24 hours\)/);
		assert.match(t.description, /never estimate it/);
		assert.match(t.description, /say so first, before any number/);
		assert.match(QUERY_PROMPT_RULE, /"今天" is since "today", not "day"/);
		assert.match(QUERY_PROMPT_RULE, /room_stats/);
	});
});

describe('room_stats: how many rooms, and members in each', () => {
	const members = (n: number) => ok({ result: { members: Array.from({ length: n }, (_, i) => ({ user_id: `@u${i}:ag2.space`, display_name: `u${i}` })) } });
	const ROOMS_ABC = [...ROOMS, { room_id: '!c:ag2.space' }];

	it('counts every room it is in, named or not, and ranks them by members', async () => {
		const f = fake((_n, a) => (a.action === 'room.members.list' ? members(({ '!a:ag2.space': 5, '!b:ag2.space': 12, '!c:ag2.space': 2 } as Record<string, number>)[a.room_id as string]) : null), ROOMS_ABC);
		const r = await roomStats({}, f.deps);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.rooms_total, 3);
			assert.deepEqual(r.rooms, [{ room: 'Beta', members: 12 }, { room: 'Alpha', members: 5 }, { room: 'an unnamed room', members: 2 }]);
			assert.equal(r.complete, true);
		}
		const least = await roomStats({ rank: 'least' }, f.deps);
		assert.ok(least.ok && least.rooms[0].members === 2);
	});

	it('a room it cannot count is named, never ranked; no client or no room list is an error', async () => {
		const f = fake((_n, a) => (a.room_id === '!b:ag2.space' ? err('FORBIDDEN') : members(3)), ROOMS_ABC);
		const r = await roomStats({}, f.deps);
		assert.ok(r.ok && !r.complete && r.rooms_unread === 1);
		if (r.ok) { assert.deepEqual(r.unread_rooms, ['Beta']); assert.equal(r.rooms.length, 2); }
		assert.deepEqual(await roomStats({}, { ...f.deps, client: () => null }), { ok: false, error: 'not_connected' });
		const broken = { ...f.deps, client: () => ({ callTool: async () => { throw new Error('down'); }, close() {} }) };
		assert.deepEqual(await roomStats({}, broken as never), { ok: false, error: 'unavailable' });
	});

	it('the tool answers through roomStats', async () => {
		const t = createRoomStatsTool(fake(() => members(1)).deps);
		assert.equal(t.name, 'room_stats');
		assert.equal(t.execution, 'inline');
		assert.equal(((await t.execute({}, {} as never)) as { ok: boolean }).ok, true);
		assert.match(t.description, /rooms you, the agent, are in/);
	});
});

describe('answers, never throws', () => {
	it('no client, an unknown room, an inbox refusal', async () => {
		const f = fake(() => err('FORBIDDEN'), []);
		assert.deepEqual(await queryMessages({}, { ...f.deps, client: () => null }), { ok: false, error: 'not_connected' });
		const nf = await queryMessages({ room: 'Gamma' }, f.deps, () => NOW);
		assert.equal(nf.ok, false);
		assert.deepEqual(await queryMessages({}, f.deps, () => NOW), { ok: false, error: 'FORBIDDEN' });
	});

	it('the tool answers through queryMessages, and the rules name it', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false } }));
		const t = createQueryMessagesTool(f.deps);
		assert.equal(t.name, 'query_messages');
		assert.equal(t.execution, 'inline');
		assert.equal(((await t.execute({}, {} as never)) as { ok: boolean }).ok, true);
		assert.equal(((await createQueryMessagesTool({ ...f.deps, client: () => null }).execute({ scope: 'all', room: 'Alpha', from: 'x', contains: 'y', since: 'hour' }, {} as never)) as { ok: boolean }).ok, false);
		assert.match(t.description, /no read\/unread marker/);
		assert.match(QUERY_PROMPT_RULE, /query_messages/);
		assert.match(QUERY_TURN_CHECK_RULE, /^- fetch query_messages: .*"the quietest room".* fetch room_stats: how many rooms there are in all, or members in a room .*rooms that have new messages are query_messages\./);
		assert.match(QUERY_TURN_CHECK_RULE, /Never work\. A follow-up about the result just given .* is none\./);
	});
});
}
