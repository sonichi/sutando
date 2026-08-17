import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// _claimedElsewhere is the evidence the retirement decision reads. The
// decision itself is pinned in task-bridge-skip-marker-ownership.test.ts by
// handing it a boolean — so if this function silently returned false those
// tests would all still pass and the fix would be inert.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-claim-ledger-'));
const EXT = mkdtempSync(join(tmpdir(), 'sutando-sparrow-ext-'));
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'state'), { recursive: true });
mkdirSync(join(EXT, 'state'), { recursive: true });
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';

const { _claimedElsewhere, _taskOrigin } = await import('../src/task-bridge.js');

const ledger = (dir: string, name: string, ids: string[]) =>
	writeFileSync(join(dir, 'state', name), JSON.stringify(ids));
const raw = (dir: string, name: string, text: string) =>
	writeFileSync(join(dir, 'state', name), text);

after(() => { rmSync(TMP, { recursive: true, force: true }); rmSync(EXT, { recursive: true, force: true }); });

describe('_claimedElsewhere reads other consumers\' durable in-flight ledgers', () => {
	it('no ledger present yields no claims', () => {
		assert.equal(_claimedElsewhere('task-none'), false);
	});

	it('a claimed tid reads as claimed', () => {
		ledger(TMP, 'remote-task-inflight.json', ['task-a', 'task-b']);
		assert.equal(_claimedElsewhere('task-a'), true);
		assert.equal(_claimedElsewhere('task-not-in-it'), false);
	});

	it('a claim added mid-drain is seen without a restart', () => {
		// Cache is keyed on (mtime, size), so a rewrite invalidates it.
		ledger(TMP, 'remote-task-inflight.json', ['task-a', 'task-b', 'task-added-later']);
		assert.equal(_claimedElsewhere('task-added-later'), true);
	});

	it('reads every instance-suffixed ledger', () => {
		ledger(TMP, 'remote-task-inflight-inst2.json', ['task-second-instance']);
		assert.equal(_claimedElsewhere('task-second-instance'), true);
	});

	it('a corrupt ledger yields no claims rather than throwing', () => {
		raw(TMP, 'remote-task-inflight-bad.json', '{not json');
		assert.equal(_claimedElsewhere('task-a'), true, 'the readable ledger is still read');
		assert.equal(_claimedElsewhere('task-ghost'), false);
	});

	it('a reshaped ledger yields no claims rather than throwing', () => {
		raw(TMP, 'remote-task-inflight-shape.json', '{"tasks": ["task-wrong-shape"]}');
		assert.equal(_claimedElsewhere('task-wrong-shape'), false);
	});

	it('finds a ledger the WRITER placed outside the injected dir', () => {
		// _dirs.state_dir() resolves injected -> $AGENT_CONNECT_STATE_DIR ->
		// ~/.ag2-sparrow/state. Globbing only the injected dir reports "no
		// claim" for a whole deployment shape, silently.
		ledger(EXT, 'remote-task-inflight.json', ['task-external-client']);
		process.env.AGENT_CONNECT_STATE_DIR = join(EXT, 'state');
		assert.equal(_claimedElsewhere('task-external-client'), true);
		delete process.env.AGENT_CONNECT_STATE_DIR;
	});

	it('a claim reaches the origin record the decision actually reads', () => {
		// The wiring: _taskOrigin must carry the claim through, or the
		// ledger is read and then discarded.
		writeFileSync(join(TMP, 'tasks', 'task-a.txt'),
			'id: task-a\nsource: acme-corp-relay\ntask: hi\n');
		const o = _taskOrigin('task-a');
		assert.equal(o!.source, 'acme-corp-relay');
		assert.equal(o!.claimedElsewhere, true,
			'an operator-set label the list cannot enumerate is still caught by the claim');
	});

	it('is total — a failure never escapes into the drain loop', () => {
		// This runs inside the result-scan for-loop, whose catch inspects
		// err.code; a ReferenceError has none, so the guard is false, nothing
		// is logged, and the pass aborts for every later-sorting file. It then
		// repeats every tick, because the file it stops on is one this PR
		// deliberately leaves resident. The directory resolution therefore has
		// to be inside the try, not just the reads.
		process.env.AGENT_CONNECT_STATE_DIR = '\0not-a-path';
		assert.doesNotThrow(() => _claimedElsewhere('task-a'));
		delete process.env.AGENT_CONNECT_STATE_DIR;
	});
});
