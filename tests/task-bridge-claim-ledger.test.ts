import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, renameSync, rmSync } from 'node:fs';
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

	it('reads ONLY ledger-named files, not every JSON in state/', () => {
		// <workspace>/state also holds core-status.json, voice-state.json,
		// quota-state.json and friends. Without the name+suffix filter, any of
		// them that happens to be a JSON array of strings injects false claims
		// and wrongly suppresses retirement of a local result.
		const decoys = ['core-status.json', 'voice-state.json', 'quota-state.json'];
		for (const name of decoys) raw(TMP, name, JSON.stringify(['task-decoy']));
		raw(TMP, 'remote-task-inflight.txt', JSON.stringify(['task-wrong-suffix']));
		assert.equal(_claimedElsewhere('task-decoy'), false,
			'a non-ledger JSON array in state/ must not read as a claim');
		assert.equal(_claimedElsewhere('task-wrong-suffix'), false,
			'a ledger-named file without .json must not be read');
		assert.equal(_claimedElsewhere('task-a'), true, 'the real ledger still reads');
	});

	it('a reshaped ledger yields no claims rather than throwing', () => {
		raw(TMP, 'remote-task-inflight-shape.json', '{"tasks": ["task-wrong-shape"]}');
		assert.equal(_claimedElsewhere('task-wrong-shape'), false);
	});

	it('a ledger outside this workspace is NOT ownership of a file inside it', () => {
		// _dirs.py resolves task/result/state independently and sutando injects
		// all three together, so a consumer pointed at another tree writes its
		// RESULTS there too. Its claims describe files that are not in the
		// results/ scanned here — counting them would mistake a foreign
		// namespace's claim for ownership of this file.
		process.env.AGENT_CONNECT_STATE_DIR = join(EXT, 'state');
		ledger(EXT, 'remote-task-inflight.json', ['task-elsewhere']);
		assert.equal(_claimedElsewhere('task-elsewhere'), false,
			'a claim from another tree must not count as ownership here');
		assert.equal(_claimedElsewhere('task-a'), true, 'this workspace still reads');
		delete process.env.AGENT_CONNECT_STATE_DIR;
	});

	it('a bare JSON string is not a list of claims', () => {
		// A JSON string is ITERABLE, so `for (const id of parsed)` walks its
		// characters instead of throwing — the shape check is what stops them
		// entering the id set, not the surrounding catch.
		raw(TMP, 'remote-task-inflight-str.json', JSON.stringify('x'));
		assert.equal(_claimedElsewhere('x'), false,
			'characters of a JSON string must not read as claims');
		assert.equal(_claimedElsewhere('task-a'), true, 'the real ledger still reads');
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

	it('a failing directory read is contained, not propagated', () => {
		// Inject through the dir src ACTUALLY reads. Driving this through
		// AGENT_CONNECT_STATE_DIR passed for an unrelated reason once the
		// boundary fix stopped reading that variable: readdirSync was never
		// called with the bad path at all.
		// Note the guards are redundant, so removing either one alone still
		// passes; this pins that containment EXISTS, not which guard supplies it.
		const state = join(TMP, 'state');
		const stash = join(TMP, 'state-stashed');
		renameSync(state, stash);              // readdirSync now throws ENOENT
		try {
			assert.doesNotThrow(() => _claimedElsewhere('task-a'));
			assert.equal(_claimedElsewhere('task-a'), false,
				'an unreadable state dir yields no claims, it does not propagate');
		} finally {
			renameSync(stash, state);
		}
		assert.equal(_claimedElsewhere('task-a'), true, 'and it recovers once readable');
	});

});
