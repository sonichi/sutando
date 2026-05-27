import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// NOTES_DIR in inline-tools.ts is set at module load via sharedPersonalPath('notes', cwd).
// sharedPersonalPath reads SUTANDO_MEMORY_DIR dynamically at call time, so set env before
// import AND create TMP/notes so existsSync check returns true → NOTES_DIR = TMP/notes.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-inlinenotes-'));
const PRIOR_MEM = process.env.SUTANDO_MEMORY_DIR;
const PRIOR_WS = process.env.SUTANDO_WORKSPACE;
const PRIOR_HOME = process.env.SUTANDO_HOME;
delete process.env.SUTANDO_HOME;
process.env.SUTANDO_MEMORY_DIR = TMP;
process.env.SUTANDO_WORKSPACE = TMP;
mkdirSync(join(TMP, 'notes'));

const { getCurrentTimeTool, saveNoteTool, readNoteTool, deleteNoteTool } =
	await import('../src/inline-tools.js');

after(() => {
	if (PRIOR_MEM !== undefined) process.env.SUTANDO_MEMORY_DIR = PRIOR_MEM;
	else delete process.env.SUTANDO_MEMORY_DIR;
	if (PRIOR_WS !== undefined) process.env.SUTANDO_WORKSPACE = PRIOR_WS;
	else delete process.env.SUTANDO_WORKSPACE;
	if (PRIOR_HOME !== undefined) process.env.SUTANDO_HOME = PRIOR_HOME;
	else delete process.env.SUTANDO_HOME;
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ── getCurrentTimeTool ────────────────────────────────────────────────────────

describe('getCurrentTimeTool', () => {
	it('returns an object with a time string', async () => {
		const result = await (getCurrentTimeTool as any).execute({}, null);
		assert.equal(typeof result, 'object');
		assert.ok('time' in result, 'result should have a time property');
		assert.equal(typeof result.time, 'string');
		assert.ok(result.time.length > 0, 'time string should be non-empty');
	});

	it('time string includes the current year', async () => {
		const result = await (getCurrentTimeTool as any).execute({}, null);
		const year = new Date().getFullYear().toString();
		assert.ok(result.time.includes(year), `time "${result.time}" should include year ${year}`);
	});
});

// ── saveNoteTool ──────────────────────────────────────────────────────────────

describe('saveNoteTool', () => {
	it('creates a markdown file in the notes directory', async () => {
		const result = await (saveNoteTool as any).execute(
			{ title: 'Test Note Alpha', content: 'Body of the test note', tags: 'test, alpha' },
			null,
		);
		assert.equal(result.status, 'saved');
		assert.ok(result.slug, 'should return a slug');
		assert.ok(existsSync(join(TMP, 'notes', `${result.slug}.md`)), 'file should exist');
	});

	it('includes YAML frontmatter with title, date, and tags', async () => {
		const result = await (saveNoteTool as any).execute(
			{ title: 'Frontmatter Check', content: 'note body', tags: 'a, b' },
			null,
		);
		const content = readFileSync(join(TMP, 'notes', `${result.slug}.md`), 'utf-8');
		assert.ok(content.startsWith('---'), 'should start with frontmatter');
		assert.ok(content.includes('title: Frontmatter Check'), 'should include title');
		assert.ok(content.includes('tags: [a, b]'), 'should include tags');
	});

	it('generates a slug from the title', async () => {
		const result = await (saveNoteTool as any).execute(
			{ title: 'My Cool Note!', content: 'content' },
			null,
		);
		assert.equal(result.slug, 'my-cool-note', `unexpected slug: ${result.slug}`);
	});

	it('uses personal tag when no tags provided', async () => {
		const result = await (saveNoteTool as any).execute(
			{ title: 'No Tags Note', content: 'content' },
			null,
		);
		const content = readFileSync(join(TMP, 'notes', `${result.slug}.md`), 'utf-8');
		assert.ok(content.includes('tags: [personal]'), 'should default to personal tag');
	});
});

// ── readNoteTool ──────────────────────────────────────────────────────────────

describe('readNoteTool', () => {
	it('finds a previously saved note by name', async () => {
		// Save then read
		const saved = await (saveNoteTool as any).execute(
			{ title: 'Readable Note', content: 'This is the readable content.' },
			null,
		);
		const result = await (readNoteTool as any).execute({ name: 'readable-note' }, null);
		assert.ok(!('error' in result), `should not error: ${result.error}`);
		assert.ok(result.content.includes('readable content'), 'should return note content');
	});

	it('strips YAML frontmatter from returned content', async () => {
		await (saveNoteTool as any).execute(
			{ title: 'Stripped Note', content: 'Pure content here.' },
			null,
		);
		const result = await (readNoteTool as any).execute({ name: 'stripped-note' }, null);
		assert.ok(!result.content.includes('---'), 'frontmatter should be stripped');
		assert.ok(result.content.includes('Pure content here'), 'body content should be present');
	});

	it('returns error when note not found', async () => {
		const result = await (readNoteTool as any).execute({ name: 'nonexistent-note-xyz' }, null);
		assert.ok('error' in result, 'should return error for missing note');
		assert.ok(result.error.includes('nonexistent-note-xyz'), 'error should mention searched name');
	});
});

// ── deleteNoteTool ────────────────────────────────────────────────────────────

describe('deleteNoteTool', () => {
	it('deletes an existing note and returns status:deleted', async () => {
		const saved = await (saveNoteTool as any).execute(
			{ title: 'Deletable Note', content: 'Will be deleted.' },
			null,
		);
		const notePath = join(TMP, 'notes', `${saved.slug}.md`);
		assert.ok(existsSync(notePath), 'file should exist before delete');

		const result = await (deleteNoteTool as any).execute({ name: 'deletable-note' }, null);
		assert.equal(result.status, 'deleted');
		assert.ok(!existsSync(notePath), 'file should not exist after delete');
	});

	it('returns error when trying to delete nonexistent note', async () => {
		const result = await (deleteNoteTool as any).execute({ name: 'does-not-exist-abc' }, null);
		assert.ok('error' in result, 'should return error');
		assert.ok(result.error.includes('does-not-exist-abc'), 'error should mention the name');
	});
});
