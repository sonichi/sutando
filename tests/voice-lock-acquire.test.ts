import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { acquireVoiceLock, type SpawnSyncFn } from '../src/voice-lock.js';

const OPTS = {
	pidfile: '/tmp/x.pid',
	guard: '/tmp/x.guard',
	pid: 123,
	pythonBin: 'python3',
	entry: '/tmp/entry.ts',
	workspace: '/tmp/ws',
};

const spawnReturning = (status: number, stdout: string): SpawnSyncFn =>
	(() => ({ status, stdout, stderr: '', error: undefined })) as unknown as SpawnSyncFn;

describe('acquireVoiceLock — acquisition token surfacing (PID-reuse defense)', () => {
	it("surfaces the helper's per-acquisition lockId on success", () => {
		const out = JSON.stringify({ ok: true, lock: { v: 1, lockId: 'vl1-abc', pid: 123, startTimeMs: 1 } });
		const res = acquireVoiceLock(OPTS, spawnReturning(0, out));
		assert.deepEqual(res, { status: 'acquired', lockId: 'vl1-abc' });
	});

	it('acquisition still succeeds when the token is unparseable — marker just stays unbound', () => {
		const res = acquireVoiceLock(OPTS, spawnReturning(0, 'not json'));
		assert.deepEqual(res, { status: 'acquired', lockId: undefined });
	});
});
