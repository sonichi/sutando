import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, it } from 'node:test';

// The voice tools reach the board and the Doc through the relay's surface switch.
describe('room-collab surface tools', () => {
	const hits: string[] = [];
	let server: Server;
	let tools: typeof import('../skills/room-collab/tools.js');

	before(async () => {
		server = createServer((req, res) => {
			hits.push(`${req.method} ${req.url}`);
			res.setHeader('content-type', 'application/json');
			if (req.url === '/surface/kanban') {
				res.statusCode = 400;
				res.end(JSON.stringify({ ok: false, error: 'not a surface' }));
				return;
			}
			res.end(JSON.stringify({ ok: true, surface: req.url?.split('/')[2] ?? 'html' }));
		});
		await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
		process.env.ROOM_COLLAB_RELAY_URL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
		tools = await import('../skills/room-collab/tools.js');
	});
	after(() => server.close());

	it('room_surface switches the relay, or asks which surface it holds', async () => {
		assert.deepEqual(await tools.roomSurfaceTool.execute({ surface: 'board' }, {} as never), {
			ok: true,
			surface: 'board',
		});
		await tools.roomSurfaceTool.execute({}, {} as never);
		assert.deepEqual(hits.slice(-2), ['POST /surface/board', 'GET /surface']);
		assert.ok(tools.tools.includes(tools.roomSurfaceTool), 'the tool is contributed');
	});

	it('the moving and pointing tools say they work on the board and the Doc', () => {
		for (const t of [tools.roomSlideTool, tools.roomPointTool, tools.roomOutlineTool]) {
			assert.match(t.description, /whiteboard/, t.name);
			assert.match(t.description, /Doc/, t.name);
		}
		assert.match(tools.ROOM_SLIDE_RULE, /room_surface/);
	});
});
