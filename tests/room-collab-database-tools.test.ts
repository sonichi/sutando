import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, it } from 'node:test';

// The database voice tools speak the relay's /db routes: values ride the query string, by property name.
describe('room-collab database tools', () => {
	const hits: string[] = [];
	let server: Server;
	let tools: typeof import('../skills/room-collab/tools.js');

	before(async () => {
		server = createServer((req, res) => {
			hits.push(`${req.method} ${req.url}`);
			res.setHeader('content-type', 'application/json');
			if (req.url?.includes('Maybe')) {
				res.statusCode = 400;
				res.end(JSON.stringify({ ok: false, error: 'Killer use case: not an option' }));
				return;
			}
			res.end(JSON.stringify({ ok: true }));
		});
		await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
		process.env.ROOM_COLLAB_RELAY_URL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
		tools = await import('../skills/room-collab/tools.js');
	});
	after(() => server.close());

	it('list and read', async () => {
		await tools.roomDbListTool.execute({}, {} as never);
		await tools.roomDbReadTool.execute({ database: 'Demo day', view: 'By status' }, {} as never);
		await tools.roomDbReadTool.execute({}, {} as never);
		assert.deepEqual(hits.slice(-3), ['GET /db', 'GET /db/Demo%20day/view/By%20status', 'GET /db/-']);
	});

	it('add, update and move encode each value whole', async () => {
		await tools.roomDbAddTool.execute(
			{ database: 'Demo day', set: ['Presenter=Mark', 'Killer use case=Confirmed', 'Notes=a&b=c'] },
			{} as never
		);
		assert.equal(
			hits.at(-1),
			'POST /db/Demo%20day/row?set=Presenter%3DMark&set=Killer%20use%20case%3DConfirmed&set=Notes%3Da%26b%3Dc'
		);
		await tools.roomDbUpdateTool.execute({ row: 'Shared browser', set: ['Minutes=10'] }, {} as never);
		assert.equal(hits.at(-1), 'POST /db/-/row/Shared%20browser?set=Minutes%3D10');
		assert.deepEqual(await tools.roomDbUpdateTool.execute({ row: 'x', set: [] }, {} as never), {
			error: 'nothing to set',
		});
		await tools.roomDbMoveTool.execute({ row: 'Shared browser', to: 'Tried by others' }, {} as never);
		assert.equal(hits.at(-1), 'POST /db/-/move?row=Shared%20browser&to=Tried%20by%20others');
	});

	it('room_db_row reads a row page, or sets or appends its body', async () => {
		await tools.roomDbRowTool.execute({ database: 'Standups', row: 'Monday standup' }, {} as never);
		assert.equal(hits.at(-1), 'GET /db/Standups/row/Monday%20standup');
		await tools.roomDbRowTool.execute({ row: 'Monday', body: '# Notes\n- a&b=c' }, {} as never);
		assert.equal(hits.at(-1), 'POST /db/-/row/Monday/body?text=%23%20Notes%0A-%20a%26b%3Dc');
		await tools.roomDbRowTool.execute({ row: 'Monday', body: '- ship', append: true }, {} as never);
		assert.equal(hits.at(-1), 'POST /db/-/row/Monday/body?text=-%20ship&append=1');
		await tools.roomDbRowTool.execute({ row: 'Monday', body: '' }, {} as never);
		assert.equal(hits.at(-1), 'POST /db/-/row/Monday/body?text=', 'an empty body clears the page');
	});

	it('a refusal comes back as the relay worded it', async () => {
		const res = await tools.roomDbAddTool.execute({ set: ['Killer use case=Maybe'] }, {} as never);
		assert.deepEqual(res, { error: 'Killer use case: not an option' });
	});

	it('the tools are contributed and the existing ones are kept', () => {
		const names = tools.tools.map((t) => t.name);
		for (const n of ['room_db_list', 'room_db_read', 'room_db_add', 'room_db_update', 'room_db_move', 'room_db_row', 'room_surface'])
			assert.ok(names.includes(n), n);
		assert.equal(new Set(names).size, names.length, 'tool names are unique');
	});
});
