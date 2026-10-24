/**
 * open_file with a path the core gave relative to the workspace (2026-10-21 17:43:48: the result said
 * "workspace/data/apps/injury-recovery.html", the model passed "~/workspace/data/apps/injury-recovery.html",
 * that missed under $HOME, and the model sent a work task the core took 55 s over). A literal path that
 * misses is tried under the workspace and the repo root, and never outside them.
 *
 * Nothing is opened on the test machine: `open` (and `xdg-open`) on PATH are stubs that record their argv,
 * HOME and the workspace are temp dirs.
 *
 * Run: npx tsx --test tests/open-file-relative-path.test.ts
 */
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-open-file-'));
const REPO = join(TMP, 'repo');
const WS = join(REPO, 'workspace');
const HOME = join(TMP, 'home');
const BIN = join(TMP, 'bin');
const ARGV = join(TMP, 'open-argv.txt');
for (const d of [join(WS, 'data', 'apps'), join(REPO, 'docs'), HOME, BIN]) mkdirSync(d, { recursive: true });
const APP = join(WS, 'data', 'apps', 'x.html');
writeFileSync(APP, '<html></html>');
writeFileSync(join(REPO, 'docs', 'notes.md'), '# notes');
writeFileSync(join(REPO, 'README.md'), '# the repo');
writeFileSync(join(TMP, 'secret.txt'), 'outside every root');
for (const name of ['open', 'xdg-open']) {
	writeFileSync(join(BIN, name), `#!/bin/sh\nprintf '%s\\n' "$@" > "${ARGV}"\n`);
	chmodSync(join(BIN, name), 0o755);
}

process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = WS;
process.env.HOME = HOME;
process.env.PATH = `${BIN}:${process.env.PATH}`;

// Not inline-tools.js: its load imports every skill, and one with a live connection kept the run from exiting.
const mod = await import('../src/open-file-tool.js');
const { openFileTool } = mod;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const resolveOpenFilePath = (mod as any).resolveOpenFilePath as (raw: string, expanded: string, roots: { workspace: string; repoRoot?: string }) => string | undefined;

after(() => rmSync(TMP, { recursive: true, force: true }));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const execute = (path: string) => (openFileTool.execute as any)({ path }, null) as Promise<{ status?: string; path?: string; error?: string }>;
const openedWith = () => (existsSync(ARGV) ? readFileSync(ARGV, 'utf-8').trim().split('\n') : []);

describe('open_file: a path the core gave relative to the workspace opens the file', () => {
	it('the incident path, "~/" put in front of a workspace-relative one, opens the workspace file', async () => {
		rmSync(ARGV, { force: true });
		const r = await execute('~/workspace/data/apps/x.html');
		assert.equal(r.error, undefined, `no error: ${r.error}`);
		assert.equal(r.status, 'opened');
		assert.equal(r.path, APP, 'the result names the file actually opened');
		assert.deepEqual(openedWith(), [APP], 'the OS handler got the resolved absolute path');
	});

	it('the path exactly as the result gave it opens the same file', async () => {
		rmSync(ARGV, { force: true });
		const r = await execute('workspace/data/apps/x.html');
		assert.equal(r.path, APP);
		assert.deepEqual(openedWith(), [APP]);
	});

	it('a file in the home folder that is not there is not swapped for the repo\'s or the workspace\'s file of that name', async () => {
		rmSync(ARGV, { force: true });
		for (const p of ['~/README.md', '~/docs/notes.md', '~/data/apps/x.html']) {
			const r = await execute(p);
			assert.match(r.error ?? '', /^File not found: /, `${p} asked for the home folder: ${JSON.stringify(r)}`);
		}
		assert.deepEqual(openedWith(), [], 'nothing was opened');
	});

	it('a true miss, absolute or under ~, is still "File not found" and opens nothing', async () => {
		rmSync(ARGV, { force: true });
		for (const p of ['/tmp/__nonexistent_open_file_rel__', '~/__nonexistent__/x', '~/../../secret.txt', 'workspace/data/apps/missing.html']) {
			const r = await execute(p);
			assert.match(r.error ?? '', /^File not found: .*Do not invent paths/, `${p}: the existing error text`);
		}
		assert.deepEqual(openedWith(), [], 'nothing was opened');
	});
});

describe('resolveOpenFilePath', () => {
	const roots = { workspace: WS, repoRoot: REPO };
	const missing = (p: string) => join(HOME, p); // what expandHome made of "~/<p>": not there

	it('an existing literal path comes back unchanged, before any fallback', () => {
		assert.equal(resolveOpenFilePath(APP, APP, roots), APP);
	});

	it('"~/workspace/...", "workspace/..." and "data/..." all land on the workspace file', () => {
		assert.equal(resolveOpenFilePath('~/workspace/data/apps/x.html', missing('workspace/data/apps/x.html'), roots), APP);
		assert.equal(resolveOpenFilePath('workspace/data/apps/x.html', 'workspace/data/apps/x.html', roots), APP);
		assert.equal(resolveOpenFilePath('data/apps/x.html', 'data/apps/x.html', roots), APP);
	});

	it('a repo-relative path lands under the repo root', () => {
		assert.equal(resolveOpenFilePath('docs/notes.md', 'docs/notes.md', roots), join(REPO, 'docs', 'notes.md'));
	});

	it('a leading "workspace/" is stripped even when the workspace folder has another name', () => {
		const other = join(TMP, 'sutando-ws');
		mkdirSync(join(other, 'data'), { recursive: true });
		writeFileSync(join(other, 'data', 'y.txt'), 'y');
		assert.equal(resolveOpenFilePath('workspace/data/y.txt', 'workspace/data/y.txt', { workspace: other, repoRoot: REPO }), join(other, 'data', 'y.txt'));
	});

	it('nothing outside the workspace or the repo root, and no fallback for an absolute or $VAR path', () => {
		assert.equal(resolveOpenFilePath('../secret.txt', '../secret.txt', roots), undefined);
		assert.equal(resolveOpenFilePath('../../secret.txt', '../../secret.txt', roots), undefined);
		assert.equal(resolveOpenFilePath('~/../../secret.txt', missing('../../secret.txt'), roots), undefined);
		assert.equal(resolveOpenFilePath('/data/apps/x.html', '/data/apps/x.html', roots), undefined);
		assert.equal(resolveOpenFilePath('$NOPE/data/apps/x.html', '/data/apps/x.html', roots), undefined);
		assert.equal(resolveOpenFilePath('workspace/data/apps/missing.html', 'workspace/data/apps/missing.html', roots), undefined);
	});
});
