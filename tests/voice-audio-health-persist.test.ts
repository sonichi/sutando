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
			.prepare('SELECT session_id, epoch, nonce, reason, payload FROM voice_audio_health')
			.all() as Array<Record<string, unknown>>;
		db.close();
		assert.equal(rows.length, 1);
		assert.equal(rows[0].session_id, 'session_test');
		assert.equal(rows[0].epoch, 123);
		assert.equal(rows[0].reason, 'anomaly');
		assert.equal(rows[0].payload, '{"coverage":"session-only"}');
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

	it('row cap retention: only the newest maxRows survive', async () => {
		const dbPath = join(dir, 'cap.sqlite');
		const p = createHealthPersistence({ dbPath, maxRows: 3 });
		for (let i = 0; i < 5; i++) {
			assert.equal(p.tryEnqueue(row({ epoch: i })), true);
			await p.drain();
		}
		await p.close();
		const db = new DatabaseSync(dbPath);
		const epochs = (db.prepare('SELECT epoch FROM voice_audio_health ORDER BY id').all() as Array<{ epoch: number }>).map(
			(r) => r.epoch,
		);
		db.close();
		assert.deepEqual(epochs, [2, 3, 4], 'oldest rows pruned at the cap');
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
