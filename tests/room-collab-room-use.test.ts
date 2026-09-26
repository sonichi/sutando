import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { matchRoom, originRoom, type RoomEntry } from '../skills/room-collab/room-match.js';

// room_use turns a spoken room name into the id the relay switches to; a wrong guess presents in the wrong room.
describe('room-collab room_use name resolution', () => {
	const rooms: RoomEntry[] = [
		{ id: '!qg:ag2.space', name: 'Qingyun Group' },
		{ id: '!qgx:ag2.space', name: 'Qingyun Group Extended' },
		{ id: '!design:ag2.space', name: 'Design Review' },
		{ id: '!anon:ag2.space', name: null },
	];
	const id = (q: string) => {
		const m = matchRoom(q, rooms);
		return 'room' in m ? m.room.id : m.error;
	};

	it('takes a room id as given, named when it is a joined room', () => {
		assert.deepEqual(matchRoom('!design:ag2.space', rooms), { room: rooms[2] });
		assert.deepEqual(matchRoom(' !new:ag2.space ', rooms), { room: { id: '!new:ag2.space', name: null } });
	});

	it('prefers an exact name, case-insensitively, over the names containing it', () => {
		assert.equal(id('qingyun group'), '!qg:ag2.space');
		assert.equal(id('QINGYUN  GROUP'), '!qg:ag2.space');
	});

	it('accepts a unique substring and the spoken "the … room" form', () => {
		assert.equal(id('design'), '!design:ag2.space');
		assert.equal(id('the Qingyun Group room'), '!qg:ag2.space');
		assert.equal(id('the design room'), '!design:ag2.space');
		assert.equal(id('extended'), '!qgx:ag2.space');
	});

	it('refuses an ambiguous or unknown name, naming the choices', () => {
		assert.match(id('qingyun'), /matches several rooms: Qingyun Group \(!qg:ag2.space\); Qingyun Group Extended/);
		assert.match(id('marketing'), /no joined room is named "marketing". The rooms are: Qingyun Group; .*Design Review/);
		assert.match(id('!bad'), /no joined room/);
	});

	it('follows only an AG2 Space room origin', () => {
		assert.deepEqual(originRoom({ channel: 'ag2space', target: '!qg:ag2.space', label: 'Qingyun Group' }),
			{ id: '!qg:ag2.space', name: 'Qingyun Group' });
		assert.deepEqual(originRoom({ channel: 'ag2space', target: '!qg:ag2.space' }), { id: '!qg:ag2.space', name: null });
		assert.equal(originRoom({ channel: 'discord', target: '!qg:ag2.space' }), null);
		assert.equal(originRoom({ channel: 'ag2space', target: 'general' }), null);
		assert.equal(originRoom(null), null);
	});
});

// The tools against a stand-in relay: which requests reach it, in order.
describe('room-collab tools follow the docked room', () => {
	it('switch by name, and follow the session origin only on hosts that expose it', async () => {
		const http = await import('node:http');
		const seen: string[] = [];
		const server = http.createServer((req, res) => {
			seen.push(`${req.method} ${req.url}`);
			const body =
				req.url === '/rooms'
					? { ok: true, rooms: [{ id: '!qg:x', name: 'Qingyun Group' }, { id: '!d:x', name: 'Design' }] }
					: req.url?.startsWith('/room/')
						? { ok: true, room: decodeURIComponent(req.url.slice(6)), connected: true, has_page: true }
						: { ok: true };
			res.setHeader('Content-Type', 'application/json');
			res.end(JSON.stringify(body));
		});
		await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
		process.env.ROOM_COLLAB_RELAY_URL = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
		try {
			const { roomUseTool, roomSlideTool, setup } = await import('../skills/room-collab/tools.js');
			const exec = (t: typeof roomUseTool, a: unknown) => (t.execute as (a: unknown) => Promise<Record<string, unknown>>)(a);

			assert.deepEqual(await exec(roomUseTool, { room: 'the qingyun group room' }),
				{ ok: true, room: '!qg:x', name: 'Qingyun Group', connected: true, has_page: true });
			assert.deepEqual(seen.splice(0), ['GET /rooms', 'POST /room/!qg%3Ax']);

			setup({ session: {}, injectText: () => {} }); // an older host: no origin getter
			await exec(roomSlideTool, { action: 'next' });
			assert.deepEqual(seen.splice(0), ['POST /slide/next']);

			let origin: unknown = { channel: 'ag2space', target: '!d:x', label: 'Design' };
			setup({ session: {}, injectText: () => {}, getVoiceSessionOrigin: () => origin });
			await exec(roomSlideTool, { action: 'next' });
			await exec(roomSlideTool, { action: 'next' });
			assert.deepEqual(seen.splice(0), ['POST /room/!d%3Ax', 'POST /slide/next', 'POST /slide/next']);

			await exec(roomUseTool, { room: '!qg:x' }); // an explicit choice holds while the origin is unchanged
			await exec(roomSlideTool, { action: 'prev' });
			assert.deepEqual(seen.splice(0), ['POST /room/!qg%3Ax', 'POST /slide/prev']);

			origin = null;
			await exec(roomSlideTool, { action: 'prev' });
			origin = { channel: 'ag2space', target: '!qg:x' };
			await exec(roomSlideTool, { action: 'prev' });
			assert.deepEqual(seen.splice(0), ['POST /slide/prev', 'POST /room/!qg%3Ax', 'POST /slide/prev']);
		} finally {
			server.close();
		}
	});
});
