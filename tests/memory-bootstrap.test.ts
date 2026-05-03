import { describe, it, before, after, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, existsSync, readFileSync, mkdtempSync, rmSync, chmodSync } from 'node:fs';
import { join } from 'node:path';
import { homedir, tmpdir } from 'node:os';

/**
 * Tests for bootstrapMemoryDir() in src/voice-agent.ts.
 *
 * The predicate is replicated locally rather than imported because pulling
 * voice-agent.ts loads the entire bodhi runtime + ts-side env reads — same
 * pattern as end-session-gate.test.ts and replay-gate.test.ts.
 *
 * Coverage: directory creation, placeholder MEMORY.md content, no-clobber
 * on existing index, SUTANDO_MEMORY_DIR env override, silent failure when
 * the parent is unwritable.
 */

function bootstrapMemoryDir(workspaceDir: string, envOverride?: string): { memDir: string; created: boolean; error?: string } {
	const slug = '-' + workspaceDir.replace(/\/$/, '').split('/').filter(Boolean).join('-');
	const memDir = envOverride || join(homedir(), '.claude', 'projects', slug, 'memory');
	let created = false;
	try {
		mkdirSync(memDir, { recursive: true });
		const indexPath = join(memDir, 'MEMORY.md');
		if (!existsSync(indexPath)) {
			writeFileSync(indexPath, '# Sutando memory index\n\nDurable facts about the user, project, and references. One line per entry: `- [Title](file.md) — one-line hook`. See CLAUDE.md `## Memory` for the schema.\n');
			created = true;
		}
	} catch (err) {
		return { memDir, created: false, error: err instanceof Error ? err.message : String(err) };
	}
	return { memDir, created };
}

let scratch: string;

beforeEach(() => {
	scratch = mkdtempSync(join(tmpdir(), 'sutando-mem-bootstrap-'));
});

afterEach(() => {
	try { chmodSync(scratch, 0o755); } catch {}
	try { rmSync(scratch, { recursive: true, force: true }); } catch {}
});

describe('bootstrapMemoryDir — directory creation', () => {
	it('creates memDir and MEMORY.md when nothing exists', () => {
		const memDir = join(scratch, 'memory');
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		assert.equal(out.error, undefined);
		assert.equal(out.created, true);
		assert.equal(existsSync(memDir), true);
		assert.equal(existsSync(join(memDir, 'MEMORY.md')), true);
	});

	it('writes the placeholder header pointing to CLAUDE.md schema', () => {
		const memDir = join(scratch, 'memory');
		bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		const body = readFileSync(join(memDir, 'MEMORY.md'), 'utf-8');
		assert.match(body, /^# Sutando memory index/);
		assert.match(body, /CLAUDE\.md `## Memory`/);
	});

	it('is idempotent on re-run with no MEMORY.md changes', () => {
		const memDir = join(scratch, 'memory');
		const first = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		const second = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		assert.equal(first.created, true);
		assert.equal(second.created, false, 'second call should NOT recreate MEMORY.md');
	});
});

describe('bootstrapMemoryDir — no-clobber on existing index', () => {
	it('leaves existing MEMORY.md untouched', () => {
		const memDir = join(scratch, 'memory');
		mkdirSync(memDir, { recursive: true });
		const custom = '# My existing memory\n\n- [user_profile](user_profile.md) — Chi\n';
		writeFileSync(join(memDir, 'MEMORY.md'), custom);
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		assert.equal(out.created, false);
		const after = readFileSync(join(memDir, 'MEMORY.md'), 'utf-8');
		assert.equal(after, custom, 'existing MEMORY.md must be preserved verbatim');
	});

	it('still ensures the dir exists even if MEMORY.md is the only thing missing inside an existing dir', () => {
		const memDir = join(scratch, 'memory');
		mkdirSync(memDir, { recursive: true });
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		assert.equal(out.created, true);
		assert.equal(existsSync(join(memDir, 'MEMORY.md')), true);
	});
});

describe('bootstrapMemoryDir — env override', () => {
	it('uses SUTANDO_MEMORY_DIR when provided instead of the slug-derived default', () => {
		const customDir = join(scratch, 'custom-memory');
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando', customDir);
		assert.equal(out.memDir, customDir, 'override path must be honoured');
		assert.equal(existsSync(customDir), true);
	});

	it('derives slug from workspace dir when no override is given', () => {
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando');
		const expected = join(homedir(), '.claude', 'projects', '-Users-test-GitHub-sutando', 'memory');
		assert.equal(out.memDir, expected);
	});

	it('strips a trailing slash from the workspace dir before slugging', () => {
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando/');
		const expected = join(homedir(), '.claude', 'projects', '-Users-test-GitHub-sutando', 'memory');
		assert.equal(out.memDir, expected);
	});
});

describe('bootstrapMemoryDir — failure-silent', () => {
	it('returns an error string instead of throwing when the parent is unwritable', () => {
		const lockedParent = join(scratch, 'locked');
		mkdirSync(lockedParent, { recursive: true });
		chmodSync(lockedParent, 0o500); // r-x — cannot create children
		const memDir = join(lockedParent, 'memory');
		const out = bootstrapMemoryDir('/Users/test/GitHub/sutando', memDir);
		assert.notEqual(out.error, undefined, 'unwritable parent should be reported as an error');
		assert.equal(out.created, false);
		assert.equal(existsSync(memDir), false);
	});
});
