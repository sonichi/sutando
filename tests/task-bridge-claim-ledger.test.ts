import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
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

	it('a failing directory read is contained, not propagated', () => {
		// Honest scope: readdirSync throws ERR_INVALID_ARG_VALUE on a NUL-bearing
		// path, and that call was ALREADY inside the per-directory try. This pins
		// containment; it does NOT exercise the outer wrap, which guards the
		// directory-list construction instead. Asserted separately below.
		process.env.AGENT_CONNECT_STATE_DIR = '\0not-a-path';
		assert.doesNotThrow(() => _claimedElsewhere('task-a'));
		assert.equal(_claimedElsewhere('task-a'), true, 'the readable ledger is still read');
		delete process.env.AGENT_CONNECT_STATE_DIR;
	});

	it('the whole body is guarded, including directory resolution', () => {
		// The outer try covers homedir() and join(REPO_DIR, …), which run BEFORE
		// the per-directory try. Neither is injectable from here — REPO_DIR is
		// captured at module load — so this asserts the structural property the
		// runtime guarantee rests on rather than simulating the throw. Removing
		// the wrap fails this; a scratch harness with an unset REPO_DIR confirms
		// the behaviour it stands for. Known weakness: a structural pin can also
		// break on a safe refactor, and it never executes the guarded path. The
		// behavioural version needs mock.module on node:os, which requires
		// --experimental-test-module-mocks; the runner does not pass it, so that
		// control cannot run here without changing it for every suite.
		const src = readFileSync(new URL('../src/task-bridge.ts', import.meta.url), 'utf-8');
		const fn = src.slice(src.indexOf('export function _claimedElsewhere'));
		const body = fn.slice(fn.indexOf('{') + 1, fn.indexOf('\n}\n'));
		const firstStmt = body.split('\n').find(l => l.trim() && !l.trim().startsWith('//'));
		assert.match(firstStmt ?? '', /^\s*try \{/,
			'directory resolution must be inside the try, not above it');
	});
});
