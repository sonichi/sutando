/**
 * Regression guard: conversation-server must archive task-phone-*.txt after
 * the result is consumed, not leave them in tasks/ indefinitely.
 *
 * Background (sonichi/sutando#1235): delegateTask() wrote a task file to
 * tasks/ and polled for the result in results/. On result receipt it unlinked
 * the result file but never touched the task file. After a busy day of calls,
 * tasks/ fills with stale task-phone-*.txt files that the archive watcher
 * never cleans up (it only archives processed task-bridge tasks).
 *
 * The fix adds archivePhoneTask() — mirrors task-bridge archiveFile() — and
 * calls it from both the success path and the timeout path.
 *
 * Guards:
 *  1. `renameSync` is imported from node:fs (needed by archive move)
 *  2. `archivePhoneTask` function is present and uses renameSync
 *  3. `archivePhoneTask(taskPath, taskId)` is called after unlinkSync(resultPath) in success path
 *  4. `archivePhoneTask(taskPath, taskId)` is called in the timeout path
 *  5. archivePhoneTask falls back to unlinkSync on rename error (fail-safe)
 *  6. Archive destination is tasks/archive/YYYY-MM/ (matches task-bridge pattern)
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

// Extract just the archivePhoneTask function body for targeted assertions.
const fnMatch = SRC.match(/function archivePhoneTask\([\s\S]*?\n\}/);
const FN_BODY = fnMatch ? fnMatch[0] : '';

// Extract the delegateTask poll loop body.
const pollMatch = SRC.match(/const poll = setInterval\(\(\) => \{[\s\S]*?\}, TASK_POLL_INTERVAL_MS\)/);
const POLL_BODY = pollMatch ? pollMatch[0] : '';

describe('conversation-server — phone task archive (#1235)', () => {
	it('imports renameSync from node:fs', () => {
		assert.match(
			SRC,
			/import\s*\{[^}]*renameSync[^}]*\}\s*from\s*['"]node:fs['"]/,
			'renameSync must be imported from node:fs',
		);
	});

	it('defines archivePhoneTask function', () => {
		assert.ok(FN_BODY.length > 0, 'archivePhoneTask function not found in conversation-server.ts');
		assert.ok(
			FN_BODY.includes('renameSync'),
			'archivePhoneTask must use renameSync to move the task file',
		);
	});

	it('calls archivePhoneTask after unlinkSync(resultPath) in success path', () => {
		// The call must appear in the poll interval body, after the result unlink.
		assert.ok(POLL_BODY.length > 0, 'poll setInterval body not found in source');
		const unlinkPos = POLL_BODY.indexOf('unlinkSync(resultPath)');
		const archivePos = POLL_BODY.indexOf('archivePhoneTask(taskPath, taskId)');
		assert.ok(unlinkPos >= 0, 'unlinkSync(resultPath) not found in poll body');
		assert.ok(archivePos >= 0, 'archivePhoneTask(taskPath, taskId) not called in poll body');
		assert.ok(
			archivePos > unlinkPos,
			'archivePhoneTask must be called AFTER unlinkSync(resultPath) in success path',
		);
	});

	it('calls archivePhoneTask in the timeout path', () => {
		// Find the timeout block: after `POLL_TIMEOUT_MS` check
		const timeoutMatch = SRC.match(/if\s*\(Date\.now\(\)\s*-\s*startTime\s*>\s*POLL_TIMEOUT_MS\)[\s\S]*?}\s*},\s*TASK_POLL_INTERVAL_MS/);
		assert.ok(timeoutMatch, 'timeout block not found in source');
		assert.ok(
			timeoutMatch[0].includes('archivePhoneTask(taskPath, taskId)'),
			'archivePhoneTask must be called in the timeout path to clean up stale task files',
		);
	});

	it('archivePhoneTask falls back to unlinkSync on rename error', () => {
		assert.ok(FN_BODY.length > 0, 'archivePhoneTask function not found');
		assert.ok(
			FN_BODY.includes('unlinkSync'),
			'archivePhoneTask must fall back to unlinkSync when rename fails (fail-safe)',
		);
	});

	it('archive destination uses tasks/archive/YYYY-MM pattern', () => {
		assert.ok(FN_BODY.length > 0, 'archivePhoneTask function not found');
		assert.ok(
			FN_BODY.includes("'archive'"),
			"archive destination must include 'archive' subdirectory",
		);
		assert.ok(
			FN_BODY.includes('.slice(0, 7)'),
			'archive destination must use YYYY-MM date prefix (isoString.slice(0, 7))',
		);
	});
});
