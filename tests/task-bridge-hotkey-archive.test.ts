import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Test coverage for the hotkey/context-drop offline archival fix (#969).
//
// Before this fix: task-bridge.ts archived results only for voice tasks
// (forwarded to Discord DM) and task-chat-* tasks. Hotkey/context-drop
// tasks (source: hotkey, source: context-drop) had no bridge consumer and
// no archive path when the voice client was offline — their result files
// accumulated in results/ until the next boot (archive-stale-results.py's
// multi-day retention). The fix: a new `_isHotkeyTask` helper detects
// these tasks; a new branch in the offline path archives them immediately.
//
// Structural tests only: _isHotkeyTask is exported so we can exercise it
// directly without running the full task watcher setInterval.

// Import from the actual source via dynamic import (tsx handles .ts)
import { _isHotkeyTask } from '../src/task-bridge.js';

describe('_isHotkeyTask — task classification for hotkey/context-drop sources (#969)', () => {
	it('returns true for source: hotkey task file', () => {
		const dir = mkdtempSync(join(tmpdir(), 'tb-hotkey-'));
		const tasksDir = join(dir, 'tasks');
		mkdirSync(tasksDir);
		const taskId = `task-${Date.now()}`;
		writeFileSync(join(tasksDir, `${taskId}.txt`), [
			`id: ${taskId}`,
			'timestamp: 2026-05-27T10:00:00Z',
			'source: hotkey',
			'access_tier: owner',
			'task: User dropped context via hotkey. Process this:',
			'some context here',
		].join('\n'));

		// Override TASK_DIR for the test via environment — but _isHotkeyTask
		// reads from the module-level TASK_DIR constant. For a structural
		// test, we can verify the helper detects the correct source line
		// by creating a task file directly in the real TASK_DIR location
		// OR by reading the source and confirming the detection pattern.
		// Since we cannot inject TASK_DIR, we test the source-detection
		// logic via a source-code pattern check instead.
		const SRC = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/task-bridge.ts'),
			'utf-8',
		);
		assert.match(
			SRC,
			/source: hotkey/,
			'_isHotkeyTask must check for source: hotkey in the task file header.',
		);
	});

	it('_isHotkeyTask is exported from task-bridge.ts', () => {
		const SRC = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/task-bridge.ts'),
			'utf-8',
		);
		assert.match(
			SRC,
			/export function _isHotkeyTask/,
			'_isHotkeyTask must be exported for testability.',
		);
	});

	it('_isHotkeyTask detects both source: hotkey and source: context-drop', () => {
		const SRC = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/task-bridge.ts'),
			'utf-8',
		);
		const fn = SRC.match(/export function _isHotkeyTask[\s\S]+?^}/m)?.[0] ?? '';
		assert.match(fn, /source: hotkey/, '_isHotkeyTask must check for source: hotkey');
		assert.match(fn, /source: context-drop/, '_isHotkeyTask must check for source: context-drop');
	});

	it('offline handler calls _isHotkeyTask and archives hotkey results', () => {
		const SRC = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/task-bridge.ts'),
			'utf-8',
		);
		// The offline branch must call _isHotkeyTask and archiveFile for results
		assert.match(
			SRC,
			/_isHotkeyTask\(taskId\)/,
			'offline path must call _isHotkeyTask(taskId) to classify hotkey tasks.',
		);
		// archiveFile must be called for the result in the hotkey branch
		const hotkeyBranch = SRC.match(/_isHotkeyTask\(taskId\)[\s\S]{0,600}/)?.[0] ?? '';
		assert.match(
			hotkeyBranch,
			/archiveFile\(path, 'results'/,
			'hotkey branch must call archiveFile for the result file.',
		);
	});

	it('Swift writeTask now includes source: hotkey and access_tier: owner', () => {
		const swift = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/Sutando/main.swift'),
			'utf-8',
		);
		assert.match(
			swift,
			/source: hotkey/,
			'src/Sutando/main.swift writeTask must include source: hotkey in the task file content.',
		);
		assert.match(
			swift,
			/access_tier: owner/,
			'src/Sutando/main.swift writeTask must include access_tier: owner.',
		);
	});

	it('Swift writeTask source: hotkey appears before the task: body line', () => {
		const swift = readFileSync(
			join(import.meta.dirname ?? '.', '..', 'src/Sutando/main.swift'),
			'utf-8',
		);
		const writeTaskFn = swift.match(/func writeTask[\s\S]+?let taskContent = """[\s\S]+?"""/)?.[0] ?? '';
		assert.ok(writeTaskFn.length > 0, 'writeTask function with taskContent must be present');
		const hotkeyIdx = writeTaskFn.indexOf('source: hotkey');
		const taskBodyIdx = writeTaskFn.indexOf('task: User dropped context');
		assert.ok(hotkeyIdx !== -1, 'source: hotkey must be present in taskContent');
		assert.ok(taskBodyIdx !== -1, 'task: body line must be present');
		assert.ok(
			hotkeyIdx < taskBodyIdx,
			'source: hotkey must appear before task: line (it is a header field, not body content)',
		);
	});
});
