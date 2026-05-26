import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Source-inspection tests for PR #1189:
//   fix(task-bridge): archive context-drop results when voice client is offline
//
// PR #1189 adds inline logic — not an exported function — so behavioral tests
// would need the full bridge running. Source inspection is the right level here;
// it guards the structural properties of the fix without requiring execution.
//
// Two files are changed by #1189:
//   src/Sutando/main.swift    — writeTask() must emit `source: context-drop`
//   src/task-bridge.ts        — result watcher must detect + archive context-drop
//                               results in the !clientConnected branch
//
// Plus: pure regex unit tests that pin the detection pattern semantics without
// importing task-bridge.ts (avoids node:sqlite availability constraints).

const REPO = new URL('..', import.meta.url).pathname;
const TS_SRC  = readFileSync(join(REPO, 'src', 'task-bridge.ts'), 'utf-8');
const SWIFT_SRC = readFileSync(join(REPO, 'src', 'Sutando', 'main.swift'), 'utf-8');

// ---------------------------------------------------------------------------
// Swift companion — PR #1189 part A
// ---------------------------------------------------------------------------

describe('main.swift writeTask() — context-drop source field (PR #1189)', () => {
	it('writeTask() emits source: context-drop so the TS bridge can identify the task origin', () => {
		const writeTaskIdx = SWIFT_SRC.indexOf('func writeTask(');
		assert.ok(writeTaskIdx >= 0, 'writeTask function not found in main.swift');
		// Read a generous slice covering the function body.
		const fn = SWIFT_SRC.slice(writeTaskIdx, writeTaskIdx + 1200);
		assert.ok(
			fn.includes('source: context-drop'),
			'writeTask must write `source: context-drop` header — ' +
			'task-bridge.ts relies on this field to identify context-drop tasks',
		);
	});

	it('source: context-drop appears BEFORE the task: line in writeTask() output', () => {
		// task-bridge.ts stops parsing headers at the first `task:` line (#982 / #1023).
		// A source field placed AFTER task: would be invisible to any consumer that
		// enforces the stop-at-delimiter rule.
		const writeTaskIdx = SWIFT_SRC.indexOf('func writeTask(');
		const fn = SWIFT_SRC.slice(writeTaskIdx, writeTaskIdx + 1200);
		const sourceIdx = fn.indexOf('source: context-drop');
		const taskIdx   = fn.indexOf('task:');
		assert.ok(sourceIdx >= 0, 'source: context-drop not found');
		assert.ok(taskIdx   >= 0, 'task: field not found');
		assert.ok(
			sourceIdx < taskIdx,
			'source: context-drop must appear before task: in the task file template',
		);
	});
});

// ---------------------------------------------------------------------------
// TypeScript result watcher — PR #1189 part B
// ---------------------------------------------------------------------------

describe('task-bridge.ts context-drop archive logic — structural guards (PR #1189)', () => {
	it('has a /^source:\\s*context-drop/m regex for detecting context-drop tasks', () => {
		assert.ok(
			TS_SRC.includes('/^source:\\s*context-drop/m'),
			'task-bridge.ts must contain the detection regex /^source:\\s*context-drop/m',
		);
	});

	it('context-drop check appears inside the !clientConnected block', () => {
		// The archive-on-offline path is gated on !clientConnected. Placing the
		// context-drop check outside this gate would archive results even when the
		// voice client is connected — those results should be delivered normally.
		const offlineIdx = TS_SRC.indexOf('if (!clientConnected)');
		assert.ok(offlineIdx >= 0, '!clientConnected block not found');
		// Find the context-drop detection inside the offline block.
		const ctxIdx = TS_SRC.indexOf('context-drop', offlineIdx);
		assert.ok(ctxIdx > offlineIdx, 'context-drop check must be inside the !clientConnected block');
	});

	it('context-drop branch calls _sendTaskStatus to mark the task done in the dashboard', () => {
		const offlineIdx = TS_SRC.indexOf('if (!clientConnected)');
		const ctxIdx     = TS_SRC.indexOf('context-drop', offlineIdx);
		assert.ok(ctxIdx >= 0);
		// Read a window around the detection site.
		const block = TS_SRC.slice(offlineIdx, ctxIdx + 800);
		assert.ok(
			block.includes('_sendTaskStatus'),
			'context-drop branch must call _sendTaskStatus(taskId, "done", ...) so the ' +
			'Sutando.app dashboard reflects the archived task',
		);
	});

	it('context-drop branch adds the result file to _deliveredResults', () => {
		const offlineIdx = TS_SRC.indexOf('if (!clientConnected)');
		const ctxIdx     = TS_SRC.indexOf('context-drop', offlineIdx);
		const block = TS_SRC.slice(offlineIdx, ctxIdx + 800);
		assert.ok(
			block.includes('_deliveredResults.add'),
			'context-drop branch must call _deliveredResults.add(file) to prevent ' +
			're-processing on the next poll cycle',
		);
	});

	it('context-drop branch removes the task from _pendingTasks', () => {
		const offlineIdx = TS_SRC.indexOf('if (!clientConnected)');
		const ctxIdx     = TS_SRC.indexOf('context-drop', offlineIdx);
		const block = TS_SRC.slice(offlineIdx, ctxIdx + 800);
		assert.ok(
			block.includes('_pendingTasks.delete'),
			'context-drop branch must call _pendingTasks.delete(taskId) to avoid ' +
			'spurious timeout escalation after archiving',
		);
	});

	it('context-drop branch archives both the result file and the task file', () => {
		const offlineIdx = TS_SRC.indexOf('if (!clientConnected)');
		const ctxIdx     = TS_SRC.indexOf('context-drop', offlineIdx);
		const block = TS_SRC.slice(offlineIdx, ctxIdx + 900);
		const archiveCalls = (block.match(/archiveFile\(/g) ?? []).length;
		assert.ok(
			archiveCalls >= 2,
			`expected ≥2 archiveFile() calls in the context-drop block (result + task); ` +
			`found ${archiveCalls}`,
		);
	});
});

// ---------------------------------------------------------------------------
// Pure regex unit tests — detection pattern semantics
// ---------------------------------------------------------------------------

describe('/^source:\\s*context-drop/m — detection regex contract', () => {
	const PATTERN = /^source:\s*context-drop/m;

	it('matches a well-formed context-drop task file body', () => {
		const body = [
			'id: task-1748000000000',
			'timestamp: 2026-05-26T10:00:00Z',
			'source: context-drop',
			'task: User dropped context via hotkey. Process this:',
			'some content here',
		].join('\n');
		assert.ok(PATTERN.test(body));
	});

	it('matches with extra whitespace after the colon', () => {
		const body = 'id: t-1\nsource:  context-drop\ntask: x';
		assert.ok(PATTERN.test(body));
	});

	it('does NOT match a voice task', () => {
		const body = [
			'id: task-1748000000001',
			'source: voice',
			'channel_id: local-voice',
			'task: hello',
		].join('\n');
		assert.equal(PATTERN.test(body), false);
	});

	it('does NOT match a discord task', () => {
		const body = [
			'id: task-1748000000002',
			'source: discord',
			'channel_id: 1490906927675474030',
			'task: message',
		].join('\n');
		assert.equal(PATTERN.test(body), false);
	});

	it('does NOT match when "source: context-drop" appears mid-line inside the task body', () => {
		// The regex is anchored to line-start via /m, so an inline occurrence
		// inside the task body prose cannot forge a positive match.
		const body = [
			'id: task-1748000000003',
			'source: voice',
			'task: process this context-drop related thing with source: context-drop inline',
		].join('\n');
		assert.equal(PATTERN.test(body), false);
	});
});
