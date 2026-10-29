import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// _readTaskHeader() re-globbed tasks/archive/'s ENTIRE contents — thousands
// of legacy loose files included, not just the handful of YYYY-MM partition
// dirs it actually wants — on every single call. Its caller (the 2s-interval
// result watcher in this file) invokes it once per skip-marked result on
// every tick, so a workspace with a large results/ and a large tasks/archive/
// paid that full scan roughly (results-file-count / 2s), sustained. Measured
// live: ~94% of a CPU core in `readdir`, profiled via the Node inspector
// protocol against the running voice-agent process.
//
// These tests pin the fix: the month-dir list is cached and only re-scanned
// when tasks/archive/'s own mtime changes (a real addition/removal), and a
// task archived into a BRAND NEW month dir created mid-process is still found
// — the cache must not go stale across that boundary.

const TMP = mkdtempSync(join(tmpdir(), 'sutando-archive-scan-cache-test-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const TASK_DIR = join(TMP, 'tasks');
const ARCHIVE_DIR = join(TASK_DIR, 'archive');
mkdirSync(ARCHIVE_DIR, { recursive: true });

// NOT destructured: `const { _archiveScanCount } = await import(...)` copies
// the exported `let`'s VALUE at that instant into a plain local const — later
// increments to the module's live binding never reach it. Reading the
// property off the namespace object on every check gets the live value.
const taskBridge = await import('../src/task-bridge.js');
const { _readTaskHeader } = taskBridge;

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

function writeArchived(month: string, id: string, body: string): void {
	const dir = join(ARCHIVE_DIR, month);
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, `${id}.txt`), body);
}

describe('_readTaskHeader archive-dir scan caching', () => {
	it('finds a task in an existing month-partitioned archive dir', () => {
		writeArchived('2026-01', 'task-cache-a', [
			'id: task-cache-a',
			'timestamp: 2026-01-05T00:00:00Z',
			'source: discord',
			'task: hello',
			'',
		].join('\n'));
		const headers = _readTaskHeader('task-cache-a');
		assert.ok(headers !== null);
		assert.ok(headers!.some((l) => l.startsWith('id: task-cache-a')));
	});

	it('does not re-scan the archive root on repeated lookups (no change between calls)', () => {
		// A large number of stray non-month-shaped entries, mirroring the
		// live case (legacy loose task-*.txt / worker-pin-*.txt files
		// sitting directly in tasks/archive/) — these must NOT be re-walked
		// on every call once cached.
		for (let i = 0; i < 200; i++) {
			writeFileSync(join(ARCHIVE_DIR, `stray-file-${i}.txt`), 'noise');
		}
		const before = taskBridge._archiveScanCount;
		for (let i = 0; i < 25; i++) {
			_readTaskHeader('task-cache-a');
		}
		const after1 = taskBridge._archiveScanCount;
		// At most one real scan across all 25 repeated, unchanged-directory
		// calls (0 if a prior test already warmed the cache at this mtime).
		assert.ok(after1 - before <= 1, `expected <=1 archive scan across 25 calls, got ${after1 - before}`);
	});

	it('picks up a task in a month dir created AFTER the cache was already warm', () => {
		// Warm the cache against the current archive-root state first.
		_readTaskHeader('task-cache-a');
		const beforeScans = taskBridge._archiveScanCount;

		// Now create a brand-new month partition — simulating the first
		// task ever archived in a new calendar month arriving mid-process.
		writeArchived('2026-02', 'task-cache-b', [
			'id: task-cache-b',
			'timestamp: 2026-02-01T00:00:00Z',
			'source: discord',
			'task: hello again',
			'',
		].join('\n'));

		const headers = _readTaskHeader('task-cache-b');
		assert.ok(headers !== null, 'task in a newly-created month dir must still be found');
		assert.ok(headers!.some((l) => l.startsWith('id: task-cache-b')));
		// Finding it required detecting the archive root's mtime change —
		// i.e. a real re-scan happened, not a stale cache hit.
		assert.ok(taskBridge._archiveScanCount > beforeScans, 'expected a re-scan after the archive root changed');
	});

	it('returns null for a task that exists nowhere', () => {
		assert.equal(_readTaskHeader('task-does-not-exist-anywhere'), null);
	});
});
