import { describe, it, before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// Prevent zod / bodhi-realtime-agent from failing if types are absent —
// these are runtime-only concerns that don't affect the logic under test.
const {
	setActiveArtifactTool,
	queryActiveArtifactTool,
	clearActiveArtifactTool,
	clearActiveArtifact,
} = await import('../src/artifact-cache-tools.js');

const TMP = mkdtempSync(join(tmpdir(), 'sutando-artifact-'));

function tmpFile(name: string, content: string): string {
	const p = join(TMP, name);
	writeFileSync(p, content);
	return p;
}

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* ignore */ }
});

afterEach(() => {
	// Reset module-level state between tests.
	clearActiveArtifact();
});

// ── clearActiveArtifact ─────────────────────────────────────────────────────

describe('clearActiveArtifact', () => {
	it('does not throw when nothing is loaded', () => {
		assert.doesNotThrow(() => clearActiveArtifact());
	});
});

// ── clear_active_artifact tool ──────────────────────────────────────────────

describe('clearActiveArtifactTool', () => {
	it('returns ok:true with cleared:null when nothing was loaded', async () => {
		const result = await clearActiveArtifactTool.execute({}, null as never) as { ok: boolean; cleared: string | null };
		assert.equal(result.ok, true);
		assert.equal(result.cleared, null);
	});

	it('returns cleared path after loading a file', async () => {
		const p = tmpFile('clear-test.md', '# Hello\ncontent');
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await clearActiveArtifactTool.execute({}, null as never) as { ok: boolean; cleared: string };
		assert.equal(result.ok, true);
		assert.equal(result.cleared, p);
	});
});

// ── set_active_artifact tool ────────────────────────────────────────────────

describe('setActiveArtifactTool — error cases', () => {
	it('returns error for missing file', async () => {
		const result = await setActiveArtifactTool.execute({ path: '/nonexistent/does-not-exist.md' }, null as never) as { error: string };
		assert.ok(result.error);
		assert.match(result.error, /File not found/);
	});
});

describe('setActiveArtifactTool — Markdown file', () => {
	it('loads a .md file and returns summary with section count', async () => {
		const content = '# Section A\nfoo bar\n\n## Section B\nbaz qux\n\n### Section C\ndeep';
		const p = tmpFile('doc.md', content);
		const result = await setActiveArtifactTool.execute({ path: p }, null as never) as {
			artifact_id: string; summary: string; n_chars: number; n_sections: number;
		};
		assert.equal(result.artifact_id, p);
		assert.ok(result.n_chars > 0);
		assert.equal(result.n_sections, 3);
		assert.match(result.summary, /Section A/);
	});

	it('reports 0 sections for plain text without headers', async () => {
		const p = tmpFile('plain.txt', 'just some text\nno headers here\nmore lines');
		const result = await setActiveArtifactTool.execute({ path: p }, null as never) as { n_sections: number; summary: string };
		assert.equal(result.n_sections, 0);
		assert.match(result.summary, /No section headers/);
	});

	it('counts chars correctly', async () => {
		const content = 'hello world';
		const p = tmpFile('short.md', content);
		const result = await setActiveArtifactTool.execute({ path: p }, null as never) as { n_chars: number };
		assert.equal(result.n_chars, content.length);
	});
});

describe('setActiveArtifactTool — TypeScript file', () => {
	it('detects function and class definitions as sections', async () => {
		const content = [
			'export function foo() {',
			'  return 1;',
			'}',
			'',
			'async function bar() {',
			'  return 2;',
			'}',
			'',
			'class MyClass {',
			'  x = 1;',
			'}',
		].join('\n');
		const p = tmpFile('code.ts', content);
		const result = await setActiveArtifactTool.execute({ path: p }, null as never) as { n_sections: number };
		assert.ok(result.n_sections >= 2);
	});
});

describe('setActiveArtifactTool — path expansion', () => {
	it('expands env var in path (uppercase name required by expandPath)', async () => {
		const content = '# Env var test';
		const p = tmpFile('envvar.md', content);
		// expandPath only matches [A-Z_][A-Z0-9_]* — must be uppercase.
		const varName = 'ARTIFACT_TEST_DIR_SUTANDO';
		process.env[varName] = TMP;
		try {
			const result = await setActiveArtifactTool.execute(
				{ path: `$\{${varName}\}/envvar.md` },
				null as never,
			) as { artifact_id: string; n_chars: number };
			assert.equal(result.artifact_id, p);
			assert.ok(result.n_chars > 0);
		} finally {
			delete process.env[varName];
		}
	});
});

// ── query_active_artifact tool ──────────────────────────────────────────────

describe('queryActiveArtifactTool — no artifact loaded', () => {
	it('returns error when no file is loaded', async () => {
		const result = await queryActiveArtifactTool.execute({ query: 'foo' }, null as never) as { error: string };
		assert.ok(result.error);
		assert.match(result.error, /No active artifact/);
	});
});

describe('queryActiveArtifactTool — section header match', () => {
	const mdContent = '# Introduction\nhello world\n\n## Methods\nalgorithm steps\n\n## Results\nfindings here\n';

	it('finds section by exact header keyword', async () => {
		const p = tmpFile('paper.md', mdContent);
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'Methods' }, null as never) as {
			excerpt: string; section?: string; line_range: [number, number];
		};
		assert.match(result.excerpt, /algorithm|Methods/);
		assert.ok(result.section);
		assert.match(result.section!, /Methods/);
	});

	it('finds section by partial keyword', async () => {
		const p = tmpFile('paper2.md', mdContent);
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'results findings' }, null as never) as {
			excerpt: string; section?: string;
		};
		assert.match(result.excerpt, /Results|findings/);
	});

	it('returns line_range tuple', async () => {
		const p = tmpFile('paper3.md', mdContent);
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'Introduction' }, null as never) as {
			line_range: [number, number];
		};
		assert.ok(Array.isArray(result.line_range));
		assert.equal(result.line_range.length, 2);
	});
});

describe('queryActiveArtifactTool — keyword scan (no section match)', () => {
	function makeScanContent(): string {
		const lines = Array.from({ length: 50 }, (_, i) => `line ${i}: content here`);
		lines[20] = 'line 20: special keyword zebra lives here';
		return lines.join('\n');
	}

	it('finds content by keyword when no section header matches', async () => {
		const p = tmpFile('scan.txt', makeScanContent());
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'zebra' }, null as never) as {
			excerpt: string;
		};
		assert.match(result.excerpt, /zebra/);
	});

	it('returns no-matches message for unknown keyword', async () => {
		const p = tmpFile('scan2.txt', makeScanContent());
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'xyzzy-impossible-string-abc' }, null as never) as {
			excerpt: string; line_range: [number, number];
		};
		assert.match(result.excerpt, /No matches found/);
		assert.deepEqual(result.line_range, [0, 0]);
	});
});

describe('queryActiveArtifactTool — artifact_id in response', () => {
	it('includes artifact_id in query response', async () => {
		const p = tmpFile('withid.md', '# Title\ncontent');
		await setActiveArtifactTool.execute({ path: p }, null as never);
		const result = await queryActiveArtifactTool.execute({ query: 'Title' }, null as never) as {
			artifact_id: string;
		};
		assert.equal(result.artifact_id, p);
	});
});
