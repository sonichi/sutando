import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Pin the #1235 fix: phone task + result files MUST be archived (not
// unlink'd, not left behind in tasks/) after the result is consumed by
// the in-call poll loop. Matches the canonical src/task-bridge.ts
// archiveFile() audit-trail pattern Chi asked for 2026-04-18.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

describe('conversation-server phone-task archiving (#1235)', () => {
	it('imports renameSync from node:fs', () => {
		assert.match(
			SRC,
			/import\s*\{[^}]*renameSync[^}]*\}\s*from\s*['"]node:fs['"]/,
			'conversation-server.ts must import renameSync from node:fs — used by archivePhoneFile to move task/result files into archive/YYYY-MM/.',
		);
	});

	it('defines an archivePhoneFile helper', () => {
		assert.match(
			SRC,
			/function\s+archivePhoneFile\s*\(\s*srcPath\s*:\s*string\s*,\s*kind\s*:\s*['"]tasks['"]\s*\|\s*['"]results['"]\s*,\s*taskId\s*:\s*string\s*\)/,
			'conversation-server.ts must define archivePhoneFile(srcPath, kind, taskId) — mirrors src/task-bridge.ts:archiveFile() signature so phone surfaces match the canonical audit-trail pattern.',
		);
	});

	it('archive helper computes destDir as <baseDir>/archive/YYYY-MM', () => {
		assert.match(
			SRC,
			/const\s+destDir\s*=\s*join\(\s*baseDir\s*,\s*['"]archive['"]\s*,\s*ym\s*\)/,
			'archivePhoneFile must put files under archive/YYYY-MM/ (same partition scheme as task-bridge.ts).',
		);
	});

	it('archive helper falls back to unlinkSync on rename failure', () => {
		// The two unlinkSync calls are inside the archivePhoneFile helper's
		// catch block — fallback when rename fails (cross-device, permission,
		// etc.) so we never leave a stale file behind.
		assert.match(
			SRC,
			/catch\s*\{\s*try\s*\{\s*unlinkSync\(\s*srcPath\s*\)\s*;?\s*\}\s*catch\s*\{[^}]*\}\s*\}/,
			'archivePhoneFile must fall back to unlinkSync(srcPath) when renameSync throws — same defensive pattern as src/task-bridge.ts:archiveFile().',
		);
	});

	it('result-consume branch archives both result + task (not just unlinks result)', () => {
		// Match the in-call poll's result-consume sequence:
		//   archivePhoneFile(resultPath, 'results', taskId);
		//   archivePhoneFile(taskPath, 'tasks', taskId);
		assert.match(
			SRC,
			/archivePhoneFile\(\s*resultPath\s*,\s*['"]results['"]\s*,\s*taskId\s*\)/,
			'result-consume branch must archive resultPath via archivePhoneFile(_, "results", taskId).',
		);
		assert.match(
			SRC,
			/archivePhoneFile\(\s*taskPath\s*,\s*['"]tasks['"]\s*,\s*taskId\s*\)/,
			'result-consume branch must ALSO archive taskPath via archivePhoneFile(_, "tasks", taskId) — closes the #1235 lingering-task-file leak.',
		);
	});

	it('result-consume branch does NOT just unlinkSync(resultPath)', () => {
		// Loose check — the *raw* `unlinkSync(resultPath)` pattern that #1235
		// flagged (no surrounding archive call) should be gone. We allow
		// unlinkSync(srcPath) inside the helper's fallback, but not the bare
		// resultPath delete at the call-site.
		assert.doesNotMatch(
			SRC,
			/try\s*\{\s*unlinkSync\(\s*resultPath\s*\)\s*;?\s*\}\s*catch\s*\{\s*\}\s*\/\/\s*Cache\s+result/,
			'The pre-fix `try { unlinkSync(resultPath); } catch {} // Cache result` pattern (#1235 site) must be replaced with archivePhoneFile calls.',
		);
	});

	it('timeout branch archives the task file (≥2 archivePhoneFile(taskPath,…) call-sites total)', () => {
		// The result-consume branch + the POLL_TIMEOUT branch each archive
		// the task file. Counting at least two call-sites for the
		// archivePhoneFile(taskPath, "tasks", taskId) shape catches the case
		// where the timeout branch was dropped. (A tighter regex against the
		// exact `[Task] timeout` log message is brittle to logging-text
		// changes; counting call-sites is more durable.)
		const re = /archivePhoneFile\(\s*taskPath\s*,\s*['"]tasks['"]\s*,\s*taskId\s*\)/g;
		const matches = SRC.match(re) ?? [];
		assert.ok(
			matches.length >= 2,
			`Expected ≥2 archivePhoneFile(taskPath, "tasks", taskId) call-sites (result-consume + POLL_TIMEOUT), found ${matches.length}. POLL_TIMEOUT branch likely missing — otherwise long-running tasks that exceed POLL_TIMEOUT_MS leave the task file in tasks/ forever.`,
		);
	});
});
