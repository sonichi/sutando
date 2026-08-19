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
import { LocalTaskBackend, RelayTaskBackend, selectBackend, parseTaskSource } from '../src/task-delegation.js';

const noopArchive = () => {};

test('parseTaskSource reads the surface bucket that task_processed telemetry is tagged with', () => {
	// Every locally-created task carries a `source:` header; the emit keys on it
	// so voice/chat/context-drop each count under their own bucket (the gap the
	// messaging bridges never had). Missing header → a safe `unknown` bucket.
	assert.strictEqual(parseTaskSource('id: t\nsource: voice\ntask: hi\n'), 'voice');
	assert.strictEqual(parseTaskSource('id: t\nsource: chat\ntask: hi\n'), 'chat');
	assert.strictEqual(parseTaskSource('source: context-drop\ntask: x\n'), 'context-drop');
	assert.strictEqual(parseTaskSource('id: t\ntask: no source header\n'), 'unknown');
	// Must anchor to line start — not match `resource:` or an inline word.
	assert.strictEqual(parseTaskSource('id: t\nresource: pool\nsource: phone\n'), 'phone');
});

test('context-drop writer emits telemetry from the exact serialized task body', () => {
	const src = readFileSync(
		join(import.meta.dirname ?? '.', '..', 'src', 'task-bridge.ts'),
		'utf-8',
	);
	// The invariant is telemetry-sees-the-written-bytes: the stamped content
	// is what lands on disk, and the same variable feeds the emit.
	assert.match(
		src,
		/const stampedContent = tryStampText\(taskContent\);[\s\S]*?writeFileSync\([\s\S]*?stampedContent,[\s\S]*?\);\s*emitTaskProcessed\(stampedContent\);/,
	);
});

test('phone telemetry child errors are handled and cannot crash a live call', () => {
	const src = readFileSync(
		join(
			import.meta.dirname ?? '.',
			'..',
			'skills',
			'phone-conversation',
			'scripts',
			'conversation-server.ts',
		),
		'utf-8',
	);
	assert.match(
		src,
		/spawn\('python3', \[telemetryPy, 'task_processed', 'phone'\][\s\S]*?\.on\('error', \(\) => \{[\s\S]*?\}\)[\s\S]*?\.unref\(\)/,
	);
});

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
	// Since the #3058 writer-edge stamping, the write is the input PLUS one
	// canonical stamp line after `id:` — stripping it must restore the input
	// byte-identically (the pre-seam contract, modulo the stamp).
	const written = readFileSync(join(taskDir, 'task-1.txt'), 'utf-8');
	const lines = written.split('\n');
	assert.match(lines[1], /^envelope_hmac: v1:[0-9a-f]{64}$/);
	lines.splice(1, 1);
	assert.strictEqual(lines.join('\n'), content);
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

test('selectBackend: no CORE_API_URL + writable workspace → local mode', () => {
	delete process.env.CORE_API_URL;
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	const backend = selectBackend(join(dir, 'tasks'), join(dir, 'results'), noopArchive);
	assert.strictEqual(backend.mode, 'local');
	assert.ok(existsSync(join(dir, 'tasks'))); // local path mkdir -p'd it
	assert.ok(existsSync(join(dir, 'results')));
});

test('selectBackend: CORE_API_URL wins even on a writable workspace (positive config)', () => {
	// Codex P1: a normal voice-host checkout has a WRITABLE workspace — relay
	// must be reachable by explicit configuration, not only by probe failure.
	const dir = mkdtempSync(join(tmpdir(), 'deleg-'));
	process.env.CORE_API_URL = 'http://127.0.0.1:1';
	try {
		const backend = selectBackend(join(dir, 'tasks'), join(dir, 'results'), noopArchive);
		assert.strictEqual(backend.mode, 'relay');
		assert.ok(backend instanceof RelayTaskBackend);
		assert.ok(!existsSync(join(dir, 'tasks'))); // relay mode creates no local dirs
	} finally {
		delete process.env.CORE_API_URL;
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
