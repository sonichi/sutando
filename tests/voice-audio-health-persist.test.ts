import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createHealthPersistence } from '../src/voice-audio-health-persist.js';
import type { HealthRow } from '../src/voice-audio-health.js';

const dir = mkdtempSync(join(tmpdir(), 'vah-test-'));
after(() => {
	try {
		rmSync(dir, { recursive: true, force: true });
	} catch {
		/* best effort */
	}
});

function row(over: Partial<HealthRow> = {}): HealthRow {
	return {
		tsUnix: Math.floor(Date.now() / 1000),
		sessionId: 'session_test',
		epoch: 123,
		nonce: 'abcd1234',
		reason: 'timer',
		payload: '{"coverage":"session-only"}',
		...over,
	};
}

describe('P7 D7.1 voice_audio_health persistence (worker_threads)', () => {
	it('a drained row is on disk BEFORE the ack — surviving an abrupt worker kill (crash path)', async () => {
		const dbPath = join(dir, 'crash.sqlite');
		const p = createHealthPersistence({ dbPath });
		assert.equal(p.tryEnqueue(row({ reason: 'anomaly' })), true);
		await p.drain(); // ack ⇒ INSERT committed
		await p.close(); // terminate(): the worker-thread SIGKILL analog — no flush
		const db = new DatabaseSync(dbPath);
		const rows = db
			.prepare('SELECT session_id, epoch, nonce, reason, payload_json FROM voice_audio_health')
			.all() as Array<Record<string, unknown>>;
		db.close();
		assert.equal(rows.length, 1);
		assert.equal(rows[0].session_id, 'session_test');
		assert.equal(rows[0].epoch, 123);
		assert.equal(rows[0].reason, 'anomaly');
		assert.equal(rows[0].payload_json, '{"coverage":"session-only"}');
	});

	it('one-slot mailbox: a second enqueue while the slot is in flight is refused', async () => {
		const dbPath = join(dir, 'slot.sqlite');
		const p = createHealthPersistence({ dbPath });
		assert.equal(p.tryEnqueue(row()), true);
		// Synchronously after the first postMessage the ack cannot have run yet.
		assert.equal(p.tryEnqueue(row()), false, 'slot busy → skip, never queue');
		await p.drain();
		assert.equal(p.tryEnqueue(row()), true, 'slot free again after the ack');
		await p.drain();
		await p.close();
	});

	it('row cap retention is PER SESSION: one busy session cannot evict another\'s evidence', async () => {
		const dbPath = join(dir, 'cap.sqlite');
		const p = createHealthPersistence({ dbPath, maxRows: 2 });
		for (const [sid, epoch] of [
			['A', 1],
			['A', 2],
			['B', 3],
			['B', 4],
			['B', 5],
		] as Array<[string, number]>) {
			assert.equal(p.tryEnqueue(row({ sessionId: sid, epoch })), true);
			await p.drain();
		}
		await p.close();
		const db = new DatabaseSync(dbPath);
		const rows = (db
			.prepare('SELECT session_id, epoch FROM voice_audio_health ORDER BY id')
			.all() as Array<{ session_id: string; epoch: number }>).map((r) => `${r.session_id}:${r.epoch}`);
		db.close();
		assert.deepEqual(rows, ['A:1', 'A:2', 'B:4', 'B:5'], 'B capped to 2 without touching A');
	});

	it('30-day retention sweep prunes ancient rows on the next write', async () => {
		const dbPath = join(dir, 'age.sqlite');
		const p = createHealthPersistence({ dbPath });
		assert.equal(p.tryEnqueue(row({ tsUnix: Math.floor(Date.now() / 1000) - 40 * 24 * 3600, epoch: 1 })), true);
		await p.drain();
		assert.equal(p.tryEnqueue(row({ epoch: 2 })), true);
		await p.drain();
		await p.close();
		const db = new DatabaseSync(dbPath);
		const epochs = (db.prepare('SELECT epoch FROM voice_audio_health ORDER BY id').all() as Array<{ epoch: number }>).map(
			(r) => r.epoch,
		);
		db.close();
		assert.deepEqual(epochs, [2], 'the 40-day-old row was swept');
	});

	it('a busy/locked health DB never blocks the caller: tryEnqueue refuses instead of waiting', async () => {
		const dbPath = join(dir, 'locked.sqlite');
		const p = createHealthPersistence({ dbPath });
		assert.equal(p.tryEnqueue(row({ epoch: 1 })), true);
		await p.drain();
		// Hold an exclusive transaction from THIS thread — the worker's next
		// write will sit in its busy_timeout inside the worker.
		const locker = new DatabaseSync(dbPath);
		locker.exec('BEGIN EXCLUSIVE');
		const t0 = Date.now();
		assert.equal(p.tryEnqueue(row({ epoch: 2 })), true, 'slot accepts — the WORKER blocks, not us');
		assert.equal(p.tryEnqueue(row({ epoch: 3 })), false, 'slot busy → skip, zero waiting');
		assert.ok(Date.now() - t0 < 100, 'the voice loop side never blocked');
		locker.exec('ROLLBACK');
		locker.close();
		await p.drain();
		await p.close();
	});

	it('a legacy database (pre-rename payload column) is migrated, not bricked', async () => {
		const dbPath = join(dir, 'legacy.sqlite');
		const legacy = new DatabaseSync(dbPath);
		legacy.exec('CREATE TABLE voice_audio_health (' +
			'id INTEGER PRIMARY KEY AUTOINCREMENT,' +
			'ts_unix INTEGER NOT NULL,' +
			'session_id TEXT NOT NULL,' +
			'epoch INTEGER,' +
			'nonce TEXT,' +
			'reason TEXT NOT NULL,' +
			'payload TEXT NOT NULL)');
		legacy
			.prepare('INSERT INTO voice_audio_health (ts_unix, session_id, epoch, nonce, reason, payload) VALUES (?, ?, ?, ?, ?, ?)')
			.run(Math.floor(Date.now() / 1000), 'session_old', 1, 'aaaa', 'timer', '{"old":true}');
		legacy.close();
		const p = createHealthPersistence({ dbPath });
		assert.equal(p.tryEnqueue(row({ epoch: 2 })), true);
		await p.drain();
		await p.close();
		assert.equal(p.failedWrites, 0, 'insert succeeded against the migrated schema');
		const db = new DatabaseSync(dbPath);
		const rows = (db.prepare('SELECT payload_json FROM voice_audio_health ORDER BY id').all() as Array<{ payload_json: string }>).map((r) => r.payload_json);
		db.close();
		assert.deepEqual(rows, ['{"old":true}', '{"coverage":"session-only"}'], 'old rows preserved under the new column');
	});

	it('a failing write acks ok:false — visible in failedWrites, worker stays up', async () => {
		// A file path whose parent is a FILE cannot be opened as a database.
		const bad = join(dir, 'not-a-dir.sqlite');
		const p0 = createHealthPersistence({ dbPath: bad });
		assert.equal(p0.tryEnqueue(row()), true);
		await p0.drain();
		await p0.close();
		const p = createHealthPersistence({ dbPath: join(bad, 'child.sqlite') });
		assert.equal(p.tryEnqueue(row()), true);
		await p.drain();
		assert.equal(p.failedWrites, 1);
		assert.equal(p.broken, false, 'a write failure is a skipped sample, not a dead worker');
		assert.equal(p.tryEnqueue(row()), true, 'later samples still accepted');
		await p.drain();
		await p.close();
	});
});
