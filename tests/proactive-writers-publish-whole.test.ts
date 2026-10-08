/**
 * The TS proactive writers publish whole through task-bridge's publishResultFile (#3956).
 *
 * A drain claims `results/proactive-*.txt` on sight, so the bytes must be written under a
 * staged dotfile no drain globs, and the drain-visible name must appear only by rename.
 * Covered: the timeout DM (task-bridge) and the stuck-voice fallback (live-agent-runtime).
 */
import { describe, it, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-proactive-whole-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
process.env.TMPDIR = TMP;
const RESULTS = join(TMP, 'results');
for (const d of ['tasks', 'results', join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const { _sweepTimeouts, _pendingTasksForTest, _resultFileOps } = await import('../src/task-bridge.js');
const { wireDurableChannels } = await import('../src/live-agent-runtime.js');

type Op = { op: 'write' | 'rename'; from: string; to?: string; visibleAtWrite?: string[] };
const ops: Op[] = [];
const drainVisible = () => readdirSync(RESULTS).filter((n) => n.startsWith('proactive-') && n.endsWith('.txt'));
const realWrite = _resultFileOps.write, realRename = _resultFileOps.rename;
_resultFileOps.write = ((p: string, body: string) => {
	ops.push({ op: 'write', from: p, visibleAtWrite: drainVisible() });
	return realWrite(p, body);
}) as typeof realWrite;
_resultFileOps.rename = ((from: string, to: string) => {
	ops.push({ op: 'rename', from, to });
	return realRename(from, to);
}) as typeof realRename;

after(() => {
	_resultFileOps.write = realWrite;
	_resultFileOps.rename = realRename;
	rmSync(TMP, { recursive: true, force: true });
});
beforeEach(() => {
	ops.length = 0;
	for (const n of readdirSync(RESULTS)) rmSync(join(RESULTS, n), { force: true });
});

/** The file under `prefix` was staged as a dotfile while no drain-visible name existed, then renamed. */
function assertPublishedWhole(prefix: string, expected: RegExp) {
	const final = drainVisible().filter((n) => n.startsWith(prefix));
	assert.equal(final.length, 1, `one ${prefix}* file published, saw ${drainVisible()}`);
	assert.match(readFileSync(join(RESULTS, final[0]), 'utf-8'), expected);
	const write = ops.find((o) => o.op === 'write');
	assert.ok(write, `${prefix}: written directly, not through publishResultFile`);
	assert.ok(basename(write.from).startsWith('.'), `staged under a dotfile, got ${write.from}`);
	assert.deepEqual(write.visibleAtWrite, [], 'no drain-visible name existed while the bytes were written');
	assert.ok(ops.some((o) => o.op === 'rename' && o.to === join(RESULTS, final[0])), 'the name appeared by rename');
}

describe('TS proactive writers publish whole', () => {
	it('the timeout DM (task-bridge)', () => {
		writeFileSync(join(TMP, 'tasks', 'task-t1.txt'), 'id: task-t1\nsource: voice\ntask: summarize the thread\n');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-t1', { submittedAt: t0, timeoutMs: 60_000, dmOnTimeout: true, taskText: 'summarize the thread' });
		_sweepTimeouts(() => {}, t0 + 120_000);
		assertPublishedWhole('proactive-timeout-task-t1-', /timed out after 1m/);
	});

	it('the stuck-voice fallback (live-agent-runtime)', async () => {
		const session = { sessionManager: { isActive: false }, clientConnected: true };
		wireDurableChannels(session as never, {});
		writeFileSync(join(RESULTS, 'voice-1.txt'), 'the answer the voice session could not speak');
		const t0 = Date.now();
		while (Date.now() - t0 < 12_000 && !drainVisible().some((n) => n.startsWith('proactive-voice-stuck-'))) {
			await new Promise((r) => setTimeout(r, 100));
		}
		assertPublishedWhole('proactive-voice-stuck-', /could not speak/);
	});
});
