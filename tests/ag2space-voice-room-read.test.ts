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
	it('one candidate is that room; several are ambiguous with their names; none is not_found', () => {
		assert.deepEqual(pickRoom('triage sys', [rooms[1]]), { ok: true, room: rooms[1] });
		assert.deepEqual(pickRoom('triage', rooms), { ok: false, error: 'ambiguous', candidates: ['Sutando Triage', 'triage system'] });
		assert.deepEqual(pickRoom('GTM', []), { ok: false, error: 'not_found' });
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

	it('a named room is resolved, then read with the default limit', async () => {
		const f = fake((name) => name === 'room.list'
			? listed([{ room_id: '!a:ag2.space', name: 'Sutando Triage' }])
			: read([{ sender: '@pro:ag2.space', body: 'Approved #5453.', ts: 1 }]));
		const r = await readRoom({ room: 'Sutando Triage' }, { client: () => f.client, currentRoom: () => null });
		assert.equal(r.ok, true);
		assert.equal(r.room, 'Sutando Triage');
		assert.deepEqual(r.messages.map((m: { text: string }) => m.text), ['Approved #5453.']);
		assert.deepEqual(f.calls[1], { name: 'room.action.read', args: { room_id: '!a:ag2.space', action: 'room.context.read', arguments: { limit: ROOM_READ_DEFAULT_LIMIT } } });
	});

	it('no room named → the room this session is in; none → no_room', async () => {
		const f = fake(() => read([]));
		const r = await readRoom({}, { client: () => f.client, currentRoom: () => ({ id: '!cur:ag2.space', name: 'GTM' }) });
		assert.equal(r.ok && r.room, 'GTM');
		assert.equal(f.calls[0].args.room_id, '!cur:ag2.space');
		assert.deepEqual(await readRoom({}, { client: () => f.client, currentRoom: () => null }), { ok: false, error: 'no_room' });
	});

	it('a room id is read directly; the limit is capped', async () => {
		const f = fake(() => read([]));
		await readRoom({ room: '!cAAaogVSFcIYaNmohw:ag2.space', limit: 500 }, { client: () => f.client, currentRoom: () => null });
		assert.equal(f.calls.length, 1, 'no room.list for an id');
		assert.equal((f.calls[0].args.arguments as { limit: number }).limit, 40);
	});

	it('ambiguous and not_found come back with what to say', async () => {
		const two = fake(() => listed([{ room_id: '!a:x', name: 'GTM' }, { room_id: '!b:x', name: 'GTM planning' }]));
		assert.deepEqual(await readRoom({ room: 'gt' }, { client: () => two.client, currentRoom: () => null }), { ok: false, error: 'ambiguous', candidates: ['GTM', 'GTM planning'] });
		const none = fake(() => listed([]));
		assert.deepEqual(await readRoom({ room: 'nope' }, { client: () => none.client, currentRoom: () => null }), { ok: false, error: 'not_found' });
	});

	it('no MCP, a server error, a timeout and a refused read are errors, never throws', async () => {
		assert.deepEqual(await readRoom({ room: 'x' }, { client: () => null, currentRoom: () => null }), { ok: false, error: 'not_connected' });
		const slow = fake(() => new Error('tools/call timed out after 6000ms'));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => slow.client, currentRoom: () => null }), { ok: false, error: 'timeout' });
		const down = fake(() => new Error('ag2-space MCP exited (1)'));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => down.client, currentRoom: () => null }), { ok: false, error: 'unavailable' });
		const refused = fake(() => ({ isError: true, content: [{ type: 'text', text: '{"code":"ROOM_ACCESS_DENIED"}' }] }));
		assert.deepEqual(await readRoom({ room: '!a:x' }, { client: () => refused.client, currentRoom: () => null }), { ok: false, error: 'ROOM_ACCESS_DENIED' });
	});

	it('the tool is inline and returns readRoom\'s answer', async () => {
		const f = fake(() => read([{ sender: '@a:x', body: 'hi', ts: 1 }]));
		const tool = createReadRoomTool({ client: () => f.client, currentRoom: () => ({ id: '!c:x', name: 'C' }) });
		assert.equal(tool.name, 'read_room');
		assert.equal(tool.execution, 'inline');
		assert.match(tool.description, /every time the user asks about a room/, 'the model reads again instead of reusing an earlier answer');
		assert.match(ROOM_READ_PROMPT_RULE, /every time/);
		assert.match(ROOM_READ_PROMPT_RULE, /earlier answer or task result about a room is never the answer now/);
		const out = await tool.execute({}, {} as never);
		assert.equal(out.ok, true);
		assert.equal(out.messages[0].text, 'hi');
	});

	it('read_room is declared as a tool that fetches the answer, so the turn check does not also send the turn', () => {
		const src = readFileSync(join(PLUGIN_DIR, 'tools.ts'), 'utf-8');
		assert.match(src, /answeringTools: roomRead\.tools\.map\(\(t\) => t\.name\)/);
	});
});
}
