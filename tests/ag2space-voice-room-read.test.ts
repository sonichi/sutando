/**
 * read_room — the voice model reads a room's recent messages through the AG2 Space MCP.
 *
 * Pins: the core's `ag2-space` MCP entry is what the voice agent starts (found under
 * CLAUDE_CONFIG_DIR, absent → no tool); the stdio JSON-RPC client against a real child
 * process (initialize, tools/call, timeout, restart after the child dies); a spoken room
 * name resolved to one room (exact wins, one candidate, ambiguous, not found); the
 * current room when none is named; messages oldest first, trimmed for speech; every
 * failure is an answer the model can say, never a throw.
 */
import { describe, it, test, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

// The plugin is optional: with skills/ag2space-voice/ removed this file skips.
const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'room-read.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const {
	findAg2SpaceMcpEntry, createMcpStdioClient, mcpPayload, pickRoom, toMessageLines, readRoom, createReadRoomTool,
	listRooms, createRoomIndex, roomIndexContextLines, createListRoomsTool, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_READ_CALL_TIMEOUT_MS,
	searchRoom, createSearchRoomTool, toHitLines, ROOM_SEARCH_DEFAULT_LIMIT, ROOM_TURN_CHECK_RULE,
	ROOM_READ_BODY_MAX, ROOM_READ_DEFAULT_LIMIT, ROOM_READ_PROMPT_RULE,
} = await import(pathToFileURL(join(PLUGIN_DIR, 'room-read.ts')).href);

const dirs: string[] = [];
after(() => { for (const d of dirs) rmSync(d, { recursive: true, force: true }); });
const tmp = () => { const d = mkdtempSync(join(tmpdir(), 'room-read-')); dirs.push(d); return d; };

describe('the voice agent starts the MCP server the core registered', () => {
	it('reads the ag2-space stdio entry from CLAUDE_CONFIG_DIR/.claude.json', () => {
		const d = tmp();
		writeFileSync(join(d, '.claude.json'), JSON.stringify({ mcpServers: {
			other: { type: 'stdio', command: 'x' },
			'ag2-space': { type: 'stdio', command: '/app/node', args: ['/app/ag2-mcp-proxy.mjs'], env: { AG2_MCP_DESCRIPTOR: '/app/d.json', N: 3 } },
		} }));
		assert.deepEqual(findAg2SpaceMcpEntry({ CLAUDE_CONFIG_DIR: d }), { command: '/app/node', args: ['/app/ag2-mcp-proxy.mjs'], env: { AG2_MCP_DESCRIPTOR: '/app/d.json' } });
	});

	it('no entry, no file, or a non-stdio entry → null, so read_room is not declared', () => {
		const d = tmp();
		assert.equal(findAg2SpaceMcpEntry({ CLAUDE_CONFIG_DIR: d }), null, 'no file');
		writeFileSync(join(d, '.claude.json'), JSON.stringify({ mcpServers: { other: { command: 'x' } } }));
		assert.equal(findAg2SpaceMcpEntry({ CLAUDE_CONFIG_DIR: d }), null, 'no entry');
		writeFileSync(join(d, '.claude.json'), JSON.stringify({ mcpServers: { 'ag2-space': { type: 'http', url: 'https://x' } } }));
		assert.equal(findAg2SpaceMcpEntry({ CLAUDE_CONFIG_DIR: d }), null, 'http entry');
	});
});

// A fake MCP server: answers initialize and tools/call; "slow" never answers; "die" exits.
const FAKE_SERVER = `
const rl = require('node:readline').createInterface({ input: process.stdin });
let calls = 0;
rl.on('line', (l) => {
	const m = JSON.parse(l);
	if (m.id === undefined) return;
	if (m.method === 'initialize') return process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: '2025-06-18' } }) + '\\n');
	const name = m.params && m.params.name;
	if (name === 'slow') return;
	if (name === 'die') process.exit(3);
	calls++;
	process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, result: { structuredContent: { echo: m.params.arguments, calls, pid: process.pid } } }) + '\\n');
});
`;

describe('the stdio MCP client', () => {
	const entry = { command: process.execPath, args: ['-e', FAKE_SERVER], env: {} };

	it('initializes once and calls tools', async () => {
		const c = createMcpStdioClient(entry);
		try {
			const a = mcpPayload(await c.callTool('room.list', { limit: 1 }));
			const b = mcpPayload(await c.callTool('room.list', { limit: 2 }));
			assert.deepEqual(a.echo, { limit: 1 });
			assert.equal(b.calls, 2, 'same child for both calls');
			assert.equal(a.pid, b.pid);
		} finally { c.close(); }
	});

	it('a call with no answer times out instead of hanging', async () => {
		const c = createMcpStdioClient(entry);
		try {
			await c.callTool('room.list', {});
			const t0 = Date.now();
			await assert.rejects(c.callTool('slow', {}, 150), /timed out/);
			assert.ok(Date.now() - t0 < 1000);
		} finally { c.close(); }
	});

	it('a server that dies fails its call at once, and the next call starts a new one', async () => {
		const c = createMcpStdioClient(entry);
		try {
			const first = mcpPayload(await c.callTool('room.list', {}));
			await assert.rejects(c.callTool('die', {}, 3000), /exited/);
			const again = mcpPayload(await c.callTool('room.list', {}));
			assert.notEqual(again.pid, first.pid, 'a fresh server');
		} finally { c.close(); }
	});

	it('a command that cannot start is an error, not a hang', async () => {
		const c = createMcpStdioClient({ command: join(tmp(), 'no-such-binary'), args: [] });
		await assert.rejects(c.callTool('room.list', {}, 2000));
		c.close();
	});
});

describe('a spoken room name → one room', () => {
	const rooms = [
		{ room_id: '!a:ag2.space', name: 'Sutando Triage' },
		{ room_id: '!b:ag2.space', name: 'triage system' },
	];
	it('an exact name wins over other matches, ignoring case and spacing', () => {
		assert.deepEqual(pickRoom('sutando  triage', rooms), { ok: true, room: rooms[0] });
	});
	it('one room containing what was said is it; several are ambiguous with their names; none is not_found', () => {
		assert.deepEqual(pickRoom('triage sys', rooms), { ok: true, room: rooms[1] });
		assert.deepEqual(pickRoom('the Sutando Triage room', rooms), { ok: true, room: rooms[0] }, 'the name inside more words');
		assert.deepEqual(pickRoom('triage', rooms), { ok: false, error: 'ambiguous', candidates: ['Sutando Triage', 'triage system'] });
		assert.deepEqual(pickRoom('GTM', []), { ok: false, error: 'not_found' });
		// 2026-10-13 17:41: "Sutando Triage" was heard as "Sutando Triaging"; no guessing here — the model sees the index.
		assert.deepEqual(pickRoom('Sutando Triaging', rooms), { ok: false, error: 'not_found' });
	});
});

describe('messages are given oldest first and short enough to retell', () => {
	it('newest-first server order is reversed; the sender is the local part; long bodies are cut', () => {
		const long = 'x'.repeat(ROOM_READ_BODY_MAX + 50);
		const lines = toMessageLines([
			{ sender: '@sutando-sonichi:ag2.space', body: 'second', ts: 2, reply_to_event: '$e' },
			{ sender: '@liususan091219:ag2.space', body: long, ts: 1 },
			{ sender: '@x:ag2.space', body: '   ' },
		], 'UTC');
		assert.equal(lines.length, 2, 'an empty body is dropped');
		assert.equal(lines[0].from, 'liususan091219');
		assert.equal(lines[0].text.length, ROOM_READ_BODY_MAX + 1);
		assert.deepEqual({ from: lines[1].from, text: lines[1].text, reply: lines[1].reply }, { from: 'sutando-sonichi', text: 'second', reply: true });
	});
});

describe('the room index: the rooms the agent can read, with their real names', () => {
	type Call = { name: string; args: Record<string, unknown>; timeoutMs?: number };
	const fake = (handler: (name: string, args: Record<string, unknown>) => unknown) => {
		const calls: Call[] = [];
		return {
			calls,
			client: { async callTool(name: string, args: Record<string, unknown>, timeoutMs?: number) { calls.push({ name, args, timeoutMs }); const r = handler(name, args); if (r instanceof Error) throw r; return r; }, close() {} },
		};
	};

	it('lists the rooms the agent is in, following pages, named rooms only', async () => {
		const f = fake((_n, args) => args.cursor
			? { structuredContent: { rooms: [{ room_id: '!c:x', name: 'Bodhi-migration' }], next_cursor: null } }
			: { structuredContent: { rooms: [{ room_id: '!a:x', name: 'Sutando Triage' }, { room_id: '!dm:x', name: '' }, { room_id: '!b:x', name: ' triage system ' }], next_cursor: 'p2' } });
		assert.deepEqual(await listRooms(f.client), [{ room_id: '!a:x', name: 'Sutando Triage' }, { room_id: '!b:x', name: 'triage system' }, { room_id: '!c:x', name: 'Bodhi-migration' }]);
		assert.deepEqual(f.calls.map(c => c.args), [{ scope: 'self', limit: 50 }, { scope: 'self', limit: 50, cursor: 'p2' }]);
		const bad = fake(() => ({ isError: true, content: [{ type: 'text', text: '{"code":"UNAUTHENTICATED"}' }] }));
		await assert.rejects(listRooms(bad.client), /room.list: UNAUTHENTICATED/);
	});

	it('is empty before the first fetch, refreshed once at a time, kept when a refresh fails, and refreshed again when stale', async () => {
		let now = 1_000_000;
		let fail = false;
		const f = fake(() => fail ? new Error('ag2-space MCP exited (1)') : { structuredContent: { rooms: [{ room_id: '!a:x', name: 'Sutando Triage' }] } });
		const logs: string[] = [];
		const index = createRoomIndex({ client: () => f.client, log: (m) => logs.push(m), ttlMs: 1000, now: () => now });
		assert.deepEqual(index.rooms(), [], 'nothing yet; a fetch starts');
		assert.equal(index.fetchedAt(), 0);
		const [a, b] = await Promise.all([index.refresh(), index.refresh()]);
		assert.equal(a, b);
		assert.equal(f.calls.length, 1, 'one fetch for the first rooms() and both refresh()es');
		assert.deepEqual(index.rooms().map(r => r.name), ['Sutando Triage']);
		assert.equal(index.fetchedAt(), now);
		assert.equal(f.calls.length, 1, 'fresh: no fetch');
		fail = true;
		await assert.rejects(index.refresh(), /exited/);
		assert.deepEqual(index.rooms().map(r => r.name), ['Sutando Triage'], 'the last list is kept');
		assert.ok(logs.some(l => l.includes('[RoomIndex] refresh failed')));
		fail = false;
		now += 1001;
		index.rooms();
		await new Promise(r => setImmediate(r));
		assert.equal(f.calls.length, 3, 'stale: a background fetch');
		assert.equal(index.fetchedAt(), now);
		// The first fetch mints the token and lists cold (6.7 s measured): it waits longer; later ones do not.
		assert.deepEqual(f.calls.map(c => c.timeoutMs), [ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_READ_CALL_TIMEOUT_MS, ROOM_READ_CALL_TIMEOUT_MS]);
		assert.deepEqual(roomIndexContextLines({ rooms: () => [], refresh: async () => [], fetchedAt: () => 0 }), []);
	});

	it('the context line names every room exactly and says a heard name is matched to one of them', () => {
		const index = { rooms: () => [{ room_id: '!a:x', name: 'Sutando Triage' }, { room_id: '!b:x', name: 'open.space launch video' }], refresh: async () => [], fetchedAt: () => 1 };
		const [line, ...rest] = roomIndexContextLines(index);
		assert.deepEqual(rest, []);
		assert.match(line, /^ROOMS: AG2 Space rooms you can read with read_room: Sutando Triage \| open\.space launch video\./);
		assert.match(line, /often misheard/);
		assert.match(line, /spelled exactly like this/);
	});

	it('list_rooms fetches now and is inline; when the fetch fails it says so with the last list', async () => {
		let fail = false;
		const f = fake(() => fail ? new Error('tools/call timed out after 6000ms') : { structuredContent: { rooms: [{ room_id: '!a:x', name: 'Sutando Triage' }] } });
		const index = createRoomIndex({ client: () => f.client });
		const tool = createListRoomsTool({ index });
		assert.equal(tool.name, 'list_rooms');
		assert.equal(tool.execution, 'inline');
		assert.deepEqual(await tool.execute({}, {} as never), { ok: true, rooms: ['Sutando Triage'] });
		fail = true;
		assert.deepEqual(await tool.execute({}, {} as never), { ok: false, error: 'unavailable', rooms: ['Sutando Triage'] });
	});
});

describe('readRoom: every outcome is something the model can say', () => {
	type Call = { name: string; args: Record<string, unknown> };
	const fake = (handler: (name: string, args: Record<string, unknown>) => unknown) => {
		const calls: Call[] = [];
		return {
			calls,
			client: { async callTool(name: string, args: Record<string, unknown>) { calls.push({ name, args }); const r = handler(name, args); if (r instanceof Error) throw r; return r; }, close() {} },
		};
	};
	const listed = (rooms: Array<{ room_id: string; name: string }>) => ({ structuredContent: { rooms } });
	const read = (messages: unknown[]) => ({ structuredContent: { status: 'completed', result: { messages } } });
	/** An index already holding these rooms; refresh() fetches through the client like the real one. */
	const idx = (rooms: Array<{ room_id: string; name: string }>, client: { callTool: (n: string, a: Record<string, unknown>) => Promise<unknown>; close(): void }) => {
		const index = createRoomIndex({ client: () => client as never, ttlMs: 1e9, now: () => 1 });
		let list = rooms;
		return { rooms: () => list, refresh: async () => { list = await index.refresh(); return list; }, fetchedAt: () => 1 };
	};

	it('a named room is found in the index, then read with the default limit — no room.list on the way', async () => {
		const f = fake(() => read([{ sender: '@pro:ag2.space', body: 'Approved #5453.', ts: 1 }]));
		const index = idx([{ room_id: '!a:ag2.space', name: 'Sutando Triage' }], f.client);
		const r = await readRoom({ room: 'Sutando Triage' }, { client: () => f.client, currentRoom: () => null, index });
		assert.equal(r.ok, true);
		assert.equal(r.room, 'Sutando Triage');
		assert.deepEqual(r.messages.map((m: { text: string }) => m.text), ['Approved #5453.']);
		assert.deepEqual(f.calls, [{ name: 'room.action.read', args: { room_id: '!a:ag2.space', action: 'room.context.read', arguments: { limit: ROOM_READ_DEFAULT_LIMIT } } }]);
	});

	it('a name not in the index refreshes it once: a newly joined room is read; a miss lists every room', async () => {
		const f = fake((name) => name === 'room.list' ? listed([{ room_id: '!a:x', name: 'Sutando Triage' }, { room_id: '!new:x', name: 'GTM' }]) : read([]));
		const index = idx([{ room_id: '!a:x', name: 'Sutando Triage' }], f.client);
		const r = await readRoom({ room: 'GTM' }, { client: () => f.client, currentRoom: () => null, index });
		assert.equal(r.ok && r.room, 'GTM');
		assert.deepEqual(f.calls.map(c => c.name), ['room.list', 'room.action.read']);
		// 2026-10-13 17:41: heard as "Sutando Triaging" — the answer carries the names so the model can say the right one.
		assert.deepEqual(await readRoom({ room: 'Sutando Triaging' }, { client: () => f.client, currentRoom: () => null, index }),
			{ ok: false, error: 'not_found', rooms: ['Sutando Triage', 'GTM'] });
	});

	it('no room named → the room this session is in; none → no_room', async () => {
		const f = fake(() => read([]));
		const index = idx([], f.client);
		const r = await readRoom({}, { client: () => f.client, currentRoom: () => ({ id: '!cur:ag2.space', name: 'GTM' }), index });
		assert.equal(r.ok && r.room, 'GTM');
		assert.equal(f.calls[0].args.room_id, '!cur:ag2.space');
		assert.deepEqual(await readRoom({}, { client: () => f.client, currentRoom: () => null, index }), { ok: false, error: 'no_room' });
	});

	it('a room id is read directly; the limit is capped', async () => {
		const f = fake(() => read([]));
		await readRoom({ room: '!cAAaogVSFcIYaNmohw:ag2.space', limit: 500 }, { client: () => f.client, currentRoom: () => null, index: idx([], f.client) });
		assert.equal(f.calls.length, 1, 'no room.list for an id');
		assert.equal((f.calls[0].args.arguments as { limit: number }).limit, 40);
	});

	it('ambiguous comes back with the candidates; a miss on an empty index is not_found with no rooms', async () => {
		const two = fake(() => listed([{ room_id: '!a:x', name: 'GTM' }, { room_id: '!b:x', name: 'GTM planning' }]));
		const index = idx([{ room_id: '!a:x', name: 'GTM' }, { room_id: '!b:x', name: 'GTM planning' }], two.client);
		assert.deepEqual(await readRoom({ room: 'gt' }, { client: () => two.client, currentRoom: () => null, index }), { ok: false, error: 'ambiguous', candidates: ['GTM', 'GTM planning'] });
		assert.equal(two.calls.length, 0, 'ambiguous does not refresh');
		const none = fake(() => listed([]));
		assert.deepEqual(await readRoom({ room: 'nope' }, { client: () => none.client, currentRoom: () => null, index: idx([], none.client) }), { ok: false, error: 'not_found', rooms: [] });
	});

	it('no MCP, a server error, a timeout and a refused read are errors, never throws', async () => {
		const noClient = idx([], { callTool: async () => { throw new Error('x'); }, close() {} });
		assert.deepEqual(await readRoom({ room: 'x' }, { client: () => null, currentRoom: () => null, index: noClient }), { ok: false, error: 'not_connected' });
		const slow = fake(() => new Error('tools/call timed out after 6000ms'));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => slow.client, currentRoom: () => null, index: idx([], slow.client) }), { ok: false, error: 'timeout' });
		const down = fake(() => new Error('ag2-space MCP exited (1)'));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => down.client, currentRoom: () => null, index: idx([], down.client) }), { ok: false, error: 'unavailable' });
		const refused = fake(() => ({ isError: true, content: [{ type: 'text', text: '{"code":"ROOM_ACCESS_DENIED"}' }] }));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => refused.client, currentRoom: () => null, index: idx([], refused.client) }), { ok: false, error: 'ROOM_ACCESS_DENIED' });
		// A miss whose refresh fails is still not_found, with what the index last held.
		const stale = idx([{ room_id: '!a:x', name: 'Sutando Triage' }], down.client);
		assert.deepEqual(await readRoom({ room: 'Sutando Triaging' }, { client: () => down.client, currentRoom: () => null, index: stale }), { ok: false, error: 'not_found', rooms: ['Sutando Triage'] });
	});

	it('around a search hit: the read is placed at that message, not the latest', async () => {
		const f = fake(() => read([{ sender: '@a:x', body: 'before', ts: 1 }, { sender: '@b:x', body: 'the hit', ts: 2 }]));
		const index = idx([{ room_id: '!a:x', name: 'Sutando Triage' }], f.client);
		const r = await readRoom({ room: 'Sutando Triage', around: '$hit', limit: 10 }, { client: () => f.client, currentRoom: () => null, index });
		assert.equal(r.ok, true);
		assert.deepEqual(f.calls[0].args.arguments, { limit: 10, around_event_id: '$hit' });
		await readRoom({ room: 'Sutando Triage', around: '  ' }, { client: () => f.client, currentRoom: () => null, index });
		assert.deepEqual(f.calls[1].args.arguments, { limit: ROOM_READ_DEFAULT_LIMIT }, 'a blank around is no around');
	});

	it('the tool is inline and returns readRoom\'s answer', async () => {
		const f = fake(() => read([{ sender: '@a:x', body: 'hi', ts: 1 }]));
		const tool = createReadRoomTool({ client: () => f.client, currentRoom: () => ({ id: '!c:x', name: 'C' }), index: idx([], f.client) });
		assert.equal(tool.name, 'read_room');
		assert.equal(tool.execution, 'inline');
		assert.match(tool.description, /every time the user asks about a room/, 'the model reads again instead of reusing an earlier answer');
		assert.match(tool.description, /"not_found" with rooms .* call read_room again with that exact name/, 'a miss is not the end: the names come back');
		assert.doesNotMatch(tool.description, /say there is no room by that name/);
		assert.match(ROOM_READ_PROMPT_RULE, /every time/);
		assert.match(ROOM_READ_PROMPT_RULE, /earlier answer or task result about a room is never the answer now/);
		assert.match(ROOM_READ_PROMPT_RULE, /as it appears in the ROOMS line, not as it was heard/);
		const out = await tool.execute({}, {} as never);
		assert.equal(out.ok, true);
		assert.equal(out.messages[0].text, 'hi');
	});

	it('search hits become lines newest first, each with the id to read around; a hit without a snippet is dropped', () => {
		const hits = [
			{ event_id: '$new', sender: '@liususan091219-susan-s-bot.agent:ag2.space', origin_server_ts: 1791910190908, snippet: 'Re-reviewed 5473 at b8844ca4:  Ready to approve.' },
			{ event_id: '$old', sender: '@sutando-sonichi:ag2.space', origin_server_ts: 1791908065772, snippet: 'Could you review 5473?', thread_root: '$t' },
			{ event_id: '$blank', sender: '@x:y', origin_server_ts: 1, snippet: '  ' },
			{ sender: '@noid:y', snippet: 'no id' },
		];
		const lines = toHitLines(hits, 'UTC');
		assert.deepEqual(lines.map(l => [l.event_id, l.from, l.text, l.thread ?? false]), [
			['$new', 'liususan091219-susan-s-bot.agent', 'Re-reviewed 5473 at b8844ca4: Ready to approve.', false],
			['$old', 'sutando-sonichi', 'Could you review 5473?', true],
		]);
		assert.equal(lines[0].at, 'Oct 13, 16:49 UTC', 'the zone is in the stamp, so the model never guesses one (2026-10-13 20:50: "that is Eastern Time")');
	});
	it('read_room is declared as a tool that fetches the answer, so the turn check does not also send the turn', () => {
		const src = readFileSync(join(PLUGIN_DIR, 'tools.ts'), 'utf-8');
		assert.match(src, /answeringTools: roomRead\.tools\.map\(\(t\) => t\.name\)/);
		assert.match(src, /tools: \[read, search, list\]/, 'list_rooms and search_room are declared with read_room');
		assert.match(src, /roomIndexContextLines\(index\)/, 'the room names are in every prompt');
		assert.match(src, /_rooms\.refresh\(\)\.then\(/, 'the index is the warm-up');
	});
describe('searchRoom: what was said about something, however far back', () => {
	type Call = { name: string; args: Record<string, unknown> };
	const fake = (handler: (name: string, args: Record<string, unknown>) => unknown) => {
		const calls: Call[] = [];
		return {
			calls,
			client: { async callTool(name: string, args: Record<string, unknown>) { calls.push({ name, args }); const r = handler(name, args); if (r instanceof Error) throw r; return r; }, close() {} },
		};
	};
	const found = (results: unknown[]) => ({ structuredContent: { status: 'completed', result: { results, cursor: null } } });
	const idx = (rooms: Array<{ room_id: string; name: string }>, client: { callTool: (n: string, a: Record<string, unknown>) => Promise<unknown>; close(): void }) => {
		const index = createRoomIndex({ client: () => client as never, ttlMs: 1e9, now: () => 1 });
		let list = rooms;
		return { rooms: () => list, refresh: async () => { list = await index.refresh(); return list; }, fetchedAt: () => 1 };
	};
	const triage = [{ room_id: '!cAAaogVSFcIYaNmohw:ag2.space', name: 'Sutando Triage' }];

	it('searches the named room with the words and the default limit; hits come back with their ids', async () => {
		// 2026-10-13 20:19: the #5473 review was the 37th-42nd newest message in Sutando Triage, past read_room's reach.
		const f = fake(() => found([{ event_id: '$r', sender: '@liususan091219-susan-s-bot.agent:ag2.space', origin_server_ts: 1791910190908, snippet: 'Re-reviewed 5473 at b8844ca4: Ready to approve.' }]));
		const r = await searchRoom({ room: 'Sutando Triage', query: ' 5473 ' }, { client: () => f.client, currentRoom: () => null, index: idx(triage, f.client) });
		assert.equal(r.ok, true);
		assert.equal(r.room, 'Sutando Triage');
		assert.equal(r.query, '5473');
		assert.deepEqual(r.hits.map(h => [h.event_id, h.text]), [['$r', 'Re-reviewed 5473 at b8844ca4: Ready to approve.']]);
		assert.deepEqual(f.calls, [{ name: 'room.action.read', args: { room_id: '!cAAaogVSFcIYaNmohw:ag2.space', action: 'room.events.search', arguments: { query: '5473', limit: ROOM_SEARCH_DEFAULT_LIMIT } } }]);
	});

	it('no words → no_query; the current room when none is named; the limit is capped at the server\'s 50', async () => {
		const f = fake(() => found([]));
		const index = idx([], f.client);
		assert.deepEqual(await searchRoom({ query: '  ' }, { client: () => f.client, currentRoom: () => null, index }), { ok: false, error: 'no_query' });
		const r = await searchRoom({ query: 'guest line', limit: 500 }, { client: () => f.client, currentRoom: () => ({ id: '!cur:x', name: 'GTM' }), index });
		assert.equal(r.ok && r.room, 'GTM');
		assert.deepEqual(f.calls[0].args, { room_id: '!cur:x', action: 'room.events.search', arguments: { query: 'guest line', limit: 50 } });
		assert.deepEqual(await searchRoom({ query: 'x' }, { client: () => f.client, currentRoom: () => null, index }), { ok: false, error: 'no_room' });
	});

	it('a misheard room name lists the rooms; a failed or refused search is an error the model can say', async () => {
		const f = fake((name) => name === 'room.list' ? ({ structuredContent: { rooms: triage } }) : found([]));
		assert.deepEqual(await searchRoom({ room: 'Sutando Triaging', query: '5473' }, { client: () => f.client, currentRoom: () => null, index: idx(triage, f.client) }), { ok: false, error: 'not_found', rooms: ['Sutando Triage'] });
		const slow = fake(() => new Error('tools/call timed out after 6000ms'));
		assert.deepEqual(await searchRoom({ room: '!a:x', query: '5473' }, { client: () => slow.client, currentRoom: () => null, index: idx([], slow.client) }), { ok: false, error: 'timeout' });
		const refused = fake(() => ({ isError: true, content: [{ type: 'text', text: '{"code":"ROOM_ACCESS_DENIED"}' }] }));
		assert.deepEqual(await searchRoom({ room: '!a:x', query: '5473' }, { client: () => refused.client, currentRoom: () => null, index: idx([], refused.client) }), { ok: false, error: 'ROOM_ACCESS_DENIED' });
		assert.deepEqual(await searchRoom({ room: '!a:x', query: '5473' }, { client: () => null, currentRoom: () => null, index: idx([], refused.client) }), { ok: false, error: 'not_connected' });
	});

	it('the tool is inline; its description and the prompt rule send "about <PR/topic>" questions here, never to memory', async () => {
		const f = fake(() => found([{ event_id: '$r', sender: '@a:x', origin_server_ts: 1, snippet: 'hit' }]));
		const tool = createSearchRoomTool({ client: () => f.client, currentRoom: () => ({ id: '!c:x', name: 'C' }), index: idx([], f.client) });
		assert.equal(tool.name, 'search_room');
		assert.equal(tool.execution, 'inline');
		assert.match(tool.description, /about a particular PR, issue, topic or person/);
		assert.match(tool.description, /never answer such a question from memory or from an earlier answer; search every time/);
		assert.match(tool.description, /call read_room with `around` = its event_id/);
		assert.match(tool.description, /"not_found" with rooms .* call search_room again with that exact name/);
		const out = await tool.execute({ query: '5473' }, {} as never);
		assert.equal(out.ok, true);
		assert.equal(out.hits[0].event_id, '$r');
		assert.match(ROOM_READ_PROMPT_RULE, /"What was said about <PR\/topic\/person> in <room>".*→ search_room, then read_room with `around`/);
		assert.match(ROOM_READ_PROMPT_RULE, /never from memory or an earlier answer/);
		assert.match(ROOM_READ_PROMPT_RULE, /What a message in a room means, or why someone said it, is answered from that message: read_room with `around`/);
		assert.match(ROOM_READ_PROMPT_RULE, /submit work only when the room's messages do not hold the answer/);
		assert.match(ROOM_TURN_CHECK_RULE, /^- none: a question about an AG2 Space room's messages/);
		assert.match(ROOM_TURN_CHECK_RULE, /never work, even when it names a PR/);
		const src = readFileSync(join(PLUGIN_DIR, 'tools.ts'), 'utf-8');
		assert.match(src, /tools: \[read, search, list\]/, 'search_room is declared with read_room, and is an answering tool through the same map');
		assert.match(src, /turnCheckRules: \[ROOM_TURN_CHECK_RULE\]/, 'the turn check is told');
	});
});

});
}
