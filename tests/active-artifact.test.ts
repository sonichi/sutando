import { describe, it, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { setActiveArtifactTool, queryActiveArtifactTool, clearActiveArtifactTool } from '../src/artifact-cache-tools.js';
import { clearActiveArtifact } from '../src/artifact-cache-tools.js';

// Helpers to call tools without the full ToolDefinition plumbing
async function set(path: string): Promise<any> {
	return (setActiveArtifactTool.execute as any)({ path }, null);
}
async function query(q: string): Promise<any> {
	return (queryActiveArtifactTool.execute as any)({ query: q }, null);
}
async function clear(): Promise<any> {
	return (clearActiveArtifactTool.execute as any)({}, null);
}

let tmpDir: string;

describe('active artifact cache', () => {
	beforeEach(() => {
		// Ensure clean state before each test
		clearActiveArtifact();
		tmpDir = mkdtempSync(join(tmpdir(), 'artifact-test-'));
	});

	after(() => {
		clearActiveArtifact();
		try { rmSync(tmpDir, { recursive: true }); } catch { /* best-effort */ }
	});

	it('set_active_artifact returns summary for a markdown file', async () => {
		const file = join(tmpDir, 'doc.md');
		writeFileSync(file, '# Introduction\n\nHello world.\n\n# Methods\n\nWe used X.\n\n# Results\n\nResult: 42.\n');
		const result = await set(file);
		assert.equal(result.artifact_id, file);
		assert.ok(typeof result.n_chars === 'number' && result.n_chars > 0, 'n_chars should be positive');
		assert.ok(typeof result.n_sections === 'number' && result.n_sections >= 3, `expected ≥3 sections, got ${result.n_sections}`);
		assert.match(result.summary, /Introduction/, 'summary should mention section headers');
	});

	it('set_active_artifact returns summary for a TypeScript file', async () => {
		const file = join(tmpDir, 'tool.ts');
		writeFileSync(file, 'export const myTool = {\n  name: "my_tool",\n};\n\nfunction helper() {\n  return 1;\n}\n');
		const result = await set(file);
		assert.equal(result.artifact_id, file);
		assert.ok(result.n_chars > 0);
	});

	it('set_active_artifact returns error for missing file', async () => {
		const result = await set('/nonexistent/path/file.md');
		assert.ok(result.error, 'should return an error for missing file');
		assert.match(result.error, /not found/i);
	});

	it('query_active_artifact returns error when no artifact loaded', async () => {
		const result = await query('anything');
		assert.ok(result.error, 'should return error when no artifact is loaded');
		assert.match(result.error, /no active artifact/i);
	});

	it('query_active_artifact finds a section by header', async () => {
		const file = join(tmpDir, 'paper.md');
		writeFileSync(file, '# Introduction\n\nBackground text.\n\n# Methods\n\nWe applied pdftotext.\n\n# Results\n\nP-value: 0.001\n');
		await set(file);
		const result = await query('Methods');
		assert.ok(!result.error, `unexpected error: ${result.error}`);
		assert.match(result.excerpt, /pdftotext/);
		assert.equal(result.section, '# Methods');
	});

	it('query_active_artifact finds a keyword when no section matches', async () => {
		const file = join(tmpDir, 'notes.txt');
		writeFileSync(file, 'Line one.\nLine two.\nLine three contains latency data.\nLine four.\n');
		await set(file);
		const result = await query('latency');
		assert.ok(!result.error, `unexpected error: ${result.error}`);
		assert.match(result.excerpt, /latency/);
		assert.ok(Array.isArray(result.line_range) && result.line_range.length === 2, 'line_range should be [start, end]');
	});

	it('query_active_artifact returns no-match message for absent content', async () => {
		const file = join(tmpDir, 'empty.md');
		writeFileSync(file, '# Title\n\nSome text.\n');
		await set(file);
		const result = await query('xyzquuxbazblorpnothere');
		assert.ok(!result.error);
		assert.match(result.excerpt, /No matches found/);
	});

	it('clear_active_artifact returns the cleared path', async () => {
		const file = join(tmpDir, 'toClear.md');
		writeFileSync(file, '# H\nContent.\n');
		await set(file);
		const result = await clear();
		assert.equal(result.ok, true);
		assert.equal(result.cleared, file);
	});

	it('query_active_artifact returns error after clear_active_artifact', async () => {
		const file = join(tmpDir, 'cleared.md');
		writeFileSync(file, '# H\nContent.\n');
		await set(file);
		await clear();
		const result = await query('anything');
		assert.ok(result.error, 'should error after clearing');
		assert.match(result.error, /no active artifact/i);
	});

	it('clearActiveArtifact() function clears state (session-end simulation)', async () => {
		const file = join(tmpDir, 'session.md');
		writeFileSync(file, '# Session\nData.\n');
		await set(file);
		clearActiveArtifact(); // simulates onSessionEnd
		const result = await query('Session');
		assert.ok(result.error, 'should error after programmatic clearActiveArtifact()');
	});

	it('set_active_artifact replaces a previously loaded artifact', async () => {
		const file1 = join(tmpDir, 'doc1.md');
		const file2 = join(tmpDir, 'doc2.md');
		writeFileSync(file1, '# DocOne\nOriginal content.\n');
		writeFileSync(file2, '# DocTwo\nReplacement content.\n');
		await set(file1);
		await set(file2);
		const result = await query('DocTwo');
		assert.ok(!result.error);
		assert.match(result.excerpt, /DocTwo/);
	});
});
