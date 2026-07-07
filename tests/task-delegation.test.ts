/**
 * TaskDelegationService seam tests (step 4 / #1947).
 *
 * The load-bearing property: LocalTaskBackend.submitTask writes EXACTLY the
 * bytes the pre-seam `writeFileSync(join(TASK_DIR, `${taskId}.txt`), content)`
 * wrote — same path, same content, byte-identical. Plus selectBackend's
 * probe order: writable workspace → local; else CORE_API_URL → relay; else
 * loud throw.
 *
 * Run: npx tsx tests/task-delegation.test.ts
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { mkdtempSync, readFileSync, writeFileSync, chmodSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { LocalTaskBackend, RelayTaskBackend, selectBackend } from '../src/task-delegation.js';

const noopArchive = () => {};

test('LocalTaskBackend.submitTask is byte-identical to the pre-seam write', () => {
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const taskDir = join(dir, 'tasks');
	const resultDir = join(dir, 'results');
	mkdirSync(taskDir); mkdirSync(resultDir);
	const content =
		'id: task-1\ntimestamp: 2026-07-07T00:00:00Z\nsource: voice\n' +
		'interaction_type: realtime_audio\nchannel_id: local-voice\n' +
		'user_id: o\naccess_tier: owner\npriority: urgent\ntask: hello\n\n--- ctx ---\nline\n';
	const backend = new LocalTaskBackend(taskDir, resultDir, noopArchive);
	backend.submitTask('task-1', content);
	// The exact bytes the old inline writeFileSync produced:
	const expected = join(taskDir, 'task-1.txt');
	assert.strictEqual(readFileSync(expected, 'utf-8'), content);
});

test('LocalTaskBackend result primitives mirror the watcher I/O', () => {
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const taskDir = join(dir, 'tasks');
	const resultDir = join(dir, 'results');
	mkdirSync(taskDir); mkdirSync(resultDir);
	writeFileSync(join(resultDir, 'task-b.txt'), 'B');
	writeFileSync(join(resultDir, 'task-a.txt'), 'A');
	writeFileSync(join(resultDir, 'notes.md'), 'ignored');
	const archived: string[] = [];
	const backend = new LocalTaskBackend(taskDir, resultDir,
		(src, kind, tid) => archived.push(`${kind}:${tid}:${src}`));
	assert.deepStrictEqual(backend.listResultFiles(), ['task-a.txt', 'task-b.txt']);
	assert.strictEqual(backend.readResultFile('task-a.txt'), 'A');
	backend.archiveResultFile('task-a.txt', 'task-a');
	assert.strictEqual(archived.length, 1);
	assert.ok(archived[0].startsWith('results:task-a:'));
});

test('selectBackend: writable workspace → local mode', () => {
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const backend = selectBackend(join(dir, 'tasks'), join(dir, 'results'), noopArchive);
	assert.strictEqual(backend.mode, 'local');
	assert.ok(existsSync(join(dir, 'tasks'))); // probe mkdir -p'd it
});

test('selectBackend: unwritable workspace + CORE_API_URL → relay mode', () => {
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const roParent = join(dir, 'ro');
	mkdirSync(roParent);
	chmodSync(roParent, 0o500); // tasks/ can't be created under it
	process.env.CORE_API_URL = 'http://127.0.0.1:1';
	try {
		const backend = selectBackend(join(roParent, 'tasks'), join(roParent, 'results'), noopArchive);
		assert.strictEqual(backend.mode, 'relay');
		assert.ok(backend instanceof RelayTaskBackend);
	} finally {
		delete process.env.CORE_API_URL;
		chmodSync(roParent, 0o700);
	}
});

test('selectBackend: neither viable → loud throw', () => {
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const roParent = join(dir, 'ro');
	mkdirSync(roParent);
	chmodSync(roParent, 0o500);
	delete process.env.CORE_API_URL;
	try {
		assert.throws(
			() => selectBackend(join(roParent, 'tasks'), join(roParent, 'results'), noopArchive),
			/no viable backend/);
	} finally {
		chmodSync(roParent, 0o700);
	}
});
