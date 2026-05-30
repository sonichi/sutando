/**
 * Unit tests for src/task-archive.ts (#1335 sub-PR-1).
 *
 * Covers the TypeScript `archiveFile` + `findTaskFile` impls in
 * isolation. The cross-language parity assertion lives in
 * tests/task-archive-parity.test.py.
 *
 * Behavioral contract: docs/bridge-helpers-design.md § task-archive helper.
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { archiveFile, findTaskFile } from '../src/task-archive.js';

function ymNow(): string {
	return new Date().toISOString().slice(0, 7);
}

describe('findTaskFile', () => {
	let dir: string;

	beforeEach(() => {
		dir = mkdtempSync(join(tmpdir(), 'find-task-'));
	});

	afterEach(() => {
		rmSync(dir, { recursive: true, force: true });
	});

	it('returns the bare task file when present', () => {
		writeFileSync(join(dir, 'task-123.txt'), 'body');
		const result = findTaskFile(dir, 'task-123');
		assert.equal(result, join(dir, 'task-123.txt'));
	});

	it('returns the claimed variant when bare is missing', () => {
		writeFileSync(join(dir, 'task-456.claimed-core-2.txt'), 'body');
		const result = findTaskFile(dir, 'task-456');
		assert.equal(result, join(dir, 'task-456.claimed-core-2.txt'));
	});

	it('prefers bare over claimed when both exist', () => {
		writeFileSync(join(dir, 'task-789.txt'), 'body');
		writeFileSync(join(dir, 'task-789.claimed-core-1.txt'), 'body');
		const result = findTaskFile(dir, 'task-789');
		assert.equal(result, join(dir, 'task-789.txt'));
	});

	it('returns null when no file exists', () => {
		const result = findTaskFile(dir, 'task-nonexistent');
		assert.equal(result, null);
	});

	it('returns first lexicographic match among multiple claimed', () => {
		writeFileSync(join(dir, 'task-000.claimed-core-3.txt'), 'body');
		writeFileSync(join(dir, 'task-000.claimed-core-2.txt'), 'body');
		const result = findTaskFile(dir, 'task-000');
		// Sorted lex → claimed-core-2 comes before claimed-core-3.
		assert.equal(result, join(dir, 'task-000.claimed-core-2.txt'));
	});

	it('returns null when the tasks dir does not exist', () => {
		const ghost = join(dir, 'never-existed');
		const result = findTaskFile(ghost, 'task-X');
		assert.equal(result, null);
	});
});

describe('archiveFile', () => {
	let base: string;
	let tasksDir: string;
	let resultsDir: string;
	const ym = ymNow();

	beforeEach(() => {
		base = mkdtempSync(join(tmpdir(), 'archive-'));
		tasksDir = join(base, 'tasks');
		resultsDir = join(base, 'results');
		mkdirSync(tasksDir);
		mkdirSync(resultsDir);
	});

	afterEach(() => {
		rmSync(base, { recursive: true, force: true });
	});

	it('moves a task file into base/tasks/archive/YYYY-MM/', () => {
		const src = join(tasksDir, 'task-1.txt');
		writeFileSync(src, 'body');
		archiveFile(src, 'tasks', 'task-1', base);
		assert.equal(existsSync(src), false, 'src should be moved');
		const dest = join(base, 'tasks', 'archive', ym, 'task-1.txt');
		assert.equal(existsSync(dest), true, `dest should exist at ${dest}`);
		assert.equal(readFileSync(dest, 'utf-8'), 'body');
	});

	it('moves a result file into base/results/archive/YYYY-MM/', () => {
		const src = join(resultsDir, 'task-2.txt');
		writeFileSync(src, 'result-body');
		archiveFile(src, 'results', 'task-2', base);
		assert.equal(existsSync(src), false);
		const dest = join(base, 'results', 'archive', ym, 'task-2.txt');
		assert.equal(existsSync(dest), true);
		assert.equal(readFileSync(dest, 'utf-8'), 'result-body');
	});

	it('silently no-ops when src is missing', () => {
		const missing = join(tasksDir, 'task-missing.txt');
		// Should not throw:
		archiveFile(missing, 'tasks', 'task-missing', base);
		// No archive dir should have been created:
		assert.equal(existsSync(join(base, 'tasks', 'archive')), false);
	});

	it('creates the archive dir recursively', () => {
		const src = join(tasksDir, 'task-3.txt');
		writeFileSync(src, 'body');
		assert.equal(existsSync(join(base, 'tasks', 'archive')), false);
		archiveFile(src, 'tasks', 'task-3', base);
		assert.equal(existsSync(join(base, 'tasks', 'archive', ym)), true);
	});

	it('idempotent — second call no-ops because src is already gone', () => {
		const src = join(tasksDir, 'task-4.txt');
		writeFileSync(src, 'body');
		archiveFile(src, 'tasks', 'task-4', base);
		// Second call:
		archiveFile(src, 'tasks', 'task-4', base);
		// Archived file still there:
		const dest = join(base, 'tasks', 'archive', ym, 'task-4.txt');
		assert.equal(readFileSync(dest, 'utf-8'), 'body');
	});

	it('falls back to unlink when move fails', () => {
		const src = join(tasksDir, 'task-5.txt');
		writeFileSync(src, 'body');
		// Make base unwritable by setting it to a non-dir path:
		const evilBase = join(base, 'evil');
		writeFileSync(evilBase, 'not-a-dir');
		archiveFile(src, 'tasks', 'task-5', evilBase);
		// src should have been unlinked (fallback):
		assert.equal(existsSync(src), false, 'src should be unlinked after move failure');
	});
});
