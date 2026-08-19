/**
 * Behavioural guard for the context-drop → live-session injection path.
 *
 * Two producers write a context drop and only one of them creates
 * context-drop.txt: the desktop app writes its `source: context-drop` task file
 * directly. scanDropTask() is what lets the watcher see both, so it is tested
 * against the real bytes each producer emits.
 *
 * The load-bearing case is `incomplete`: a half-written file must NOT classify
 * as `other`, because the watcher records `other` permanently and would drop
 * that context on the floor.
 *
 * Run: node --import tsx/esm tests/task-bridge-context-drop-scan.test.ts
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { scanDropTask } from '../src/task-bridge.js';

let dir: string;
before(() => { dir = mkdtempSync(join(tmpdir(), 'drop-scan-')); });
after(() => { rmSync(dir, { recursive: true, force: true }); });

function write(name: string, body: string): string {
	const p = join(dir, name);
	writeFileSync(p, body);
	return p;
}

describe('scanDropTask', () => {

	it('reads a drop written by the desktop app (no channel_id, single-line task:)', () => {
		// Verbatim shape of write_context_task() in the desktop app.
		const p = write('task-1787158088259.txt',
			'id: task-1787158088259\n' +
			'timestamp: 2026-08-19T16:48:08Z\n' +
			'source: context-drop\n' +
			'interaction_type: system_event\n' +
			'task: User dropped context via hotkey. Process this: the selected paragraph\n');
		assert.deepEqual(scanDropTask(p), { kind: 'drop', body: 'the selected paragraph' });
	});

	it('reads a drop written by the context-drop.txt path (multi-line body)', () => {
		const p = write('task-1787000000000.txt',
			'id: task-1787000000000\n' +
			'timestamp: 2026-08-19T10:00:00Z\n' +
			'source: context-drop\n' +
			'interaction_type: system_event\n' +
			'channel_id: local-hotkey\n' +
			'user_id: voice-local\n' +
			'access_tier: owner\n' +
			'priority: normal\n' +
			'task: User dropped context via hotkey. Process this:\nline one\nline two\n');
		assert.deepEqual(scanDropTask(p), { kind: 'drop', body: 'line one\nline two' });
	});

	it('a half-written drop is incomplete, not other — else the drop is lost', () => {
		// Header truncated mid-write: source line present, task: line not yet.
		const p = write('task-1787158099999.txt',
			'id: task-1787158099999\n' +
			'timestamp: 2026-08-19T16:48:19Z\n' +
			'source: context-drop\n');
		assert.deepEqual(scanDropTask(p), { kind: 'incomplete' });
	});

	it('a file with no source line yet is incomplete, not other', () => {
		const p = write('task-1787158077777.txt', 'id: task-1787158077777\n');
		assert.deepEqual(scanDropTask(p), { kind: 'incomplete' });
	});

	it('another producer\'s task is other, so it is never injected', () => {
		const p = write('task-3ea4daa4e764cf6acf.txt',
			'id: task-3ea4daa4e764cf6acf\n' +
			'source: ag2space\n' +
			'channel_id: !room:ag2.space\n' +
			'access_tier: owner\n' +
			'task: some owner message\n');
		assert.deepEqual(scanDropTask(p), { kind: 'other' });
	});

	it('a source that merely contains context-drop does not match', () => {
		const p = write('task-1787158066666.txt',
			'id: task-1787158066666\n' +
			'source: context-drop-replay\n' +
			'task: User dropped context via hotkey. Process this: nope\n');
		assert.deepEqual(scanDropTask(p), { kind: 'other' });
	});

	it('an empty payload is incomplete rather than an empty injection', () => {
		const p = write('task-1787158055555.txt',
			'id: task-1787158055555\n' +
			'source: context-drop\n' +
			'task: User dropped context via hotkey. Process this:   \n');
		assert.deepEqual(scanDropTask(p), { kind: 'incomplete' });
	});
});
